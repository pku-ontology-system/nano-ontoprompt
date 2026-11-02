import { apiClientV2 } from '@/api/client'

export interface ServicePrincipalGrant {
  id: string
  service_principal_id: string
  resource_type: string
  resource_id: string
  permission: string
  active: boolean
  created_by: string | null
  revoked_by: string | null
  reason: string | null
  created_at: string | null
  revoked_at: string | null
}

export interface ServicePrincipal {
  id: string
  kind: 'service' | 'system'
  name: string
  description: string | null
  status: 'active' | 'suspended'
  builtin: boolean
  owner_user_id: string | null
  revision: number
  created_at: string | null
  updated_at: string | null
  grants: ServicePrincipalGrant[]
}

export interface ServicePrincipalAuditEvent {
  id: string
  sequence: number
  event_type: string
  actor_user_id: string | null
  grant_id: string | null
  reason: string | null
  payload: Record<string, unknown>
  previous_digest: string | null
  event_digest: string
  chain_verified: boolean
  created_at: string | null
}

export interface ServicePrincipalAuditChain {
  items: ServicePrincipalAuditEvent[]
  chain_status: 'verified' | 'invalid' | 'truncated' | 'empty'
  verified_event_count: number
  total_event_count: number
}

const servicePrincipalsApi = {
  list: (params?: { pipeline_id?: string }) =>
    apiClientV2.get<{ items: ServicePrincipal[] }>('/service-principals', { params }).then(r => r),
  create: (body: { name: string; description?: string }) =>
    apiClientV2.post<{ principal: ServicePrincipal }>('/service-principals', body).then(r => r),
  grantPipelineRun: (principalId: string, body: { pipeline_id: string; reason: string }) =>
    apiClientV2.post<{ grant: ServicePrincipalGrant; principal_revision: number }>(
      `/service-principals/${principalId}/pipeline-run-grants`, body,
    ).then(r => r),
  revokeGrant: (principalId: string, grantId: string, reason: string) =>
    apiClientV2.post<{ grant: ServicePrincipalGrant; principal_revision: number }>(
      `/service-principals/${principalId}/grants/${grantId}/revoke`, { reason },
    ).then(r => r),
  updateStatus: (principalId: string, status: ServicePrincipal['status'], reason: string) =>
    apiClientV2.patch<{ principal: ServicePrincipal }>(
      `/service-principals/${principalId}/status`, { status, reason },
    ).then(r => r),
  events: (principalId: string) =>
    apiClientV2.get<ServicePrincipalAuditChain>(
      `/service-principals/${principalId}/events`,
    ).then(r => r),
}

export default servicePrincipalsApi
