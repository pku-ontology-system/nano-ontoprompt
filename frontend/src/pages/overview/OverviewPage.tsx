import { Fragment, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Activity, ArrowRight, Boxes, Database, GitBranch, HeartPulse, Layers, Merge, Network, Workflow } from 'lucide-react'
import { analysisApi, type OverviewOntology } from '@/api/analysis'
import { ontologyApi } from '@/api/ontologies'
import Chart from './Chart'

const NEON = ['#22d3ee', '#a78bfa', '#fbbf24', '#34d399', '#f472b6', '#60a5fa', '#fb923c']
const KIND_ICON: Record<string, typeof Activity> = { health: HeartPulse, resolution: Merge, pipeline: Workflow }
const KIND_LABEL: Record<string, string> = { health: '数据健康', resolution: '实体消歧', pipeline: '流水线' }

function useCountUp(target: number, ms = 1500) {
  const [value, setValue] = useState(0)
  useEffect(() => {
    let frame = 0
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms)
      setValue(Math.round(target * (1 - Math.pow(1 - t, 3))))
      if (t < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [target, ms])
  return value
}

function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(id) }, [])
  return now
}

function Kpi({ label, value, icon: Icon, accent }: { label: string; value: number; icon: typeof Activity; accent: string }) {
  const shown = useCountUp(value)
  return <div className="bs-panel p-3" data-testid={`kpi-${label}`}>
    <div className="flex items-center gap-2 text-[11px] tracking-[.18em] text-cyan-200/70"><Icon size={13} style={{ color: accent }} />{label}</div>
    <p className="bs-glow mt-1 text-[28px] font-semibold leading-none tabular-nums" style={{ color: accent }}>{shown.toLocaleString('zh-CN')}</p>
  </div>
}

function Panel({ title, children, className = '' }: { title: string; children: React.ReactNode; className?: string }) {
  return <section className={`bs-panel flex min-h-0 flex-col p-4 ${className}`}>
    <h2 className="mb-2 flex items-center gap-2 text-xs font-medium tracking-[.2em] text-cyan-200/80"><span className="bs-dot" />{title}</h2>
    <div className="min-h-0 flex-1">{children}</div>
  </section>
}

function ago(iso: string | null) {
  if (!iso) return ''
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 90) return '刚刚'
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟前`
  if (seconds < 129600) return `${Math.round(seconds / 3600)} 小时前`
  return `${Math.round(seconds / 86400)} 天前`
}

function runLine(run: { kind: string; title: string; summary: Record<string, unknown> }) {
  const s = run.summary
  if (run.kind === 'health') return `${s.successful_expectations ?? '?'}/${s.evaluated_expectations ?? '?'} 条期望通过`
  if (run.kind === 'resolution') return `${s.records ?? '?'} → ${s.entities_found ?? '?'} 个实体`
  return '健康检查 + 实体消歧 + 血缘'
}

export default function OverviewPage() {
  const navigate = useNavigate()
  const now = useClock()
  const overview = useQuery({ queryKey: ['platform-overview'], queryFn: analysisApi.overview, refetchInterval: 30000 })
  const data = overview.data
  const featured = useMemo<OverviewOntology[]>(() => (data?.ontologies ?? []).filter(o => o.types > 0 && o.objects > 0).sort((a, b) => b.objects - a.objects).slice(0, 5), [data])
  const [turn, setTurn] = useState(0)
  const [pinned, setPinned] = useState<string | null>(null)
  useEffect(() => { const id = setInterval(() => setTurn(t => t + 1), 9000); return () => clearInterval(id) }, [])
  const current = featured.find(o => o.id === pinned) ?? featured[turn % Math.max(featured.length, 1)] ?? null
  const graph = useQuery({ queryKey: ['overview-graph', current?.id], queryFn: () => ontologyApi.getGraph(current!.id), enabled: !!current })

  const graphOption = useMemo(() => {
    const nodes = graph.data?.nodes ?? []
    const edges = graph.data?.edges ?? []
    const degree = new Map<string, number>()
    edges.forEach(e => { degree.set(e.data.source, (degree.get(e.data.source) ?? 0) + 1); degree.set(e.data.target, (degree.get(e.data.target) ?? 0) + 1) })
    return {
      animationDuration: 1200, tooltip: { backgroundColor: 'rgba(4,10,28,.92)', borderColor: '#22d3ee', textStyle: { color: '#cfeaff' } },
      series: [{ type: 'graph', layout: 'force', roam: true, draggable: true, force: { repulsion: 640, edgeLength: [120, 230], gravity: 0.06 }, emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
        label: { show: true, position: 'right', color: '#e6f6ff', fontSize: 13 }, lineStyle: { color: 'rgba(34,211,238,.5)', width: 1.6, curveness: 0.18 }, edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 6,
        data: nodes.map((n, i) => ({ id: n.data.id, name: n.data.label, symbolSize: 30 + Math.min(34, (degree.get(n.data.id) ?? 0) * 4.5), itemStyle: { color: NEON[i % NEON.length], shadowBlur: 16, shadowColor: NEON[i % NEON.length] } })),
        links: edges.map(e => ({ source: e.data.source, target: e.data.target, value: e.data.label ?? '' })) }],
    }
  }, [graph.data])

  const barOption = useMemo(() => {
    const top = [...(data?.ontologies ?? [])].filter(o => o.objects > 0).sort((a, b) => a.objects - b.objects).slice(-7)
    return {
      grid: { left: 6, right: 40, top: 4, bottom: 4, containLabel: true }, tooltip: { trigger: 'axis', axisPointer: { type: 'none' }, backgroundColor: 'rgba(4,10,28,.92)', borderColor: '#22d3ee', textStyle: { color: '#cfeaff' } },
      xAxis: { type: 'value', show: false }, yAxis: { type: 'category', data: top.map(o => (o.name.length > 12 ? `${o.name.slice(0, 11)}…` : o.name)), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#9bc6e8', fontSize: 11 } },
      series: [{ type: 'bar', data: top.map((o, i) => ({ value: o.objects, itemStyle: { color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: 'rgba(34,211,238,.15)' }, { offset: 1, color: NEON[i % NEON.length] }] }, borderRadius: [0, 6, 6, 0] } })),
        barWidth: 12, label: { show: true, position: 'right', color: '#cfeaff', fontSize: 11 }, animationDuration: 1400 }],
    }
  }, [data])

  const gaugeOption = useMemo(() => ({
    series: [{ type: 'gauge', startAngle: 215, endAngle: -35, min: 0, max: 100, radius: '92%', progress: { show: true, width: 12, itemStyle: { color: '#34d399', shadowBlur: 14, shadowColor: '#34d399' } },
      axisLine: { lineStyle: { width: 12, color: [[1, 'rgba(148,163,184,.18)']] } }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, pointer: { show: false },
      detail: { valueAnimation: true, formatter: '{value}%', color: '#a7f3d0', fontSize: 30, offsetCenter: [0, '8%'] }, title: { color: '#9bc6e8', fontSize: 12, offsetCenter: [0, '58%'] },
      data: [{ value: data?.health.average ?? 0, name: data?.health.average == null ? '还没有检查记录' : `最近 ${data.health.runs} 次检查的平均通过率` }] }],
  }), [data])

  const totals = data?.totals
  const stages = [
    { label: '数据源', value: totals?.imports ?? 0, icon: Database }, { label: '对象类型', value: totals?.types ?? 0, icon: Layers }, { label: '对象', value: totals?.objects ?? 0, icon: Boxes },
    { label: '关系类型', value: totals?.link_types ?? 0, icon: Network }, { label: '血缘作业', value: totals?.lineage_jobs ?? 0, icon: GitBranch }, { label: '分析运行', value: totals?.runs ?? 0, icon: Activity },
  ]

  return <div className="bs-root bs-grid fixed inset-0 z-[100] flex flex-col overflow-auto" data-testid="home-screen">
    <div className="bs-scan pointer-events-none absolute inset-x-0 top-0 h-24" aria-hidden="true" />
    <header className="relative z-10 flex items-center justify-between gap-6 px-6 pt-5">
      <div className="flex items-center gap-4">
        <img src="/pku-seal.png" alt="" className="h-11 w-11 rounded-full ring-2 ring-cyan-300/40" />
        <div>
          <h1 className="bs-glow bg-gradient-to-r from-cyan-200 via-sky-100 to-violet-300 bg-clip-text text-[30px] font-semibold leading-none tracking-[.12em] text-transparent">本体图谱平台</h1>
          <p className="mt-1.5 text-[11px] tracking-[.32em] text-cyan-200/60">ONTOLOGY GRAPH PLATFORM · 数据 → 本体 → 分析 → 决策</p>
        </div>
      </div>
      <div className="flex items-center gap-5">
        <div className="text-right"><p className="bs-glow text-2xl font-light tabular-nums text-cyan-200">{now.toLocaleTimeString('zh-CN', { hour12: false })}</p>
          <p className="text-[11px] tracking-[.2em] text-cyan-200/60">{now.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })}</p></div>
        <nav aria-label="快速入口" className="flex gap-2">
          {[['数据源', '/sources'], ['本体建模', '/ontologies'], ['数据分析', '/analysis/pipeline']].map(([label, path]) => <button key={path} type="button" onClick={() => navigate(path)}
            className="rounded-lg border border-cyan-300/30 bg-cyan-400/10 px-3.5 py-2 text-sm text-cyan-100 transition hover:bg-cyan-400/25">{label}</button>)}
          <button type="button" onClick={() => navigate(current ? `/ontologies/${current.id}?tab=overview` : '/ontologies')} className="flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-cyan-400 to-violet-400 px-4 py-2 text-sm font-medium text-slate-900 shadow-[0_0_18px_rgba(34,211,238,.45)] transition hover:brightness-110">进入工作台<ArrowRight size={15} /></button>
        </nav>
      </div>
    </header>

    <main className="relative z-10 grid min-h-0 flex-1 grid-cols-[300px_minmax(520px,1fr)_340px] grid-rows-[1fr_auto] gap-4 p-6">
      <div className="flex min-h-0 flex-col gap-4">
        <div className="grid grid-cols-2 gap-3">
          <Kpi label="本体" value={totals?.ontologies ?? 0} icon={Network} accent="#22d3ee" /><Kpi label="对象类型" value={totals?.types ?? 0} icon={Layers} accent="#a78bfa" />
          <Kpi label="对象" value={totals?.objects ?? 0} icon={Boxes} accent="#fbbf24" /><Kpi label="关系类型" value={totals?.link_types ?? 0} icon={GitBranch} accent="#34d399" />
        </div>
        <Panel title="各本体的对象数" className="flex-1"><Chart option={barOption} className="h-full min-h-[220px] w-full" label="各本体的对象数" /></Panel>
      </div>

      <Panel title={current ? `本体图谱 · ${current.name}` : '本体图谱'} className="min-h-[420px]">
        <div className="flex h-full flex-col">
          <div className="mb-2 flex flex-wrap items-center gap-2" role="tablist" aria-label="切换本体">
            {featured.map(o => <button key={o.id} type="button" role="tab" aria-selected={o.id === current?.id} onClick={() => setPinned(o.id)}
              className={`rounded-full border px-3 py-1 text-xs transition ${o.id === current?.id ? 'border-cyan-300 bg-cyan-400/20 text-cyan-50' : 'border-cyan-300/20 text-cyan-200/70 hover:bg-cyan-400/10'}`}>{o.name.length > 14 ? `${o.name.slice(0, 13)}…` : o.name}</button>)}
            {current && <span className="ml-auto text-xs text-cyan-200/60">{current.types} 个类型 · {current.objects.toLocaleString('zh-CN')} 个对象 · {current.link_types} 种关系</span>}
          </div>
          <div className="min-h-0 flex-1">{current ? <Chart option={graphOption} className="h-full min-h-[340px] w-full" label="本体类型关系图" />
            : <p className="flex h-full items-center justify-center text-sm text-cyan-200/60">还没有带对象的本体：到“数据源”导入一份数据。</p>}</div>
        </div>
      </Panel>

      <div className="flex min-h-0 flex-col gap-4">
        <Panel title="数据健康"><Chart option={gaugeOption} className="h-[190px] w-full" label="数据健康通过率" /></Panel>
        <Panel title="最近的分析" className="flex-1">
          <ul className="space-y-2.5 overflow-auto" data-testid="recent-runs">
            {(data?.runs ?? []).slice(0, 7).map(run => { const Icon = KIND_ICON[run.kind] ?? Activity
              return <li key={run.id} className="flex items-start gap-2.5 text-xs"><Icon size={14} className="mt-0.5 shrink-0 text-cyan-300" />
                <span className="min-w-0 flex-1"><span className="block truncate text-cyan-50" title={run.title}>{run.title}</span><span className="text-cyan-200/60">{KIND_LABEL[run.kind]} · {runLine(run)}</span></span>
                <span className="shrink-0 text-cyan-200/40">{ago(run.created_at)}</span></li> })}
            {data && data.runs.length === 0 && <li className="text-xs text-cyan-200/60">还没有分析记录。</li>}
          </ul>
        </Panel>
      </div>

      <section className="bs-panel col-span-3 px-6 py-4" aria-label="数据流向">
        <ol className="flex items-center gap-3">
          {stages.map((stage, i) => <Fragment key={stage.label}>
            <li className="flex w-24 shrink-0 flex-col items-center gap-1 text-center" data-testid={`stage-${stage.label}`}>
              <span className="flex h-11 w-11 items-center justify-center rounded-full border border-cyan-300/40 bg-cyan-400/10 text-cyan-200 shadow-[0_0_14px_rgba(34,211,238,.25)]"><stage.icon size={18} /></span>
              <span className="text-sm font-medium tabular-nums text-cyan-50">{stage.value.toLocaleString('zh-CN')}</span><span className="text-[11px] tracking-[.15em] text-cyan-200/60">{stage.label}</span></li>
            {i < stages.length - 1 && <li role="presentation" className="h-3 min-w-6 flex-1"><svg className="h-3 w-full" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="2" x2="100" y2="2" stroke="#22d3ee" strokeWidth="1.6" className="bs-flow" /></svg></li>}
          </Fragment>)}
        </ol>
      </section>
    </main>
  </div>
}
