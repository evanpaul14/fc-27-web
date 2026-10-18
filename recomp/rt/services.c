/* Registry of host services reachable through sm:GetServiceHandle. */
#include "ipc.h"

static const Service *const services[] = {
  0,
};

const Service *service_find(const char *name) {
  for (int i = 0; services[i]; i++)
    if (!strcmp(services[i]->name, name)) return services[i];
  return 0;
}
