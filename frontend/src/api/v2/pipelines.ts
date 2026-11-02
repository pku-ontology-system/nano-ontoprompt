import { apiClientV2 } from '@/api/client'

export interface Pipeline {
  id: string
  name: string
  domain?: string
  description?: string
  source_dataset_id?: string | null
  route?: string | null
  spec?: Record<string, unknown>
  definition?: { nodes: unknown[]; edges: unknown[] } | null
  status: string
  branch?: string
  version?: number
  target_curated_ids?: string[]
  created_at?: string | null
  updated_at?: string | null
  last_run_status?: string | null
  last_run_at?: string | null
}

export interface PipelineCreateBody {
  name: string
  domain?: string
  description?: string
  source_dataset_id?: string | null
  route?: string | null
  spec?: Record<string, unknown>
  definition?: { nodes: unknown[]; edges: unknown[] } | null
}

export interface PipelineRunItem {
  id: string
  status: string
  dispatch_status?: string | null
  dispatch_attempts?: number
  execution_mode?: string | null
  execution_digest?: string | null
  pipeline_version_id?: string | null
  service_principal_id?: string | null
  source_versions?: PipelineRunSourceVersion[]
  started_at: string | null
  finished_at: string | null
}

export interface PipelineSchedule {
  id: string
  pipeline_id: string
  owner_id: string
  steward_id: string
  service_principal_id: string
  pipeline_version_id: string
  cron_expression: string
  timezone: string
  enabled: boolean
  revision: number
  misfire_policy: 'skip' | 'run_once'
  misfire_grace_seconds: number
  overlap_policy: 'forbid' | 'allow'
  next_fire_at: string | null
  last_fire_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface PipelineScheduleRunItem {
  id: string
  schedule_revision_id: string
  scheduled_for: string
  local_slot: string
  status: string
  pipeline_run_id: string | null
  coalesced_count: number
  dispatch_attempts: number
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

export interface PipelineScheduleUpsert {
  cron_expression: string
  timezone: string
  enabled: boolean
  pipeline_version_id?: string
  service_principal_id: string
  misfire_policy: 'skip' | 'run_once'
  misfire_grace_seconds: number
  overlap_policy: 'forbid' | 'allow'
  expected_revision?: number
}

export interface PipelineRunSourceVersion {
  dataset_id: string | null
  dataset_version_id: string | null
  version_no: number | null
  checksum: string | null
}

export interface RunDetail {
  id: string
  status: string
  dispatch_status?: string | null
  dispatch_attempts?: number
  dispatch_error?: string | null
  stats: Record<string, unknown> | null
  error_log: string | null
  execution_mode?: string | null
  execution_digest?: string | null
  pipeline_version_id?: string | null
  service_principal_id?: string | null
  source_versions?: PipelineRunSourceVersion[]
  llm_model_pin?: {
    requested: boolean
    model_config_id: string | null
    model_config_digest: string | null
    provider: string | null
    model_name: string | null
    prompt_provenance: Record<string, { prompt_id: string; prompt_digest: string }>
    provider_model_artifact_reproducibility: string
  } | null
  llm_invocations?: Array<{
    id: string
    attempt_number: number | null
    source_dataset_version_id: string | null
    phase: string | null
    provider: string | null
    model_name: string
    model_config_id: string | null
    model_config_digest: string | null
    prompt_id: string | null
    prompt_digest: string | null
    request_hash: string | null
    response_hash: string | null
    status: string
    error_code: string | null
    duration_ms: number | null
    provider_model_artifact_reproducibility: string
    request_payload_persisted: false
    response_payload_persisted: false
    event_chain_status: 'verified' | 'invalid' | 'unavailable'
    events: Array<{
      id: string
      sequence: number
      event_type: string
      status: string
      error_code: string | null
      request_hash: string | null
      response_hash: string | null
      previous_digest: string | null
      event_digest: string
      occurred_at: string | null
    }>
  }>
  started_at: string | null
  finished_at: string | null
}

interface RunMonitorOptions {
  signal?: AbortSignal
  pollIntervalMs?: number
  onUpdate?: (run: RunDetail) => void
  onPollError?: (error: unknown) => void
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    if (signal?.aborted) { resolve(); return }
    const finish = () => {
      signal?.removeEventListener('abort', abort)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    const abort = () => { clearTimeout(timer); finish() }
    signal?.addEventListener('abort', abort, { once: true })
  })
}

export interface ValidateResult {
  valid: boolean
  errors: Array<{ node_id: string; severity: string; message: string }>
  warnings: Array<{ node_id: string; severity: string; message: string }>
}

const pipelinesApi = {
  /** Pipeline CRUD */
  list: (params?: { search?: string; domain?: string; status?: string }) =>
    apiClientV2.get<Pipeline[]>('/pipelines', { params }).then(r => r),
  get: (id: string) =>
    apiClientV2.get<Pipeline>(`/pipelines/${id}`).then(r => r),
  create: (body: PipelineCreateBody) =>
    apiClientV2.post<Pipeline>('/pipelines', body).then(r => r),
  update: (id: string, body: Partial<PipelineCreateBody> & { status?: string }) =>
    apiClientV2.put<Pipeline>(`/pipelines/${id}`, body).then(r => r),
  delete: (id: string) =>
    apiClientV2.delete(`/pipelines/${id}`).then(r => r),

  /** Validate & Publish */
  validate: (id: string) =>
    apiClientV2.post<ValidateResult>(`/pipelines/${id}/validate`).then(r => r),
  publish: (id: string) =>
    apiClientV2.post<{ id: string; status: string; version: number; version_id: string; definition_digest: string; snapshot_digest: string }>(`/pipelines/${id}/publish`).then(r => r),
  versions: (id: string) =>
    apiClientV2.get<Array<{ id: string; version: number; status: string; definition_digest?: string | null; snapshot_digest?: string | null; created_at: string | null }>>(`/pipelines/${id}/versions`).then(r => r),

  /** Durable, immutable-revision cron schedule and its persisted Run history. */
  getSchedule: (id: string) =>
    apiClientV2.get<{ schedule: PipelineSchedule | null }>(`/pipelines/${id}/schedule`).then(r => r),
  upsertSchedule: (id: string, body: PipelineScheduleUpsert) =>
    apiClientV2.put<{ schedule: PipelineSchedule; revision_id: string }>(`/pipelines/${id}/schedule`, body).then(r => r),
  disableSchedule: (id: string) =>
    apiClientV2.delete<{ status: string; schedule?: PipelineSchedule }>(`/pipelines/${id}/schedule`).then(r => r),
  scheduleRuns: (id: string, limit = 20) =>
    apiClientV2.get<PipelineScheduleRunItem[]>(`/pipelines/${id}/schedule/runs`, { params: { limit } }).then(r => r),
  cancelScheduleRun: (id: string, scheduleRunId: string) =>
    apiClientV2.post<{ id: string; status: string }>(`/pipelines/${id}/schedule/runs/${scheduleRunId}/cancel`).then(r => r),

  /** Runs */
  run: async (id: string) => {
    const storageKey = `project4:pipeline-run-request:${id}`
    const now = Date.now()
    const memoryEntry = pendingRunKeys.get(id)
    let requestKey = memoryEntry && now - memoryEntry.createdAt < RUN_REQUEST_TTL_MS
      ? memoryEntry.key
      : ''
    if (!requestKey && typeof window !== 'undefined') {
      try {
        const raw = window.sessionStorage.getItem(storageKey)
        const saved = raw ? JSON.parse(raw) as { key?: string; createdAt?: number } : null
        if (saved?.key && typeof saved.createdAt === 'number' && now - saved.createdAt < RUN_REQUEST_TTL_MS) {
          requestKey = saved.key
        }
      } catch {
        // Storage may be unavailable in private browsing; use the in-memory key below.
      }
    }
    if (!requestKey) {
      requestKey = typeof window !== 'undefined' && window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${now}-${Math.random().toString(36).slice(2)}`
    }
    pendingRunKeys.set(id, { key: requestKey, createdAt: now })
    if (typeof window !== 'undefined') {
      try { window.sessionStorage.setItem(storageKey, JSON.stringify({ key: requestKey, createdAt: now })) } catch { /* best effort */ }
    }

    const result = await apiClientV2.post<{
      run_id: string
      status: string
      dispatch_status?: string
      execution_mode?: string
      execution_digest?: string
    }>(`/pipelines/${id}/run`, undefined, { headers: { 'Idempotency-Key': requestKey } }).then(r => r)
    pendingRunKeys.delete(id)
    if (typeof window !== 'undefined') {
      try { window.sessionStorage.removeItem(storageKey) } catch { /* best effort */ }
    }
    return result
  },
  runSync: (id: string) =>
    apiClientV2.post<{ run_id: string; status: string; stats?: Record<string, unknown> }>(`/pipelines/${id}/run-sync`).then(r => r),
  runs: (id: string) =>
    apiClientV2.get<PipelineRunItem[]>(`/pipelines/${id}/runs`).then(r => r),
  getRun: (runId: string) =>
    apiClientV2.get<RunDetail>(`/pipelines/runs/${runId}`).then(r => r),
}

const RUN_REQUEST_TTL_MS = 30 * 60 * 1000
const pendingRunKeys = new Map<string, { key: string; createdAt: number }>()

/** Follow the persisted server Run after an async launch; abort only stops polling. */
export async function waitForPipelineRun(
  runId: string,
  options: RunMonitorOptions = {},
): Promise<RunDetail | null> {
  const terminal = new Set(['success', 'degraded', 'failed', 'cancelled'])
  const pollIntervalMs = Math.max(250, options.pollIntervalMs ?? 1000)
  while (!options.signal?.aborted) {
    let detail: RunDetail
    try {
      detail = await pipelinesApi.getRun(runId)
    } catch (error) {
      options.onPollError?.(error)
      await delay(pollIntervalMs, options.signal)
      continue
    }
    options.onUpdate?.(detail)
    if (terminal.has(detail.status)) return detail
    await delay(pollIntervalMs, options.signal)
  }
  return null
}

export default pipelinesApi
