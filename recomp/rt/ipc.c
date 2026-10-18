/* IPC transport: parses HIPC messages from guest memory, routes CMIF/TIPC
 * commands to host services, and writes the reply in place. */
#include "ipc.h"
#include <pthread.h>
#include <stdlib.h>

#define MAGIC_SFCI 0x49434653u
#define MAGIC_SFCO 0x4F434653u

typedef struct Object { const Service *svc; void *state; } Object;
/* A domain is shared by every session cloned from it (CloneCurrentObject on a
 * domain session gives another session into the same domain). */
typedef struct Domain { Object objs[256]; } Domain;  /* id = index + 1 */
typedef struct Session {
  Object root;
  Domain *domain;
} Session;

static pthread_mutex_t ipc_lock = PTHREAD_MUTEX_INITIALIZER;

static uint32_t session_new(const Service *s, void *state, Domain *domain) {
  Session *ss = calloc(1, sizeof *ss);
  ss->root.svc = s;
  ss->root.state = state;
  ss->domain = domain;
  return kobj_new(H_SESSION, ss);
}

static void object_close(Object *o) {
  if (o->svc && o->svc->close) o->svc->close(o->state);
  o->svc = 0;
  o->state = 0;
}

void ipc_push_object(IpcMsg *m, const Service *s, void *state) {
  m->obj_out_svc[m->n_obj_out] = s;
  m->obj_out_state[m->n_obj_out++] = state;
}

void service_stub(void *state, IpcMsg *m) {
  (void)state;
  (void)m;
}

/* ---- sm: (TIPC) ---- */
static void sm_handle(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* RegisterClient */
    return;
  case 1: {  /* GetServiceHandle(name u64) */
    char name[9] = {0};
    memcpy(name, m->in, 8);
    const Service *s = service_find(name);
    if (!s) {
      HLOG("[sm] GetService(\"%s\"): no host service, using stub\n", name);
      static Service stubs[256];
      static int nstubs;
      for (int i = 0; i < nstubs; i++)
        if (!strcmp(stubs[i].name, name)) { s = &stubs[i]; break; }
      if (!s) { stubs[nstubs].name = strdup(name); stubs[nstubs].handle = service_stub; s = &stubs[nstubs++]; }
    } else if (host_trace) HLOG("[sm] GetService(\"%s\")\n", name);
    m->move_out[m->nmove_out++] = session_new(s, 0, 0);
    return;
  }
  default:
    HLOG("[sm] unhandled command %u\n", m->cmd);
  }
}
static const Service sm_service = {"sm:", sm_handle};

uint32_t ipc_connect_port(const char *name, uint32_t *out) {
  if (!strcmp(name, "sm:")) { *out = session_new(&sm_service, 0, 0); return 0; }
  HLOG("[ipc] ConnectToNamedPort(\"%s\") unsupported\n", name);
  return 0xE01;  /* NotFound */
}

/* ---- message codec ---- */
static uint32_t rd32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static void wr32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }

static void control(Session *ss, IpcMsg *m, uint32_t h) {
  if (host_trace) HLOG("[ipc] control %u on %#x (%s%s)\n", m->cmd, h, ss->root.svc->name, ss->domain ? ", domain" : "");
  switch (m->cmd) {
  case 0:  /* ConvertCurrentObjectToDomain -> object id */
    if (!ss->domain) {
      ss->domain = calloc(1, sizeof *ss->domain);
      ss->domain->objs[0] = ss->root;
    }
    SET_OUT(uint32_t, 0, 1);
    return;
  case 1: {  /* CopyFromCurrentDomain(object id) -> session handle */
    uint32_t id = IN(uint32_t, 0);
    Object *o = ss->domain && id && id <= 256 ? &ss->domain->objs[id - 1] : 0;
    if (!o || !o->svc) { HLOG("[ipc] CopyFromCurrentDomain: bad object %u\n", id); m->result = 0xE601; return; }
    m->move_out[m->nmove_out++] = session_new(o->svc, o->state, 0);
    return;
  }
  case 2: case 4:  /* CloneCurrentObject(Ex) */
    m->move_out[m->nmove_out++] = session_new(ss->root.svc, ss->root.state, ss->domain);
    return;
  case 3:  /* QueryPointerBufferSize */
    SET_OUT(uint16_t, 0, 0x8000);
    return;
  default:
    HLOG("[ipc] control command %u on %#x\n", m->cmd, h);
  }
}

uint32_t ipc_request(uint32_t h, uint64_t buf) {
  Session *ss = kobj_get(h, H_SESSION);
  if (!ss) { HLOG("[ipc] request on bad handle %#x\n", h); return 0xE401; }
  pthread_mutex_lock(&ipc_lock);
  uint8_t *msg = gptr(buf);
  IpcMsg *m = calloc(1, sizeof *m);
  uint32_t w0 = rd32(msg), w1 = rd32(msg + 4);
  m->type = w0 & 0xffff;
  int nx = (w0 >> 16) & 15, na = (w0 >> 20) & 15, nb = (w0 >> 24) & 15, nw = (w0 >> 28) & 15;
  uint32_t ndata = w1 & 0x3ff, cflags = (w1 >> 10) & 15;
  const uint8_t *p = msg + 8;
  if (w1 >> 31) {  /* special header */
    uint32_t sh = rd32(p);
    p += 4;
    if (sh & 1) { memcpy(&m->pid, p, 8); p += 8; }
    m->ncopy_in = (sh >> 1) & 15;
    m->nmove_in = (sh >> 5) & 15;
    for (int i = 0; i < m->ncopy_in; i++, p += 4) m->copy_in[i] = rd32(p);
    for (int i = 0; i < m->nmove_in; i++, p += 4) m->move_in[i] = rd32(p);
  }
  for (int i = 0; i < nx; i++, p += 8) {
    uint32_t a = rd32(p), lo = rd32(p + 4);
    m->x[i].size = a >> 16;
    m->x[i].addr = lo | ((uint64_t)((a >> 12) & 15) << 32) | ((uint64_t)((a >> 6) & 7) << 36);
  }
  m->nx = nx;
  IpcBuf *abw[3] = {m->a, m->b, m->w};
  int nabw[3] = {na, nb, nw};
  for (int k = 0; k < 3; k++)
    for (int i = 0; i < nabw[k]; i++, p += 12) {
      uint32_t sz = rd32(p), lo = rd32(p + 4), hi = rd32(p + 8);
      abw[k][i].size = sz | ((uint64_t)((hi >> 24) & 15) << 32);
      abw[k][i].addr = lo | ((uint64_t)((hi >> 28) & 15) << 32) | ((uint64_t)((hi >> 2) & 7) << 36);
    }
  m->na = na; m->nb = nb; m->nw = nw;
  const uint8_t *raw = p;
  const uint8_t *cdesc = raw + 4 * ndata;
  if (cflags >= 2) {
    m->nc = cflags == 2 ? 1 : (int)cflags - 2;
    for (int i = 0; i < m->nc; i++) {
      uint32_t lo = rd32(cdesc + 8 * i), hi = rd32(cdesc + 8 * i + 4);
      m->c[i].addr = lo | ((uint64_t)(hi & 0xffff) << 32);
      m->c[i].size = hi >> 16;
    }
  }

  Object *obj = &ss->root;
  int tipc = m->type >= 16, close = m->type == 2, is_control = m->type == 5 || m->type == 7;
  uint32_t domain_obj = 0;
  int domain_close = 0;
  if (tipc) {
    m->cmd = m->type - 16;
    m->in = raw;
    m->in_size = 4 * ndata;
  } else if (!close) {
    const uint8_t *d = msg + ((raw - msg + 15) & ~15);
    if (ss->domain && !is_control) {
      uint8_t dtype = d[0], nobj = d[1];
      uint16_t dlen = (uint16_t)(d[2] | d[3] << 8);
      domain_obj = rd32(d + 4);
      if (dtype == 2) domain_close = 1;
      m->n_in_objects = nobj;
      for (int i = 0; i < nobj; i++) m->in_objects[i] = rd32(d + 16 + dlen + 4 * i);
      d += 16;
      if (domain_obj == 0 || domain_obj > 256 || !ss->domain->objs[domain_obj - 1].svc) {
        HLOG("[ipc] bad domain object %u\n", domain_obj);
      } else obj = &ss->domain->objs[domain_obj - 1];
    }
    if (!domain_close) {
      if (rd32(d) != MAGIC_SFCI) {
        HLOG("[ipc] missing SFCI magic on %#x (type %u):", h, m->type);
        for (int i = 0; i < 24; i++) HLOG(" %08x", rd32(msg + 4 * i));
        HLOG("\n");
      }
      m->cmd = rd32(d + 8);
      m->in = d + 16;
      m->in_size = 4 * ndata > 32 ? 4 * ndata - 32 : 0;
    }
  }

  if (close) {
    kobj_close(h);
  } else if (domain_close) {
    if (domain_obj && domain_obj <= 256) object_close(&ss->domain->objs[domain_obj - 1]);
  } else if (is_control) {
    control(ss, m, h);
  } else {
    if (host_trace) HLOG("[ipc] %s cmd %u (in %u bytes, a%d b%d x%d c%d)\n", obj->svc->name, m->cmd, m->in_size,
                         m->na, m->nb, m->nx, m->nc);
    obj->svc->handle(obj->state, m);
    if (obj->svc->handle == service_stub)
      HLOG("[ipc] stub %s cmd %u -> success (in %u bytes, a%d b%d x%d)\n", obj->svc->name, m->cmd, m->in_size,
           m->na, m->nb, m->nx);
  }

  /* new interface objects: domain ids in a domain, sessions otherwise */
  uint32_t out_obj_ids[8];
  for (int i = 0; i < m->n_obj_out; i++) {
    if (ss->domain) {
      int id = 0;
      while (id < 256 && ss->domain->objs[id].svc) id++;
      if (id == 256) HDIE("[ipc] domain of %s is full\n", ss->root.svc->name);
      ss->domain->objs[id].svc = m->obj_out_svc[i];
      ss->domain->objs[id].state = m->obj_out_state[i];
      out_obj_ids[i] = id + 1;
    } else {
      m->move_out[m->nmove_out++] = session_new(m->obj_out_svc[i], m->obj_out_state[i], 0);
    }
  }

  /* ---- reply ---- */
  if (!close) {
    uint8_t *q = msg + 8;
    int special = m->ncopy_out || m->nmove_out;
    if (special) {
      wr32(q, (uint32_t)(m->ncopy_out << 1 | m->nmove_out << 5));
      q += 4;
      for (int i = 0; i < m->ncopy_out; i++, q += 4) wr32(q, m->copy_out[i]);
      for (int i = 0; i < m->nmove_out; i++, q += 4) wr32(q, m->move_out[i]);
    }
    uint32_t osz = (m->out_size + 3) & ~3u;
    uint8_t *start = q;
    if (tipc) {
      wr32(q, m->result);
      memcpy(q + 4, m->out, osz);
      q += 4 + osz;
    } else {
      uint8_t *d = msg + ((q - msg + 15) & ~15);
      memset(q, 0, d - q);
      if (ss->domain && !is_control) {
        memset(d, 0, 16);
        wr32(d, (uint32_t)m->n_obj_out);
        d += 16;
      }
      wr32(d, MAGIC_SFCO);
      wr32(d + 4, 0);
      wr32(d + 8, m->result);
      wr32(d + 12, 0);
      memcpy(d + 16, m->out, osz);
      q = d + 16 + osz;
      if (ss->domain && !is_control)
        for (int i = 0; i < m->n_obj_out; i++, q += 4) wr32(q, out_obj_ids[i]);
      q += 16;  /* alignment slack the client expects in the data size */
    }
    uint32_t words = (uint32_t)((q - start + 3) / 4);
    wr32(msg, 0);
    wr32(msg + 4, words | (special ? 1u << 31 : 0));
  }
  free(m);
  pthread_mutex_unlock(&ipc_lock);
  return 0;
}
