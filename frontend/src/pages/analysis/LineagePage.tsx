import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { analysisApi } from '@/api/analysis'
import { LayerLegend, LineageGraph, NodeDetail } from './LineageGraph'
import { OntologyPicker, SourceTabs } from './OntologySource'
import { DatasetCards, ErrorNote, PageHeader, Section, Stat } from './shared'
import { LAYER_STYLE } from './format'
import { useOntologyChoice, useSourceMode } from './useSource'

export default function LineagePage() {
  const queryClient = useQueryClient()
  const { mode, setMode } = useSourceMode()
  const ontologyMode = mode === 'ontology'
  const choice = useOntologyChoice(ontologyMode, { withTypes: false })
  const oid = choice.ontologyId
  const [namespace, setNamespace] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [focusMode, setFocusMode] = useState(false)
  const [depth, setDepth] = useState(2)
  const [direction, setDirection] = useState<'both' | 'upstream' | 'downstream'>('both')
  const [ask, setAsk] = useState<{ node: string; field: string | null } | null>(null) // the impact that was asked for; another selection hides it

  const demos = useQuery({ queryKey: ['analysis', 'demos'], queryFn: analysisApi.demos })
  const dataset = demos.data?.find(d => d.tool === 'marquez')
  const focusing = focusMode && !!selected
  const scopeKey = ontologyMode ? `o:${oid}` : `n:${namespace}`
  const graph = useQuery({
    queryKey: ['analysis', 'lineage', scopeKey, focusing ? selected : null, depth, direction],
    queryFn: () => ontologyMode
      ? analysisApi.ontologyLineageGraph(oid, focusing ? { node: selected!, depth, direction } : {})
      : analysisApi.lineageGraph({ namespace: namespace || undefined, ...(focusing ? { node: selected!, depth, direction } : {}) }),
    enabled: !ontologyMode || !!oid,
  })

  const node = useMemo(() => graph.data?.nodes.find(n => n.id === selected) ?? null, [graph.data, selected])
  const asking = ask && ask.node === selected ? ask : null
  const impact = useQuery({
    queryKey: ['analysis', 'impact', scopeKey, selected, asking?.field ?? null],
    queryFn: () => ontologyMode ? analysisApi.ontologyImpact(oid, selected!, asking?.field ?? undefined) : analysisApi.lineageImpact(selected!, namespace || undefined),
    enabled: !!selected && !!asking,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['analysis', 'lineage'] })
  const seed = useMutation({ mutationFn: analysisApi.seedLineage, onSuccess: async data => { setNamespace(data.namespace); setSelected(null); await refresh() } })
  const reset = useMutation({ mutationFn: () => analysisApi.resetLineage({ source: 'demo:marquez' }), onSuccess: async () => { setSelected(null); setNamespace(''); await refresh() } })
  const generate = useMutation({ mutationFn: () => analysisApi.generateOntologyLineage(oid), onSuccess: async () => { setSelected(null); setAsk(null); await refresh() } })

  const jobs = graph.data?.nodes.filter(n => n.kind === 'job').length ?? 0
  const sets = graph.data?.nodes.filter(n => n.kind === 'dataset').length ?? 0
  const empty = graph.isSuccess && (graph.data?.nodes.length ?? 0) === 0
  const fieldImpact = ontologyMode ? { field: asking?.field ?? null, result: impact.data?.column, onAsk: (field: string) => setAsk({ node: selected!, field }) } : undefined

  return <div className="wb-page mx-auto max-w-[1400px] p-6 pb-16">
    <PageHeader title="数据血缘" subtitle="数据从哪里来、经过哪些作业、流向哪里。接收 OpenLineage 运行事件，画出作业和数据集的图，点一个节点看它的模式、SQL、运行记录和数据质量，再问一句“它改了会影响谁”。"
      credit={<>做法照 <a className="underline" href="https://github.com/OpenLineage/OpenLineage" target="_blank" rel="noreferrer">OpenLineage</a> 和 <a className="underline" href="https://github.com/MarquezProject/marquez" target="_blank" rel="noreferrer">Marquez</a>（Apache-2.0）。</>} />

    <Section title={ontologyMode ? '本体的血缘' : '演示数据'} hint={ontologyMode
      ? '从本体已有的记录画出：来源表 → Auto Ontology 生成 → 对象类型 → 规则 → 告警 → 处置，以及在对象上跑过的数据健康和实体消歧。只画平台记着的事：手工建的类型没有上游，图里就没有。'
      : '默认用 Marquez 自带的演示事件：一家虚构外卖公司的 13 个作业、13 个数据集。载入会替换上一次载入的这份演示，不会重复。'}>
      <SourceTabs mode={mode} onChange={next => { setMode(next); setSelected(null); setAsk(null) }} />
      {ontologyMode ? <>
        <OntologyPicker choice={choice} onlyOntology />
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button type="button" className="wb-button-primary" disabled={generate.isPending || !oid} onClick={() => generate.mutate()}>{generate.isPending ? '生成中…' : '从本体生成血缘'}</button>
          <span className="text-xs text-slate-500">会替换这个本体上一次生成的血缘；在对象上跑过新的检查或消歧后，再生成一次就能看到。</span>
        </div>
        {generate.data && <p role="status" className="mt-2 text-xs text-slate-600" data-testid="lineage-generated">已写入 {generate.data.events} 条事件，共 {Object.values(generate.data.jobs).reduce((a, b) => a + b, 0)} 个作业：{Object.entries(generate.data.jobs).map(([k, v]) => `${LAYER_STYLE[k]?.label ?? k} ${v}`).join('，')}。</p>}
      </> : <>
        {dataset && <DatasetCards datasets={[dataset]} value={dataset.id} onChange={() => undefined} badge={() => '默认演示'} />}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button type="button" className="wb-button-primary" disabled={seed.isPending || !dataset?.available} onClick={() => seed.mutate()}>{seed.isPending ? '载入中…' : '载入 Marquez 外卖演示'}</button>
          <button type="button" className="wb-button-secondary" disabled={reset.isPending} onClick={() => reset.mutate()}>清除这份演示</button>
          <span className="text-xs text-slate-500">事件时间取“12 分钟前开始”，让运行时间落在最近，做法同 Marquez 自己的 seed.sh。</span>
        </div>
      </>}
      <div className="mt-2"><ErrorNote error={seed.error ?? reset.error ?? generate.error ?? graph.error} /></div>
    </Section>

    {empty ? <Section title={ontologyMode ? '这个本体还没有血缘' : '还没有血缘事件'} hint={ontologyMode ? '点上面的“从本体生成血缘”。' : '点上面的“载入 Marquez 外卖演示”，或到“流水线”页跑一遍，也可以把 OpenLineage 事件 POST 到 /api/v2/analysis/lineage/events。'}>
      <p className="text-sm text-slate-500">这里会出现作业和数据集组成的图。</p></Section> : graph.data && <>
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="作业" value={jobs} /><Stat label="数据集" value={sets} /><Stat label="依赖关系" value={graph.data.edges.length} /><Stat label="事件" value={graph.data.event_count ?? '—'} />
      </div>
      <div className="flex flex-wrap items-center gap-4 text-sm">
        {!ontologyMode && <label className="flex items-center gap-2 text-slate-700">命名空间
          <select aria-label="命名空间" className="wb-input w-auto" value={namespace} onChange={e => { setNamespace(e.target.value); setSelected(null) }}>
            <option value="">全部</option>{graph.data.namespaces.map(n => <option key={n} value={n}>{n}</option>)}</select></label>}
        <label className="flex items-center gap-2 text-slate-700"><input type="checkbox" checked={focusMode} onChange={e => setFocusMode(e.target.checked)} />只看所选节点附近</label>
        {focusMode && <><label className="flex items-center gap-2 text-slate-700">范围
          <select aria-label="方向" className="wb-input w-auto" value={direction} onChange={e => setDirection(e.target.value as typeof direction)}><option value="both">上下游</option><option value="upstream">只看上游</option><option value="downstream">只看下游</option></select></label>
          <label className="flex items-center gap-2 text-slate-700">跳数<input type="number" aria-label="跳数" min={1} max={8} value={depth} onChange={e => setDepth(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} className="w-16 wb-input" /></label>
          {!selected && <span className="text-xs text-amber-700">先点一个节点。</span>}</>}
        <span className="text-xs text-slate-500">点节点：高亮它的上下游；点空白处取消。</span>
      </div>
      {ontologyMode && <LayerLegend nodes={graph.data.nodes} />}
      <div className="space-y-4">
        <LineageGraph nodes={graph.data.nodes} edges={graph.data.edges} selected={selected} focus={graph.data.focus} onSelect={setSelected} />
        {node ? <NodeDetail node={node} impact={asking ? impact.data : undefined} impactBusy={impact.isFetching} onImpact={() => setAsk({ node: node.id, field: null })} fieldImpact={fieldImpact} />
          : <aside className="wb-surface p-4 text-sm text-slate-500">点图里的一个作业或数据集，在这里看它的详情。<ul className="mt-3 space-y-1 text-xs"><li>● 圆点：作业最近一次运行的状态</li><li>○ 椭圆：数据集；圆点徽标表示数据质量检查是否有未通过项</li>
            {ontologyMode && <li>点数据集的某个字段旁的“影响”：看这一列改了会影响哪些规则、告警和分析结果</li>}</ul></aside>}
      </div>
    </>}
  </div>
}
