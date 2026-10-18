/* Filesystem (fsp-srv): IFileSystemProxy and the objects it hands out.
 * Command ids and layouts per switchbrew "Filesystem services".
 *
 * - RomFS (OpenDataStorageByCurrentProcess) is read straight out of the NSP
 *   and decrypted with AES-128-CTR on the fly; tools/nsp_extract.py writes
 *   the parameters to extracted/romfs_program.json (FIFA_ROMFS overrides).
 * - Save data and the SD card are host directories under build/fs
 *   (FIFA_FS overrides): save/<program id>/<type>_<user id>_<index>, sdcard. */
#include "ipc.h"
#include <CommonCrypto/CommonCryptor.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#define R_FS_PATH_NOT_FOUND 0x202u
#define R_FS_PATH_ALREADY_EXISTS 0x402u
#define R_FS_DIRECTORY_NOT_EMPTY 0x1002u
#define R_FS_OUT_OF_RANGE 0x2EE202u      /* fs 6001 */
#define R_FS_UNSUPPORTED 0x31A802u       /* fs 6324: UnsupportedOperation */
#define R_FS_INVALID_PATH 0x2EE602u      /* fs 6003 */
#define R_FS_TARGET_NOT_FOUND 0x7D202u   /* fs 1001 */

enum { ENTRY_DIR = 0, ENTRY_FILE = 1 };
enum { OPEN_READ = 1, OPEN_WRITE = 2, OPEN_APPEND = 4 };

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[fs] %s cmd %u unhandled -> success (in %u bytes, a%d b%d x%d)\n", iface, m->cmd, m->in_size, m->na, m->nb,
       m->nx);
}

static uint32_t errno_result(void) {
  switch (errno) {
  case ENOENT: case ENOTDIR: return R_FS_PATH_NOT_FOUND;
  case EEXIST: return R_FS_PATH_ALREADY_EXISTS;
  case ENOTEMPTY: return R_FS_DIRECTORY_NOT_EMPTY;
  default:
    HLOG("[fs] host error: %s\n", strerror(errno));
    return R_FS_PATH_NOT_FOUND;
  }
}

static void mkdirs(const char *path) {
  char p[1024];
  snprintf(p, sizeof p, "%s", path);
  for (char *s = p + 1; *s; s++)
    if (*s == '/') { *s = 0; mkdir(p, 0755); *s = '/'; }
  mkdir(p, 0755);
}

/* ---- RomFS image in the NSP ---- */
typedef struct {
  int fd;
  uint64_t nca_offset, offset, size;
  int encrypted;
  uint8_t key[16], ctr_hi[8];
} RomFs;
static RomFs romfs = {.fd = -1};

/* Tiny reader for the flat JSON nsp_extract.py writes. */
static const char *json_field(const char *js, const char *key) {
  char k[64];
  snprintf(k, sizeof k, "\"%s\":", key);
  const char *p = strstr(js, k);
  if (!p) HDIE("[fs] romfs config has no \"%s\"\n", key);
  p += strlen(k);
  while (*p == ' ') p++;
  return p;
}
static void json_string(const char *js, const char *key, char *out, size_t n) {
  const char *p = json_field(js, key) + 1, *e = strchr(p, '"');
  snprintf(out, n, "%.*s", (int)(e - p), p);
}
static void json_hex(const char *js, const char *key, uint8_t *out, size_t n) {
  char s[80];
  json_string(js, key, s, sizeof s);
  for (size_t i = 0; i < n; i++) sscanf(s + 2 * i, "%2hhx", &out[i]);
}

static int romfs_open(void) {
  if (romfs.fd >= 0) return 1;
  const char *cfg = getenv("FIFA_ROMFS");
  if (!cfg) cfg = "extracted/romfs_program.json";
  FILE *f = fopen(cfg, "r");
  if (!f) { HLOG("[fs] no RomFS config at %s (run tools/nsp_extract.py)\n", cfg); return 0; }
  char js[4096] = {0}, nsp[1024];
  fread(js, 1, sizeof js - 1, f);
  fclose(f);
  json_string(js, "nsp", nsp, sizeof nsp);
  romfs.nca_offset = strtoull(json_field(js, "nca_offset"), 0, 10);
  romfs.offset = strtoull(json_field(js, "offset"), 0, 10);
  romfs.size = strtoull(json_field(js, "size"), 0, 10);
  romfs.encrypted = atoi(json_field(js, "encryption")) == 3;
  json_hex(js, "key", romfs.key, 16);
  json_hex(js, "ctr_hi", romfs.ctr_hi, 8);
  romfs.fd = open(nsp, O_RDONLY);
  if (romfs.fd < 0) { HLOG("[fs] can't open %s: %s\n", nsp, strerror(errno)); return 0; }
  HLOG("[fs] RomFS: %llu bytes at %#llx in %s\n", (unsigned long long)romfs.size,
       (unsigned long long)romfs.offset, nsp);
  return 1;
}

/* Read [off, off+size) of the RomFS image; the caller clamps to its size. */
static void romfs_read(uint8_t *dst, uint64_t off, uint64_t size) {
  static uint8_t buf[1 << 22];  /* callers hold ipc_lock */
  while (size) {
    uint64_t pos = romfs.offset + off, a = pos & ~15ull, pad = pos - a;
    uint64_t n = size < sizeof buf - 16 ? size : sizeof buf - 16;
    uint64_t len = (pad + n + 15) & ~15ull;
    ssize_t got = pread(romfs.fd, buf, len, (off_t)a);
    if (got < (ssize_t)(pad + n)) HDIE("[fs] short RomFS read at %#llx\n", (unsigned long long)a);
    if (romfs.encrypted) {
      uint8_t iv[16];
      uint64_t blk = (a - romfs.nca_offset) >> 4;
      memcpy(iv, romfs.ctr_hi, 8);
      for (int i = 0; i < 8; i++) iv[15 - i] = (uint8_t)(blk >> (8 * i));
      CCCryptorRef c;
      size_t moved;
      if (CCCryptorCreateWithMode(kCCEncrypt, kCCModeCTR, kCCAlgorithmAES, ccNoPadding, iv, romfs.key, 16, 0, 0, 0,
                                  kCCModeOptionCTR_BE, &c) != kCCSuccess ||
          CCCryptorUpdate(c, buf, len, buf, len, &moved) != kCCSuccess)
        HDIE("[fs] AES-CTR failed\n");
      CCCryptorRelease(c);
    }
    memcpy(dst, buf + pad, n);
    dst += n;
    off += n;
    size -= n;
  }
}

/* ---- IStorage (RomFS) ---- */
static void storage(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: {  /* Read(s64 offset, s64 size, B buffer) */
    uint64_t off = IN(uint64_t, 0), size = IN(uint64_t, 8);
    if (m->nb && m->b[0].size < size) size = m->b[0].size;
    if (off > romfs.size) { m->result = R_FS_OUT_OF_RANGE; return; }
    if (size > romfs.size - off) size = romfs.size - off;
    if (host_trace) HLOG("[fs] RomFS read %#llx +%#llx\n", (unsigned long long)off, (unsigned long long)size);
    romfs_read(gptr(m->b[0].addr), off, size);
    return;
  }
  case 1: case 3: m->result = R_FS_UNSUPPORTED; return;  /* Write, SetSize: read-only */
  case 2: return;                                        /* Flush */
  case 4: SET_OUT(uint64_t, 0, romfs.size); return;      /* GetSize */
  case 5:                                                /* OperateRange -> QueryRangeInfo */
    memset(m->out, 0, 0x40);
    out_size(m, 0x40);
    return;
  default: unhandled("IStorage", m);
  }
}
static const Service storage_svc = {"IStorage", storage};

/* ---- host-directory filesystems ---- */
typedef struct { char root[1024]; } HostFs;

static const char *fs_root(void) {
  const char *r = getenv("FIFA_FS");
  return r ? r : "build/fs";
}

static HostFs *hostfs_new(const char *sub) {
  HostFs *fs = calloc(1, sizeof *fs);
  snprintf(fs->root, sizeof fs->root, "%s/%s", fs_root(), sub);
  mkdirs(fs->root);
  return fs;
}

/* Guest path from X descriptor i, joined to the filesystem root. */
static int host_path(HostFs *fs, IpcMsg *m, int i, char *out, size_t n) {
  if (i >= m->nx) { m->result = R_FS_INVALID_PATH; return 0; }
  const char *p = gptr(m->x[i].addr);
  size_t len = strnlen(p, m->x[i].size);
  if (strstr(p, "..") && len) {
    HLOG("[fs] rejecting path %.*s\n", (int)len, p);
    m->result = R_FS_INVALID_PATH;
    return 0;
  }
  snprintf(out, n, "%s/%.*s", fs->root, (int)len, *p == '/' ? p + 1 : p);
  if (host_trace) HLOG("[fs] path %s\n", out);
  return 1;
}

typedef struct { int fd, mode; } HostFile;
static void file_close(void *state) {
  HostFile *f = state;
  close(f->fd);
  free(f);
}

static void file(void *state, IpcMsg *m) {
  HostFile *f = state;
  switch (m->cmd) {
  case 0: {  /* Read(u32 option, s64 offset, s64 size, B buffer) -> s64 read */
    uint64_t off = IN(uint64_t, 8), size = IN(uint64_t, 16);
    if (m->nb && m->b[0].size < size) size = m->b[0].size;
    ssize_t got = pread(f->fd, gptr(m->b[0].addr), size, (off_t)off);
    if (got < 0) { m->result = errno_result(); return; }
    SET_OUT(uint64_t, 0, (uint64_t)got);
    return;
  }
  case 1: {  /* Write(u32 option, s64 offset, s64 size, A buffer) */
    uint64_t off = IN(uint64_t, 8), size = IN(uint64_t, 16);
    if (m->na && m->a[0].size < size) size = m->a[0].size;
    if (pwrite(f->fd, gptr(m->a[0].addr), size, (off_t)off) != (ssize_t)size) m->result = errno_result();
    return;
  }
  case 2: return;  /* Flush */
  case 3:          /* SetSize(s64) */
    if (ftruncate(f->fd, (off_t)IN(uint64_t, 0))) m->result = errno_result();
    return;
  case 4: {  /* GetSize */
    struct stat st;
    fstat(f->fd, &st);
    SET_OUT(uint64_t, 0, (uint64_t)st.st_size);
    return;
  }
  case 5:  /* OperateRange */
    memset(m->out, 0, 0x40);
    out_size(m, 0x40);
    return;
  default: unhandled("IFile", m);
  }
}
static const Service file_svc = {"IFile", file, file_close};

typedef struct {
  uint8_t (*ents)[0x310];
  int64_t n, pos;
} HostDir;
static void dir_close(void *state) {
  HostDir *d = state;
  free(d->ents);
  free(d);
}

static void directory(void *state, IpcMsg *m) {
  HostDir *d = state;
  switch (m->cmd) {
  case 0: {  /* Read(B buffer of DirectoryEntry) -> s64 count */
    int64_t cap = m->nb ? (int64_t)(m->b[0].size / 0x310) : 0, k = 0;
    uint8_t *out = m->nb ? gptr(m->b[0].addr) : 0;
    for (; k < cap && d->pos < d->n; k++, d->pos++) memcpy(out + 0x310 * k, d->ents[d->pos], 0x310);
    SET_OUT(int64_t, 0, k);
    return;
  }
  case 1: SET_OUT(int64_t, 0, d->n); return;  /* GetEntryCount */
  default: unhandled("IDirectory", m);
  }
}
static const Service directory_svc = {"IDirectory", directory, dir_close};

static int rm_tree(const char *path, int keep_root) {
  DIR *dh = opendir(path);
  if (!dh) return -1;
  struct dirent *e;
  char p[1024];
  while ((e = readdir(dh))) {
    if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
    snprintf(p, sizeof p, "%s/%s", path, e->d_name);
    if (e->d_type == DT_DIR) rm_tree(p, 0);
    else unlink(p);
  }
  closedir(dh);
  return keep_root ? 0 : rmdir(path);
}

static void filesystem(void *state, IpcMsg *m) {
  HostFs *fs = state;
  char p[1024], q[1024];
  struct stat st;
  switch (m->cmd) {
  case 0: {  /* CreateFile(X path, u32 option, s64 size) */
    if (!host_path(fs, m, 0, p, sizeof p)) return;
    int fd = open(p, O_CREAT | O_EXCL | O_WRONLY, 0644);
    if (fd < 0) { m->result = errno_result(); return; }
    if (ftruncate(fd, (off_t)IN(uint64_t, 8))) m->result = errno_result();
    close(fd);
    return;
  }
  case 1:  /* DeleteFile */
    if (host_path(fs, m, 0, p, sizeof p) && unlink(p)) m->result = errno_result();
    return;
  case 2:  /* CreateDirectory */
    if (host_path(fs, m, 0, p, sizeof p) && mkdir(p, 0755)) m->result = errno_result();
    return;
  case 3:  /* DeleteDirectory */
    if (host_path(fs, m, 0, p, sizeof p) && rmdir(p)) m->result = errno_result();
    return;
  case 4: case 13:  /* DeleteDirectoryRecursively, CleanDirectoryRecursively */
    if (host_path(fs, m, 0, p, sizeof p) && rm_tree(p, m->cmd == 13)) m->result = errno_result();
    return;
  case 5: case 6:  /* RenameFile, RenameDirectory(X old, X new) */
    if (host_path(fs, m, 0, p, sizeof p) && host_path(fs, m, 1, q, sizeof q) && rename(p, q))
      m->result = errno_result();
    return;
  case 7:  /* GetEntryType -> u32 */
    if (!host_path(fs, m, 0, p, sizeof p)) return;
    if (stat(p, &st)) { m->result = errno_result(); return; }
    SET_OUT(uint32_t, 0, S_ISDIR(st.st_mode) ? ENTRY_DIR : ENTRY_FILE);
    return;
  case 8: {  /* OpenFile(X path, u32 mode) -> IFile */
    if (!host_path(fs, m, 0, p, sizeof p)) return;
    int mode = (int)IN(uint32_t, 0);
    int fd = open(p, mode & (OPEN_WRITE | OPEN_APPEND) ? O_RDWR : O_RDONLY);
    if (fd < 0 || (fstat(fd, &st), S_ISDIR(st.st_mode))) {
      if (fd >= 0) { close(fd); errno = ENOENT; }
      m->result = errno_result();
      return;
    }
    HostFile *f = calloc(1, sizeof *f);
    f->fd = fd;
    f->mode = mode;
    ipc_push_object(m, &file_svc, f);
    return;
  }
  case 9: {  /* OpenDirectory(X path, u32 mode) -> IDirectory */
    if (!host_path(fs, m, 0, p, sizeof p)) return;
    uint32_t mode = IN(uint32_t, 0);
    DIR *dh = opendir(p);
    if (!dh) { m->result = errno_result(); return; }
    HostDir *d = calloc(1, sizeof *d);
    struct dirent *e;
    int64_t cap = 0;
    while ((e = readdir(dh))) {
      if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
      snprintf(q, sizeof q, "%s/%s", p, e->d_name);
      if (stat(q, &st)) continue;
      int is_dir = S_ISDIR(st.st_mode);
      if (!(mode & (is_dir ? 1 : 2))) continue;  /* bit 0 directories, bit 1 files */
      if (d->n == cap) d->ents = realloc(d->ents, (size_t)(cap = cap ? 2 * cap : 16) * 0x310);
      uint8_t *ent = d->ents[d->n++];
      memset(ent, 0, 0x310);
      snprintf((char *)ent, 0x301, "%s", e->d_name);
      ent[0x304] = is_dir ? ENTRY_DIR : ENTRY_FILE;
      int64_t size = is_dir ? 0 : st.st_size;
      memcpy(ent + 0x308, &size, 8);
    }
    closedir(dh);
    ipc_push_object(m, &directory_svc, d);
    return;
  }
  case 10: return;  /* Commit */
  case 11: case 12: SET_OUT(int64_t, 0, 4ll << 30); return;  /* GetFreeSpaceSize, GetTotalSpaceSize */
  case 14:  /* GetFileTimeStampRaw -> {created, modified, accessed, valid} */
    if (!host_path(fs, m, 0, p, sizeof p)) return;
    if (stat(p, &st)) { m->result = errno_result(); return; }
    memset(m->out, 0, 0x20);
    OUT(int64_t, 0) = st.st_birthtimespec.tv_sec;
    OUT(int64_t, 8) = st.st_mtimespec.tv_sec;
    OUT(int64_t, 16) = st.st_atimespec.tv_sec;
    OUT(uint8_t, 24) = 1;
    out_size(m, 0x20);
    return;
  case 15: return;  /* QueryEntry */
  case 16:          /* GetFileSystemAttribute -> FileSystemAttribute (0xC0): no limits defined */
    memset(m->out, 0, 0xC0);
    out_size(m, 0xC0);
    return;
  default: unhandled("IFileSystem", m);
  }
}
static void filesystem_close(void *state) { free(state); }
static const Service filesystem_svc = {"IFileSystem", filesystem, filesystem_close};

/* ---- ISaveDataInfoReader: no save data to enumerate ---- */
static void save_info_reader(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: SET_OUT(int64_t, 0, 0); return;  /* ReadSaveDataInfo -> count */
  default: unhandled("ISaveDataInfoReader", m);
  }
}
static const Service save_info_reader_svc = {"ISaveDataInfoReader", save_info_reader};

/* SaveDataAttribute at `off`: program id, user id (u128), system save id,
 * type, rank, index. */
static void open_save(IpcMsg *m, uint32_t off) {
  const uint8_t *a = m->in + off;
  uint64_t program, uid[2], sys;
  memcpy(&program, a, 8);
  memcpy(uid, a + 8, 16);
  memcpy(&sys, a + 24, 8);
  uint8_t type = a[32];
  uint16_t index;
  memcpy(&index, a + 34, 2);
  char sub[160];
  if (!program) program = 0x0100C49025D3E000ull;  /* 0 = the current program */
  if (sys) snprintf(sub, sizeof sub, "save/system/%016llx", (unsigned long long)sys);
  else
    snprintf(sub, sizeof sub, "save/%016llx/%u_%016llx%016llx_%u", (unsigned long long)program, type,
             (unsigned long long)uid[1], (unsigned long long)uid[0], index);
  HLOG("[fs] save data -> %s/%s\n", fs_root(), sub);
  ipc_push_object(m, &filesystem_svc, hostfs_new(sub));
}

/* ---- fsp-srv: IFileSystemProxy ---- */
static void fsp_srv(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 1: return;  /* SetCurrentProcess(pid) */
  case 18:         /* OpenSdCardFileSystem */
    ipc_push_object(m, &filesystem_svc, hostfs_new("sdcard"));
    return;
  case 22: case 23: return;  /* CreateSaveDataFileSystem(BySystemSaveDataId): created on open */
  case 51: case 52:          /* OpenSaveDataFileSystem(BySystemSaveDataId)(u8 space, SaveDataAttribute) */
    open_save(m, 8);
    return;
  case 60: case 61:  /* OpenSaveDataInfoReader(BySaveDataSpaceId) */
    ipc_push_object(m, &save_info_reader_svc, 0);
    return;
  case 200:  /* OpenDataStorageByCurrentProcess */
    if (!romfs_open()) { m->result = R_FS_TARGET_NOT_FOUND; return; }
    ipc_push_object(m, &storage_svc, 0);
    return;
  case 202:  /* OpenDataStorageByDataId(u8 storage, u64 data id): no add-on content or system data */
    m->result = R_FS_TARGET_NOT_FOUND;
    return;
  case 1005: SET_OUT(uint32_t, 0, 0); return;  /* GetGlobalAccessLogMode: off */
  case 1011:                                   /* GetProgramIndexForAccessLog -> index, count */
    SET_OUT(uint32_t, 0, 0);
    SET_OUT(uint32_t, 4, 1);
    return;
  case 1003: case 1004: case 1006: case 1016:
    /* DisableAutoSaveDataCreation, SetGlobalAccessLogMode, OutputAccessLogToSdCard,
     * OutputApplicationInfoAccessLog */
    return;
  default: unhandled("IFileSystemProxy", m);
  }
}
const Service fsp_srv_service = {"fsp-srv", fsp_srv};
