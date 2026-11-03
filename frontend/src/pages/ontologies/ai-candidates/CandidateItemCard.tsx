import { useState } from 'react'
import { formatApiError } from '@/api/client'
import {
  cardinalityLabel, decisionLabel, typeOptions,
  type CandidateItem, type Decision, type EdgeEvidence, type EntityItem, type PropertyDraft, type RelationshipItem,
} from './types'

export type ReviewBody = { decision: Decision; final?: Record<string, unknown>; reject_category?: string; reject_reason?: string }
type Props = {
  item: CandidateItem
  editable: boolean
  categories: Array<{ id: string; label: string }>
  onReview: (item: CandidateItem, body: ReviewBody) => Promise<void>
}

const basisStyle: Record<string, string> = {
  declared_fk: 'border-emerald-300 bg-emerald-50 text-emerald-800',
  semantic_fk: 'border-amber-300 bg-amber-50 text-amber-800',
  bridge_table: 'border-indigo-300 bg-indigo-50 text-indigo-800',
  term: 'border-slate-300 bg-slate-50 text-slate-700',
}
const decisionStyle: Record<Decision, string> = {
  pending: 'bg-slate-100 text-slate-600', accepted: 'bg-emerald-100 text-emerald-800',
  modified: 'bg-blue-100 text-blue-800', rejected: 'bg-red-100 text-red-800',
}

const samples = (values: string[] | undefined) => (values?.length ? values.join('、') : '—')

function EdgeLine({ edge }: { edge: EdgeEvidence }) {
  return <li className="text-xs text-slate-600">
    <code className="rounded bg-slate-100 px-1">{edge.source_table}.{edge.source_column}</code> → <code className="rounded bg-slate-100 px-1">{edge.target_table}.{edge.target_column}</code>
    <span className="ml-2 text-slate-500">样本：{samples(edge.sample_values.source)} → {samples(edge.sample_values.target)}</span>
    {!edge.used_in_join_paths && <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">生成方没有把它用于连接路径</span>}
  </li>
}

function Evidence({ item }: { item: CandidateItem }) {
  if (item.kind === 'entity_type') {
    const evidence = item.evidence
    return <div className="text-xs text-slate-600">
      来源表 <code className="rounded bg-slate-100 px-1">{evidence.source_table}</code>
      {evidence.primary_key.length > 0 && <> · 主键 <code className="rounded bg-slate-100 px-1">{evidence.primary_key.join(', ')}</code></>}
      · {evidence.columns.length} 列
      <details className="mt-1"><summary className="cursor-pointer text-slate-500">列与样本值</summary>
        <table className="mt-1 w-full text-left"><tbody>{evidence.columns.map(column => <tr key={column.name} className="border-t">
          <td className="py-0.5 pr-3 font-mono">{column.name}</td><td className="pr-3 text-slate-500">{column.type ?? ''}</td><td>{samples(column.sample_values)}</td></tr>)}</tbody></table>
      </details>
    </div>
  }
  const evidence = item.evidence
  const edges = evidence.edges ?? (evidence.source_table ? [evidence as EdgeEvidence] : [])
  return <div>
    {evidence.via_table && <p className="text-xs text-slate-600">经桥表 <code className="rounded bg-slate-100 px-1">{evidence.via_table}</code> 的两个外键合成：</p>}
    <ul className="mt-0.5 space-y-0.5">{edges.map(edge => <EdgeLine key={`${edge.source_table}.${edge.source_column}`} edge={edge} />)}</ul>
  </div>
}

function EntityEditor({ item, onSave, onCancel }: { item: EntityItem; onSave: (final: Record<string, unknown>) => void; onCancel: () => void }) {
  const [name, setName] = useState(item.effective.name_cn)
  const [description, setDescription] = useState(item.effective.description)
  const [properties, setProperties] = useState<PropertyDraft[]>(item.effective.properties)
  const change = (id: string, patch: Partial<PropertyDraft>) => setProperties(rows => rows.map(row => row.id === id ? { ...row, ...patch } : row))
  return <div className="mt-3 space-y-2 rounded-lg border bg-slate-50 p-3" data-testid="item-editor">
    <label className="block text-xs font-medium">显示名称<input className="wb-input mt-1" aria-label="显示名称" value={name} onChange={event => setName(event.target.value)} /></label>
    <label className="block text-xs font-medium">说明<textarea className="wb-input mt-1" aria-label="说明" rows={2} value={description} onChange={event => setDescription(event.target.value)} /></label>
    <details><summary className="cursor-pointer text-xs font-medium">属性（{properties.length}）</summary>
      <table className="mt-1 w-full text-xs"><thead><tr className="text-left text-slate-500"><th>列</th><th>名称</th><th>类型</th><th>说明</th></tr></thead><tbody>
        {properties.map(row => <tr key={row.id} className="border-t"><td className="py-1 pr-2 font-mono">{row.id}</td>
          <td className="pr-2"><input className="wb-input py-1" aria-label={`属性名称 ${row.id}`} value={row.name} onChange={event => change(row.id, { name: event.target.value })} /></td>
          <td className="pr-2"><select className="wb-input py-1" aria-label={`属性类型 ${row.id}`} value={row.type} onChange={event => change(row.id, { type: event.target.value })}>{typeOptions.map(option => <option key={option}>{option}</option>)}</select></td>
          <td><input className="wb-input py-1" aria-label={`属性说明 ${row.id}`} value={row.description} onChange={event => change(row.id, { description: event.target.value })} /></td></tr>)}
      </tbody></table>
    </details>
    <div className="flex gap-2"><button type="button" className="wb-button-primary" onClick={() => onSave({ name_cn: name, description, properties: properties.map(({ id, name: label, type, description: text }) => ({ id, name: label, type, description: text })) })}>保存并采纳</button>
      <button type="button" className="wb-button-secondary" onClick={onCancel}>取消</button></div>
  </div>
}

function RelationshipEditor({ item, onSave, onCancel }: { item: RelationshipItem; onSave: (final: Record<string, unknown>) => void; onCancel: () => void }) {
  const [name, setName] = useState(item.effective.type)
  const [cardinality, setCardinality] = useState(item.effective.cardinality)
  const [description, setDescription] = useState(item.effective.description)
  return <div className="mt-3 space-y-2 rounded-lg border bg-slate-50 p-3" data-testid="item-editor">
    <label className="block text-xs font-medium">关系名称<input className="wb-input mt-1" aria-label="关系名称" value={name} onChange={event => setName(event.target.value)} /></label>
    <label className="block text-xs font-medium">基数
      <select className="wb-input mt-1" aria-label="基数" value={cardinality} onChange={event => setCardinality(event.target.value)}>
        {Object.entries(cardinalityLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label className="block text-xs font-medium">说明<textarea className="wb-input mt-1" aria-label="说明" rows={2} value={description} onChange={event => setDescription(event.target.value)} /></label>
    <div className="flex gap-2"><button type="button" className="wb-button-primary" onClick={() => onSave({ type: name, cardinality, description })}>保存并采纳</button>
      <button type="button" className="wb-button-secondary" onClick={onCancel}>取消</button></div>
  </div>
}

export default function CandidateItemCard({ item, editable, categories, onReview }: Props) {
  const [mode, setMode] = useState<'view' | 'edit' | 'reject'>('view')
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const run = async (body: ReviewBody) => {
    setBusy(true); setError('')
    try { await onReview(item, body); setMode('view') } catch (err) { setError(formatApiError(err)) } finally { setBusy(false) }
  }
  const effective = item.effective
  const last = item.history[item.history.length - 1]
  const title = item.kind === 'entity_type' ? `${item.effective.name_cn}` : `${item.effective.source} → ${item.effective.target}`
  const subtitle = item.kind === 'entity_type'
    ? <code className="text-xs text-slate-500">{item.name}</code>
    : <span className="text-xs text-slate-500">{item.effective.type} · {cardinalityLabel[item.effective.cardinality] ?? item.effective.cardinality}</span>
  const canAct = editable && !item.exists_in_ontology

  return <li data-testid={`candidate-${item.key}`} data-decision={item.decision} className={`rounded-xl border bg-white p-3 ${item.decision === 'rejected' ? 'opacity-80' : ''}`}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h4 className="text-sm font-semibold">{title}</h4>
          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${basisStyle[item.basis] ?? basisStyle.term}`}>{item.basis_label}</span>
          {item.exists_in_ontology && <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[11px]">本体里已有同名对象</span>}
        </div>
        <div className="mt-0.5">{subtitle}</div>
      </div>
      <span className={`rounded px-2 py-0.5 text-xs font-medium ${decisionStyle[item.decision]}`}>{decisionLabel[item.decision]}</span>
    </div>
    {item.kind === 'entity_type' && effective.description && <p className="mt-1.5 text-sm text-slate-700">{effective.description}</p>}
    {item.kind === 'relationship' && effective.description && <p className="mt-1.5 text-sm text-slate-700">{effective.description}</p>}
    <div className="mt-2"><Evidence item={item} /></div>

    {item.decision === 'rejected' && <p className="mt-2 rounded bg-red-50 p-2 text-xs text-red-800">驳回理由（{categories.find(row => row.id === item.reject_category)?.label ?? item.reject_category}）：{item.reject_reason}</p>}
    {last && <p className="mt-1.5 text-[11px] text-slate-400">{last.actor} 于 {new Date(last.at).toLocaleString('zh-CN')} {decisionLabel[last.decision]}</p>}
    {item.publish_result && <p className="mt-1.5 text-xs text-emerald-700">已写入修订 #{item.publish_result.revision_no}</p>}

    {canAct && mode === 'view' && <div className="mt-3 flex flex-wrap gap-2">
      <button type="button" className="wb-button-primary" disabled={busy || item.decision === 'accepted'} onClick={() => void run({ decision: 'accepted' })}>采纳</button>
      <button type="button" className="wb-button-secondary" disabled={busy} onClick={() => setMode('edit')}>修改后采纳</button>
      <button type="button" className="wb-button-secondary" disabled={busy || item.decision === 'rejected'} onClick={() => setMode('reject')}>驳回</button>
      {item.decision !== 'pending' && <button type="button" className="wb-button-secondary" disabled={busy} onClick={() => void run({ decision: 'pending' })}>撤销决定</button>}
    </div>}
    {canAct && mode === 'edit' && (item.kind === 'entity_type'
      ? <EntityEditor item={item} onCancel={() => setMode('view')} onSave={final => void run({ decision: 'modified', final })} />
      : <RelationshipEditor item={item} onCancel={() => setMode('view')} onSave={final => void run({ decision: 'modified', final })} />)}
    {canAct && mode === 'reject' && <div className="mt-3 space-y-2 rounded-lg border border-red-200 bg-red-50 p-3" data-testid="reject-form">
      <label className="block text-xs font-medium">原因类别
        <select className="wb-input mt-1 bg-white" aria-label="驳回原因类别" value={category} onChange={event => setCategory(event.target.value)}>
          <option value="">请选择</option>{categories.map(row => <option key={row.id} value={row.id}>{row.label}</option>)}</select></label>
      <label className="block text-xs font-medium">驳回理由<textarea className="wb-input mt-1 bg-white" aria-label="驳回理由" rows={2} value={reason} onChange={event => setReason(event.target.value)} placeholder="例如：source_unit 是机组编号，不是资源 ID" /></label>
      <div className="flex gap-2"><button type="button" className="wb-button-primary" disabled={busy || !category || reason.trim().length < 2}
        onClick={() => void run({ decision: 'rejected', reject_category: category, reject_reason: reason.trim() })}>确认驳回</button>
        <button type="button" className="wb-button-secondary" onClick={() => setMode('view')}>取消</button></div>
    </div>}
    {error && <p role="alert" className="mt-2 text-xs text-red-700">{error}</p>}
  </li>
}
