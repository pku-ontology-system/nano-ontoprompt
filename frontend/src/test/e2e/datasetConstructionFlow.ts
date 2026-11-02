import fs from 'node:fs'
import path from 'node:path'
import type { APIRequestContext } from '@playwright/test'

type JsonRecord = Record<string, unknown>
type Method = 'GET' | 'POST' | 'PATCH'

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord
    : {}
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${label} missing from Construction API response`)
  }
  return value
}

async function requestJson(
  request: APIRequestContext,
  apiBase: string,
  token: string,
  method: Method,
  route: string,
  payload?: JsonRecord,
): Promise<JsonRecord> {
  const response = await request.fetch(`${apiBase}${route}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: payload === undefined ? undefined : JSON.stringify(payload),
  })
  const body = asRecord(await response.json().catch(() => ({})))
  if (!response.ok()) {
    throw new Error(`${method} ${route} → ${response.status()}: ${JSON.stringify(body).slice(0, 300)}`)
  }
  return body
}

async function pollStatus(
  readStatus: () => Promise<JsonRecord>,
  label: string,
  timeoutMs: number,
): Promise<JsonRecord> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const body = await readStatus()
    const status = String(body.status || '')
    if (status === 'completed') return body
    if (['failed', 'cancelled', 'waiting_for_model'].includes(status)) {
      throw new Error(`${label} ended with status=${status}: ${String(body.error || 'no error detail')}`)
    }
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
  throw new Error(`${label} timed out after ${timeoutMs / 1000}s`)
}

export interface DatasetConstructionResult {
  datasetId: string
  draftId: string
  mappingTaskId: string
  runId: string
  ontologyId: string
  status: string
}

export async function buildPrivateStructuredDataset(
  request: APIRequestContext,
  options: {
    apiBase: string
    token: string
    filePath: string
    domain: string
    ontologyName: string
  },
): Promise<DatasetConstructionResult> {
  const filename = path.basename(options.filePath)
  const upload = await request.post(`${options.apiBase}/api/v2/datasets/upload?privacy_level=private`, {
    headers: { Authorization: `Bearer ${options.token}` },
    multipart: {
      file: {
        name: filename,
        mimeType: 'application/octet-stream',
        buffer: fs.readFileSync(options.filePath),
      },
    },
  })
  const uploaded = asRecord(await upload.json().catch(() => ({})))
  if (!upload.ok()) {
    throw new Error(`Dataset upload ${filename} → ${upload.status()}: ${JSON.stringify(uploaded).slice(0, 300)}`)
  }
  const dataset = asRecord(uploaded.data)
  const datasetId = requiredString(dataset.id, 'dataset id')

  const draftBody = await requestJson(request, options.apiBase, options.token, 'POST', '/api/v2/construction/drafts', {
    dataset_id: datasetId,
    data_class: 'regular',
    target_mode: 'create',
    new_ontology_name: options.ontologyName,
    new_ontology_domain: options.domain,
    privacy_level: 'private',
    selection: { row_limit: 500 },
  })
  const draftId = requiredString(draftBody.id, 'draft id')

  const preflightBody = await requestJson(
    request, options.apiBase, options.token, 'POST',
    `/api/v2/construction/drafts/${draftId}/m3-preflight`,
  )
  if (asRecord(preflightBody.preflight).allowed !== true) {
    throw new Error(`Private Dataset preflight did not allow construction: ${JSON.stringify(preflightBody.preflight)}`)
  }

  const generated = await requestJson(
    request, options.apiBase, options.token, 'POST',
    `/api/v2/construction/drafts/${draftId}/generate-mapping`,
  )
  const mappingTaskId = requiredString(generated.mapping_task_id, 'mapping task id')
  await pollStatus(
    () => requestJson(request, options.apiBase, options.token, 'GET', `/api/v2/mapping-tasks/${mappingTaskId}`),
    `MappingTask ${mappingTaskId}`,
    600_000,
  )

  const draft = await requestJson(
    request, options.apiBase, options.token, 'GET', `/api/v2/construction/drafts/${draftId}`,
  )
  const mapping = asRecord(draft.mapping)
  const suggestions = Array.isArray(mapping.suggestions) ? mapping.suggestions.map(asRecord) : []
  if (!suggestions.length) throw new Error(`Construction draft ${draftId} returned no mapping candidates`)
  await requestJson(request, options.apiBase, options.token, 'PATCH', `/api/v2/construction/drafts/${draftId}`, {
    mapping: { suggestions, confirmed: suggestions },
  })

  const run = await requestJson(
    request, options.apiBase, options.token, 'POST',
    `/api/v2/construction/drafts/${draftId}/build`,
    { mapping_confirmed: true, mode: 'create' },
  )
  const runId = requiredString(run.id || run.run_id, 'construction run id')
  const completed = await pollStatus(
    () => requestJson(request, options.apiBase, options.token, 'GET', `/api/v2/construction-runs/${runId}`),
    `ConstructionRun ${runId}`,
    900_000,
  )
  return {
    datasetId,
    draftId,
    mappingTaskId,
    runId,
    ontologyId: requiredString(completed.ontology_id, 'ontology id'),
    status: String(completed.status || ''),
  }
}
