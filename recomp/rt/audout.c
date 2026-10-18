/* Audio output (audout:u) -> IAudioOut: a null sink that plays in real time.
 * Appended buffers are released one after another, each after its own
 * duration at 48 kHz, and the buffer event is signaled, so the game's audio
 * thread runs at the pace it would on hardware. Samples are discarded; a
 * host backend (CoreAudio, WebAudio) would read them in sink_thread.
 * Command ids per switchbrew "Audio services". */
#include "ipc.h"
#include <pthread.h>
#include <stdlib.h>
#include <time.h>

#define R_AUDIO_BUFFER_FULL 0x299u  /* audio 1: too many buffers appended */
#define SAMPLE_RATE 48000
#define MAX_BUFS 32

enum { STATE_STARTED = 0, STATE_STOPPED = 1 };

typedef struct {
  uint64_t tag, frames;
} Buf;

typedef struct {
  pthread_mutex_t lock;
  pthread_cond_t cv;
  KEvent *event;
  uint32_t channels, state;
  float volume;
  Buf queued[MAX_BUFS], released[MAX_BUFS];
  int nqueued, nreleased;
  uint64_t played;  /* samples per channel */
  int closing;
} AudioOut;

static void sink_thread_sleep(uint64_t ns) {
  struct timespec ts = {(time_t)(ns / 1000000000), (long)(ns % 1000000000)};
  nanosleep(&ts, 0);
}

static void *sink_thread(void *arg) {
  AudioOut *o = arg;
  pthread_mutex_lock(&o->lock);
  for (;;) {
    while (!o->closing && (o->state != STATE_STARTED || !o->nqueued)) pthread_cond_wait(&o->cv, &o->lock);
    if (o->closing) break;
    uint64_t frames = o->queued[0].frames;
    pthread_mutex_unlock(&o->lock);
    sink_thread_sleep(frames * 1000000000ull / SAMPLE_RATE);
    pthread_mutex_lock(&o->lock);
    if (o->state != STATE_STARTED || !o->nqueued) continue;  /* stopped or flushed meanwhile */
    if (o->nreleased < MAX_BUFS) o->released[o->nreleased++] = o->queued[0];
    memmove(o->queued, o->queued + 1, --o->nqueued * sizeof *o->queued);
    o->played += frames;
    kevent_signal(o->event);
  }
  pthread_mutex_unlock(&o->lock);
  return 0;
}

static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

/* AudioOutBuffer { u64 next, sample_buffer, capacity, data_size, data_offset } */
static void append(AudioOut *o, IpcMsg *m, const IpcBuf *b) {
  uint64_t tag = IN(uint64_t, 0), size = 0;
  if (b && b->size >= 0x28) size = *(uint64_t *)gptr(b->addr + 0x18);
  pthread_mutex_lock(&o->lock);
  if (o->nqueued == MAX_BUFS) {
    m->result = R_AUDIO_BUFFER_FULL;
  } else {
    o->queued[o->nqueued++] = (Buf){tag, size / (2 * o->channels)};
    pthread_cond_signal(&o->cv);
  }
  pthread_mutex_unlock(&o->lock);
}

static void released(AudioOut *o, IpcMsg *m, const IpcBuf *b) {
  uint32_t cap = b ? (uint32_t)(b->size / 8) : 0, n = 0;
  uint64_t *tags = b ? gptr(b->addr) : 0;
  pthread_mutex_lock(&o->lock);
  for (; n < cap && n < (uint32_t)o->nreleased; n++) tags[n] = o->released[n].tag;
  memmove(o->released, o->released + n, (o->nreleased - n) * sizeof *o->released);
  o->nreleased -= n;
  if (!o->nreleased) kevent_clear(o->event);
  pthread_mutex_unlock(&o->lock);
  if (b && n < cap) tags[n] = 0;
  SET_OUT(uint32_t, 0, n);
}

static int contains(AudioOut *o, uint64_t tag) {
  for (int i = 0; i < o->nqueued; i++)
    if (o->queued[i].tag == tag) return 1;
  for (int i = 0; i < o->nreleased; i++)
    if (o->released[i].tag == tag) return 1;
  return 0;
}

/* ---- IAudioOut ---- */
static void audio_out(void *state, IpcMsg *m) {
  AudioOut *o = state;
  switch (m->cmd) {
  case 0: SET_OUT(uint32_t, 0, o->state); return;  /* GetAudioOutState */
  case 1: case 2:                                  /* Start, Stop */
    pthread_mutex_lock(&o->lock);
    o->state = m->cmd == 1 ? STATE_STARTED : STATE_STOPPED;
    pthread_cond_signal(&o->cv);
    pthread_mutex_unlock(&o->lock);
    return;
  case 3: append(o, m, m->na ? &m->a[0] : 0); return;  /* AppendAudioOutBuffer(tag, A buffer) */
  case 7: append(o, m, m->nx ? &m->x[0] : 0); return;  /* AppendAudioOutBufferAuto */
  case 4: copy_event(m, o->event); return;             /* RegisterBufferEvent */
  case 5: released(o, m, m->nb ? &m->b[0] : 0); return;  /* GetReleasedAudioOutBuffers */
  case 8: released(o, m, m->nc ? &m->c[0] : 0); return;  /* GetReleasedAudioOutBuffersAuto */
  case 6:                                                /* ContainsAudioOutBuffer(tag) */
    pthread_mutex_lock(&o->lock);
    SET_OUT(uint8_t, 0, (uint8_t)contains(o, IN(uint64_t, 0)));
    pthread_mutex_unlock(&o->lock);
    return;
  case 9: SET_OUT(uint32_t, 0, (uint32_t)o->nqueued); return;  /* GetAudioOutBufferCount */
  case 10: SET_OUT(uint64_t, 0, o->played); return;            /* GetAudioOutPlayedSampleCount */
  case 11:                                                     /* FlushAudioOutBuffers -> any flushed */
    pthread_mutex_lock(&o->lock);
    SET_OUT(uint8_t, 0, o->nqueued != 0);
    while (o->nqueued && o->nreleased < MAX_BUFS) {
      o->released[o->nreleased++] = o->queued[0];
      memmove(o->queued, o->queued + 1, --o->nqueued * sizeof *o->queued);
    }
    if (o->nreleased) kevent_signal(o->event);
    pthread_mutex_unlock(&o->lock);
    return;
  case 12: o->volume = IN(float, 0); return;  /* SetAudioOutVolume */
  case 13: SET_OUT(float, 0, o->volume); return;
  default: HLOG("[audout] IAudioOut cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}

static void audio_out_close(void *state) {
  AudioOut *o = state;
  pthread_mutex_lock(&o->lock);
  o->closing = 1;  /* the sink thread frees nothing: o stays allocated (rare, small) */
  pthread_cond_signal(&o->cv);
  pthread_mutex_unlock(&o->lock);
}
static const Service audio_out_svc = {"IAudioOut", audio_out, audio_out_close};

static void write_name(const IpcBuf *b) {
  static const char name[] = "DeviceOut";
  if (b && b->size) {
    memset(gptr(b->addr), 0, b->size);
    memcpy(gptr(b->addr), name, b->size < sizeof name ? b->size : sizeof name);
  }
}

/* ---- audout:u (IAudioOutManager) ---- */
static void audout(void *state, IpcMsg *m) {
  (void)state;
  switch (m->cmd) {
  case 0: case 2:  /* ListAudioOuts(Auto) -> count, names */
    write_name(m->cmd == 0 ? (m->nb ? &m->b[0] : 0) : (m->nc ? &m->c[0] : 0));
    SET_OUT(uint32_t, 0, 1);
    return;
  case 1: case 3: {  /* OpenAudioOut(Auto)(AudioOutInputParameter, aruid, name) */
    uint32_t rate = IN(uint32_t, 0), ch = IN(uint16_t, 4);
    if (rate && rate != SAMPLE_RATE) HLOG("[audout] requested %u Hz, using %u\n", rate, SAMPLE_RATE);
    AudioOut *o = calloc(1, sizeof *o);
    pthread_mutex_init(&o->lock, 0);
    pthread_cond_init(&o->cv, 0);
    uint32_t r;
    o->event = kevent_create(&r, 0);
    o->channels = ch == 1 ? 1 : ch > 2 ? 6 : 2;
    o->state = STATE_STOPPED;
    o->volume = 1.0f;
    pthread_t t;
    pthread_create(&t, 0, sink_thread, o);
    pthread_detach(t);
    write_name(m->cmd == 1 ? (m->nb ? &m->b[0] : 0) : (m->nc ? &m->c[0] : 0));
    /* AudioOutInputParameterInternal: sample rate, channels, format (2 = PCM int16), state */
    SET_OUT(uint32_t, 0, SAMPLE_RATE);
    SET_OUT(uint32_t, 4, o->channels);
    SET_OUT(uint32_t, 8, 2);
    SET_OUT(uint32_t, 12, STATE_STOPPED);
    ipc_push_object(m, &audio_out_svc, o);
    return;
  }
  default: HLOG("[audout] IAudioOutManager cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}
const Service audout_u_service = {"audout:u", audout};
