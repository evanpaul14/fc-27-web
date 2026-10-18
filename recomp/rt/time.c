/* time:u (IStaticService): clocks and time zone from the host's clock, in UTC.
 *
 * nnSdk reads most clocks straight from the service's shared memory
 * (nn::time::detail::SharedMemoryClient); layout from its readers. Each
 * field is a lock-free pair: u32 counter, then two copies, read from copy
 * (counter & 1) and retried if the counter moved.
 *   0x38 standard user (local) system clock context, 2 x 0x20
 *   0x80 standard network system clock context, 2 x 0x20
 *   0xC8 automatic correction enabled, 2 x u8 at 0xCC
 *   0xD0 steady clock ContinuousAdjustmentTimePoint, 2 x 0x38 at 0xD8:
 *        {base_ns, mult, shift, offset, limit, source_id[16]}; steady
 *        seconds = (now_ns + clamp(((now_ns - base_ns) * mult >> shift) + offset, limit)) / 1e9
 * System clock time = steady seconds + context.offset, valid only while the
 * context's source id matches the steady clock's. */
#include "ipc.h"
#include <pthread.h>
#include <time.h>

typedef struct { int64_t time_point; uint8_t source_id[16]; } SteadyClockTimePoint;
typedef struct { int64_t offset; SteadyClockTimePoint steady; } SystemClockContext;
typedef struct {
  int64_t base_ns, mult, shift, offset, limit;
  uint8_t source_id[16];
} ContinuousAdjustmentTimePoint;
_Static_assert(sizeof(SystemClockContext) == 0x20, "SystemClockContext");
_Static_assert(sizeof(ContinuousAdjustmentTimePoint) == 0x38, "ContinuousAdjustmentTimePoint");

static const uint8_t clock_source_id[16] = "fifa-web-steady";
static KSharedMemory *time_shmem;
static int64_t clock_offset;  /* unix seconds - steady seconds */

/* Same tick source as CNTPCT_EL0 (CLOCK_MONOTONIC). */
static int64_t steady_seconds(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec;
}
static SteadyClockTimePoint steady_now(void) {
  SteadyClockTimePoint p = {steady_seconds(), {0}};
  memcpy(p.source_id, clock_source_id, 16);
  return p;
}
static SystemClockContext system_clock_context(void) {
  SystemClockContext c = {clock_offset, steady_now()};
  return c;
}

static void time_setup(void) {
  clock_offset = (int64_t)time(0) - steady_seconds();
  time_shmem = kshmem_create(0x1000, 0);
  uint8_t *p = kshmem_ptr(time_shmem);
  SystemClockContext ctx = system_clock_context();
  ContinuousAdjustmentTimePoint adj = {0};
  memcpy(adj.source_id, clock_source_id, 16);
  for (int i = 0; i < 2; i++) {
    memcpy(p + 0x40 + 0x20 * i, &ctx, 0x20);
    memcpy(p + 0x88 + 0x20 * i, &ctx, 0x20);
    p[0xCC + i] = 0;
    memcpy(p + 0xD8 + 0x38 * i, &adj, 0x38);
  }
}
static void time_init(void) {
  static pthread_once_t once = PTHREAD_ONCE_INIT;
  pthread_once(&once, time_setup);
}

static void unhandled(const char *iface, IpcMsg *m) {
  HLOG("[time] %s cmd %u unhandled -> success (in %u bytes)\n", iface, m->cmd, m->in_size);
}

/* ---- ISystemClock ---- */
static void system_clock(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: SET_OUT(int64_t, 0, steady_seconds() + clock_offset); return;  /* GetCurrentTime */
  case 2: {                                                              /* GetSystemClockContext */
    SystemClockContext c = system_clock_context();
    memcpy(m->out, &c, sizeof c);
    out_size(m, sizeof c);
    return;
  }
  case 1: case 3: return;  /* SetCurrentTime, SetSystemClockContext: ignored */
  default: unhandled("ISystemClock", m);
  }
}
static const Service system_clock_svc = {"ISystemClock", system_clock};

/* ---- ISteadyClock ---- */
static void steady_clock(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: {  /* GetCurrentTimePoint */
    SteadyClockTimePoint p = steady_now();
    memcpy(m->out, &p, sizeof p);
    out_size(m, sizeof p);
    return;
  }
  case 2: case 200: SET_OUT(int64_t, 0, 0); return;  /* GetTestOffset, GetInternalOffset (ns) */
  case 100: SET_OUT(int64_t, 0, (int64_t)time(0)); return;  /* GetRtcValue */
  case 101: SET_OUT(uint8_t, 0, 0); return;                 /* IsRtcResetDetected */
  case 102: SET_OUT(uint32_t, 0, 0); return;                /* GetSetupResultValue */
  case 3: case 201: return;                                 /* SetTestOffset, SetInternalOffset */
  default: unhandled("ISteadyClock", m);
  }
}
static const Service steady_clock_svc = {"ISteadyClock", steady_clock};

/* ---- ITimeZoneService (UTC only) ---- */
typedef struct { int16_t year; int8_t month, day, hour, minute, second, pad; } CalendarTime;  /* month 1-12 */
typedef struct {
  uint32_t day_of_week, day_of_year;
  char tz_name[8];
  uint32_t is_dst;
  int32_t gmt_offset;
} CalendarAdditionalInfo;

static void to_calendar(IpcMsg *m, int64_t posix) {
  time_t t = (time_t)posix;
  struct tm tm;
  gmtime_r(&t, &tm);
  CalendarTime ct = {(int16_t)(tm.tm_year + 1900), (int8_t)(tm.tm_mon + 1), (int8_t)tm.tm_mday, (int8_t)tm.tm_hour,
                     (int8_t)tm.tm_min, (int8_t)tm.tm_sec, 0};
  CalendarAdditionalInfo ai = {(uint32_t)tm.tm_wday, (uint32_t)tm.tm_yday, "UTC", 0, 0};
  memcpy(m->out, &ct, 8);
  memcpy(m->out + 8, &ai, sizeof ai);
  out_size(m, 8 + sizeof ai);
}

static void to_posix(IpcMsg *m) {
  CalendarTime ct;
  memcpy(&ct, m->in, sizeof ct);
  struct tm tm = {.tm_year = ct.year - 1900, .tm_mon = ct.month - 1, .tm_mday = ct.day,
                  .tm_hour = ct.hour, .tm_min = ct.minute, .tm_sec = ct.second};
  int64_t t = (int64_t)timegm(&tm);
  IpcBuf *out = m->nb ? &m->b[0] : m->nc ? &m->c[0] : 0;
  if (out && out->size >= 8) memcpy(gptr(out->addr), &t, 8);
  SET_OUT(uint32_t, 0, out ? 1 : 0);
}

static void time_zone(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0:  /* GetDeviceLocationName -> LocationName char[0x24] */
    memset(m->out, 0, 0x24);
    memcpy(m->out, "UTC", 3);
    out_size(m, 0x24);
    return;
  case 2: SET_OUT(uint32_t, 0, 1); return;  /* GetTotalLocationNameCount */
  case 4:                                   /* LoadTimeZoneRule(name) -> B TimeZoneRule: opaque, we only do UTC */
    if (m->nb) memset(gptr(m->b[0].addr), 0, m->b[0].size);
    return;
  case 5:  /* GetTimeZoneRuleVersion -> u128 */
    memset(m->out, 0, 16);
    memcpy(m->out, "2026a", 5);
    out_size(m, 16);
    return;
  case 100: case 101: to_calendar(m, IN(int64_t, 0)); return;  /* ToCalendarTime(WithMyRule) */
  case 201: case 202: to_posix(m); return;                      /* ToPosixTime(WithMyRule) */
  case 1: return;                                               /* SetDeviceLocationName */
  default: unhandled("ITimeZoneService", m);
  }
}
static const Service time_zone_svc = {"ITimeZoneService", time_zone};

/* ---- IStaticService ---- */
static void static_service(void *state, IpcMsg *m) {
  (void)state;
  time_init();
  switch (m->cmd) {
  case 0: case 1: case 4: case 5:
    /* GetStandardUserSystemClock, GetStandardNetworkSystemClock, GetStandardLocalSystemClock,
     * GetEphemeralNetworkSystemClock */
    ipc_push_object(m, &system_clock_svc, 0);
    return;
  case 2: ipc_push_object(m, &steady_clock_svc, 0); return;  /* GetStandardSteadyClock */
  case 3: ipc_push_object(m, &time_zone_svc, 0); return;     /* GetTimeZoneService */
  case 20: m->copy_out[m->ncopy_out++] = kobj_new(H_SHMEM, time_shmem); return;  /* GetSharedMemoryNativeHandle */
  case 100: SET_OUT(uint8_t, 0, 0); return;     /* IsStandardUserSystemClockAutomaticCorrectionEnabled */
  case 102: SET_OUT(int32_t, 0, 2026); return;  /* GetStandardUserSystemClockInitialYear */
  case 200: SET_OUT(uint8_t, 0, 1); return;     /* IsStandardNetworkSystemClockAccuracySufficient */
  case 101: return;                             /* SetStandardUserSystemClockAutomaticCorrectionEnabled */
  default: unhandled("IStaticService", m);
  }
}
const Service time_u_service = {"time:u", static_service};
const Service time_a_service = {"time:a", static_service};
const Service time_s_service = {"time:s", static_service};
