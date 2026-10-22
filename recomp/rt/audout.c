/* Audio output (audout:u) -> IAudioOut. Appended buffers (PCM int16 at
 * 48 kHz, copied at append) are played by a CoreAudio output unit: its render
 * callback consumes them in order, releases each one when its last sample is
 * out and signals the buffer event, so the device clock paces the game's audio
 * thread. 6 channels (FL FR FC LFE BL BR) are mixed down to stereo.
 * Without a device (FIFA_AUDIO=0, or FIFA_HIDDEN=1 unless FIFA_AUDIO=1) a null
 * sink thread releases each buffer after its duration instead.
 * Command ids per switchbrew "Audio services". */
#include "ipc.h"
#include <AudioToolbox/AudioToolbox.h>
#include <pthread.h>
#include <stdlib.h>
#include <time.h>

#define R_AUDIO_BUFFER_FULL 0x299u  /* audio 1: too many buffers appended */
#define SAMPLE_RATE 48000
#define MAX_BUFS 32

enum { STATE_STARTED = 0, STATE_STOPPED = 1 };

typedef struct {
  uint64_t tag, frames, pos;  /* pos: frames already played */
  int16_t *pcm;               /* frames * channels, interleaved; freed once released */
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
  AudioComponentInstance unit;  /* 0: null sink */
} AudioOut;

static void release_head(AudioOut *o) {  /* o->lock held */
  if (o->nreleased < MAX_BUFS) o->released[o->nreleased++] = o->queued[0];
  else free(o->queued[0].pcm);
  memmove(o->queued, o->queued + 1, --o->nqueued * sizeof *o->queued);
}

/* CoreAudio render callback: stereo float32 interleaved. */
static OSStatus render(void *arg, AudioUnitRenderActionFlags *flags, const AudioTimeStamp *ts, UInt32 bus, UInt32 n,
                       AudioBufferList *bl) {
  (void)flags; (void)ts; (void)bus;
  AudioOut *o = arg;
  float *out = bl->mBuffers[0].mData;
  uint32_t i = 0;
  int signal = 0;
  pthread_mutex_lock(&o->lock);
  float v = o->volume * (1.0f / 32768.0f);
  while (i < n && o->state == STATE_STARTED && o->nqueued) {
    Buf *b = &o->queued[0];
    uint32_t take = (uint32_t)(b->frames - b->pos) < n - i ? (uint32_t)(b->frames - b->pos) : n - i;
    const int16_t *p = b->pcm ? b->pcm + b->pos * o->channels : 0;
    for (uint32_t k = 0; k < take; k++, i++) {
      float l = 0, r = 0;
      if (p) {
        const int16_t *f = p + k * o->channels;
        if (o->channels == 1) l = r = f[0];
        else if (o->channels == 2) l = f[0], r = f[1];
        else l = f[0] + 0.7071f * (f[2] + f[4]), r = f[1] + 0.7071f * (f[2] + f[5]);
      }
      out[2 * i] = l * v, out[2 * i + 1] = r * v;
    }
    b->pos += take;
    o->played += take;
    if (b->pos == b->frames) release_head(o), signal = 1;
  }
  pthread_mutex_unlock(&o->lock);
  for (; i < n; i++) out[2 * i] = out[2 * i + 1] = 0;  /* underrun or stopped */
  if (signal) kevent_signal(o->event);
  return noErr;
}

static int audio_enabled(void) {
  const char *e = getenv("FIFA_AUDIO");
  return e ? atoi(e) != 0 : !getenv("FIFA_HIDDEN");
}

static AudioComponentInstance open_unit(AudioOut *o) {
  AudioComponentDescription d = {kAudioUnitType_Output, kAudioUnitSubType_DefaultOutput, kAudioUnitManufacturer_Apple, 0, 0};
  AudioComponent comp = AudioComponentFindNext(0, &d);
  AudioComponentInstance u = 0;
  if (!comp || AudioComponentInstanceNew(comp, &u)) return 0;
  AudioStreamBasicDescription f = {
      .mSampleRate = SAMPLE_RATE, .mFormatID = kAudioFormatLinearPCM,
      .mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
      .mBytesPerPacket = 8, .mFramesPerPacket = 1, .mBytesPerFrame = 8, .mChannelsPerFrame = 2, .mBitsPerChannel = 32};
  AURenderCallbackStruct cb = {render, o};
  if (AudioUnitSetProperty(u, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &f, sizeof f) ||
      AudioUnitSetProperty(u, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &cb, sizeof cb) ||
      AudioUnitInitialize(u) || AudioOutputUnitStart(u)) {
    AudioComponentInstanceDispose(u);
    return 0;
  }
  return u;
}

static uint64_t now_ns(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }

/* Each buffer ends at an absolute deadline (the last one's end, or now after an underrun), so sleep overshoot doesn't
 * add up: relative sleeps ran the audio clock slower than real time, and the game waits on its clips' ends. */
static void *sink_thread(void *arg) {
  AudioOut *o = arg;
  uint64_t deadline = 0;
  pthread_mutex_lock(&o->lock);
  for (;;) {
    while (!o->closing && (o->state != STATE_STARTED || !o->nqueued)) pthread_cond_wait(&o->cv, &o->lock);
    if (o->closing) break;
    uint64_t frames = o->queued[0].frames, t = now_ns();
    if (deadline < t) deadline = t;
    deadline += frames * 1000000000ull / SAMPLE_RATE;
    pthread_mutex_unlock(&o->lock);
    if (deadline > t) {
      struct timespec ts = {(time_t)((deadline - t) / 1000000000), (long)((deadline - t) % 1000000000)};
      nanosleep(&ts, 0);
    }
    pthread_mutex_lock(&o->lock);
    if (o->state != STATE_STARTED || !o->nqueued) continue;  /* stopped or flushed meanwhile */
    release_head(o);
    o->played += frames;
    kevent_signal(o->event);
  }
  pthread_mutex_unlock(&o->lock);
  return 0;
}

static void copy_event(IpcMsg *m, KEvent *e) { m->copy_out[m->ncopy_out++] = kobj_new(H_EVENT_R, e); }

/* AudioOutBuffer { u64 next, sample_buffer, capacity, data_size, data_offset } */
static void append(AudioOut *o, IpcMsg *m, const IpcBuf *b) {
  uint64_t tag = IN(uint64_t, 0), size = 0, data = 0;
  if (b && b->size >= 0x28) {
    const uint64_t *ob = gptr(b->addr);
    data = ob[1] + ob[4], size = ob[3];
  }
  uint64_t frames = size / (2 * o->channels);
  int16_t *pcm = 0;
  if (o->unit && frames && data) {
    pcm = malloc(frames * 2 * o->channels);
    memcpy(pcm, gptr(data), frames * 2 * o->channels);
  }
  pthread_mutex_lock(&o->lock);
  if (o->nqueued == MAX_BUFS) {
    m->result = R_AUDIO_BUFFER_FULL;
    free(pcm);
  } else {
    o->queued[o->nqueued++] = (Buf){tag, frames, 0, pcm};
    pthread_cond_signal(&o->cv);
  }
  pthread_mutex_unlock(&o->lock);
}

static void released(AudioOut *o, IpcMsg *m, const IpcBuf *b) {
  uint32_t cap = b ? (uint32_t)(b->size / 8) : 0, n = 0;
  uint64_t *tags = b ? gptr(b->addr) : 0;
  pthread_mutex_lock(&o->lock);
  for (; n < cap && n < (uint32_t)o->nreleased; n++) tags[n] = o->released[n].tag, free(o->released[n].pcm);
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
    while (o->nqueued) release_head(o);
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
  if (o->unit) AudioOutputUnitStop(o->unit);
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
    if (audio_enabled()) o->unit = open_unit(o);
    if (!o->unit) {
      pthread_t t;
      pthread_create(&t, 0, sink_thread, o);
      pthread_detach(t);
    }
    HLOG("[audout] open: %u Hz, %u channels -> %s\n", rate, ch, o->unit ? "CoreAudio" : "null sink");
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
