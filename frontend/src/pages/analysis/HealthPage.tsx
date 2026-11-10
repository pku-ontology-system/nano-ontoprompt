import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { analysisApi, type AnalysisRun, type ExpectationResult, type HealthReport, type Suite } from '@/api/analysis'
import { DatasetCards, ErrorNote, PageHeader, RecordTable, Section, Stat } from './shared'
import { OntologyPicker, SourceTabs } from './OntologySource'
import { useOntologyChoice, useSourceMode } from './useSource'
import { cell, objectHref, shortObjectId } from './format'

const short = (value: unknown) => { const text = cell(value); return text.length > 38 ? `${text.slice(0, 36)}…` : text }

function describe(result: ExpectationResult): string {
  const { kwargs } = result
  const parts = Object.entries(kwargs).filter(([key]) => key !== 'column').map(([key, value]) => `${key}=${Array.isArray(value) ? `[${value.slice(0, 6).map(short).join(', ')}${value.length > 6 ? ', …' : ''}]` : short(value)}`)
  return parts.join('，')
}

const SOURCE_STYLE: Record<string, string> = { 本体: 'bg-blue-50 text-blue-800 ring-blue-200', 数据现状: 'bg-slate-100 text-slate-700 ring-slate-200' }

/** The objects that broke an expectation, as links to the object explorer (the report keeps the first few). */
function BrokenObjects({ ids, total, ontologyId, objectType }: { ids: string[]; total: number; ontologyId: string; objectType: string }) {
  return <div className="mt-2 pl-8 text-xs text-slate-700" data-testid="broken-objects">
    <span className="text-slate-500">有问题的对象：</span>
    {ids.map(id => <Link key={id} to={objectHref(ontologyId, objectType, id)} title={id} className="mr-2 inline-block rounded bg-white px-1.5 py-0.5 font-mono text-[11px] underline ring-1 ring-slate-200">{shortObjectId(id)}</Link>)}
    {total > ids.length && <span className="text-slate-500">…共 {total.toLocaleString('zh-CN')} 个，这里只列前 {ids.length} 个</span>}
  </div>
}

function ResultRow({ result, origin }: { result: ExpectationResult; origin?: { ontologyId: string; objectType: string } }) {
  const r = result.result
  const column = result.kwargs.column ?? result.kwargs.column_A ?? (Array.isArray(result.kwargs.column_list) ? 'ordered' : undefined)
  const failedRows = r.unexpected_count ?? 0
  const source = result.meta?.source
  const objectIds = r.partial_unexpected_object_ids ?? []
  return <li data-testid="expectation-result" data-success={result.success} className={`rounded-lg border px-3 py-2.5 ${result.success ? 'border-slate-200 bg-white' : 'border-red-200 bg-red-50/60'}`}>
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span aria-hidden="true" className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-bold text-white ${result.success ? 'bg-emerald-500' : 'bg-red-500'}`}>{result.success ? '✓' : '✕'}</span>
      <span className="text-sm font-medium text-slate-900">{result.label}{typeof column === 'string' && column !== 'ordered' ? <span className="ml-1.5 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">{column}</span> : null}</span>
      <code className="text-[11px] text-slate-500">{result.expectation_type}</code>
      {source && <span className={`rounded px-1.5 py-0.5 text-[11px] ring-1 ${SOURCE_STYLE[source] ?? SOURCE_STYLE['数据现状']}`} title="这条期望从哪里来">{source === '本体' ? '本体定义' : source}</span>}
      <span className="ml-auto text-xs text-slate-500">{result.success ? '通过' : '未通过'}</span>
    </div>
    {describe(result) && <p className="mt-1 pl-8 text-xs text-slate-500">{describe(result)}</p>}
    {result.meta?.notes && <p className="mt-1 pl-8 text-xs text-slate-600">{result.meta.notes}</p>}
    {result.exception_info.raised_exception && <p role="alert" className="mt-1 pl-8 text-xs text-red-700">这条期望本身有问题：{result.exception_info.exception_message}</p>}
    {!result.success && !result.exception_info.raised_exception && failedRows > 0 && <div className="mt-2 pl-8 text-xs text-red-800">
      {failedRows.toLocaleString('zh-CN')} 行不符合（占 {r.unexpected_percent ?? 0}%）
      {(r.partial_unexpected_counts?.length ?? 0) > 0 && <span className="ml-2 inline-flex flex-wrap gap-1 align-middle">
        {r.partial_unexpected_counts!.slice(0, 6).map((c, i) => <span key={i} className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-red-200">{short(c.value)}{c.count > 1 ? ` ×${c.count}` : ''}</span>)}
      </span>}
    </div>}
    {origin && objectIds.length > 0 && <BrokenObjects ids={objectIds.slice(0, 8)} total={failedRows || objectIds.length} ontologyId={origin.ontologyId} objectType={origin.objectType} />}
    {!result.success && result.result.observed_value !== undefined && failedRows === 0 && !result.exception_info.raised_exception && <p className="mt-1 pl-8 text-xs text-red-800">实际值：{short(result.result.observed_value)}</p>}
  </li>
}

export default function HealthPage() {
  const { mode, setMode } = useSourceMode()
  const ontologyMode = mode === 'ontology'
  const choice = useOntologyChoice(ontologyMode)
  const [chosen, setChosen] = useState('')
  const [withBaseline, setWithBaseline] = useState(false)
  const [typed, setTyped] = useState<{ key: string; text: string } | null>(null) // what the user typed, and for which source; another source discards it
  const [parseError, setParseError] = useState('')
  const [ran, setRan] = useState<{ key: string; run: AnalysisRun<HealthReport> } | null>(null)
  const [onlyFailed, setOnlyFailed] = useState(false)

  const datasets = useQuery({ queryKey: ['analysis', 'demos'], queryFn: analysisApi.demos })
  const tables = useMemo(() => (datasets.data ?? []).filter(d => d.tool !== 'marquez'), [datasets.data])
  const defaultId = (tables.find(d => d.tool === 'great_expectations' && d.available) ?? tables.find(d => d.available) ?? tables[0])?.id ?? ''
  const datasetId = chosen || defaultId
  const active = tables.find(d => d.id === datasetId)

  const demoSuite = useQuery({ queryKey: ['analysis', 'suite', datasetId], queryFn: () => analysisApi.healthSuite(datasetId), enabled: !ontologyMode && !!datasetId && !!active?.available })
  const ontologySuite = useQuery({ queryKey: ['analysis', 'onto-suite', choice.ontologyId, choice.objectType, withBaseline], queryFn: () => analysisApi.ontologySuite(choice.ontologyId, choice.objectType, withBaseline),
    enabled: ontologyMode && !!choice.ontologyId && !!choice.objectType })
  const suite = ontologyMode ? ontologySuite : demoSuite
  const preview = useQuery({ queryKey: ['analysis', 'preview', datasetId], queryFn: () => analysisApi.preview(datasetId, 8), enabled: !ontologyMode && !!datasetId && !!active?.available })

  const key = ontologyMode ? `o:${choice.ontologyId}:${choice.objectType}:${withBaseline}` : `d:${datasetId}`
  const edited = typed?.key === key ? typed.text : null
  const editing = edited ?? (suite.data ? JSON.stringify(suite.data.suite, null, 2) : '')
  const ready = ontologyMode ? !!choice.objectType : !!active?.available

  const run = useMutation({
    mutationFn: async () => {
      let custom: Suite | undefined
      if (edited !== null) {
        try { custom = JSON.parse(edited) as Suite } catch { setParseError('套件不是合法的 JSON'); throw new Error('套件不是合法的 JSON') }
      }
      setParseError('')
      return ontologyMode ? analysisApi.runOntologyHealth(choice.ontologyId, choice.objectType, custom, withBaseline) : analysisApi.runHealth(datasetId, custom)
    },
    onSuccess: data => setRan({ key, run: data }),
  })
  const baseline = useMutation({
    mutationFn: () => analysisApi.healthSuite(datasetId, true),
    onSuccess: data => { setTyped({ key, text: JSON.stringify(data.suite, null, 2) }); setParseError('') },
  })

  const report: HealthReport | undefined = ran?.key === key ? ran.run.result : undefined
  const shown = report?.results.filter(r => !onlyFailed || !r.success) ?? []
  const read = ontologyMode ? ontologySuite.data?.read : undefined
  const reset = () => { setParseError(''); setTyped(null) }

  return <div className="wb-page mx-auto max-w-6xl p-6 pb-16">
    <PageHeader title="数据健康" subtitle="用期望逐条检查一张表：哪些列不该为空、取值该在什么范围、哪些值不该出现。每条期望都说清楚有多少行不符合、举出样例。"
      credit={<>做法照 <a className="underline" href="https://github.com/great-expectations/great_expectations" target="_blank" rel="noreferrer">Great Expectations</a>（Apache-2.0）：期望的名字、`mostly` 容差、结果的字段都按它的文档。</>} />

    <Section title="检查什么" hint={ontologyMode ? '检查本体里一个对象类型的所有对象：期望由类型本身推出来（主键、属性类型、必填、外键、定义里写明的约束），不合格的对象可以点开。' : '默认是 Great Expectations 教程用的数据；也可以拿 Splink 的数据看看它的缺口。'}>
      <SourceTabs mode={mode} onChange={next => { setMode(next); reset() }} />
      {ontologyMode ? <OntologyPicker choice={choice} /> : <>
        <DatasetCards datasets={tables} value={datasetId} onChange={id => { setChosen(id); reset() }} badge={d => (d.tool === 'great_expectations' ? '默认演示' : null)} />
        {preview.data && <details className="mt-4"><summary className="cursor-pointer text-sm text-slate-600">看一眼数据（前 8 行，共 {preview.data.total.toLocaleString('zh-CN')} 行）</summary>
          <div className="mt-2"><RecordTable columns={preview.data.columns} rows={preview.data.rows.map(row => Object.fromEntries(preview.data.columns.map((c, i) => [c, row[i]])))} /></div></details>}
      </>}
    </Section>

    <Section title="期望套件" hint="可以直接改：每条是 {expectation_type, kwargs}，写法和 Great Expectations 一样。">
      {ontologyMode && read?.counts && <p className="mb-2 text-sm text-slate-600" data-testid="suite-origin">共 {read.counts.ontology + read.counts.baseline} 条：<b>{read.counts.ontology}</b> 条由本体推出{read.counts.baseline > 0 ? <>，<b>{read.counts.baseline}</b> 条是按现有数据生成的基线</> : ''}。
        {read.source_table && <> 这个类型由 Auto Ontology 从表 <code className="rounded bg-slate-100 px-1">{read.source_table}</code> 建出。</>}</p>}
      <textarea aria-label="期望套件" spellCheck={false} value={editing} onChange={e => setTyped({ key, text: e.target.value })} rows={10} className="wb-input font-mono text-xs leading-5" />
      {parseError && <p role="alert" className="mt-2 text-sm text-red-700">{parseError}</p>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className="wb-button-primary" disabled={!editing || run.isPending || !ready} onClick={() => run.mutate()}>{run.isPending ? '检查中…' : '运行检查'}</button>
        <button type="button" className="wb-button-secondary" disabled={edited === null} onClick={reset}>恢复默认套件</button>
        {ontologyMode
          ? <label className="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={withBaseline} onChange={e => setWithBaseline(e.target.checked)} />再加上按现有数据生成的基线</label>
          : <button type="button" className="wb-button-secondary" disabled={baseline.isPending || !active?.available} onClick={() => baseline.mutate()}>按这份数据生成基线套件</button>}
        <span className="text-xs text-slate-500">基线套件描述这份数据现在的样子（哪些列从不为空、取值范围、少数几个取值），适合拿来发现下一批数据的漂移。</span>
      </div>
      <div className="mt-2"><ErrorNote error={run.error && !parseError ? run.error : baseline.error ?? suite.error} /></div>
    </Section>

    {report && <Section title="检查结果" testId="health-report" hint={`${report.meta.suite_name} · ${report.meta.row_count.toLocaleString('zh-CN')} ${ontologyMode ? '个对象' : '行'} × ${report.meta.column_count} 列 · ${report.meta.run_time_ms} 毫秒`}>
      {report.source?.read.truncated && <p role="note" className="wb-alert wb-alert-warning mb-3">这个类型的对象多于 10,000 个，这里只读了前 10,000 个，结果只代表这一部分。</p>}
      {ontologyMode && report.meta.object_count === 0 && <p role="note" className="wb-alert wb-alert-warning mb-3">这个类型下没有对象，列上的期望没有数据可查（都算通过）；有意义的只有“应当已有对象”这一条。类型建好了但对象没有进来，通常是数据还没导入或没绑定数据源。</p>}
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="通过率" value={`${report.statistics.success_percent}%`} tone={report.success ? 'good' : 'warn'} />
        <Stat label="通过" value={report.statistics.successful_expectations} tone="good" hint={`共 ${report.statistics.evaluated_expectations} 条`} />
        <Stat label="未通过" value={report.statistics.unsuccessful_expectations} tone={report.statistics.unsuccessful_expectations ? 'bad' : 'plain'} />
        <Stat label="整体" value={report.success ? '全部通过' : '有未通过项'} tone={report.success ? 'good' : 'bad'} hint={report.statistics.errored_expectations ? `其中 ${report.statistics.errored_expectations} 条期望本身出错` : report.success ? '没有要处理的' : `${report.statistics.unsuccessful_expectations} 条要处理`} />
      </div>
      <div className="mt-4 h-2 overflow-hidden rounded-full bg-red-100"><div className="h-full bg-emerald-500" style={{ width: `${report.statistics.success_percent}%` }} /></div>
      <label className="mt-4 flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={onlyFailed} onChange={e => setOnlyFailed(e.target.checked)} />只看未通过的</label>
      <ul className="mt-3 space-y-2">{shown.map((r, i) => <ResultRow key={i} result={r} origin={report.source ? { ontologyId: report.source.ontology_id, objectType: report.source.object_type } : undefined} />)}</ul>
    </Section>}
  </div>
}
