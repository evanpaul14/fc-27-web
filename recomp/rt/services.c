/* Registry of host services reachable through sm:GetServiceHandle. */
#include "ipc.h"

extern const Service applet_oe_service, time_u_service, time_a_service, time_s_service, fsp_srv_service,
    apm_service, pctl_service, pctl_s_service, pctl_a_service, pctl_r_service;

static const Service *const services[] = {
  &applet_oe_service,
  &apm_service,
  &pctl_service,
  &pctl_s_service,
  &pctl_a_service,
  &pctl_r_service,
  &time_u_service,
  &time_a_service,
  &time_s_service,
  &fsp_srv_service,
  0,
};

const Service *service_find(const char *name) {
  for (int i = 0; services[i]; i++)
    if (!strcmp(services[i]->name, name)) return services[i];
  return 0;
}
