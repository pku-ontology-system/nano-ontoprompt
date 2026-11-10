import type { Tool } from '@/api/analysis'

export const num = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('zh-CN'))
export const pct = (n: number | null | undefined, digits = 1) => (n == null ? '—' : `${(n * 100).toFixed(digits)}%`)
export const size = (bytes: number | null | undefined) => (bytes == null ? '' : bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`)
export const cell = (value: unknown) => (value == null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value))

export const TOOL_LABEL: Record<Tool, string> = { splink: 'Splink', great_expectations: 'Great Expectations', marquez: 'Marquez / OpenLineage' }

/** Where an object is opened in the object explorer. */
export const objectHref = (ontologyId: string, objectType: string, objectId: string) =>
  `/ontologies/${encodeURIComponent(ontologyId)}/object?objectType=${encodeURIComponent(objectType)}&objectId=${encodeURIComponent(objectId)}`

/** An object id is `<ontology prefix>:<Type>:<key>`; the key is what a person recognises. */
export const shortObjectId = (id: string) => id.split(':').pop() || id

export const LAYER_STYLE: Record<string, { label: string; fill: string; stroke: string }> = {
  source: { label: '数据源表', fill: '#f1f5f9', stroke: '#64748b' },
  build: { label: 'Auto Ontology 生成', fill: '#ede9fe', stroke: '#7c3aed' },
  object: { label: '对象类型', fill: '#dbeafe', stroke: '#2563eb' },
  rule: { label: '规则', fill: '#fef3c7', stroke: '#d97706' },
  alert: { label: '告警', fill: '#fee2e2', stroke: '#dc2626' },
  decision: { label: '处置', fill: '#dcfce7', stroke: '#16a34a' },
  analysis: { label: '数据分析', fill: '#cffafe', stroke: '#0891b2' },
}

const enc = encodeURIComponent

/** A node of an ontology's lineage knows where it lives in the platform; this is the page that shows it. */
export function refLink(ref: Record<string, string> | null | undefined): { to: string; label: string } | null {
  if (!ref?.ontology_id && ref?.kind !== 'source_table') return null
  const oid = enc(ref!.ontology_id ?? '')
  switch (ref!.kind) {
    case 'object_type': return { to: `/analysis/health?source=ontology&ontology=${oid}&type=${enc(ref!.api_name)}`, label: '检查这个类型的数据健康' }
    case 'candidate_set': return { to: `/ontologies/${oid}/ai-candidates`, label: '打开 AI 本体候选' }
    case 'rule': return { to: `/ontologies/${oid}?tab=rules`, label: '打开运行规则' }
    case 'alerts': return { to: `/ontologies/${oid}?tab=alerts`, label: '打开告警中心' }
    case 'decision_cases': return { to: `/ontologies/${oid}?tab=decisions`, label: '打开决策与影响链' }
    case 'analysis_run': return { to: `/analysis/${ref!.run_kind === 'resolution' ? 'resolution' : 'health'}?source=ontology&ontology=${oid}&type=${enc(ref!.object_type)}`, label: ref!.run_kind === 'resolution' ? '打开实体消歧' : '打开数据健康' }
    default: return null
  }
}

export function countText(type: { object_count: number | null; object_count_capped: boolean }) {
  if (type.object_count == null) return '对象数未知'
  if (type.object_count === 0) return '没有对象'
  return `${type.object_count}${type.object_count_capped ? '+' : ''} 个对象`
}
