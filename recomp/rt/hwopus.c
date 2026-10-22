/* Opus decoding (hwopus) -> IHardwareOpusDecoderManager, IHardwareOpusDecoder,
 * on libopus (tools/build_opus.sh). The game streams music and commentary
 * through multistream decoders. Each input packet carries an 8-byte header
 * (big-endian u32 size, u32 final range) before the Opus data; the output is
 * interleaved int16. Work buffers (transfer memory) are not used.
 * Command ids per switchbrew "Audio services". */
#include "ipc.h"
#include <opus_multistream.h>
#include <stdlib.h>

#define R_OPUS_INVALID_INPUT 0x1a06fu  /* hwopus (111) desc 208 */
#define R_OPUS_BAD_PARAMETERS 0x1006fu /* desc 128 */

typedef struct {
  uint32_t sample_rate, channels, streams, stereo_streams;
  uint8_t mapping[255];
} Params;

typedef struct {
  OpusMSDecoder *dec;
  uint32_t channels;
} Decoder;

/* OpusParameters(Ex) { u32 sample_rate, channel_count, ... }: one stream, coupled when stereo */
static Params single(uint32_t rate, uint32_t channels) {
  Params p = {rate, channels, 1, channels == 2, {0, 1}};
  return p;
}

/* MultiStreamParameters(Ex) { u32 sample_rate, channel_count, total_stream_count, stereo_stream_count;
 * u8 mapping[0x100]; ... } in an X buffer */
static int multi(IpcMsg *m, Params *p) {
  if (!m->nx || m->x[0].size < 0x10 + 1) return 0;
  const uint32_t *h = gptr(m->x[0].addr);
  *p = (Params){h[0], h[1], h[2], h[3]};
  if (p->channels > 255) return 0;
  memcpy(p->mapping, (const uint8_t *)h + 0x10, p->channels);
  return 1;
}

static int valid(const Params *p) {
  return p->channels && p->channels <= 255 && p->streams && p->stereo_streams <= p->streams &&
         p->streams + p->stereo_streams <= 255;
}

static uint32_t work_size(const Params *p) {
  int32_t n = valid(p) ? opus_multistream_decoder_get_size((int)p->streams, (int)p->stereo_streams) : 0;
  return n > 0 ? ((uint32_t)n + 0xfff) & ~0xfffu : 0x1000;
}

/* DecodeInterleaved*: (reset) packet -> (consumed, samples per channel[, perf]) */
static void decode(Decoder *d, IpcMsg *m, int reset, int perf) {
  const IpcBuf *in = m->na && m->a[0].size ? &m->a[0] : m->nx ? &m->x[0] : 0;
  const IpcBuf *out = m->nb && m->b[0].size ? &m->b[0] : m->nc ? &m->c[0] : 0;
  if (!in || !out || in->size < 8) { m->result = R_OPUS_INVALID_INPUT; return; }
  const uint8_t *src = gptr(in->addr);
  uint32_t size = (uint32_t)src[0] << 24 | src[1] << 16 | src[2] << 8 | src[3];
  if (size > in->size - 8) { m->result = R_OPUS_INVALID_INPUT; return; }
  if (reset) opus_multistream_decoder_ctl(d->dec, OPUS_RESET_STATE);
  int frames = (int)(out->size / (2 * d->channels));
  int n = opus_multistream_decode(d->dec, src + 8, (opus_int32)size, gptr(out->addr), frames, 0);
  if (n < 0) {
    HLOG("[hwopus] decode of %u bytes failed: %s\n", size, opus_strerror(n));
    m->result = R_OPUS_INVALID_INPUT;
    return;
  }
  static int logged;
  if (!logged && n > 0 && __atomic_fetch_add(&logged, 1, __ATOMIC_RELAXED) == 0)
    HLOG("[hwopus] first packet: %u bytes -> %d samples x %u channels\n", size, n, d->channels);
  SET_OUT(uint32_t, 0, 8 + size);
  SET_OUT(uint32_t, 4, (uint32_t)n);
  if (perf) SET_OUT(uint64_t, 8, 0);
}

/* ---- IHardwareOpusDecoder ---- */
static void decoder(void *state, IpcMsg *m) {
  Decoder *d = state;
  switch (m->cmd) {
  case 0: case 2: decode(d, m, 0, 0); return;                 /* DecodeInterleaved(ForMultiStream)Old */
  case 4: case 5: decode(d, m, 0, 1); return;                 /* ...WithPerfOld */
  case 6: case 7: case 8: case 9: decode(d, m, IN(uint8_t, 0) != 0, 1); return;  /* ...WithPerfAndResetOld, ... */
  case 1: case 3: return;                                     /* SetContext(ForMultiStream) */
  default: HLOG("[hwopus] IHardwareOpusDecoder cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}

static void decoder_close(void *state) {
  Decoder *d = state;
  opus_multistream_decoder_destroy(d->dec);
  free(d);
}
static const Service decoder_svc = {"IHardwareOpusDecoder", decoder, decoder_close};

static void open_decoder(IpcMsg *m, const Params *p) {
  int err = OPUS_BAD_ARG;
  OpusMSDecoder *dec = valid(p) ? opus_multistream_decoder_create((opus_int32)p->sample_rate, (int)p->channels,
                                                                 (int)p->streams, (int)p->stereo_streams, p->mapping, &err)
                                : 0;
  if (!dec) {
    HLOG("[hwopus] can't open a decoder (%u Hz, %u channels, %u streams, %u stereo): %s\n", p->sample_rate, p->channels,
         p->streams, p->stereo_streams, opus_strerror(err));
    m->result = R_OPUS_BAD_PARAMETERS;
    return;
  }
  Decoder *d = calloc(1, sizeof *d);
  d->dec = dec;
  d->channels = p->channels;
  ipc_push_object(m, &decoder_svc, d);
}

/* ---- hwopus (IHardwareOpusDecoderManager) ---- */
static void hwopus(void *state, IpcMsg *m) {
  (void)state;
  Params p;
  switch (m->cmd) {
  case 0: case 4:  /* OpenHardwareOpusDecoder(Ex)(OpusParameters(Ex), u32 work size, tmem) */
    p = single(IN(uint32_t, 0), IN(uint32_t, 4));
    open_decoder(m, &p);
    return;
  case 1: case 5: case 8:  /* GetWorkBufferSize(Ex)(ExEx)(OpusParameters(Ex)) */
    p = single(IN(uint32_t, 0), IN(uint32_t, 4));
    SET_OUT(uint32_t, 0, work_size(&p));
    return;
  case 2: case 6:  /* OpenHardwareOpusDecoderForMultiStream(Ex)(u32 work size, tmem, X params) */
    if (!multi(m, &p)) { m->result = R_OPUS_BAD_PARAMETERS; return; }
    open_decoder(m, &p);
    return;
  case 3: case 7: case 9:  /* GetWorkBufferSizeForMultiStream(Ex)(ExEx)(X params) */
    if (!multi(m, &p)) { m->result = R_OPUS_BAD_PARAMETERS; return; }
    SET_OUT(uint32_t, 0, work_size(&p));
    return;
  default: HLOG("[hwopus] IHardwareOpusDecoderManager cmd %u unhandled -> success (in %u bytes)\n", m->cmd, m->in_size);
  }
}
const Service hwopus_service = {"hwopus", hwopus};
