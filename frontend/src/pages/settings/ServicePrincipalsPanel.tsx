import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, History, Plus, ShieldCheck, X } from 'lucide-react'
import servicePrincipalsApi, { type ServicePrincipal } from '@/api/v2/servicePrincipals'
import pipelinesApi from '@/api/v2/pipelines'

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'detail' in error) {
    const detail = (error as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object' && 'message' in detail) {
      const message = (detail as { message?: unknown }).message
      if (typeof message === 'string') return message
    }
  }
  return '请求失败，请检查管理员权限和服务端状态。'
}

export default function ServicePrincipalsPanel() {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [selectedPipelineId, setSelectedPipelineId] = useState('')
  const [reason, setReason] = useState('')
  const [message, setMessage] = useState('')
  const [expandedId, setExpandedId] = useState('')

  const principalsQuery = useQuery({
    queryKey: ['service-principals'],
    queryFn: () => servicePrincipalsApi.list(),
  })
  const pipelinesQuery = useQuery({
    queryKey: ['service-principal-grant-pipelines'],
    queryFn: () => pipelinesApi.list(),
  })
  const eventsQuery = useQuery({
    queryKey: ['service-principal-events', expandedId],
    queryFn: () => servicePrincipalsApi.events(expandedId),
    enabled: !!expandedId,
  })

  const principals = useMemo(() => principalsQuery.data?.items || [], [principalsQuery.data])
  const pipelines = pipelinesQuery.data || []
  const activeGrants = useMemo(
    () => principals.flatMap(principal => principal.grants.filter(grant => grant.active)),
    [principals],
  )

  const createMutation = useMutation({
    mutationFn: () => servicePrincipalsApi.create({ name: name.trim(), description: description.trim() || undefined }),
    onSuccess: async () => {
      setName('')
      setDescription('')
      setMessage('服务主体已登记；还需要授予精确的 Pipeline Run 范围。')
      await qc.invalidateQueries({ queryKey: ['service-principals'] })
    },
    onError: error => setMessage(errorMessage(error)),
  })

  const grantMutation = useMutation({
    mutationFn: (principalId: string) => servicePrincipalsApi.grantPipelineRun(principalId, {
      pipeline_id: selectedPipelineId,
      reason: reason.trim(),
    }),
    onSuccess: async () => {
      setReason('')
      setMessage('已授予指定 Pipeline 的 Run 权限。')
      await qc.invalidateQueries({ queryKey: ['service-principals'] })
      await qc.invalidateQueries({ queryKey: ['pipeline-schedule-principals'] })
    },
    onError: error => setMessage(errorMessage(error)),
  })

  const revokeMutation = useMutation({
    mutationFn: ({ principalId, grantId }: { principalId: string; grantId: string }) =>
      servicePrincipalsApi.revokeGrant(principalId, grantId, reason.trim()),
    onSuccess: async () => {
      setReason('')
      setMessage('授权已撤销，后续触发会被拒绝。')
      await qc.invalidateQueries({ queryKey: ['service-principals'] })
      await qc.invalidateQueries({ queryKey: ['pipeline-schedule-principals'] })
    },
    onError: error => setMessage(errorMessage(error)),
  })

  const statusMutation = useMutation({
    mutationFn: ({ principalId, status }: { principalId: string; status: ServicePrincipal['status'] }) =>
      servicePrincipalsApi.updateStatus(principalId, status, reason.trim()),
    onSuccess: async () => {
      setReason('')
      setMessage('服务主体状态已更新。')
      await qc.invalidateQueries({ queryKey: ['service-principals'] })
      await qc.invalidateQueries({ queryKey: ['pipeline-schedule-principals'] })
    },
    onError: error => setMessage(errorMessage(error)),
  })

  const hasGrant = (principalId: string) => activeGrants.some(grant =>
    grant.service_principal_id === principalId && grant.resource_type === 'pipeline' &&
    grant.resource_id === selectedPipelineId && grant.permission === 'run',
  )

  return (
    <div className="max-w-6xl space-y-4">
      <section className="wb-surface p-4">
        <div className="wb-section-kicker"><ShieldCheck size={13} /> FR-08 主体与权限</div>
        <h3 className="wb-section-title mt-1">服务主体注册表</h3>
        <p className="mt-2 text-xs text-slate-500">这里只登记内部执行身份和精确资源授权，不创建 API 密钥，也不代表外部 API/MCP 客户端认证已实现。调度触发时会同时复核主体状态、Pipeline Run 授权与委托人的当前数据权限。</p>
      </section>

      {(principalsQuery.isError || pipelinesQuery.isError) && (
        <p role="alert" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          {errorMessage(principalsQuery.error || pipelinesQuery.error)} 只有管理员可以管理注册表；编辑者仅能读取自己可运行的 Pipeline 对应主体。
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="wb-surface space-y-3 p-4">
          <div className="flex items-center justify-between">
            <div><h4 className="text-sm font-semibold">已登记主体</h4><p className="mt-1 text-xs text-slate-500">{principals.length} 个主体 · {activeGrants.length} 条有效授权</p></div>
            <button type="button" onClick={() => void principalsQuery.refetch()} className="wb-button-secondary text-xs">刷新</button>
          </div>
          {principalsQuery.isLoading ? <p className="py-6 text-center text-xs text-slate-400">读取中…</p> : principals.length === 0 ? (
            <p className="rounded-lg border border-dashed border-slate-300 p-4 text-xs text-slate-500">当前没有可显示的服务主体。管理员可在右侧登记服务身份。</p>
          ) : principals.map(principal => (
            <article key={principal.id} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2"><strong className="text-sm">{principal.name}</strong><span className={`rounded-full px-2 py-0.5 text-[10px] ${principal.status === 'active' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{principal.status === 'active' ? '启用' : '暂停'}</span><span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">{principal.kind === 'system' ? '系统主体' : '服务主体'}{principal.builtin ? ' · 内置' : ''}</span></div>
                  <p className="mt-1 break-all font-mono text-[10px] text-slate-400">{principal.id} · revision {principal.revision}</p>
                  {principal.description && <p className="mt-1 text-xs text-slate-500">{principal.description}</p>}
                </div>
                <div className="flex gap-2">
                  <button type="button" onClick={() => setExpandedId(current => current === principal.id ? '' : principal.id)} className="inline-flex items-center gap-1 text-xs text-slate-600 hover:text-black"><History size={13} />审计</button>
                  <button type="button" disabled={statusMutation.isPending || !reason.trim()} onClick={() => statusMutation.mutate({ principalId: principal.id, status: principal.status === 'active' ? 'suspended' : 'active' })} className="text-xs text-amber-700 disabled:opacity-40">{principal.status === 'active' ? '暂停' : '恢复'}</button>
                </div>
              </div>

              <div className="mt-3 space-y-2">
                {principal.grants.map(grant => (
                  <div key={grant.id} className="flex flex-wrap items-center gap-2 rounded-md bg-slate-50 px-2.5 py-2 text-[11px]">
                    <span className={grant.active ? 'text-emerald-700' : 'text-slate-400'}>{grant.active ? '有效' : '已撤销'}</span>
                    <span className="font-mono text-slate-700">{grant.resource_type}:{grant.resource_id}</span>
                    <span className="text-slate-500">{grant.permission}</span>
                    <span className="flex-1 text-slate-400">{grant.reason || '—'}</span>
                    {grant.active && <button type="button" disabled={revokeMutation.isPending || !reason.trim()} onClick={() => revokeMutation.mutate({ principalId: principal.id, grantId: grant.id })} className="text-red-600 disabled:opacity-40">撤销</button>}
                  </div>
                ))}
                {principal.grants.length === 0 && <p className="text-[11px] text-slate-400">没有精确资源授权。</p>}
              </div>

              {expandedId === principal.id && <div className="mt-3 rounded-md border border-slate-200 bg-white p-3">
                <p className="mb-2 text-xs font-medium">注册表审计</p>
                {eventsQuery.isLoading ? <p className="text-xs text-slate-400">读取中…</p> : eventsQuery.isError ? <p className="text-xs text-red-600">{errorMessage(eventsQuery.error)}</p> : <div className="space-y-2">
                  <p className={`rounded-md px-2 py-1.5 text-[10px] ${eventsQuery.data?.chain_status === 'verified' ? 'bg-emerald-50 text-emerald-700' : eventsQuery.data?.chain_status === 'invalid' ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-800'}`}>
                    审计链状态：{eventsQuery.data?.chain_status === 'verified' ? '已验证' : eventsQuery.data?.chain_status === 'invalid' ? '校验失败' : eventsQuery.data?.chain_status === 'truncated' ? '记录超过展示上限，无法完整验证' : '暂无事件'} · 已验证 {eventsQuery.data?.verified_event_count || 0}/{eventsQuery.data?.total_event_count || 0}
                  </p>
                  {(eventsQuery.data?.items || []).map(event => <div key={event.id} className={`border-l-2 pl-2 text-[10px] ${event.chain_verified ? 'border-emerald-300' : 'border-red-400'}`}><p className="font-medium">#{event.sequence} · {event.event_type} · {event.chain_verified ? '校验通过' : '校验失败'}</p><p className="text-slate-500">{event.created_at ? new Date(event.created_at).toLocaleString() : '—'} · actor {event.actor_user_id || 'system'} · {event.reason || '无原因说明'}</p><p className="break-all font-mono text-slate-400">sha256:{event.event_digest}</p></div>)}
                  {!eventsQuery.data?.items.length && <p className="text-xs text-slate-400">无审计事件。</p>}
                </div>}
              </div>}
            </article>
          ))}
        </section>

        <aside className="space-y-4">
          <section className="wb-surface space-y-3 p-4">
            <div className="wb-section-kicker"><Plus size={13} /> 新建服务主体</div>
            <input className="wb-input" value={name} onChange={event => setName(event.target.value)} maxLength={200} placeholder="主体名称" />
            <textarea className="wb-input min-h-20" value={description} onChange={event => setDescription(event.target.value)} maxLength={2000} placeholder="用途说明" />
            <button type="button" disabled={!name.trim() || createMutation.isPending} onClick={() => createMutation.mutate()} className="wb-button-primary inline-flex items-center gap-1 text-xs disabled:opacity-40"><Check size={13} />{createMutation.isPending ? '登记中…' : '登记内部主体'}</button>
          </section>

          <section className="wb-surface space-y-3 p-4">
            <div className="wb-section-kicker"><ShieldCheck size={13} /> 精确授权</div>
            <label className="block text-xs text-slate-600">Pipeline
              <select className="wb-input mt-1" value={selectedPipelineId} onChange={event => setSelectedPipelineId(event.target.value)}>
                <option value="">选择当前可见 Pipeline</option>
                {pipelines.map(pipeline => <option key={pipeline.id} value={pipeline.id}>{pipeline.name} · {pipeline.id.slice(0, 8)}</option>)}
              </select>
            </label>
            <label className="block text-xs text-slate-600">操作原因
              <input className="wb-input mt-1" value={reason} onChange={event => setReason(event.target.value)} minLength={5} maxLength={1000} placeholder="至少 5 个字符，写入审计记录" />
            </label>
            <div className="space-y-2">
              {principals.filter(principal => principal.status === 'active').map(principal => <div key={principal.id} className="flex items-center justify-between gap-2 rounded-md border border-slate-200 px-2.5 py-2 text-xs"><span className="min-w-0 truncate">{principal.name}</span><button type="button" disabled={!selectedPipelineId || !reason.trim() || hasGrant(principal.id) || grantMutation.isPending} onClick={() => grantMutation.mutate(principal.id)} className="shrink-0 text-blue-700 disabled:text-slate-400">{hasGrant(principal.id) ? '已授权' : '授予 Pipeline Run'}</button></div>)}
            </div>
            <p className="text-[10px] text-slate-400">当前只开放精确 `pipeline:run` 授权；泛化数据集、Ontology、API/MCP scope 仍未实现。</p>
          </section>

          {message && <div role="status" className="flex items-start justify-between gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600"><span>{message}</span><button type="button" aria-label="关闭" onClick={() => setMessage('')}><X size={13} /></button></div>}
        </aside>
      </div>
    </div>
  )
}
