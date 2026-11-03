import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import { describe, overrideFromError, plain, predictOverride, useRunPackage, type DecisionPackage, type DecisionRunContext, type Delivery, type OverrideInfo, type OverrideMetric, type PlanLike } from './decisionPackage'

const editLabel: Record<string, string> = { set_property: '修改', create_object: '新建', delete_object: '删除', unset_property: '清除' }
const packageStatus: Record<string, string> = { draft: '草稿', in_review: '审核中', approved: '已批准', rejected: '已驳回', applying: '执行中', applied: '已执行', apply_failed: '执行失败', reverted: '已回滚', superseded: '已替代' }

const MIN_OVERRIDE_REASON = 4
const figure = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 2 })

/** Recommended plan beside the chosen one: what the reviewer needs to judge a human override. */
function OverrideComparison({ info }: { info: OverrideInfo }) {
  const { metric, recommended, chosen } = info
  const unit = metric.unit ? ` ${metric.unit}` : ''
  const name = metric.label ?? '指标'
  const worse = metric.direction === 'maximize' ? recommended.metric_after - chosen.metric_after : chosen.metric_after - recommended.metric_after
  const gap = worse === 0 ? `${name}与推荐方案相同` : worse > 0 ? `${name}比推荐方案差 ${figure(worse)}${unit}` : `${name}比推荐方案好 ${figure(-worse)}${unit}`
  return <div className="text-xs" data-testid="override-comparison">
    <table className="w-full text-left"><thead><tr className="text-slate-500"><th className="w-28 font-normal" /><th className="font-normal">推荐方案</th><th className="font-normal">所选方案</th></tr></thead><tbody>
      <tr className="border-t"><th className="py-1 font-normal text-slate-500">方案</th><td>{plain(info.recommended_label)}</td><td>{plain(info.chosen_label)}</td></tr>
      <tr className="border-t"><th className="py-1 font-normal text-slate-500">{name}</th><td>{figure(recommended.metric_after)}{unit}</td><td>{figure(chosen.metric_after)}{unit}</td></tr>
      <tr className="border-t"><th className="py-1 font-normal text-slate-500">新增占用</th><td>{figure(recommended.cost)}</td><td>{figure(chosen.cost)}</td></tr>
      <tr className="border-t"><th className="py-1 font-normal text-slate-500">受损对象</th><td>{recommended.harmed.length}</td><td>{chosen.harmed.length}</td></tr>
    </tbody></table>
    <p className="mt-1 font-medium">{gap}</p>
  </div>
}

type PanelProps = {
  context?: DecisionRunContext
  candidate: { id: string; applicable: boolean }
  actionLabel?: Record<string, string>
  /** All plans of the run and its metric, so the panel can tell when the chosen plan is not the recommended one. */
  plans?: PlanLike[]
  metric?: OverrideMetric
}

export default function DecisionPackagePanel({ context, candidate, actionLabel = {}, plans, metric }: PanelProps) {
  const { pkg, setPkg } = useRunPackage(context)
  const [rationale, setRationale] = useState('')
  const [reviewReason, setReviewReason] = useState('')
  const [overrideReason, setOverrideReason] = useState('')
  const [demanded, setDemanded] = useState<OverrideInfo | null>(null)
  const [delivery, setDelivery] = useState<Delivery | null>(null)
  const packagesUrl = `/ontologies/${context?.ontologyId}/decision-packages`
  // The server compiles the candidate into typed edits from the run's pinned data view.
  const predicted = predictOverride(plans ?? [], candidate.id, metric)
  const needsOverride = predicted ?? (demanded?.chosen_candidate_id === candidate.id ? demanded : null)
  const createPackage = useMutation({
    mutationFn: () => apiClientV2.post<DecisionPackage>(`/ontologies/${context!.ontologyId}/scenario-study-runs/${context!.runId}/decision-packages`,
      { candidate_id: candidate.id, rationale: rationale.trim(), ...(needsOverride ? { override_reason: overrideReason.trim() } : {}) }),
    onSuccess: setPkg,
    // Not the recommended plan: the server answers with the comparison and asks for a reason.
    onError: failure => { const info = overrideFromError(failure); if (info) setDemanded(info) },
  })
  const transition = useMutation({
    mutationFn: (event: 'submit' | 'approve' | 'reject' | 'revise') => apiClientV2.post<DecisionPackage>(`${packagesUrl}/${pkg!.id}/transition`, { event, expected_etag: pkg!.etag, request_id: crypto.randomUUID(), reason: reviewReason.trim() }),
    onSuccess: setPkg,
  })
  // One request id per click; a retried request is answered from the recorded event.
  const command = useMutation({
    mutationFn: (verb: 'apply' | 'revert' | 'deliver-outbox') => apiClientV2.post<{ package: DecisionPackage; delivery?: Delivery }>(`${packagesUrl}/${pkg!.id}:${verb}`, verb === 'deliver-outbox' ? undefined : { expected_etag: pkg!.etag, request_id: crypto.randomUUID() }),
    onSuccess: result => { setPkg(result.package); if (result.delivery) setDelivery(result.delivery) },
  })
  const error = (overrideFromError(createPackage.error) ? null : createPackage.error) || transition.error || command.error
  const edits = pkg?.actions.flatMap(action => action.expected_edits.filter(edit => edit.op !== 'invoke_action').map(edit => ({ action, edit }))) ?? []
  const label = (key: string) => actionLabel[key] ?? key

  return <>
    {!pkg ? <>
      {needsOverride && <div className="my-2 space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm" data-testid="override-box">
        <p className="font-medium text-amber-900">你选择的不是推荐方案</p>
        <OverrideComparison info={needsOverride} />
        <label className="block text-xs font-medium text-amber-900">覆盖理由（必填，审核人会看到）
          <textarea className="mt-1 w-full rounded border border-amber-300 bg-white p-2 text-sm font-normal" aria-label="覆盖理由" value={overrideReason} rows={3}
            onChange={event => setOverrideReason(event.target.value)} placeholder="为什么不采用推荐方案，以及谁认可了这个取舍" />
        </label>
      </div>}
      <label className="candidate-field">决策理由<textarea value={rationale} onChange={event => setRationale(event.target.value)} rows={3} placeholder="为什么选择这个方案，以及接受哪些代价" /></label>
      <button className="wb-button-primary" disabled={!candidate.applicable || !context || !rationale.trim() || (!!needsOverride && overrideReason.trim().length < MIN_OVERRIDE_REASON) || createPackage.isPending} onClick={() => createPackage.mutate()}>创建决策包</button>
      {!context && <p className="candidate-note">运行完成后才能创建决策包。</p>}
    </> : <div className="package-panel" data-testid="decision-package">
      <p><strong>{packageStatus[pkg.status] ?? pkg.status}</strong> · 版本 {pkg.etag}</p>
      {pkg.override && <div className="my-2 space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm" data-testid="override-record">
        <p className="font-medium text-amber-900">人工覆盖了推荐方案</p>
        <OverrideComparison info={pkg.override} />
        <p className="text-xs"><span className="text-slate-500">覆盖理由：</span>{pkg.override.reason}</p>
      </div>}
      <p className="candidate-note">服务端编译的本体变更（{edits.length} 项，执行时会重新编译并核对摘要）：</p>
      <ol className="candidate-actions" data-testid="package-edits">{edits.map(({ action, edit }) => <li key={edit.sequence}><strong>{editLabel[edit.op] ?? edit.op} · {edit.target?.concrete_type} {plain(edit.target?.object_id ?? '')}</strong><small>{edit.property ? plain(`${edit.property}: ${String(edit.expected_old_value ?? '空')} → ${String(edit.value ?? '空')}`) : describe({ action_type: action.action_type_id, ...edit.properties })}</small></li>)}</ol>
      {pkg.status === 'draft' && <button className="wb-button-primary" disabled={transition.isPending} onClick={() => transition.mutate('submit')}>提交审核</button>}
      {pkg.status === 'in_review' && <>
        <label className="candidate-field">审核意见{pkg.override ? '（必填）' : ''}<textarea value={reviewReason} onChange={event => setReviewReason(event.target.value)} rows={2} placeholder={pkg.override ? '该方案覆盖了推荐方案，批准或驳回都请写明意见' : '驳回时必填'} /></label>
        <div className="package-buttons"><button className="wb-button-primary" disabled={transition.isPending || (!!pkg.override && !reviewReason.trim())} onClick={() => transition.mutate('approve')}>批准</button><button className="wb-button-secondary" disabled={transition.isPending || !reviewReason.trim()} onClick={() => transition.mutate('reject')}>驳回</button></div>
      </>}
      {pkg.status === 'rejected' && <button className="wb-button-secondary" disabled={transition.isPending} onClick={() => transition.mutate('revise')}>退回草稿修改</button>}
      {(pkg.status === 'approved' || pkg.status === 'apply_failed') && <button className="wb-button-primary" disabled={command.isPending} onClick={() => command.mutate('apply')}>{pkg.status === 'apply_failed' ? '重试执行' : '执行（合并到本体）'}</button>}
      {pkg.status === 'applied' && <>
        <ul className="package-receipts" data-testid="package-receipts">{pkg.actions.filter(action => action.external_effect_class !== 'none').map(action => <li key={action.id}>{label(action.action_type_id)} · {action.receipt ? `回执 ${String(action.receipt.external_id)}` : '待发送'}</li>)}</ul>
        {delivery?.status === 'external_effect_partial' && <p className="candidate-warning">本体变更已合并，但有 {delivery.external_effects - delivery.delivered} 项外部通知未送达。</p>}
        <div className="package-buttons">
          {pkg.outbox.some(row => !row.delivered) && <button className="wb-button-secondary" disabled={command.isPending} onClick={() => command.mutate('deliver-outbox')}>重发外部通知</button>}
          <button className="wb-button-secondary" disabled={command.isPending} onClick={() => command.mutate('revert')}>回滚本体变更</button>
        </div>
      </>}
      {pkg.status === 'reverted' && pkg.actions.some(action => action.receipt) && <p className="candidate-warning">本体变更已回滚；已发出的外部通知无法自动撤回，需要人工跟进。</p>}
      <ol className="package-events">{pkg.events.map(row => <li key={row.sequence}>{row.event_type} · {row.actor_id.slice(0, 8)}{row.reason ? ` · ${row.reason}` : ''}</li>)}</ol>
    </div>}
    {error && <p role="alert" className="candidate-warning">{formatApiError(error)}</p>}
    <p className="candidate-note">提交后引用的运行摘要和情景版本不可变；创建者不能审批自己的方案。</p>
  </>
}
