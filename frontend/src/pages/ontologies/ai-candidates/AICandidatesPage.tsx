import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import { ontologyApi } from '@/api/ontologies'
import CandidateItemCard, { type ReviewBody } from './CandidateItemCard'
import GeneratePanel from './GeneratePanel'
import ProgressPanel from './ProgressPanel'
import StatsPanel from './StatsPanel'
import { statusLabel, type CandidateItem, type CandidateSet, type PublishResult, type RunnerInfo, type Validation } from './types'

const statusStyle: Record<string, string> = {
  generating: 'bg-blue-100 text-blue-800', review: 'bg-amber-100 text-amber-800', published: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-800', cancelled: 'bg-slate-200 text-slate-700',
}

export default function AICandidatesPage() {
  const { id: ontologyId = '' } = useParams()
  const queryClient = useQueryClient()
  const base = `/ontologies/${ontologyId}/ai-candidates`
  const [pickedId, setPickedId] = useState('')
  const [validation, setValidation] = useState<Validation | null>(null)

  const ontology = useQuery({ queryKey: ['ontology', ontologyId], queryFn: () => ontologyApi.get(ontologyId), enabled: !!ontologyId })
  const runner = useQuery({ queryKey: ['ai-runner', ontologyId], queryFn: () => apiClientV2.get<RunnerInfo>(`${base}/runner`), enabled: !!ontologyId, refetchInterval: 30_000 })
  const sets = useQuery({
    queryKey: ['ai-sets', ontologyId], queryFn: () => apiClientV2.get<{ sets: CandidateSet[] }>(base), enabled: !!ontologyId,
    refetchInterval: query => (query.state.data?.sets.some(row => row.status === 'generating') ? 3000 : false),
  })
  const activeId = pickedId || sets.data?.sets[0]?.id || ''
  const detail = useQuery({
    queryKey: ['ai-set', ontologyId, activeId], queryFn: () => apiClientV2.get<CandidateSet>(`${base}/${activeId}`), enabled: !!activeId,
    refetchInterval: query => (query.state.data?.status === 'generating' ? 2000 : false),
  })
  const set = detail.data
  const items: CandidateItem[] = set?.items ?? []
  const categories = runner.data?.reject_categories ?? []

  // A generation that has just finished must also leave the generating state in the list (it gates the Generate button).
  const status = set?.status
  useEffect(() => {
    if (status && status !== 'generating') void queryClient.invalidateQueries({ queryKey: ['ai-sets', ontologyId] })
  }, [status, ontologyId, queryClient])

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['ai-sets', ontologyId] })
    void queryClient.invalidateQueries({ queryKey: ['ai-set', ontologyId] })
  }
  const choose = (id: string) => { setPickedId(id); setValidation(null) }
  const created = (row: CandidateSet) => { choose(row.id); refresh() }
  const review = async (item: CandidateItem, body: ReviewBody) => {
    await apiClientV2.post(`${base}/${set!.id}/items/${item.id}:review`, body)
    setValidation(null)
    refresh()
  }

  const validate = useMutation({
    mutationFn: () => apiClientV2.post<Validation>(`${base}/${set!.id}:validate`, {}),
    onSuccess: setValidation,
    onError: () => setValidation(null),
  })
  const publish = useMutation({
    mutationFn: () => apiClientV2.post<PublishResult>(`${base}/${set!.id}:publish`, {}),
    onSuccess: () => { setValidation(null); refresh(); void queryClient.invalidateQueries({ queryKey: ['ontology', ontologyId] }) },
  })
  const cancel = useMutation({ mutationFn: () => apiClientV2.post<CandidateSet>(`${base}/${set!.id}:cancel`, {}), onSuccess: refresh })

  const generating = (sets.data?.sets ?? []).some(row => row.status === 'generating')
  const stats = set?.stats
  const waiting = items.filter(item => item.decision === 'pending' && !item.exists_in_ontology).length
  const chosen = stats ? stats.accepted + stats.modified : 0
  const canPublish = set?.status === 'review' && waiting === 0 && chosen > 0
  const entities = items.filter(item => item.kind === 'entity_type')
  const relationships = items.filter(item => item.kind === 'relationship')
  const revision = publish.data?.revision

  return <div className="mx-auto max-w-6xl space-y-5 p-6 pb-28">
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p className="wb-eyebrow">AI 本体候选</p>
        <h1 className="wb-page-title mt-1">AI 本体候选{ontology.data ? ` · ${ontology.data.name}` : ''}</h1>
        <p className="wb-page-subtitle">用 Auto Ontology 从关系库生成候选 · 逐条采纳、修改或驳回 · 发布成一个新修订</p>
      </div>
      <Link className="wb-button-secondary" to={`/ontologies/${ontologyId}`}>返回本体</Link>
    </header>

    <GeneratePanel ontologyId={ontologyId} runner={runner.data} loading={runner.isLoading} busy={generating} onCreated={created} />

    {(sets.data?.sets.length ?? 0) > 0 && <nav aria-label="候选集" className="flex flex-wrap gap-2">
      {sets.data!.sets.map(row => <button key={row.id} type="button" data-testid={`ai-set-${row.id}`} aria-pressed={row.id === activeId} onClick={() => choose(row.id)}
        className={`rounded-lg border px-3 py-1.5 text-left text-sm ${row.id === activeId ? 'border-slate-900 bg-slate-900 text-white' : 'bg-white hover:bg-slate-50'}`}>
        <span className="font-medium">{row.title}</span>
        <span className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${row.id === activeId ? 'bg-white/20 text-white' : statusStyle[row.status]}`}>{statusLabel[row.status]}</span>
        {row.stats && row.stats.total > 0 && <span className={`ml-2 text-xs ${row.id === activeId ? 'text-slate-300' : 'text-slate-500'}`}>{row.stats.decided}/{row.stats.total}</span>}
      </button>)}
    </nav>}

    {detail.isError && <p role="alert" className="text-sm text-red-700">{formatApiError(detail.error)}</p>}

    {set && <>
      <section className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" data-testid="ai-set-header">
        <h2 className="text-lg font-semibold">{set.title}</h2>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${statusStyle[set.status]}`}>{statusLabel[set.status]}</span>
        <span className="text-xs text-slate-500">
          {set.generator.tool} {set.generator.version ?? ''}
          {set.generator.model_label && <> · {set.generator.model_label}（{set.generator.locality === 'cloud' ? '云端' : '本机'}）</>}
          {set.generator.seconds ? <> · {Math.round(set.generator.seconds)} 秒</> : null}
          {set.generator.llm_calls ? <> · {set.generator.llm_calls} 次模型调用</> : null}
          {set.generator.table_count ? <> · {set.generator.table_count} 张表</> : null}
          {set.generator.sample ? ' · 内置样例' : ''}
        </span>
      </section>

      {(set.status === 'generating' || set.status === 'failed' || set.status === 'cancelled' || (set.origin === 'runner' && set.progress.stages)) &&
        <ProgressPanel set={set} onCancel={() => cancel.mutate()} cancelling={cancel.isPending} />}
      {cancel.error && <p role="alert" className="text-sm text-red-700">{formatApiError(cancel.error)}</p>}

      {stats && items.length > 0 && <StatsPanel stats={stats} categories={categories} />}
      {set.warnings.length > 0 && <section className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm" data-testid="ai-warnings" aria-label="未能转换的内容">
        <p className="font-medium text-amber-900">有 {set.warnings.length} 处内容没能转成候选（不会被发布）：</p>
        <ul className="mt-1 list-disc pl-5 text-amber-900">{set.warnings.map(row => <li key={`${row.code}-${row.table}`}>{row.message}</li>)}</ul>
      </section>}

      {set.status === 'published' && <p role="status" className="rounded-xl border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900" data-testid="ai-published">
        已发布为新修订{revision ? ` #${revision.revision_no}` : ''}（{chosen} 项）。<Link className="ml-2 underline" to={`/ontologies/${ontologyId}?tab=graph`}>在本体图里查看</Link></p>}

      {items.length > 0 && <>
        <section aria-label="对象类型候选"><h3 className="mb-2 text-sm font-semibold">对象类型（{entities.length}）</h3>
          <ul className="space-y-2">{entities.map(item => <CandidateItemCard key={item.id} item={item} editable={set.status === 'review'} categories={categories} onReview={review} />)}</ul></section>
        <section aria-label="关系候选"><h3 className="mb-2 text-sm font-semibold">关系（{relationships.length}）</h3>
          <ul className="space-y-2">{relationships.map(item => <CandidateItemCard key={item.id} item={item} editable={set.status === 'review'} categories={categories} onReview={review} />)}</ul></section>
      </>}

      {set.status === 'review' && items.length > 0 && <div className="sticky bottom-0 z-10 rounded-xl border bg-white/95 p-3 shadow-lg backdrop-blur" data-testid="ai-publish-bar">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm" role="status">{waiting > 0 ? `还有 ${waiting} 条没有处理，处理完才能发布。` : chosen > 0 ? `将发布 ${chosen} 项为一个新修订；驳回的 ${stats?.rejected ?? 0} 项不会写入。` : '还没有采纳任何候选。'}</p>
          <div className="flex gap-2">
            <button type="button" className="wb-button-secondary" disabled={!canPublish || validate.isPending} onClick={() => validate.mutate()}>整体校验</button>
            <button type="button" className="wb-button-primary" disabled={!canPublish || publish.isPending} onClick={() => publish.mutate()}>发布 {chosen} 项为新修订</button>
          </div>
        </div>
        {validation && <p role="status" className="mt-2 text-sm text-emerald-800" data-testid="ai-validation">{validation.message}</p>}
        {(validate.error || publish.error) && <p role="alert" className="mt-2 text-sm text-red-700" data-testid="ai-publish-error">{formatApiError(validate.error ?? publish.error)}</p>}
      </div>}
    </>}
  </div>
}
