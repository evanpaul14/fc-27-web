/* Registry of host services reachable through sm:GetServiceHandle. */
#include "ipc.h"

extern const Service applet_oe_service, time_u_service, time_a_service, time_s_service, fsp_srv_service,
    apm_service, pctl_service, pctl_s_service, pctl_a_service, pctl_r_service, nifm_u_service, nifm_a_service, nifm_s_service, aoc_u_service, audout_u_service, hid_service, acc_u0_service;

static const Service *const services[] = {
  &applet_oe_service,
  &aoc_u_service,
  &audout_u_service,
  &apm_service,
  &nifm_u_service,
  &nifm_a_service,
  &nifm_s_service,
  &pctl_service,
  &pctl_s_service,
  &pctl_a_service,
  &pctl_r_service,
  &time_u_service,
  &time_a_service,
  &time_s_service,
  &fsp_srv_service,
  &hid_service,
  &acc_u0_service,
  0,
};

const Service *service_find(const char *name) {
  for (int i = 0; services[i]; i++)
    if (!strcmp(services[i]->name, name)) return services[i];
  return 0;
}
