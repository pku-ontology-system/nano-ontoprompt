import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'
import { playbackKey, usePlayback, type PlaybackAlert } from './sensorPlayback'

type Props = { ontologyId: string; playbackId: string; navigateTo?: string; onNavigate?: (page: string) => void; onChanged?: () => void }

const alertStatus: Record<PlaybackAlert['status'], string> = { open: '待处理', acknowledged: '已确认', resolved: '已解决' }
const caseStatus: Record<string, string> = { draft: '草稿', ready_for_review: '待审核', approved: '已批准', rejected: '已驳回', changes_requested: '要求修改', completed: '已执行', reverted: '已回滚' }

export default function AlertActionWidget({ ontologyId, playbackId, navigateTo, onNavigate, onChanged }: Props) {
  const queryClient = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const { data: playback, error: loadError } = usePlayback(ontologyId, playbackId)
  const [busy, setBusy] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [comments, setComments] = useState<Record<string, string>>({})
  const api = `/ontologies/${ontologyId}`

  const act = async (alert: PlaybackAlert, work: () => Promise<unknown>, changed = false) => {
    setBusy(alert.id)
    setErrors(rows => ({ ...rows, [alert.id]: '' }))
    try {
      await work()
      await queryClient.invalidateQueries({ queryKey: playbackKey(ontologyId, playbackId) })
      if (changed) onChanged?.()
    } catch (error) {
      setErrors(rows => ({ ...rows, [alert.id]: formatApiError(error) }))
      await queryClient.invalidateQueries({ queryKey: playbackKey(ontologyId, playbackId) })
    } finally {
      setBusy(null)
    }
  }

  if (loadError) return <p role="alert" className="p-3 text-sm text-red-700">{formatApiError(loadError)}</p>
  if (!playback) return <p role="status" className="p-3 text-sm text-slate-500">正在加载告警…</p>
  const alerts = [...playback.alerts].sort((a, b) => (b.end_cycle ?? 0) - (a.end_cycle ?? 0))
  return <div className="h-full space-y-2 overflow-auto p-1" data-testid="sensor-alerts">
    <div className="flex items-baseline justify-between gap-2"><div><p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">告警与处置</p>
      <p className="text-xs text-slate-500">规则只给出建议动作；改动设备风险等级要走“提交审核 → 审核人批准 → 执行”。</p></div>
      <span className="text-xs text-slate-500">{alerts.length} 条告警</span></div>
    {alerts.length === 0 && <p className="rounded-lg border border-dashed p-4 text-sm text-slate-500">还没有告警。推进回放，规则会在传感数据持续偏高时给出告警。</p>}
    {alerts.map(alert => {
      const flow = alert.case
      const comment = comments[alert.id] ?? ''
      const pending = busy === alert.id
      return <article key={alert.id} className="rounded-xl border bg-white p-3" data-testid={`sensor-alert-${alert.asset_id}`} data-case-status={flow?.status ?? 'none'}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2"><h4 className="text-sm font-semibold">{alert.name}</h4>
            <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-800">{alert.severity === 'high' ? '高' : alert.severity}</span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px]">{alertStatus[alert.status]}</span>
            {alert.recovered && <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[11px] text-emerald-800">条件已恢复</span>}</div>
          <span className="text-xs text-slate-500">第 {alert.start_cycle}–{alert.end_cycle} 周期 · 均值峰值 {alert.peak} {playback.dataset.unit} · 连续 {alert.breaching_windows} 个窗口</span>
        </div>
        <p className="mt-1 text-xs text-slate-600">建议动作：标记高风险（把风险等级改为 High，检修排程才会把它当作高风险设备）</p>

        <div className="mt-2 flex flex-wrap items-center gap-2">
          {!flow && alert.status !== 'resolved' && <button type="button" className="wb-button-primary" disabled={pending || role === 'viewer'}
            onClick={() => void act(alert, () => apiClientV2.post(`${api}/alerts/${alert.id}:propose`, {}))}>提交处置审核</button>}
          {flow && <span className="rounded bg-blue-50 px-2 py-0.5 text-xs text-blue-800" data-testid="case-status">处置单：{caseStatus[flow.status] ?? flow.status}</span>}
          {flow?.status === 'ready_for_review' && <>
            <input className="wb-input min-w-56 flex-1" aria-label="审核意见" placeholder="审核意见（可选）" value={comment} onChange={event => setComments(rows => ({ ...rows, [alert.id]: event.target.value }))} />
            <button type="button" className="wb-button-primary" disabled={pending || role !== 'admin'} title={role !== 'admin' ? '需要审核人账号' : undefined}
              onClick={() => void act(alert, () => apiClientV2.post(`${api}/decision-cases/${flow.id}:review`, { decision: 'approved', comment }))}>批准</button>
            <button type="button" className="wb-button-secondary" disabled={pending || role !== 'admin'}
              onClick={() => void act(alert, () => apiClientV2.post(`${api}/decision-cases/${flow.id}:review`, { decision: 'rejected', comment }))}>驳回</button>
          </>}
          {flow?.status === 'approved' && <button type="button" className="wb-button-primary" disabled={pending}
            onClick={() => void act(alert, () => apiClientV2.post(`${api}/decision-cases/${flow.id}:execute`, {}), true)}>执行</button>}
          {flow?.status === 'completed' && <>
            <span className="text-xs font-medium text-emerald-700">已执行：{alert.name} 的风险等级已改为 High</span>
            {alert.status !== 'resolved' && <button type="button" className="wb-button-secondary" disabled={pending}
              onClick={() => void act(alert, () => apiClientV2.post(`${api}/alerts/${alert.id}:resolve`, { note: '已处置：标记高风险', action_run_id: flow.action_run_id }), true)}>关闭告警</button>}
            {navigateTo && onNavigate && <button type="button" className="wb-button-secondary" onClick={() => onNavigate(navigateTo)}>用新的风险重排 →</button>}
          </>}
          {(flow?.status === 'rejected' || flow?.status === 'changes_requested') && <span className="text-xs text-amber-700">审核没有通过，可以在对象页重新发起</span>}
        </div>
        {errors[alert.id] && <p role="alert" className="mt-2 text-xs text-red-700">{errors[alert.id]}</p>}
      </article>
    })}
  </div>
}
