import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { apiClientV2, formatApiError } from '@/api/client'

type EvidenceSelection = string | {
  evidence_ref_id: string; asset_id?: string; asset_checksum?: string;
  locator?: Record<string, unknown>
}
type CaseSummary = {
  id: string; title: string; kind: string; status: string; created_at?: string;
  target?: { object_type: string };
  created_by?: string; reviewed_by?: string; action_run_id?: string;
  projection_status?: string; reason?: string; context?: Record<string, unknown>;
  evidence_refs?: EvidenceSelection[];
  reviewed_preview?: { diff?: Array<{ property: string; before: unknown; after: unknown }> }
}
type CaseEvent = { sequence: number; event_type: string; created_at?: string; trace_id?: string; payload?: Record<string, unknown> }
type WhatIfScenarioRef = {
  ref_id: string; source_version: string; scenario_input_digest: string;
  assumptions: unknown[]; rule_overrides: Record<string, unknown>;
  scenario_context: Record<string, unknown>
}

function isWhatIfScenarioRef(value: unknown): value is WhatIfScenarioRef {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const ref = value as Record<string, unknown>
  return typeof ref.ref_id === 'string' && typeof ref.source_version === 'string'
    && typeof ref.scenario_input_digest === 'string'
    && Array.isArray(ref.assumptions)
    && !!ref.rule_overrides && typeof ref.rule_overrides === 'object'
    && !Array.isArray(ref.rule_overrides)
    && !!ref.scenario_context && typeof ref.scenario_context === 'object'
    && !Array.isArray(ref.scenario_context)
}

function eventText(event: CaseEvent): string {
  const payload = event.payload ?? {}
  if (event.event_type === 'projection_attempt_started') {
    const kind = payload.attempt_kind === 'manual_retry' ? '人工图投影重放' : payload.actor_kind === 'system' ? '系统图投影重试' : '图投影开始'
    const reason = typeof payload.reason === 'string' && payload.reason ? ` · 理由：${payload.reason}` : ''
    return `${kind} #${String(payload.attempt_number ?? '—')}${reason}`
  }
  if (event.event_type === 'projection_attempt_finished') {
    const error = typeof payload.error === 'string' && payload.error ? ` · ${payload.error}` : ''
    return `图投影尝试结果：${String(payload.status ?? '未知')}${error}`
  }
  return event.event_type
}

const statusLabel: Record<string, string> = {
  draft: '草稿', ready_for_review: '待审核', approved: '已批准', rejected: '已驳回',
  changes_requested: '退回修改', completed: '已执行', reverted: '已撤销', failed: '失败',
}

export default function DecisionCaseHistory({ ontologyId, objectId }: {
  ontologyId: string; objectId: string
}) {
  const base = `/ontologies/${ontologyId}/decision-cases`
  const [selectedId, setSelectedId] = useState<string | null>(null)
  useEffect(() => setSelectedId(null), [ontologyId, objectId])
  const cases = useQuery({
    queryKey: ['decision-cases', ontologyId, objectId],
    queryFn: () => apiClientV2.get<{ cases: CaseSummary[] }>(
      `${base}?target_object_id=${encodeURIComponent(objectId)}`),
  })
  const selected = cases.data?.cases.find(item => item.id === selectedId)
  const whatIfRefs = (Array.isArray(selected?.context?.what_if_scenarios)
    ? selected.context.what_if_scenarios : []).filter(isWhatIfScenarioRef)
  const sourceLink = selected?.kind === 'dynamic_event' && typeof selected.context?.run_id === 'string'
    ? `/data/dynamic?run_id=${encodeURIComponent(selected.context.run_id)}&incident_id=${encodeURIComponent(objectId)}`
    : selected?.kind === 'quality_review' ? `/decision-lab?demo=ibadas&object=${encodeURIComponent(objectId)}`
    : selected?.kind === 'maintenance_review'
      ? `/decision-lab?demo=${selected.target?.object_type === 'Episode' ? 'factorynet' : 'ai4i'}&object=${encodeURIComponent(objectId)}`
      : null
  const events = useQuery({
    queryKey: ['decision-events', ontologyId, selectedId], enabled: !!selectedId,
    queryFn: () => apiClientV2.get<{ events: CaseEvent[] }>(`${base}/${selectedId}/events`),
  })
  if (cases.isLoading) return null
  if (cases.isError) return <p role="alert" className="mt-5 text-xs text-red-700">{formatApiError(cases.error, '审核历史暂不可用')}</p>
  if (!cases.data?.cases.length) return null
  return <section className="mt-5 border-t pt-4" aria-label="审核与 Action 历史">
    <h3 className="text-sm font-semibold">审核与 Action 历史</h3>
    <p className="mt-1 text-xs text-slate-500">当前正式对象的人工结论、来源引用与本地写回记录。</p>
    <div className="mt-3 flex flex-wrap gap-2">{cases.data.cases.slice(0, 20).map(item => <button
      key={item.id} type="button" onClick={() => setSelectedId(item.id === selectedId ? null : item.id)}
      className={`rounded border px-2 py-1 text-xs ${item.id === selectedId ? 'border-[var(--pku-red)] bg-rose-50' : 'border-slate-200'}`}>
      {item.title} · {statusLabel[item.status] ?? item.status}
    </button>)}</div>
    {selected && <div className="mt-3 rounded-lg border bg-slate-50 p-3 text-xs">
      <p className="break-all font-mono text-[10px] text-slate-500">Case {selected.id}</p>
      <p className="mt-2">发起人 {selected.created_by ?? '—'} · 审核人 {selected.reviewed_by ?? '待审核'} · {selected.created_at ? new Date(selected.created_at).toLocaleString('zh-CN') : '—'}</p>
      {selected.reason && <p className="mt-2 whitespace-pre-wrap">理由：{selected.reason}</p>}
      {selected.context?.window_start !== undefined && <p className="mt-2">时间窗口 {String(selected.context.window_start)}–{String(selected.context.window_end)} · 规则 {String(selected.context.rule_version ?? '—')}</p>}
      {selected.context?.event_key !== undefined && <p className="mt-2 break-all">来源事件 {String(selected.context.event_key)}</p>}
      {selected.context?.severity !== undefined && <p className="mt-2">严重度 {String(selected.context.severity)}</p>}
      {whatIfRefs.map(ref => <p key={ref.ref_id} className="mt-2 break-all font-mono text-[10px] text-slate-600">
        What-if {ref.ref_id} · 基线 {ref.source_version.slice(0, 12)} · 情景输入 {ref.scenario_input_digest.slice(0, 12)} · 回放 {String(ref.scenario_context.replay_id ?? '—')} · 时点 {String(ref.scenario_context.time_value ?? '—')}
      </p>)}
      {selected.evidence_refs?.map((ref, index) => <p key={index} className="mt-1 break-all font-mono text-[10px] text-slate-600">证据 {typeof ref === 'string' ? ref : ref.evidence_ref_id}{typeof ref === 'string' ? '' : ` · 资产 ${ref.asset_id ?? '整个样例'} · SHA-256 ${ref.asset_checksum ?? '—'} · ${JSON.stringify(ref.locator ?? {})}`}</p>)}
      {selected.reviewed_preview?.diff?.map((item, index) => <p key={index} className="mt-1 break-all font-mono text-[10px]">{item.property}: {String(item.before ?? '∅')} → {String(item.after ?? '∅')}</p>)}
      {selected.action_run_id && <p className="mt-2 break-all font-mono text-[10px]">Action Run {selected.action_run_id} · 图投影 {selected.projection_status ?? '待查询'}</p>}
      {sourceLink && <Link className="mt-2 inline-block text-blue-700 hover:underline" to={sourceLink}>打开原始演示与证据</Link>}
      {events.isLoading && <p className="mt-2 text-slate-500">正在读取生命周期…</p>}
      {events.isError && <p className="mt-2 text-red-700">{formatApiError(events.error, '生命周期暂不可用')}</p>}
      {!!events.data?.events.length && <ol className="mt-2 border-t pt-2">{events.data.events.map(event => <li key={event.sequence}>{event.sequence}. {eventText(event)} · {event.created_at ? new Date(event.created_at).toLocaleString('zh-CN') : '—'}{typeof event.payload?.revert_run_id === 'string' && <span className="block break-all font-mono text-[10px]">撤销 Run {event.payload.revert_run_id}</span>}</li>)}</ol>}
    </div>}
  </section>
}
