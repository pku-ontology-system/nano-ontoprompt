import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Activity, ArrowRight, Database, ExternalLink, Images, RotateCcw, X } from 'lucide-react'
import { apiClientV2, formatApiError } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'
import { MultimodalEvidenceWorkspace, type MultimodalEvidence } from '@/pages/data-management/multimodal/MultimodalEvidenceWorkspace'
import { DecisionCasePanel } from './DecisionCasePanel'

type DomainId = 'ai4i' | 'factorynet' | 'ibadas'
type Params = Record<string, number | string>
type Preset = { key: string; name: string; parameters: Params }
type Domain = { id: DomainId; title: string; summary: string; data_class: string; formula: string; caveat: string; ontology_id: string; dataset_id: string; dataset_version_id: string; sample_count: number; source_digest: string; source_url: string; license: string; selection: string; presets: Preset[] }
type Catalog = { domains: Domain[]; wood_path: string; wood_name: string }
type RunSummary = { id: string; domain: DomainId; case_key: string; case_name: string; rule_version: string; parameters: Params; metrics: Record<string, number>; source_digest: string; input_digest: string; output_digest: string; created_at: string }
type DetailRow = { object_id: string; source_row_id: string; name: string; review: boolean; score?: number; official_failure?: boolean; failure_modes?: string[]; details?: Record<string, number | boolean>; alert_points?: number; sample_points?: number; mean_residual?: number; max_residual?: number; tool_condition?: string; visual_inspection?: string | null; points?: Array<{ time_s: number; residual: number; source_row_id: string }>; scene_id?: string; official_label?: string; required_roles?: string[]; available_roles?: string[]; transfer_mb?: number; skip_reason?: string | null }
type RunDetail = RunSummary & { ontology_id: string; dataset_version_id: string; result: { formula: string; caveat: string; sample_count: number; metrics: Record<string, number>; rows: DetailRow[]; source: { url: string; license?: string; selection?: string } } }
type Evidence = { object_id: string; ontology_id: string; evidence_ref_id: string; properties: Record<string, unknown>; source_properties?: Record<string, unknown>; current_properties?: Record<string, unknown>; source_dataset_id: string; source_version: string; source_file: string; source_row_id: string; source_sample_id?: string; source_media_id?: string; evidence_text: string; content_hash: string; construction_run_id: string }

const domainIcons = { ai4i: Database, factorynet: Activity, ibadas: Images }
const metricLabels: Record<string, string> = {
  review_count: '需复核记录', review_minutes: '人工复核分钟', observed_failures_covered: '覆盖故障标签', observed_failures_total: '故障标签总数', observed_coverage_pct: '本切片覆盖率 %',
  alert_points: '告警采样点', review_episodes: '需复核批次', observed_failed_inspections_covered: '覆盖目检未通过批次', observed_failed_inspections_total: '目检未通过批次',
  reviewed_samples: '可复核样本', total_samples: '样本总数', transfer_mb: '传输量 MB', budget_mb: '预算 MB', evidence_roles: '证据模态数', observed_anomalies_covered: '覆盖异常标注', observed_anomalies_total: '异常标注总数',
}
const parameterLabels: Record<string, string> = { threshold: "阈值", review_minutes: "复核分钟", policy: "策略", budget_mb: "预算 MB" }
const policyLabels: Record<string, string> = { rgb: "RGB", rgb_depth: "RGB+深度", complete: "四模态" }
const classLabels: Record<string, string> = { regular: "常规", temporal: "时序", multimodal: "多模态" }

const comparisonKeys: Record<DomainId, string[]> = {
  ai4i: ['review_count', 'review_minutes', 'observed_failures_covered'],
  factorynet: ['alert_points', 'review_episodes', 'review_minutes', 'observed_failed_inspections_covered'],
  ibadas: ['reviewed_samples', 'transfer_mb', 'evidence_roles', 'observed_anomalies_covered'],
}

function pretty(value: number | string | undefined) {
  if (value === undefined) return '—'
  return typeof value === 'number' ? (Number.isInteger(value) ? String(value) : value.toFixed(2)) : String(value)
}

function Sparkline({ points, threshold }: { points: NonNullable<DetailRow['points']>; threshold: number }) {
  const max = Math.max(threshold * 1.15, ...points.map(point => point.residual), 0.01)
  const shape = points.map((point, index) => `${index ? 'L' : 'M'}${(index / Math.max(1, points.length - 1)) * 300},${65 - (point.residual / max) * 56}`).join(' ')
  const thresholdY = 65 - (threshold / max) * 56
  return <svg viewBox="0 0 300 72" role="img" aria-label="采样点速度偏差趋势，红色虚线是当前阈值" className="h-20 w-full overflow-visible">
    <line x1="0" x2="300" y1={thresholdY} y2={thresholdY} stroke="#b42318" strokeWidth="1" strokeDasharray="4 4" />
    <path d={shape} fill="none" stroke="#294f87" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
}

export default function DecisionLabPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const canEdit = role === 'admin' || role === 'editor'
  const activeId = (searchParams.get('demo') || 'ai4i') as DomainId
  const catalog = useQuery({ queryKey: ['decision-lab-catalog'], queryFn: () => apiClientV2.get<Catalog>('/decision-lab') })
  const active = catalog.data?.domains.find(item => item.id === activeId) ?? catalog.data?.domains[0]
  const domain = active?.id
  const [runId, setRunId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Params>({})
  const [selectedObject, setSelectedObject] = useState<string | null>(searchParams.get('object'))
  const [windowStart, setWindowStart] = useState<number>(0)
  const [windowEnd, setWindowEnd] = useState<number>(0)
  const runs = useQuery({ queryKey: ['decision-lab-runs', domain], queryFn: () => apiClientV2.get<{ runs: RunSummary[] }>(`/decision-lab/${domain}/runs`), enabled: !!domain })

  useEffect(() => {
    const items = runs.data?.runs ?? []
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!items.length) { setRunId(null); return }
    setRunId(current => current && items.some(item => item.id === current) ? current : (items.find(item => item.case_key === 'balanced') ?? items[0]).id)
  }, [domain, runs.data])

  const detail = useQuery({ queryKey: ['decision-lab-run', domain, runId], queryFn: () => apiClientV2.get<RunDetail>(`/decision-lab/${domain}/runs/${runId}`), enabled: !!domain && !!runId })
  // eslint-disable-next-line react-hooks/set-state-in-effect, react-hooks/exhaustive-deps
  useEffect(() => { if (detail.data) setDraft(detail.data.parameters) }, [detail.data?.id])
  const evidence = useQuery({ queryKey: ['decision-lab-evidence', domain, selectedObject], queryFn: () => apiClientV2.get<Evidence>(`/decision-lab/${domain}/objects/${encodeURIComponent(selectedObject!)}/evidence`), enabled: !!domain && !!selectedObject })
  const mediaEvidence = useQuery({ queryKey: ['decision-lab-media', evidence.data?.source_sample_id], queryFn: () => apiClientV2.get<MultimodalEvidence>(`/multimodal/samples/${encodeURIComponent(evidence.data!.source_sample_id!)}/evidence`), enabled: domain === 'ibadas' && !!evidence.data?.source_sample_id })

  const create = useMutation({
    mutationFn: () => apiClientV2.post<RunDetail>(`/decision-lab/${domain}/runs`, {
      client_request_id: crypto.randomUUID(), case_key: 'custom', case_name: '自定义方案', parameters: draft,
      expected_source_digest: active?.source_digest,
    }),
    onSuccess: async data => { await queryClient.invalidateQueries({ queryKey: ['decision-lab-runs', domain] }); setRunId(data.id) },
  })
  const result = detail.data?.result
  const selectedFactoryRow = domain === 'factorynet' ? result?.rows.find(item => item.object_id === selectedObject) : undefined
  useEffect(() => {
    if (selectedFactoryRow?.points?.length) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setWindowStart(selectedFactoryRow.points[0].time_s)
      setWindowEnd(selectedFactoryRow.points[selectedFactoryRow.points.length - 1].time_s)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFactoryRow?.object_id, detail.data?.id])
  const windowPoints = selectedFactoryRow?.points?.filter(point => point.time_s >= windowStart && point.time_s <= windowEnd) ?? []
  const representativePoints = windowPoints.length ? [windowPoints[0], windowPoints.reduce((best, item) => item.residual > best.residual ? item : best, windowPoints[0]), windowPoints[windowPoints.length - 1]] : []
  const windowEvidenceRefs = selectedFactoryRow && active ? [
    `${active.ontology_id}:evidence:${selectedFactoryRow.object_id}`,
    ...Array.from(new Set(representativePoints.map(point => `${active.ontology_id}:evidence:decision-lab:factorynet:observation:${point.source_row_id}`))),
  ] : undefined
  const baseCases = useMemo(() => active?.presets.map(preset => runs.data?.runs.find(run => run.case_key === preset.key)).filter((value): value is RunSummary => !!value) ?? [], [active?.presets, runs.data?.runs])

  function switchDomain(next: DomainId) {
    setSearchParams({ demo: next })
    setRunId(null); setSelectedObject(null)
  }

  return <div className="mx-auto max-w-[1480px] space-y-5" data-testid="decision-lab-page">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div><p className="wb-eyebrow">证据 · 计算 · 权衡</p><h1 className="wb-page-title mt-1">基准样例决策分析</h1><p className="wb-page-subtitle mt-2">三个小样例本体、可重复的规则计算和来源追溯。木材供应演示保留原有工作台。</p></div>
      <Link className="wb-button-secondary" to="/what-if">打开木材供应演示 <ArrowRight size={15} /></Link>
    </header>

    {catalog.isLoading && <div className="wb-surface p-6 text-sm text-slate-500">正在读取已安装的样例…</div>}
    {catalog.isError && <div role="alert" className="wb-surface p-6 text-sm text-red-700">{formatApiError(catalog.error, '样例目录加载失败')}</div>}
    {catalog.data && <div className="grid gap-3 lg:grid-cols-4">
      <Link to="/what-if" className="wb-surface group p-4 transition-colors hover:border-[var(--pku-red)]"><div className="flex items-center justify-between"><span className="rounded-lg bg-rose-50 p-2 text-[var(--pku-red)]"><RotateCcw size={18} /></span><ArrowRight size={15} className="text-slate-400 group-hover:text-[var(--pku-red)]" /></div><h2 className="mt-3 text-sm font-semibold">木材供应韧性</h2><p className="mt-1 text-xs text-slate-500">原 Baseline / B / C 与七阶段运行</p><span className="mt-3 inline-block text-[11px] font-medium text-emerald-700">原演示 · 独立保留</span></Link>
      {catalog.data.domains.map(item => { const Icon = domainIcons[item.id]; const selected = domain === item.id; return <button type="button" key={item.id} onClick={() => switchDomain(item.id)} className={`wb-surface p-4 text-left transition-colors ${selected ? 'border-[var(--pku-red)] bg-rose-50/40' : 'hover:border-[var(--pku-red)]'}`} aria-pressed={selected}><div className="flex items-center justify-between"><span className="rounded-lg bg-slate-100 p-2 text-slate-700"><Icon size={18} /></span><span className="text-[11px] text-slate-500">{item.sample_count} {item.id === "factorynet" ? "个采样点" : item.id === "ai4i" ? "条记录" : "组样例"}</span></div><h2 className="mt-3 text-sm font-semibold">{item.title}</h2><p className="mt-1 text-xs text-slate-500">{item.summary}</p><span className="mt-3 inline-block text-[11px] font-medium text-[var(--pku-red)]">{selected ? '正在查看' : '查看计算'}</span></button> })}
    </div>}

    {active && <>
      <section className="wb-surface p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="wb-eyebrow">{classLabels[active.data_class] ?? active.data_class} · 版本固定</p><h2 className="wb-section-title mt-1">{active.title}</h2><p className="mt-1 text-xs text-slate-500">{active.summary}</p></div><Link className="wb-button-secondary" to={`/ontologies/${active.ontology_id}?tab=data_model`}>查看对应本体 <ExternalLink size={14} /></Link></div><div className="mt-4 grid gap-3 md:grid-cols-3"><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] text-slate-500">来源与许可</p><a href={active.source_url} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-[var(--workbench-blue)] hover:underline">官方来源 <ExternalLink size={12} /></a><p className="mt-1 text-xs">{active.license}</p></div><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] text-slate-500">选样方式</p><p className="mt-1 text-xs leading-5">{active.selection}</p></div><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] text-slate-500">版本与完整性</p><p className="mt-1 break-all font-mono text-[10px]">Slice SHA-256 {active.source_digest}</p><p className="mt-1 text-xs">{active.sample_count} {active.id === "factorynet" ? "个采样点" : active.id === "ai4i" ? "条记录" : "组样例"} · 可回溯来源行</p></div></div></section>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-5">
          <section className="wb-surface p-5"><p className="wb-eyebrow">方案对比</p><h2 className="wb-section-title mt-1">同一切片、三组取舍</h2><div className="mt-4 overflow-x-auto"><table className="min-w-[620px] w-full text-left text-xs"><thead><tr className="border-b text-slate-500"><th className="pb-2">方案</th><th className="pb-2">参数</th>{comparisonKeys[active.id].map(key => <th key={key} className="pb-2">{metricLabels[key]}</th>)}</tr></thead><tbody>{baseCases.map(item => <tr key={item.id} className={`cursor-pointer border-b last:border-0 ${runId === item.id ? 'bg-rose-50' : 'hover:bg-slate-50'}`} onClick={() => setRunId(item.id)}><td className="py-3 pr-2 font-medium">{item.case_name}</td><td className="py-3 pr-2 text-slate-600">{Object.entries(item.parameters).map(([key, value]) => `${parameterLabels[key] ?? key} ${policyLabels[String(value)] ?? pretty(value)}`).join(' · ')}</td>{comparisonKeys[active.id].map(key => <td key={key} className="py-3 pr-2 tabular-nums">{pretty(item.metrics[key])}</td>)}</tr>)}</tbody></table></div>{runs.isLoading && <p className="mt-3 text-xs text-slate-500">正在读取已保存方案…</p>}{runs.isError && <p role="alert" className="mt-3 text-xs text-red-700">{formatApiError(runs.error)}</p>}{!runs.isLoading && !baseCases.length && <p className="mt-3 text-xs text-slate-500">尚无预置运行，请重新安装样例。</p>}</section>

          {detail.isLoading && <section className="wb-surface p-6 text-sm text-slate-500">正在读取计算结果…</section>}
          {detail.isError && <section role="alert" className="wb-surface p-6 text-sm text-red-700">{formatApiError(detail.error)}</section>}
          {result && <section className="wb-surface p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="wb-eyebrow">运行结果 · {detail.data?.rule_version}</p><h2 className="wb-section-title mt-1">{detail.data?.case_name}</h2></div><span className="rounded-md bg-emerald-50 px-2 py-1 text-[11px] font-medium text-emerald-700">已保存 · 可回看</span></div><div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{comparisonKeys[active.id].map(key => <div key={key} className="rounded-lg border border-slate-200 p-3"><p className="text-[11px] text-slate-500">{metricLabels[key]}</p><p className="mt-1 text-xl font-semibold tabular-nums">{pretty(result.metrics[key])}</p></div>)}</div><div className="mt-4 rounded-lg border border-blue-100 bg-blue-50/50 p-3"><p className="text-[11px] font-semibold text-slate-700">计算方法</p><p className="mt-1 text-xs leading-5 text-slate-700">{result.formula}</p></div><p className="mt-3 text-xs leading-5 text-amber-800">{result.caveat}</p><div className="mt-4 flex flex-wrap gap-3 border-t pt-3 text-[10px] text-slate-500"><span>输入摘要 {detail.data?.input_digest.slice(0, 12)}</span><span>输出摘要 {detail.data?.output_digest.slice(0, 12)}</span><span>Run ID {detail.data?.id}</span></div></section>}

          {result && <section className="wb-surface p-5"><p className="wb-eyebrow">证据下钻</p><h2 className="wb-section-title mt-1">逐对象计算依据</h2><p className="mt-1 text-xs text-slate-500">点击样例查看来源行、原始属性和媒体证据。</p>{active.id === 'ai4i' && <div className="mt-4 max-h-[450px] overflow-auto"><table className="w-full min-w-[620px] text-left text-xs"><thead className="sticky top-0 bg-white"><tr className="border-b text-slate-500"><th className="py-2">产品/来源行</th><th>规则分数</th><th>磨损</th><th>温差 K</th><th>官方标签</th><th>本方案</th></tr></thead><tbody>{result.rows.map(row => <tr key={row.object_id} onClick={() => setSelectedObject(row.object_id)} className="cursor-pointer border-b hover:bg-slate-50"><td className="py-2 font-medium">{row.name}<span className="ml-2 text-slate-400">#{row.source_row_id}</span></td><td><span className="inline-block h-1.5 rounded bg-[var(--pku-red)] align-middle" style={{ width: `${Math.max(3, row.score ?? 0)}px` }} /> <span className="tabular-nums">{row.score}</span></td><td>{row.details?.wear_min}</td><td>{row.details?.temp_gap_k}</td><td>{row.official_failure ? '故障' : '无故障'}</td><td className={row.review ? 'font-medium text-[var(--pku-red)]' : 'text-slate-500'}>{row.review ? '建议复核' : '本方案跳过'}</td></tr>)}</tbody></table></div>}
            {active.id === 'factorynet' && <div className="mt-4 grid gap-3 md:grid-cols-2">{result.rows.map(row => <button type="button" key={row.object_id} onClick={() => setSelectedObject(row.object_id)} className="rounded-lg border border-slate-200 p-3 text-left hover:border-[var(--pku-red)]"><div className="flex items-center justify-between gap-2"><strong className="text-sm">{row.name}</strong><span className={`text-[11px] ${row.review ? 'text-[var(--pku-red)]' : 'text-slate-500'}`}>{row.review ? '建议复核' : '本方案跳过'}</span></div><Sparkline points={row.points ?? []} threshold={Number(detail.data?.parameters.threshold ?? 0.5)} /><div className="flex flex-wrap gap-3 text-[11px] text-slate-600"><span>越界点 {row.alert_points}/{row.sample_points}</span><span>均值 {row.mean_residual}</span><span>最大 {row.max_residual}</span><span>原目检 {row.visual_inspection ?? '未标'}</span></div></button>)}</div>}
            {active.id === 'ibadas' && <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{result.rows.map(row => <button type="button" key={row.object_id} onClick={() => setSelectedObject(row.object_id)} className="rounded-lg border border-slate-200 p-3 text-left hover:border-[var(--pku-red)]"><div className="flex items-center justify-between gap-2"><strong className="text-xs">场景 {row.scene_id}</strong><span className={`text-[11px] ${row.review ? 'text-emerald-700' : 'text-amber-700'}`}>{row.review ? '预算内复核' : row.skip_reason ?? '跳过'}</span></div><p className="mt-1 truncate font-mono text-[10px] text-slate-500">{row.source_row_id}</p><div className="mt-2 flex gap-3 text-[11px]"><span>{row.transfer_mb} MB</span><span>官方标注 {row.official_label}</span></div></button>)}</div>}
          </section>}
        </div>

        <aside className="min-w-0 space-y-5"><section className="wb-surface p-5"><p className="wb-eyebrow">重新计算</p><h2 className="wb-section-title mt-1">调整假设</h2><p className="mt-1 text-xs text-slate-500">结果保存为新的只读 Run；不会修改原始数据或执行设备动作。</p><div className="mt-4 space-y-3">{active.id === 'ai4i' && <><NumberField label="复核阈值（0–100）" value={Number(draft.threshold ?? 40)} min={0} max={100} step={1} onChange={value => setDraft({ ...draft, threshold: value })} /><NumberField label="每条复核分钟" value={Number(draft.review_minutes ?? 8)} min={1} max={120} step={1} onChange={value => setDraft({ ...draft, review_minutes: value })} /></>}{active.id === 'factorynet' && <><NumberField label="速度偏差阈值" value={Number(draft.threshold ?? 0.5)} min={0.01} max={20} step={0.05} onChange={value => setDraft({ ...draft, threshold: value })} /><NumberField label="每批次复核分钟" value={Number(draft.review_minutes ?? 30)} min={1} max={240} step={1} onChange={value => setDraft({ ...draft, review_minutes: value })} /></>}{active.id === 'ibadas' && <><label className="block text-xs font-medium">证据策略<select className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" value={String(draft.policy ?? 'rgb_depth')} onChange={event => setDraft({ ...draft, policy: event.target.value })}><option value="rgb">RGB 快审</option><option value="rgb_depth">RGB + 深度</option><option value="complete">四模态复核</option></select></label><NumberField label="传输预算 MB" value={Number(draft.budget_mb ?? 12)} min={1} max={200} step={1} onChange={value => setDraft({ ...draft, budget_mb: value })} /></>}</div><button type="button" disabled={!canEdit || create.isPending || !Object.keys(draft).length} className="wb-button-primary mt-4 w-full" onClick={() => create.mutate()}>{create.isPending ? '正在计算…' : '计算并保存方案'}</button>{!canEdit && <p className="mt-2 text-xs text-slate-500">当前账号只读。</p>}{create.isError && <p role="alert" className="mt-2 text-xs text-red-700">{formatApiError(create.error)}</p>}{create.isSuccess && <p role="status" className="mt-2 text-xs text-emerald-700">已保存计算记录。</p>}</section><section className="wb-surface p-5"><p className="wb-eyebrow">最近运行</p><div className="mt-3 space-y-1">{runs.data?.runs.slice(0, 10).map(item => <button key={item.id} onClick={() => setRunId(item.id)} type="button" className={`flex w-full items-center justify-between rounded-lg px-2 py-2 text-left text-xs ${runId === item.id ? 'bg-rose-50 text-[var(--pku-red)]' : 'hover:bg-slate-50'}`}><span>{item.case_name}</span><span className="text-[10px] text-slate-500">{new Date(item.created_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span></button>)}</div></section></aside>
      </div>
    </>}

        {selectedObject && <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/40" onMouseDown={event => { if (event.target === event.currentTarget) setSelectedObject(null) }}><div className="h-full w-full max-w-[720px] overflow-y-auto bg-white p-5 shadow-2xl" role="dialog" aria-modal="true" aria-label="样例来源证据"><div className="flex items-start justify-between gap-3"><div><p className="wb-eyebrow">来源证据</p><h2 className="mt-1 break-all text-lg font-semibold">{selectedObject}</h2></div><button type="button" className="rounded p-2 hover:bg-slate-100" onClick={() => setSelectedObject(null)} aria-label="关闭证据"><X size={18} /></button></div>{evidence.isLoading && <p className="mt-5 text-sm text-slate-500">正在加载来源…</p>}{evidence.isError && <p role="alert" className="mt-5 text-sm text-red-700">{formatApiError(evidence.error)}</p>}{evidence.data && <><div className="mt-5 grid gap-2 rounded-lg border bg-slate-50 p-4 text-xs"><p><strong>来源行：</strong>{evidence.data.source_row_id}</p><p><strong>来源版本：</strong><span className="break-all font-mono">{evidence.data.source_version}</span></p><p><strong>证据摘要：</strong><span className="break-all font-mono">{evidence.data.content_hash}</span></p><p><strong>构筑运行：</strong>{evidence.data.construction_run_id}</p><a className="inline-flex items-center gap-1 text-[var(--workbench-blue)] hover:underline" href={evidence.data.source_file} target="_blank" rel="noreferrer">打开原始基准来源 <ExternalLink size={12} /></a></div><h3 className="mt-5 text-sm font-semibold">来源属性（安装时快照）</h3><div className="mt-2 max-h-52 overflow-auto rounded-lg border"><table className="w-full text-left text-xs"><tbody>{Object.entries(evidence.data.source_properties ?? evidence.data.properties).map(([key, value]) => <tr key={key} className="border-b last:border-0"><th className="w-1/3 p-2 font-medium text-slate-500">{key}</th><td className="break-all p-2">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</td></tr>)}</tbody></table></div>{domain === 'ibadas' && <div className="mt-5"><h3 className="text-sm font-semibold">同步媒体证据</h3>{mediaEvidence.isLoading && <p className="mt-2 text-xs text-slate-500">正在读取 RGB、深度、掩码和点云…</p>}{mediaEvidence.isError && <p role="alert" className="mt-2 text-xs text-red-700">{formatApiError(mediaEvidence.error)}</p>}{mediaEvidence.data && <MultimodalEvidenceWorkspace evidence={mediaEvidence.data} selectedRoles={['rgb', 'depth', 'mask', 'point_cloud']} />}</div>}{domain === 'factorynet' && selectedFactoryRow && <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-3"><h3 className="text-xs font-semibold">维护复核时间窗口</h3><p className="mt-1 text-[11px] text-amber-800">本演示每批次仅等距取 64 点，窗口表示固定切片内的证据范围，不代表连续故障检测。</p><div className="mt-2 grid grid-cols-2 gap-2"><label className="text-xs">起点 time_s<input className="mt-1 w-full rounded border p-2" type="number" value={windowStart} onChange={event => setWindowStart(Number(event.target.value))} /></label><label className="text-xs">终点 time_s<input className="mt-1 w-full rounded border p-2" type="number" value={windowEnd} onChange={event => setWindowEnd(Number(event.target.value))} /></label></div><p className="mt-2 text-[11px] text-slate-600">窗口内 {windowPoints.length} 个采样点；引用起点、最大偏差与终点的原始来源行。</p><div className="mt-2 max-h-24 overflow-auto text-[10px] text-slate-500">{representativePoints.map(point => <p key={point.source_row_id}>time_s {point.time_s} · 来源行 {point.source_row_id} · 偏差 {point.residual}</p>)}</div></div>}{domain && (domain !== 'factorynet' || windowPoints.length > 0) && <DecisionCasePanel key={selectedObject} domain={domain} ontologyId={evidence.data.ontology_id} objectId={selectedObject} evidenceRefId={evidence.data.evidence_ref_id} evidenceRefIds={domain === 'factorynet' ? windowEvidenceRefs : undefined} mediaAssets={mediaEvidence.data?.assets} context={domain === 'factorynet' && selectedFactoryRow ? { source: 'decision_lab', analysis_run_id: detail.data?.id, rule_version: detail.data?.rule_version, episode_id: selectedFactoryRow.name, window_start: windowStart, window_end: windowEnd, source_version: active?.source_digest } : undefined} />}</>}</div></div>}
  </div>
}

function NumberField({ label, value, min, max, step, onChange }: { label: string; value: number; min: number; max: number; step: number; onChange: (value: number) => void }) {
  return <label className="block text-xs font-medium">{label}<input className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-[var(--pku-red)] focus:outline-none focus:ring-2 focus:ring-rose-100" type="number" value={Number.isFinite(value) ? value : ''} min={min} max={max} step={step} onChange={event => onChange(Number(event.target.value))} /></label>
}
