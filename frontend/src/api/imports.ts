import { apiClientV2 } from './client'


export interface ImportSource { id: string; title: string; kind: string; location?: string | null; license?: string | null; url?: string | null; note?: string | null }
export interface ImportBundle {
  id: string
  title: string
  summary: string
  domain: string
  kind: 'relational' | 'timeseries' | 'events' | 'multimodal' | 'multisource'
  tags: string[]
  shows: string[]
  replay: boolean
  sources: ImportSource[]
  imported: { ontology_id: string; name: string; objects: number } | null
}
export interface ImportStep { key: string; title: string; status: 'pending' | 'running' | 'done' | 'failed'; detail: string | null; seconds: number | null }
export interface ImportResult {
  ontology_id: string
  ontology_name: string
  objects: number
  types: number
  links: number
  link_instances: number
  counts: Record<string, number>
  replay: boolean
  bundle: string
  conflict_ontology_id?: string
}
export interface ImportJob { id: string; title: string; status: 'queued' | 'running' | 'done' | 'failed'; steps: ImportStep[]; log: string[]; result: ImportResult | null; error: string | null; started_at: number | null; finished_at: number | null }
export interface UploadPreview {
  upload_id: string
  title: string
  preview: {
    title: string
    kind: string
    tables: { name: string; type_name: string; label: string; kind: string; rows: number; primary_key: string[]; columns: { name: string; type: string; label: string }[] }[]
    assets: Record<string, number>
    links: { name: string; from: string; to: string; basis: string; coverage: number | null }[]
    notes: string[]
  }
}
export interface ReplayState { replay: { table: string; order_by: string; initial: number; batch: number; total: number; cursor: number } | null }
export interface AdvanceResult { cursor: number; total: number; installed: number; done: boolean }

export const importApi = {
  bundles: () => apiClientV2.get<{ bundles: ImportBundle[] }>('/ontology-imports/bundles').then(r => r.bundles),
  importBundle: (id: string, replace = false) => apiClientV2.post<ImportJob>(`/ontology-imports/bundles/${id}:import`, { replace }),
  job: (id: string) => apiClientV2.get<ImportJob>(`/ontology-imports/jobs/${id}`),
  upload: (files: File[], title?: string) => {
    const form = new FormData()
    files.forEach(f => form.append('files', f, f.name))
    if (title) form.append('title', title)
    return apiClientV2.post<UploadPreview>('/ontology-imports/files', form, { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  importFiles: (uploadId: string, body: { ontology_name: string; replace?: boolean; replay?: { table: string; order_by: string; initial: number; batch: number } | null }) =>
    apiClientV2.post<ImportJob>(`/ontology-imports/files/${uploadId}:import`, body),
  replay: (ontologyId: string) => apiClientV2.get<ReplayState>(`/ontology-imports/${ontologyId}/replay`),
  advance: (ontologyId: string, count?: number) => apiClientV2.post<AdvanceResult>(`/ontology-imports/${ontologyId}/advance`, count ? { count } : {}),
}

