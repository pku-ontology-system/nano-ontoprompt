import { apiClientV2 } from '@/api/client'

export interface CuratedDataset {
  id: string
  name: string
  status: string
  row_count: number | null
  quality_score: number | null
  quality_status?: string | null
}

export function curatedReviewsBlockingDeleteCount(error: unknown): number | null {
  const value = error as {
    status?: unknown
    detail?: unknown
    response?: { status?: unknown; data?: { detail?: unknown } }
  } | null
  if ((value?.status ?? value?.response?.status) !== 409) return null
  const detail = value?.detail ?? value?.response?.data?.detail
  if (!detail || typeof detail !== 'object') return null
  const conflict = detail as { error?: unknown; review_count?: unknown }
  if (conflict.error !== 'CURATED_REVIEWS_REFERENCED'
      || typeof conflict.review_count !== 'number'
      || !Number.isSafeInteger(conflict.review_count)
      || conflict.review_count < 0) return null
  return conflict.review_count
}

export interface CuratedPreview {
  dataset_id: string
  name: string
  rows: Record<string, unknown>[]
  row_tokens: string[]
  count: number
  dataset_version_id: string | null
  latest_dataset_version_id?: string | null
  protected_fields?: string[]
  review_edits?: { applied: number; outside_preview: number }
  error?: string
}

export interface ReviewSession {
  review_id: string
  curated_dataset_id: string
  status: string
  dataset_version_id: string | null
  requested_by: string | null
  reviewer_id: string | null
  decided_by: string | null
  notes: string | null
  created_at: string
  decided_at: string | null
}

export interface CuratedRowEdit {
  id: string
  review_id: string
  edited_by: string | null
  row_pk: string
  field_name: string
  old_value: string | null
  new_value: string | null
  edited_at: string | null
}

export interface MappingApplyRun {
  run_id: string
  mapping_id: string
  review_id: string
  mapping_review_id: string | null
  requested_by: string | null
  request_actor_kind: 'user' | 'system' | null
  request_actor_id: string | null
  worker_actor_id: string | null
  ontology_id: string
  dataset_version_id: string
  mapping_snapshot: Record<string, unknown>
  mapping_digest: string
  status: string
  execution_attempts: number
  dispatch_attempts: number
  attempt_events: MappingApplyRunAttemptEvent[]
  attempt_history_state: 'complete' | 'in_progress' | 'legacy_untracked' | 'invalid'
  attempt_history_valid: boolean | null
  error_code: string | null
  error_message: string | null
  next_attempt_at: string | null
  completed_at: string | null
  projection?: MappingRowProjectionSummary
}

export interface MappingApplyRunAttemptEvent {
  sequence_no: number
  attempt_number: number
  digest_version: number
  event_type: 'attempt_started' | 'attempt_succeeded' | 'attempt_failed' | 'attempt_outcome_unknown'
  status: 'running' | 'succeeded' | 'retry_wait' | 'failed'
  actor_kind: 'worker' | 'system'
  actor_id: string
  celery_task_id: string | null
  error_code: string | null
  previous_digest: string | null
  event_digest: string
  occurred_at: string | null
}

export interface MappingDefinitionReviewEvent {
  sequence_no: number
  actor_kind: 'user' | 'system'
  actor_id: string | null
  event_type: string
  status: string
  detail: Record<string, unknown>
  created_at: string | null
}

export interface MappingDefinitionReview {
  review_id: string
  mapping_id: string
  revision_no: number
  candidate_digest: string
  status: 'pending_review' | 'approved' | 'rejected' | 'changes_requested'
  submitted_by: string | null
  reviewer_id: string | null
  decided_by: string | null
  decision: string | null
  decision_note: string | null
  created_at: string | null
  decided_at: string | null
  can_review: boolean
  deduplicated?: boolean
  events?: MappingDefinitionReviewEvent[]
}

export interface MappingLinkDefinitionReviewEvent {
  sequence_no: number
  actor_kind: 'user' | 'system'
  actor_id: string | null
  event_type: string
  status: string
  detail: Record<string, unknown>
  created_at: string | null
}

export interface MappingLinkDefinitionReview {
  review_id: string
  link_mapping_id: string
  ontology_id: string
  revision_no: number
  candidate: {
    relation_type: string
    src_key: string
    tgt_key: string
    src_dataset_id: string | null
    tgt_dataset_id: string | null
  }
  candidate_digest: string
  status: 'pending_review' | 'approved' | 'rejected' | 'changes_requested'
  submitted_by: string | null
  reviewer_id: string | null
  decided_by: string | null
  decision: string | null
  decision_note: string | null
  created_at: string | null
  decided_at: string | null
  can_review: boolean
  deduplicated?: boolean
  events?: MappingLinkDefinitionReviewEvent[]
}

export interface MappingLinkApplyPreview {
  preview_digest: string
  source_dataset_version_id: string
  target_dataset_version_id: string
  source_mapping_id: string
  target_mapping_id: string
  summary: {
    relation_type: string
    source_row_count: number
    target_row_count: number
    matched_relation_count: number
    added_relation_count: number
    removed_relation_count: number
    unchanged_relation_count: number
    unmatched_source_row_count: number
    unmatched_target_row_count: number
    cardinality: string
  }
}

export interface MappingLinkLegacyReconciliationCandidate {
  relation_id: string
  relation_type: string
  source_entity_id: string
  target_entity_id: string
  source_entity_type: string | null
  target_entity_type: string | null
  classification: string
  evidence: {
    legacy_id_convention_match: boolean
    mapping_signature_match: boolean
    exact_desired_pair_match: boolean
    source_in_current_dataset_version: boolean
    target_in_current_dataset_version: boolean
    endpoint_types_match_mapping: boolean
  }
}

export interface MappingLinkLegacyReconciliationReport {
  mode: 'read_only'
  action_taken: false
  scan_limit: number
  scanned_relation_count: number
  scan_truncated: boolean
  candidate_count: number
  candidate_list_limit: number
  candidate_list_truncated: boolean
  candidate_counts_by_classification: Record<string, number>
  candidate_relations: MappingLinkLegacyReconciliationCandidate[]
  evidence_digest: string
  resolution_required: boolean
  note: string
}

export interface MappingLinkApplyRun {
  run_id: string
  link_mapping_id: string
  link_review_id: string
  preview_digest: string
  preview_summary: MappingLinkApplyPreview['summary']
  status: 'pending' | 'dispatching' | 'queued' | 'running' | 'retry_wait' | 'succeeded' | 'failed'
  execution_attempts: number
  dispatch_attempts: number
  result: {
    relation_count?: number
    relation_changes?: number
    projection?: { projected_changes?: number; pending_changes?: number; dead_letter?: number }
  } | null
  error_code: string | null
  error_message: string | null
  attempt_events: Array<{
    sequence_no: number
    attempt_number: number
    event_type: string
    status: string
    actor_id: string
    occurred_at: string | null
  }>
  attempt_history_state: string
  attempt_history_valid: boolean | null
  created_at: string | null
  completed_at: string | null
  projection_summary?: MappingLinkProjectionSummary
}

export interface MappingLinkProjectionSummary {
  run_id: string
  change_count: number
  state_counts: { pending: number; projecting: number; projected: number; dead_letter: number }
  pending_changes: number
  dead_letter_changes: number
  automatic_attempts: number
  manual_attempts: number
  max_automatic_attempts_per_relation: number
  max_manual_attempts_per_relation: number
  attempt_event_count: number
  attempt_history_truncated: boolean
  attempt_history_valid: boolean | null
  attempt_events: Array<{
    event_id: string
    attempt_id: string
    change_id: string
    relation_id: string
    sequence_no: number
    attempt_number: number
    attempt_kind: 'automatic' | 'manual'
    event_type: 'started' | 'finished'
    actor_kind: 'system' | 'worker' | 'admin'
    actor_id: string
    reason: string | null
    client_request_id: string | null
    outcome: string | null
    error_code: string | null
    error_message: string | null
    previous_digest: string | null
    event_digest: string
    created_at: string | null
  }>
  manual_replays: Array<{
    request_id: string
    actor_id: string
    actor_role: string
    client_request_id: string
    reason: string
    status: string
    execution_state?: string
    batch_size?: number
    dispatch_attempts?: number
    celery_task_id?: string | null
    error_code?: string | null
    next_attempt_at?: string | null
    selected_count: number
    projected_count: number
    failed_count: number
    pending_count: number
    created_at: string | null
    completed_at: string | null
  }>
}

export interface MappingRowProjectionAttempt {
  attempt_id: string
  row_change_id: string
  object_id: string
  attempt_kind: 'automatic' | 'manual'
  attempt_number: number
  actor_kind: string
  actor_id: string | null
  reason: string | null
  client_request_id: string | null
  status: string
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

export interface MappingRowProjectionSummary {
  status: 'not_applicable' | 'pending' | 'projecting' | 'projected' | 'dead_letter' | 'mixed'
  row_count: number
  pending: number
  projecting: number
  projected: number
  dead_letter: number
  automatic_attempts: number
  manual_attempts: number
  max_automatic_attempts: number
  attempts: MappingRowProjectionAttempt[]
}

export interface ReviewApprovalResult extends ReviewSession {
  mapping_apply?: {
    status?: string
    triggered_mappings?: Array<{
      mapping_id: string
      mapping_review_id?: string
      run_id: string | null
      status: string
      dataset_version_id: string | null
    }>
  } | null
}

const curatedApi = {
  list: () => apiClientV2.get<CuratedDataset[]>('/curated'),
  get: (id: string) => apiClientV2.get<CuratedDataset>(`/curated/${id}`),
  preview: (id: string, limit = 200, options: { versionId?: string | null; reviewId?: string | null } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (options.versionId) params.set('version_id', options.versionId)
    if (options.reviewId) params.set('review_id', options.reviewId)
    return apiClientV2.get<CuratedPreview>(`/curated/${id}/preview?${params.toString()}`)
  },
  quality: (id: string) => apiClientV2.get(`/curated/${id}/quality`),

  reject: (id: string, notes = '') =>
    apiClientV2.post<ReviewSession>(`/curated/${id}/review?action=reject&notes=${encodeURIComponent(notes)}`),

  /** Delete a curated dataset; deleting review history requires explicit confirmation. */
  delete: (id: string, options: { cascade?: boolean } = {}) =>
    apiClientV2.delete(`/curated/${encodeURIComponent(id)}${options.cascade ? '?cascade=true' : ''}`),

  /** Start a review session for row-level edits */
  startReview: (id: string, expectedVersionId?: string | null) =>
    apiClientV2.post<ReviewSession>(
      `/curated/${id}/reviews${expectedVersionId ? `?expected_version_id=${encodeURIComponent(expectedVersionId)}` : ''}`,
    ),
  latestReview: (id: string) =>
    apiClientV2.get<ReviewSession>(`/curated/${id}/reviews/latest`),

  /** Save batch edits within a review session */
  saveEdits: (reviewId: string, edits: Array<{ row_pk: string; field_name: string; old_value: string; new_value: string }>) =>
    apiClientV2.post<{ saved: number; edits: CuratedRowEdit[] }>(`/curated/reviews/${reviewId}/edits`, { edits }),
  reviewEdits: (reviewId: string, limit = 100) =>
    apiClientV2.get<{ review_id: string; total: number; edits: CuratedRowEdit[] }>(
      `/curated/reviews/${reviewId}/edits?limit=${limit}`,
    ),

  /** Approve/reject a review session */
  approveReview: (reviewId: string, notes = '') =>
    apiClientV2.post<ReviewApprovalResult>(`/curated/reviews/${reviewId}/approve?notes=${encodeURIComponent(notes)}`),
  rejectReview: (reviewId: string, notes = '') =>
    apiClientV2.post<ReviewSession>(`/curated/reviews/${reviewId}/reject?notes=${encodeURIComponent(notes)}`),
  mappingRuns: (reviewId: string) =>
    apiClientV2.get<{ review_id: string; dataset_version_id: string | null; runs: MappingApplyRun[] }>(
      `/curated/reviews/${reviewId}/mapping-runs`,
    ),
  reconcileMappingProjection: (reviewId: string, runId: string, body: { reason: string; client_request_id: string }) =>
    apiClientV2.post<{
      run_id: string
      result: { selected: number; projected: number; failed: number; pending: number; dead_letter: number; replayed?: boolean; in_progress?: boolean }
      projection: MappingRowProjectionSummary
    }>(`/curated/reviews/${reviewId}/mapping-runs/${runId}:reconcile-projection`, body),
}

export default curatedApi
