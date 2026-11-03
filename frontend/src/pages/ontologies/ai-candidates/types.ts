export type Decision = 'pending' | 'accepted' | 'modified' | 'rejected'
export type Basis = 'term' | 'declared_fk' | 'semantic_fk' | 'bridge_table'
export type SetStatus = 'generating' | 'review' | 'published' | 'failed' | 'cancelled'

export type PropertyDraft = { id: string; name: string; type: string; description: string; source_field: string; is_identifier: boolean }
export type EntityDraft = { name_cn: string; name_en: string; type: string; description: string; properties: PropertyDraft[] }
export type RelationshipDraft = { type: string; source: string; target: string; cardinality: string; description: string; properties: Record<string, unknown> }

export type ColumnEvidence = { name: string; type: string | null; unique: boolean; sample_values: string[] }
export type EntityEvidence = { source_table: string; primary_key: string[]; term: string | null; columns: ColumnEvidence[] }
export type EdgeEvidence = {
  source_table: string; source_column: string; target_table: string; target_column: string; basis: string
  used_in_join_paths: boolean; sample_values: { source: string[]; target: string[] }
}
export type RelationshipEvidence = Partial<EdgeEvidence> & { via_table?: string; edges?: EdgeEvidence[] }

export type HistoryRow = { at: string; actor: string; decision: Decision; category: string | null; reason: string | null }
type ItemBase = {
  id: string; ordinal: number; key: string; name: string; basis: Basis; basis_label: string; decision: Decision
  reject_category: string | null; reject_reason: string | null; exists_in_ontology: boolean; history: HistoryRow[]
  publish_result: { revision_id: string; revision_no: number; target_id: string | null } | null
}
export type EntityItem = ItemBase & { kind: 'entity_type'; draft: EntityDraft; effective: EntityDraft; final: EntityDraft | null; evidence: EntityEvidence }
export type RelationshipItem = ItemBase & { kind: 'relationship'; draft: RelationshipDraft; effective: RelationshipDraft; final: RelationshipDraft | null; evidence: RelationshipEvidence }
export type CandidateItem = EntityItem | RelationshipItem

export type Counts = { total: number; accepted: number; modified: number; rejected: number; pending: number }
export type Stats = Counts & {
  decided: number; accept_rate: number | null; edits: number
  by_kind: Record<string, Counts>; by_basis: Record<string, Counts>; reject_reasons: Record<string, number>
}

export type StageRow = { key: string; label: string; status: 'pending' | 'running' | 'done' | 'failed'; started_at: number | null; finished_at: number | null }
export type Progress = {
  status?: string; stage?: string | null; stages?: StageRow[]; tables?: { total: number; done: number }; llm_calls?: number
  detail?: string; log_tail?: string[]; error?: string | null; started_at?: number | null; finished_at?: number | null; runner_unreachable?: string
}
export type Generator = {
  tool?: string; version?: string | null; chat_model?: string | null; locality?: 'local' | 'cloud'; source_label?: string; model_label?: string
  seconds?: number | null; llm_calls?: number | null; table_count?: number; column_count?: number; sample?: boolean; cloud_acknowledged?: boolean
}

export type CandidateSet = {
  id: string; ontology_id: string; origin: 'runner' | 'file' | 'example'; status: SetStatus; title: string; generator: Generator
  progress: Progress; warnings: Array<{ code: string; table: string; message: string }>; error: string | null
  created_at: string; imported_at: string | null; published_at: string | null; published_revision_id: string | null
  items?: CandidateItem[]; stats?: Stats
}

export type RunnerModel = { id: string; label: string; locality: 'local' | 'cloud'; provider: string; available: boolean; outbound: string[] }
export type RunnerSource = { id: string; label: string; privacy_level: string; description: string; tables: Array<{ name: string; columns: string[] }> }
export type Example = { id: string; label: string; locality: 'local' | 'cloud'; model_label: string }
export type RunnerInfo = {
  configured: boolean; reachable: boolean; reason?: string; health?: { auto_ontology: { version: string; commit: string } }
  models?: RunnerModel[]; sources?: RunnerSource[]; examples: Example[]; reject_categories: Array<{ id: string; label: string }>
}

export type Validation = { can_apply: boolean; message: string; operations: unknown[] }
export type PublishResult = { set: CandidateSet; revision: { id: string; revision_no: number } }

export const decisionLabel: Record<Decision, string> = { pending: '待审', accepted: '已采纳', modified: '已修改后采纳', rejected: '已驳回' }
export const statusLabel: Record<SetStatus, string> = { generating: '生成中', review: '待审阅', published: '已发布', failed: '失败', cancelled: '已取消' }
export const cardinalityLabel: Record<string, string> = { 'one-to-one': '一对一', 'one-to-many': '一对多', 'many-to-one': '多对一', 'many-to-many': '多对多' }
export const typeOptions = ['string', 'integer', 'double', 'boolean', 'date', 'timestamp', 'json']

export function errorCode(error: unknown): string | undefined {
  const detail = (error as { detail?: { code?: unknown } } | null)?.detail
  return typeof detail?.code === 'string' ? detail.code : undefined
}

export function errorOutbound(error: unknown): string[] {
  const detail = (error as { detail?: { details?: { outbound?: unknown } } } | null)?.detail
  return Array.isArray(detail?.details?.outbound) ? detail.details.outbound.map(String) : []
}
