import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import { toWhatIfScenarioRef, type WhatIfScenarioSummary } from '@/api/v2/whatIfScenarioRefs'
import { useAuthStore } from '@/stores/authStore'
import type { EvidenceAsset } from '@/pages/data-management/multimodal/MultimodalEvidenceWorkspace'

type Domain = 'ai4i' | 'factorynet' | 'ibadas' | 'dynamic'
type TargetState = { object_id: string; object_type: string; etag: string; source_version?: string; properties: Record<string, unknown> }
type Preview = { preview_digest: string; preview: { diff: Array<{ op: string; property: string; before: unknown; after: unknown }>; source_version?: string; permission_decision: { allowed: boolean; digest: string } } }
type Case = { id: string; status: string; title: string; parameters: { decision?: string; reason?: string }; reason: string; context?: Record<string, unknown>; evidence_refs?: Array<string | { evidence_ref_id: string; asset_id?: string; locator?: Record<string, unknown> }>; reviewed_preview?: Preview['preview']; preview_digest?: string; action_run_id?: string; action_run_actors?: { requester_id?: string | null; reviewer_id?: string | null; executor_id?: string | null } | null; projection_status?: string; projection_attempts?: number; projection_manual_attempts?: number; reviewed_by?: string; review_comment?: string; created_at?: string }
type CaseEvent = { sequence: number; event_type: string; actor_id?: string | null; created_at?: string; payload: Record<string, unknown> }

const specs = {
  ai4i: { kind: 'maintenance_review', suffix: 'maintenance_review', title: '维护复核', field: 'maintenance_status', options: [
    ['inspection_requested', '建议检查'], ['inspection_deferred', '暂缓检查'], ['not_required', '无需检查'],
  ] },
  factorynet: { kind: 'maintenance_review', suffix: 'maintenance_review', title: '加工批次维护复核', field: 'maintenance_status', options: [
    ['inspection_requested', '建议检查'], ['inspection_deferred', '暂缓检查'], ['not_required', '无需检查'],
  ] },
  dynamic: { kind: 'dynamic_event', suffix: 'factorynet_disposition_v1', title: '动态事件人工处置', field: 'disposition_status', options: [
    ['confirmed', '确认事件'], ['ignored', '忽略误报'], ['in_progress', '处置中'], ['resolved', '已处置'],
  ] },
  ibadas: { kind: 'quality_review', suffix: 'quality_review', title: '质量人工复核', field: 'review_status', options: [
    ['needs_followup', '需要后续复核'], ['no_issue', '未发现问题'], ['insufficient_evidence', '证据不足'],
  ] },
} as const

const statusText: Record<string, string> = {
  draft: '草稿', ready_for_review: '待审核', approved: '已批准', rejected: '已驳回',
  changes_requested: '退回修改', completed: '已执行', failed: '执行失败', reverted: '已撤销',
}

function eventText(item: CaseEvent): string {
  const payload = item.payload ?? {}
  if (item.event_type === 'projection_attempt_started') {
    const kind = payload.attempt_kind === 'manual_retry' ? '人工重放' : payload.actor_kind === 'system' ? '系统投影' : '初始投影'
    const number = payload.attempt_number === undefined ? '' : ` #${String(payload.attempt_number)}`
    const reason = typeof payload.reason === 'string' && payload.reason ? ` · 理由：${payload.reason}` : ''
    const actor = payload.actor_kind === 'system' ? ' · system worker' : item.actor_id ? ` · 操作者 ${item.actor_id}` : ''
    return `${kind}${number} 开始${reason}${actor}`
  }
  if (item.event_type === 'projection_attempt_finished') {
    const status = String(payload.status ?? 'unknown')
    const error = typeof payload.error === 'string' && payload.error ? ` · ${payload.error}` : ''
    return `投影 attempt 结束：${status}${error}`
  }
  return item.event_type
}

export function DecisionCasePanel({ domain, ontologyId, objectId, evidenceRefId, evidenceRefIds, mediaAssets, context }: {
  domain: Domain; ontologyId: string; objectId: string; evidenceRefId: string; evidenceRefIds?: string[]; mediaAssets?: EvidenceAsset[]; context?: Record<string, unknown>
}) {
  const spec = specs[domain]
  const queryClient = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const userId = useAuthStore(state => state.user?.id)
  const canWrite = role === 'admin' || role === 'editor'
  const isAdmin = role === 'admin'
  const base = `/ontologies/${ontologyId}/decision-cases`
  const [decision, setDecision] = useState<string>(spec.options[0][0])
  const [reason, setReason] = useState('')
  const [severity, setSeverity] = useState('unknown')
  const [pendingEvidence, setPendingEvidence] = useState('')
  const [whatIfScenarioId, setWhatIfScenarioId] = useState('')
  const [assetId, setAssetId] = useState('')
  const [locatorKind, setLocatorKind] = useState<'whole_asset' | 'image_region' | 'point_cloud_view'>('whole_asset')
  const [region, setRegion] = useState({ x: 0.1, y: 0.1, width: 0.8, height: 0.8 })
  const [view, setView] = useState({ yaw: 0.62, pitch: -0.34, zoom: 1 })
  const [caseId, setCaseId] = useState<string | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showReconcile, setShowReconcile] = useState(false)
  const [reconcileReason, setReconcileReason] = useState('')
  const [reconcileRequestId, setReconcileRequestId] = useState('')
  const [reconcileRequestLocked, setReconcileRequestLocked] = useState(false)
  const selectedAsset = mediaAssets?.find(item => item.id === assetId)
  useEffect(() => {
    if (domain === 'ibadas' && mediaAssets?.length && !assetId) setAssetId(mediaAssets[0].id)
  }, [domain, mediaAssets, assetId])

  function selectedEvidence(): Array<string | Record<string, unknown>> {
    if (domain !== 'ibadas' || !assetId) return evidenceRefIds?.length ? evidenceRefIds : [evidenceRefId]
    if (!selectedAsset?.checksum) throw new Error('所选媒体缺少 SHA-256，不能绑定复核证据')
    const locator = locatorKind === 'image_region'
      ? { kind: locatorKind, ...region }
      : locatorKind === 'point_cloud_view' ? { kind: locatorKind, ...view } : { kind: 'whole_asset' }
    return [{ evidence_ref_id: evidenceRefId, asset_id: selectedAsset.id,
      asset_checksum: selectedAsset.checksum, locator }]
  }

  function selectedContext(): Record<string, unknown> {
    const next = { ...(context ?? {}) }
    if (whatIfScenarioId) {
      const scenario = availableWhatIfScenarios.find(item => item.id === whatIfScenarioId)
      const whatIfRef = scenario ? toWhatIfScenarioRef(scenario) : null
      if (!whatIfRef) {
        throw new Error('所选 What-if 情景暂不可用，请刷新情景列表后重试')
      }
      next.what_if_scenarios = [whatIfRef]
    } else {
      delete next.what_if_scenarios
    }
    if (domain === 'ibadas') {
      return { ...next, severity,
        pending_evidence: pendingEvidence.split('\n').map(item => item.trim()).filter(Boolean) }
    }
    return next
  }

  const target = useQuery({ queryKey: ['decision-target', ontologyId, objectId],
    queryFn: () => apiClientV2.get<TargetState>(`${base}/target-state/${encodeURIComponent(objectId)}`) })
  const cases = useQuery({ queryKey: ['decision-cases', ontologyId, objectId],
    queryFn: () => apiClientV2.get<{ cases: Case[] }>(`${base}?target_object_id=${encodeURIComponent(objectId)}`) })
  const whatIfScenarios = useQuery({
    queryKey: ['what-if-scenario-references', ontologyId, userId],
    queryFn: () => apiClientV2.get<{ scenarios: WhatIfScenarioSummary[] }>(
      `/ontologies/${ontologyId}/what-if/scenarios?summary=true`),
    enabled: canWrite && !!userId,
  })
  const availableWhatIfScenarios = (whatIfScenarios.data?.scenarios ?? []).filter(item =>
    item.created_by === userId && item.target_ids.includes(objectId))
  const selectedWhatIfScenario = availableWhatIfScenarios.find(item => item.id === whatIfScenarioId)
  const selected = useQuery({ queryKey: ['decision-case', ontologyId, caseId],
    queryFn: () => apiClientV2.get<Case>(`${base}/${caseId}`), enabled: !!caseId })
  const events = useQuery({ queryKey: ['decision-events', ontologyId, caseId],
    queryFn: () => apiClientV2.get<{ events: CaseEvent[] }>(`${base}/${caseId}/events`), enabled: !!caseId })

  useEffect(() => {
    if (!caseId && cases.data?.cases[0]) setCaseId(cases.data.cases[0].id)
  }, [caseId, cases.data])
  const current = selected.data
  const shownPreview = preview?.preview ?? current?.reviewed_preview
  let storedReconcileRequest: string | null = null
  if (current?.action_run_id) {
    try { storedReconcileRequest = sessionStorage.getItem(`project4:action-projection:${current.action_run_id}`) }
    catch { storedReconcileRequest = null }
  }
  useEffect(() => {
    if (current) {
      setDecision(current.parameters.decision ?? spec.options[0][0]); setReason(current.reason)
      setShowReconcile(false); setReconcileReason(''); setReconcileRequestId(''); setReconcileRequestLocked(false)
      if (domain === 'ibadas') {
        setSeverity(String(current.context?.severity ?? 'unknown'))
        setPendingEvidence(Array.isArray(current.context?.pending_evidence)
          ? current.context.pending_evidence.join('\n') : '')
      }
      const whatIfRefs = Array.isArray(current.context?.what_if_scenarios)
        ? current.context.what_if_scenarios : []
      const firstWhatIf = whatIfRefs.find(item => !!item && typeof item === 'object'
        && typeof (item as Record<string, unknown>).ref_id === 'string') as Record<string, unknown> | undefined
      setWhatIfScenarioId(typeof firstWhatIf?.ref_id === 'string' ? firstWhatIf.ref_id : '')
    }
  }, [current?.id])

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['decision-target', ontologyId, objectId] }),
      queryClient.invalidateQueries({ queryKey: ['decision-cases', ontologyId, objectId] }),
      queryClient.invalidateQueries({ queryKey: ['decision-case', ontologyId] }),
      queryClient.invalidateQueries({ queryKey: ['decision-events', ontologyId] }),
      queryClient.invalidateQueries({ queryKey: ['decision-lab-evidence', domain, objectId] }),
    ])
  }

  async function perform(operation: () => Promise<void>) {
    setBusy(true); setError('')
    try { await operation(); await refresh() }
    catch (exc) { setError(formatApiError(exc, '决策事项操作失败')) }
    finally { setBusy(false) }
  }

  function create() {
    if (!target.data || !reason.trim()) return
    void perform(async () => {
      const body = {
        kind: spec.kind, title: spec.title, action_type_id: `${ontologyId}:action:${spec.suffix}`,
        target_object_id: objectId, target_object_type: target.data!.object_type,
        parameters: { target: { object_type: target.data!.object_type, object_id: objectId },
          decision, reason: reason.trim() },
        evidence_refs: selectedEvidence(), reason: reason.trim(), context: selectedContext(),
        expected_etag: target.data!.etag, client_request_id: crypto.randomUUID(),
      }
      const created = await apiClientV2.post<Case>(base, body)
      setCaseId(created.id)
      setPreview(await apiClientV2.post<Preview>(`${base}/${created.id}:preview`, {}))
    })
  }

  function previewCase() {
    if (!caseId) return
    void perform(async () => { setPreview(await apiClientV2.post<Preview>(`${base}/${caseId}:preview`, {})) })
  }

  function updateDraft() {
    if (!caseId || !target.data || !reason.trim()) return
    void perform(async () => {
      await apiClientV2.patch(`${base}/${caseId}`, {
        title: spec.title,
        parameters: { target: { object_type: target.data!.object_type, object_id: objectId },
          decision, reason: reason.trim() },
        evidence_refs: selectedEvidence(), reason: reason.trim(), context: selectedContext(),
        expected_etag: target.data!.etag,
      })
      setPreview(await apiClientV2.post<Preview>(`${base}/${caseId}:preview`, {}))
    })
  }

  function submit() {
    if (!caseId || !preview) return
    void perform(async () => {
      await apiClientV2.post(`${base}/${caseId}:submit`, { preview_digest: preview.preview_digest })
    })
  }

  function review(decisionValue: 'approved' | 'rejected' | 'changes_requested') {
    if (!caseId) return
    void perform(async () => {
      await apiClientV2.post(`${base}/${caseId}:review`, { decision: decisionValue,
        comment: decisionValue === 'approved' ? '已核对预览差异与来源证据' : '请按审核意见重新处理' })
    })
  }

  function execute() {
    if (!caseId) return
    void perform(async () => { await apiClientV2.post(`${base}/${caseId}:execute`, {}) })
  }

  function openReconcile() {
    if (!current?.action_run_id) return
    const storageKey = `project4:action-projection:${current.action_run_id}`
    const stored = sessionStorage.getItem(storageKey)
    if (stored) {
      try {
        const request = JSON.parse(stored) as { reason?: string; client_request_id?: string }
        if (request.reason && request.client_request_id) {
          setReconcileReason(request.reason)
          setReconcileRequestId(request.client_request_id)
          setReconcileRequestLocked(true)
          setShowReconcile(true)
          return
        }
      } catch { sessionStorage.removeItem(storageKey) }
    }
    setReconcileReason('')
    setReconcileRequestId(crypto.randomUUID())
    setReconcileRequestLocked(false)
    setShowReconcile(true)
  }

  function reconcile() {
    if (!current?.action_run_id || !reconcileReason.trim() || !reconcileRequestId) return
    const request = { reason: reconcileReason.trim(), client_request_id: reconcileRequestId }
    const storageKey = `project4:action-projection:${current.action_run_id}`
    sessionStorage.setItem(storageKey, JSON.stringify(request))
    setReconcileReason(request.reason)
    setReconcileRequestLocked(true)
    void perform(async () => {
      await apiClientV2.post(`${base}/action-runs/${current.action_run_id}:reconcile`, request)
      sessionStorage.removeItem(storageKey)
      setShowReconcile(false)
      setReconcileReason('')
      setReconcileRequestId('')
      setReconcileRequestLocked(false)
    })
  }

  return <section className="mt-6 rounded-xl border border-slate-200 bg-slate-50/70 p-4">
    <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="wb-eyebrow">证据 → 审核 → 写回</p><h3 className="mt-1 text-sm font-semibold">{spec.title}</h3></div><span className="rounded bg-white px-2 py-1 text-[10px] text-slate-600">本地对象状态 · 不控制设备</span></div>
    <p className="mt-2 text-xs leading-5 text-slate-600">原始标签与媒体保持不变。审核前仅保存候选事项；批准后仍需单独执行，执行记录与对象状态可回看。</p>
    {target.isError && <p role="alert" className="mt-3 text-xs text-red-700">{formatApiError(target.error)}</p>}
    {target.data && <p className="mt-3 text-xs text-slate-600">当前状态：<strong>{String(target.data.properties[spec.field] ?? '尚未复核')}</strong><span className="ml-2 font-mono text-[10px] text-slate-400">etag {target.data.etag.slice(0, 12)}</span></p>}
    {canWrite && <div className="mt-4 grid gap-2"><label className="text-xs font-medium">人工结论<select className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={decision} onChange={event => setDecision(event.target.value)}>{spec.options.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label className="text-xs font-medium">What-if 情景（可选）<select className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={whatIfScenarioId} onChange={event => setWhatIfScenarioId(event.target.value)}><option value="">不绑定情景</option>{availableWhatIfScenarios.map(item => <option key={item.id} value={item.id}>{item.name} · {item.status} · {item.id.slice(0, 8)}</option>)}</select></label>{whatIfScenarios.isError && <p className="text-[11px] text-amber-800">What-if 情景列表暂不可用；普通复核仍可继续。{formatApiError(whatIfScenarios.error)}</p>}{whatIfScenarioId && selectedWhatIfScenario && <p className="break-all font-mono text-[10px] text-slate-500">基线 {String(selectedWhatIfScenario.baseline.scenario_context?.context_digest ?? '').slice(0, 16)} · 情景输入 {selectedWhatIfScenario.scenario_input_digest.slice(0, 16)} · 更新前需刷新会话或审核事项</p>}<label className="text-xs font-medium">理由<textarea className="mt-1 min-h-20 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={reason} maxLength={4000} onChange={event => setReason(event.target.value)} placeholder="写明人工判断与需要继续核实的内容" /></label>
      {domain === 'ibadas' && <><div className="grid gap-2 rounded-lg border border-slate-200 bg-white p-3"><label className="text-xs font-medium">严重度<select className="mt-1 w-full rounded border p-2" value={severity} onChange={event => setSeverity(event.target.value)}><option value="unknown">尚不确定</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label><label className="text-xs font-medium">待补证据（每行一项）<textarea className="mt-1 min-h-16 w-full rounded border p-2" value={pendingEvidence} onChange={event => setPendingEvidence(event.target.value)} maxLength={3000} placeholder="例如：补充另一角度的点云" /></label></div><div className="rounded-lg border border-slate-200 bg-white p-3"><p className="text-xs font-semibold">媒体证据定位</p><label className="mt-2 block text-xs">引用资产<select className="mt-1 w-full rounded border border-slate-300 bg-white p-2" value={assetId} onChange={event => { setAssetId(event.target.value); setLocatorKind('whole_asset') }}><option value="">整个样例</option>{mediaAssets?.map(item => <option key={item.id} value={item.id}>{item.role} · {item.original_name ?? item.id.slice(0, 8)}{item.checksum ? '' : ' · 无摘要'}</option>)}</select></label>{selectedAsset && <><p className="mt-2 break-all font-mono text-[10px] text-slate-500">资产 {selectedAsset.id} · SHA-256 {selectedAsset.checksum ?? '缺失'}</p><label className="mt-2 block text-xs">定位方式<select className="mt-1 w-full rounded border border-slate-300 bg-white p-2" value={locatorKind} onChange={event => setLocatorKind(event.target.value as typeof locatorKind)}><option value="whole_asset">整件资产</option>{['image', 'depth', 'mask'].includes(selectedAsset.media_type) && <option value="image_region">图像区域</option>}{selectedAsset.media_type === 'point_cloud' && <option value="point_cloud_view">点云视角</option>}</select></label>{locatorKind === 'image_region' && <div className="mt-2 grid grid-cols-4 gap-2">{(['x', 'y', 'width', 'height'] as const).map(key => <label key={key} className="text-[10px]">{key}<input className="mt-1 w-full rounded border p-1" type="number" min={0} max={1} step={0.05} value={region[key]} onChange={event => setRegion(value => ({ ...value, [key]: Number(event.target.value) }))} /></label>)}</div>}{locatorKind === 'point_cloud_view' && <div className="mt-2 grid grid-cols-3 gap-2">{(['yaw', 'pitch', 'zoom'] as const).map(key => <label key={key} className="text-[10px]">{key}<input className="mt-1 w-full rounded border p-1" type="number" step={0.1} value={view[key]} onChange={event => setView(value => ({ ...value, [key]: Number(event.target.value) }))} /></label>)}</div>}</>}</div></>}
      <button type="button" className="wb-button-secondary" disabled={busy || !target.data || !reason.trim()} onClick={create}>创建复核事项并预览</button></div>}
    {!!cases.data?.cases.length && <div className="mt-5"><p className="text-xs font-semibold">事项记录</p><div className="mt-2 flex flex-wrap gap-2">{cases.data.cases.slice(0, 8).map(item => <button type="button" key={item.id} className={`rounded border px-2 py-1 text-[11px] ${caseId === item.id ? 'border-[var(--pku-red)] bg-rose-50 text-[var(--pku-red)]' : 'border-slate-200 bg-white'}`} onClick={() => { setCaseId(item.id); setPreview(null) }}>{statusText[item.status] ?? item.status} · {item.id.slice(0, 8)}</button>)}</div></div>}
    {current && <div className="mt-4 rounded-lg border bg-white p-3 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><strong>{current.title}</strong><span className="rounded bg-amber-50 px-2 py-1 text-amber-800">{statusText[current.status] ?? current.status}</span></div><p className="mt-2 break-all font-mono text-[10px] text-slate-500">Case ID {current.id}</p>{domain === 'ibadas' && <p className="mt-2 text-slate-600">严重度：{String(current.context?.severity ?? '未记录')} · 待补证据：{Array.isArray(current.context?.pending_evidence) ? current.context.pending_evidence.join('；') || '无' : '无'}</p>}{current.review_comment && <p className="mt-2 text-slate-600">审核意见：{current.review_comment}</p>}{current.evidence_refs?.map((item, index) => typeof item === 'string' ? <p key={index} className="mt-1 break-all font-mono text-[10px] text-slate-500">证据 {item}</p> : <p key={index} className="mt-1 break-all font-mono text-[10px] text-slate-500">媒体 {item.asset_id ?? '整个样例'} · {JSON.stringify(item.locator ?? {})}</p>)}{shownPreview && <div className="mt-3 rounded border border-blue-100 bg-blue-50/50 p-2"><p className="font-semibold">预览差异</p>{shownPreview.diff.map((item, index) => <p key={`${item.property}-${index}`} className="mt-1 break-all font-mono text-[10px]">{item.property}: {String(item.before ?? '∅')} → {String(item.after ?? '∅')}</p>)}<p className="mt-2 font-mono text-[10px] text-slate-500">digest {(preview?.preview_digest ?? current.preview_digest)?.slice(0, 16)}</p></div>}
      <div className="mt-3 flex flex-wrap gap-2">{canWrite && ['draft', 'changes_requested'].includes(current.status) && <button type="button" className="wb-button-secondary" disabled={busy || !target.data || !reason.trim()} onClick={updateDraft}>保存修改并预览</button>}{canWrite && current.status === 'draft' && <button type="button" className="wb-button-secondary" disabled={busy} onClick={previewCase}>刷新预览</button>}{canWrite && current.status === 'draft' && preview && <button type="button" className="wb-button-primary" disabled={busy} onClick={submit}>提交审核</button>}{isAdmin && current.status === 'ready_for_review' && <><button type="button" className="wb-button-primary" disabled={busy} onClick={() => review('approved')}>批准</button><button type="button" className="wb-button-secondary" disabled={busy} onClick={() => review('rejected')}>驳回</button><button type="button" className="wb-button-secondary" disabled={busy} onClick={() => review('changes_requested')}>退回</button></>}{canWrite && current.status === 'approved' && <button type="button" className="wb-button-primary" disabled={busy} onClick={execute}>执行本地写回</button>}{isAdmin && current.status === 'completed' && (['pending', 'failed'].includes(current.projection_status ?? '') || !!storedReconcileRequest) && <button type="button" className="wb-button-secondary" disabled={busy} onClick={openReconcile}>{storedReconcileRequest ? '恢复上次重放请求' : '修复图投影'}</button>}</div>
      {showReconcile && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/60 p-3"><p className="font-semibold">人工重放图投影</p><p className="mt-1 text-[11px] text-slate-600">仅管理员可提交。请求编号用于网络中断后的安全重试；自动重试计数不会被重置。</p><label className="mt-2 block text-[11px] font-medium">重放理由<textarea className="mt-1 min-h-16 w-full rounded border border-slate-300 bg-white p-2 text-xs" value={reconcileReason} maxLength={4000} readOnly={reconcileRequestLocked} onChange={event => setReconcileReason(event.target.value)} placeholder="说明已核对的原因或修复内容" /></label><p className="mt-1 break-all font-mono text-[10px] text-slate-500">请求编号 {reconcileRequestId}</p><div className="mt-2 flex gap-2"><button type="button" className="wb-button-primary" disabled={busy || !reconcileReason.trim()} onClick={reconcile}>{reconcileRequestLocked ? '重试同一请求' : '提交人工重放'}</button><button type="button" className="wb-button-secondary" disabled={busy} onClick={() => setShowReconcile(false)}>关闭</button>{reconcileRequestLocked && <button type="button" className="wb-button-secondary" disabled={busy} onClick={() => { sessionStorage.removeItem(`project4:action-projection:${current.action_run_id}`); setReconcileRequestId(crypto.randomUUID()); setReconcileRequestLocked(false) }}>新建请求编号</button>}</div></div>}
      {current.status === 'completed' && <p className="mt-3 text-[11px] text-slate-600">Action Run {current.action_run_id} · 图投影 {current.projection_status ?? '待查询'}（自动尝试 {current.projection_attempts ?? 0} / 人工重放 {current.projection_manual_attempts ?? 0}）{current.action_run_actors && <> · 发起 {current.action_run_actors.requester_id ?? '未知'} / 审核 {current.action_run_actors.reviewer_id ?? '未审核'} / 执行 {current.action_run_actors.executor_id ?? '未知'}</>}</p>}
      {!!events.data?.events.length && <div className="mt-3 border-t pt-2"><p className="font-semibold">生命周期记录</p><ol className="mt-1 space-y-1 text-[11px] text-slate-600">{events.data.events.map(item => <li key={item.sequence}>{item.sequence}. {eventText(item)} · {item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : ''}</li>)}</ol></div>}
    </div>}
    {error && <p role="alert" className="mt-3 text-xs text-red-700">{error}</p>}
  </section>
}
