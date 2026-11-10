import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { analysisApi, type PipelineResult } from '@/api/analysis'
import { LayerLegend, LineageGraph, NodeDetail } from './LineageGraph'
import { OntologyPicker, SourceTabs } from './OntologySource'
import { DatasetCards, ErrorNote, PageHeader, Section, Stat } from './shared'
import { useOntologyChoice, useSourceMode } from './useSource'
import { num, pct } from './format'

const DATASET = 'splink-fake-1000'

function Step({ index, title, project, children, done }: { index: number; title: string; project: string; children: React.ReactNode; done: boolean }) {
  return <li data-testid={`pipeline-step-${index}`} className={`wb-surface p-4 ${done ? '' : 'opacity-70'}`}>
    <div className="flex items-center gap-2"><span className="wb-step-index">{index}</span><span className="text-sm font-semibold text-slate-900">{title}</span></div>
    <p className="mt-0.5 text-xs text-slate-500">{project}</p>
    <div className="mt-3 text-sm text-slate-700">{done ? children : '运行后显示'}</div>
  </li>
}

function Story({ result }: { result: PipelineResult }) {
  const missing = result.health.results.filter(r => r.expectation_type === 'expect_column_values_to_not_be_null' && !r.success)
  const duplicates = result.health.results.find(r => r.expectation_type === 'expect_compound_columns_to_be_unique')
  const failed = result.health.results.filter(r => !r.success)
  const ev = result.resolution.evaluation
  const s = result.resolution.summary
  if (result.source) {
    return <Section title="三步是怎么连起来的" testId="pipeline-story">
      <ol className="space-y-2 text-sm text-slate-700">
        <li><b>数据健康先对照本体：</b>{result.source.object_type} 的 {num(result.health.meta.object_count ?? result.health.meta.row_count)} 个对象按本体推出的 {result.health.statistics.evaluated_expectations} 条期望检查，
          {failed.length === 0 ? '全部通过' : `${failed.length} 条没通过：${failed.slice(0, 3).map(r => r.label + (r.kwargs.column ? `（${String(r.kwargs.column)}）` : '')).join('、')}${failed.length > 3 ? ' 等' : ''}`}；不合格的对象在“数据健康”页可以直接点开。</li>
        <li><b>实体消歧再看对象之间：</b>{num(s.records)} 个对象归并成 {num(s.entities_found)} 个实体{ev ? `（真实 ${num(ev.true_entities)} 个），精确率 ${pct(ev.precision)}，召回率 ${pct(ev.recall)}` : ''}；一个实体里的对象，说明本体里同一个东西被记了不止一次。</li>
        <li><b>血缘把本体的来龙去脉连起来：</b>来源表怎样经 Auto Ontology 变成对象类型、哪些规则读了它的哪些属性、这两次分析挂在哪个类型上，都在下面的图里。点类型旁的字段，可以问“这一列改了会影响什么”。</li>
      </ol>
    </Section>
  }
  return <Section title="三步是怎么连起来的" testId="pipeline-story">
    <ol className="space-y-2 text-sm text-slate-700">
      <li><b>数据健康先发现问题：</b>{missing.length > 0 ? `${missing.map(r => String(r.kwargs.column)).join('、')} 有缺失值` : '没有缺失值'}
        {duplicates && !duplicates.success ? `；${num(duplicates.result.unexpected_count)} 条记录和别的记录“名字+姓氏+生日”完全一样` : ''}——同一个人被记成了好几条，这正是需要消歧的原因。</li>
      <li><b>实体消歧接着解决：</b>把 {num(s.records)} 条记录归并成 {num(s.entities_found)} 个实体{ev ? `（真实 ${num(ev.true_entities)} 个），精确率 ${pct(ev.precision)}，召回率 ${pct(ev.recall)}` : ''}；每一对为什么被连起来，都能按字段解释。</li>
      <li><b>血缘把前两步记下来：</b>每一步都是一个 OpenLineage 作业，读入的表、检查报告、候选记录对、消歧后的实体都是数据集；检查结果挂在被检查的原始表上，消歧结果的“实体编号”列追溯到参与比较的原始列。下面的血缘图就是这份记录，点 fake_1000.raw 能看到哪些检查没过，还能问“它改了会影响谁”。</li>
    </ol>
  </Section>
}

export default function PipelinePage() {
  const queryClient = useQueryClient()
  const { mode, setMode } = useSourceMode()
  const ontologyMode = mode === 'ontology'
  const choice = useOntologyChoice(ontologyMode)
  const demos = useQuery({ queryKey: ['analysis', 'demos'], queryFn: analysisApi.demos })
  const dataset = demos.data?.find(d => d.id === DATASET)
  const [selected, setSelected] = useState<string | null>(null)
  const [ask, setAsk] = useState<{ node: string; field: string | null } | null>(null)
  const [ran, setRan] = useState<{ key: string; result: PipelineResult } | null>(null)
  const key = ontologyMode ? `o:${choice.ontologyId}:${choice.objectType}` : `d:${DATASET}`
  const scope = ontologyMode ? `o:${choice.ontologyId}` : 'workbench'
  const graph = useQuery({ queryKey: ['analysis', 'lineage', 'pipeline', scope], queryFn: () => ontologyMode ? analysisApi.ontologyLineageGraph(choice.ontologyId) : analysisApi.lineageGraph({ namespace: 'workbench' }),
    enabled: !ontologyMode || !!choice.ontologyId })
  const run = useMutation({
    mutationFn: () => ontologyMode ? analysisApi.runOntologyPipeline(choice.ontologyId, choice.objectType) : analysisApi.runPipeline(DATASET),
    onSuccess: async data => { setRan({ key, result: data.result }); setSelected(data.result.lineage.focus); setAsk(null); await queryClient.invalidateQueries({ queryKey: ['analysis', 'lineage'] }) },
  })
  const result = ran?.key === key ? ran.result : undefined
  const node = graph.data?.nodes.find(n => n.id === selected) ?? null
  const asking = ask && ask.node === selected ? ask : null
  const impact = useQuery({ queryKey: ['analysis', 'impact', 'pipeline', scope, selected, asking?.field ?? null],
    queryFn: () => ontologyMode ? analysisApi.ontologyImpact(choice.ontologyId, selected!, asking?.field ?? undefined) : analysisApi.lineageImpact(selected!, 'workbench'), enabled: !!selected && !!asking })
  const failed = result?.health.results.filter(r => !r.success) ?? []
  const ev = result?.resolution.evaluation
  const ready = ontologyMode ? !!choice.objectType && (choice.selected?.object_count ?? 1) > 0 : !!dataset?.available
  const fieldImpact = ontologyMode ? { field: asking?.field ?? null, result: impact.data?.column, onAsk: (field: string) => setAsk({ node: selected!, field }) } : undefined
  const typeLinks = result?.source ? `?source=ontology&ontology=${encodeURIComponent(result.source.ontology_id)}&type=${encodeURIComponent(result.source.object_type)}` : ''

  return <div className="wb-page mx-auto max-w-[1400px] p-6 pb-16">
    <PageHeader title="流水线：一份数据走完三步" subtitle="同一份数据依次经过数据健康、实体消歧，每一步都记成 OpenLineage 事件，最后得到一张血缘图。三个功能各自的核心做法都用上了。" />

    <Section title={ontologyMode ? '走哪个对象类型' : '演示数据'} hint={ontologyMode ? '选本体里的一个对象类型：它的对象先按本体推出的期望检查，再做实体消歧，两次运行都挂到这个类型的血缘上。' : '用 Splink 的默认演示数据 fake_1000：1000 条虚构人员记录，里面有缺失值、错拼，也有同一个人的多条记录。'}>
      <SourceTabs mode={mode} onChange={next => { setMode(next); setSelected(null); setAsk(null) }} />
      {ontologyMode ? <OntologyPicker choice={choice} /> : dataset && <DatasetCards datasets={[dataset]} value={dataset.id} onChange={() => undefined} badge={() => '默认演示'} />}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button" className="wb-button-primary" disabled={run.isPending || !ready} onClick={() => run.mutate()}>{run.isPending ? '运行中…' : result ? '再运行一次' : '运行流水线'}</button>
        <span className="text-xs text-slate-500">{ontologyMode ? '再运行一次会重新检查、重新消歧，并重写这个本体的血缘。' : '再运行一次会在血缘里多记一次运行，作业的“运行次数”会加一。'}</span>
      </div>
      <div className="mt-2"><ErrorNote error={run.error} /></div>
    </Section>

    <ol className="grid gap-3 lg:grid-cols-3">
      <Step index={1} title={ontologyMode ? '读取对象' : '读入数据'} project={ontologyMode ? '平台的对象查询：权限和脱敏照常生效' : 'OpenLineage：记录读入的表'} done={!!result}>
        {result && <><p>{num(result.steps[0].summary.rows as number)} {ontologyMode ? '个对象' : '行'} × {String(result.steps[0].summary.columns)} {ontologyMode ? '个属性' : '列'}</p>
          <p className="mt-1 break-all text-xs text-slate-500">{result.sha256 ? `SHA-256 ${result.sha256.slice(0, 16)}…` : result.source ? `${result.source.object_type}${result.source.read.snapshot ? ' · 固定快照读取' : ''}` : ''}</p></>}
      </Step>
      <Step index={2} title="数据健康检查" project={ontologyMode ? 'Great Expectations 的做法，期望由本体推出' : 'Great Expectations 的做法'} done={!!result}>
        {result && <><p><b>{result.health.statistics.successful_expectations}</b> / {result.health.statistics.evaluated_expectations} 条期望通过</p>
          <ul className="mt-2 space-y-1 text-xs text-red-800">{failed.slice(0, 5).map((r, i) => <li key={i}>✕ {r.label}{r.kwargs.column ? ` · ${String(r.kwargs.column)}` : ''}{r.result.unexpected_count ? `（${num(r.result.unexpected_count)} 行）` : ''}</li>)}</ul>
          <Link to={`/analysis/health${typeLinks}`} className="mt-2 inline-block text-xs underline">到“数据健康”页{ontologyMode ? '打开不合格的对象' : '自己改期望'}</Link></>}
      </Step>
      <Step index={3} title="实体消歧" project="Splink 的做法" done={!!result}>
        {result && <><p>{num(result.resolution.summary.records)} {ontologyMode ? '个对象' : '条记录'} → <b>{num(result.resolution.summary.entities_found)}</b> 个实体</p>
          {ev && <p className="mt-1 text-xs text-slate-600">精确率 {pct(ev.precision)} · 召回率 {pct(ev.recall)} · F1 {ev.f1.toFixed(3)}</p>}
          <Link to={`/analysis/resolution${typeLinks}`} className="mt-2 inline-block text-xs underline">到“实体消歧”页看逐对解释</Link></>}
      </Step>
    </ol>

    {result && <Story result={result} />}

    {graph.data && graph.data.nodes.length > 0 && <Section title="血缘图" hint={ontologyMode ? '这个本体的血缘：来源表、Auto Ontology 生成、对象类型、规则、告警，以及刚才这两步。点节点高亮它的上下游。' : '这次流水线留下的作业和数据集。点节点高亮它的上下游。'} testId="pipeline-lineage">
      <div className="mb-3 grid gap-3 sm:grid-cols-4"><Stat label="作业" value={graph.data.nodes.filter(n => n.kind === 'job').length} /><Stat label="数据集" value={graph.data.nodes.filter(n => n.kind === 'dataset').length} />
        <Stat label="依赖关系" value={graph.data.edges.length} /><Stat label="事件" value={graph.data.event_count ?? '—'} /></div>
      {ontologyMode && <div className="mb-3"><LayerLegend nodes={graph.data.nodes} /></div>}
      <div className="space-y-4">
        <LineageGraph nodes={graph.data.nodes} edges={graph.data.edges} selected={selected} focus={result?.lineage.focus} onSelect={setSelected} />
        {node ? <NodeDetail node={node} impact={asking ? impact.data : undefined} impactBusy={impact.isFetching} onImpact={() => setAsk({ node: node.id, field: null })} fieldImpact={fieldImpact} /> : <aside className="wb-surface p-4 text-sm text-slate-500">点图里的节点看详情。</aside>}
      </div>
    </Section>}
  </div>
}
