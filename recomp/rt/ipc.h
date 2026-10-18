/* Host-side IPC services (HIPC/CMIF/TIPC, per switchbrew). */
#pragma once
#include "host.h"

typedef struct { uint64_t addr, size; } IpcBuf;

typedef struct IpcMsg {
  /* request */
  uint32_t type, cmd;
  const uint8_t *in;  /* parameters after the CMIF header */
  uint32_t in_size;
  uint64_t pid;
  uint32_t copy_in[8], move_in[8];
  int ncopy_in, nmove_in;
  IpcBuf x[16], a[16], b[16], w[16], c[16];
  int nx, na, nb, nw, nc;
  uint32_t in_objects[8];
  int n_in_objects;
  /* response */
  uint32_t result;
  uint8_t out[0x200];
  uint32_t out_size;
  uint32_t copy_out[8], move_out[8];
  int ncopy_out, nmove_out;
  struct Service const *obj_out_svc[8];
  void *obj_out_state[8];
  int n_obj_out;
} IpcMsg;

typedef struct Service {
  const char *name;
  void (*handle)(void *state, IpcMsg *m);
} Service;

/* Find a host service by sm name, or NULL. */
const Service *service_find(const char *name);
/* Return a new interface object from a command (domain object or session). */
void ipc_push_object(IpcMsg *m, const Service *s, void *state);

#define IN(type, off) (*(const type *)(m->in + (off)))
#define OUT(type, off) (*(type *)(m->out + (off)))
static inline void out_size(IpcMsg *m, uint32_t n) { if (n > m->out_size) m->out_size = n; }
#define SET_OUT(type, off, v) do { OUT(type, off) = (v); out_size(m, (off) + sizeof(type)); } while (0)

/* Generic handler: logs and answers success with zeroed outputs. */
void service_stub(void *state, IpcMsg *m);
