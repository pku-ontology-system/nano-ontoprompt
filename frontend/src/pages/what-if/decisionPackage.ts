import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClientV2 } from '@/api/client'

export type DecisionRunContext = { ontologyId: string; runId: string; scenarioId: string; scenarioRevision: number; nodeKey: string; artifact?: { id: string; content_digest: string } }
export type PackageEdit = { sequence: number; op: string; target?: { concrete_type: string; object_id: string } | null; property?: string | null; value?: unknown; expected_old_value?: unknown; properties?: Record<string, unknown> }
export type PackageAction = { id: string; action_type_id: string; expected_edits: PackageEdit[]; external_effect_class: string; receipt: Record<string, unknown> | null }
export type DecisionPackage = { id: string; run_id: string; evidence: Array<{ reference: { candidate_id?: string } }>; override: OverrideInfo | null; status: string; etag: number; created_by: string; title: string; actions: PackageAction[]; outbox: Array<{ id: string; delivered: boolean; attempts: number }>; events: Array<{ sequence: number; event_type: string; actor_id: string; reason: string }> }
export type Delivery = { external_effects: number; delivered: number; status: 'complete' | 'external_effect_partial' }

/** Choosing a plan other than the recommended one is a recorded human override: the reason is evidence on the package. */
export type OverrideFigures = { metric_after: number; cost: number; protected: string[]; harmed: string[]; applicable: boolean }
export type OverrideMetric = { key?: string; label?: string; unit?: string; direction?: 'minimize' | 'maximize'; baseline?: number }
export type OverrideInfo = {
  recommended_candidate_id: string; recommended_label: string; chosen_candidate_id: string; chosen_label: string
  metric: OverrideMetric; recommended: OverrideFigures; chosen: OverrideFigures; reason?: string
}
export type PlanLike = { id: string; label?: string; applicable?: boolean; recommended?: boolean; metric_after?: number; cost?: number; protected?: string[]; harmed?: string[] }

const figures = (plan: PlanLike): OverrideFigures => ({ metric_after: plan.metric_after ?? 0, cost: plan.cost ?? 0, protected: plan.protected ?? [], harmed: plan.harmed ?? [], applicable: plan.applicable !== false })

/** Before asking the server: the Function marks the plan it recommends (`recommended`). The server stays the authority. */
export function predictOverride(plans: PlanLike[], chosenId: string, metric?: OverrideMetric): OverrideInfo | null {
  const recommended = plans.find(plan => plan.recommended === true && plan.applicable !== false)
  const chosen = plans.find(plan => plan.id === chosenId)
  if (!recommended || !chosen || recommended.id === chosen.id) return null
  return { recommended_candidate_id: recommended.id, recommended_label: recommended.label ?? recommended.id, chosen_candidate_id: chosen.id,
    chosen_label: chosen.label ?? chosen.id, metric: metric ?? {}, recommended: figures(recommended), chosen: figures(chosen) }
}

/** The server answers a missing reason with the comparison it wants the author to see. */
export function overrideFromError(error: unknown): OverrideInfo | null {
  const detail = (error as { detail?: { code?: unknown; details?: unknown } } | null)?.detail
  return detail?.code === 'override_reason_required' && detail.details ? detail.details as OverrideInfo : null
}

// Seeded object ids may carry an ontology namespace ("5ef77329:ORD-1009"); show the business id.
export const plain = (text: string) => text.replace(/\b[0-9a-f]{8}:/g, '')

export function describe(values: Record<string, unknown>) {
  return plain(Object.entries(values).filter(([key, value]) => key !== 'action_type' && value !== null && value !== undefined && !(Array.isArray(value) && !value.length))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(',') : typeof value === 'object' ? String((value as { object_id?: string }).object_id ?? JSON.stringify(value)) : String(value)}`).join(' · '))
}

/** The run's package is server state: it survives refreshes and reflects reviews done by other people. */
export function useRunPackage(context?: DecisionRunContext) {
  const queryClient = useQueryClient()
  const key = ['decision-packages', context?.ontologyId]
  const packages = useQuery({ queryKey: key, queryFn: () => apiClientV2.get<{ packages: DecisionPackage[] }>(`/ontologies/${context!.ontologyId}/decision-packages`), enabled: !!context, refetchInterval: 5000 })
  const pkg = packages.data?.packages.find(row => row.run_id === context?.runId && row.status !== 'superseded') ?? null
  const setPkg = (row: DecisionPackage) => queryClient.setQueryData<{ packages: DecisionPackage[] }>(key, current => ({ packages: [row, ...(current?.packages ?? []).filter(item => item.id !== row.id)] }))
  return { pkg, setPkg, candidateId: pkg?.evidence.find(row => row.reference.candidate_id)?.reference.candidate_id }
}

