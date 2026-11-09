import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { analysisApi, type ModelComparison, type PairExample, type ResolutionResult } from '@/api/analysis'
import { DatasetCards, Diverging, ErrorNote, PageHeader, RecordTable, Section, Stat } from './shared'
import { num, pct } from './format'

const COMPARISON_LABEL: Record<string, string> = { name: '名字相似度', levenshtein: '编辑距离', exact: '完全一致', jaro_winkler: '字符串相似度', email: '邮箱' }

export function Histogram({ histogram }: { histogram: ResolutionResult['histogram'] }) {
  const series = histogram.match && histogram.non_match
    ? [{ name: '真正同一实体的记录对', values: histogram.match, color: '#059669' }, { name: '不是同一实体的记录对', values: histogram.non_match, color: '#94a3b8' }]
    : [{ name: '候选记录对', values: histogram.all ?? [], color: '#64748b' }]
  const bins = histogram.bin_start.length
  const peak = Math.max(1, ...series.flatMap(s => s.values.map(v => Math.log10(1 + v))))
  const W = 640, H = 170, pad = 28, step = (W - pad * 2) / bins
  return <figure>
    <svg viewBox={`0 0 ${W} ${H + 26}`} role="img" aria-label="匹配权重分布" className="w-full">
      {histogram.bin_start.map((start, i) => {
        const x = pad + i * step
        return <g key={start}>
          {series.map((s, k) => { const h = (Math.log10(1 + s.values[i]) / peak) * (H - 12); const w = step / series.length - 1
            return <rect key={k} x={x + (k * step) / series.length} y={H - h} width={Math.max(1, w)} height={h} fill={s.color}><title>{`权重 ${start} 到 ${start + histogram.bin_width}：${s.name} ${s.values[i]} 对`}</title></rect> })}
          {start % 10 === 0 && <text x={x} y={H + 14} fontSize="10" fill="#64748b" textAnchor="middle">{start}</text>}
        </g>
      })}
      <line x1={pad} x2={W - pad} y1={H} y2={H} stroke="#cbd5e1" />
      {histogram.bin_start.includes(0) && <line x1={pad + histogram.bin_start.indexOf(0) * step} x2={pad + histogram.bin_start.indexOf(0) * step} y1={0} y2={H} stroke="#e11d48" strokeDasharray="3 3" />}
    </svg>
    <figcaption className="mt-1 flex flex-wrap items-center gap-4 text-xs text-slate-600">
      {series.map(s => <span key={s.name} className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />{s.name}</span>)}
      <span>横轴：匹配权重（越大越像同一个实体），纵轴按对数画；红虚线是权重 0，即先验之上的证据正负相抵。</span>
    </figcaption>
  </figure>
}

function WeightsTable({ comparison }: { comparison: ModelComparison }) {
  const levels = comparison.levels.filter(l => l.weight != null)
  const max = Math.max(1, ...levels.map(l => Math.abs(l.weight ?? 0)))
  return <div className="rounded-lg border border-slate-200 p-3">
    <p className="text-sm font-medium text-slate-900">{comparison.column} <span className="ml-1 text-xs font-normal text-slate-500">{COMPARISON_LABEL[comparison.type] ?? comparison.type}{comparison.term_frequency_adjusted ? ' · 词频校正' : ''}</span></p>
    <table className="mt-2 w-full text-xs">
      <thead><tr className="text-left text-slate-500"><th className="font-medium">一致程度</th><th className="text-right font-medium" title="在真正同一实体的记录对里出现的概率">m</th><th className="text-right font-medium" title="在随机记录对里出现的概率">u</th><th className="w-40 pl-3 font-medium">权重 log₂(m/u)</th></tr></thead>
      <tbody>{[...levels].reverse().map(l => <tr key={l.level} className="border-t border-slate-100">
        <td className="py-1 pr-2 text-slate-800">{l.label}</td>
        <td className="text-right tabular-nums">{l.m}</td><td className="text-right tabular-nums">{l.u}</td>
        <td className="pl-3"><div className="flex items-center gap-2"><div className="w-24"><Diverging value={l.weight ?? 0} max={max} /></div><span className="w-10 text-right tabular-nums">{l.weight}</span></div></td>
      </tr>)}</tbody>
    </table>
  </div>
}

export function PairCard({ pair, columns }: { pair: PairExample; columns: string[] }) {
  const max = Math.max(1, ...pair.contributions.map(c => Math.abs(c.weight)))
  return <li data-testid="pair-example" className="rounded-lg border border-slate-200 p-3">
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <span className="text-sm font-medium text-slate-900">{pair.id_l} ↔ {pair.id_r}</span>
      <span className="text-sm tabular-nums">匹配概率 <b>{pct(pair.probability, 1)}</b>，总权重 {pair.weight}</span>
      {pair.same_entity != null && <span className={`wb-status ${pair.same_entity ? 'wb-status-success' : 'wb-status-danger'}`}>{pair.same_entity ? '确实是同一实体' : '其实不是同一实体'}</span>}
    </div>
    <div className="mt-2"><RecordTable columns={columns} rows={[pair.left, pair.right]} /></div>
    <ul className="mt-3 space-y-1">{pair.contributions.map((c, i) => <li key={i} className="grid grid-cols-[7rem_1fr_9rem_3rem] items-center gap-2 text-xs">
      <span className="truncate text-slate-700" title={c.column}>{c.column}</span><span className="truncate text-slate-500" title={c.label}>{c.label}</span>
      <Diverging value={c.weight} max={max} /><span className="text-right tabular-nums">{c.weight > 0 ? `+${c.weight}` : c.weight}</span>
    </li>)}</ul>
  </li>
}

const EXAMPLE_TABS: { key: keyof ResolutionResult['examples']; label: string; hint: string }[] = [
  { key: 'borderline', label: '拿不准的', hint: '匹配概率接近 50% 的记录对：模型最犹豫的地方。' },
  { key: 'false_positives', label: '错并', hint: '被连到一起、但其实不是同一实体的记录对。' },
  { key: 'false_negatives', label: '漏并', hint: '其实是同一实体、却没有被连起来的记录对（比阈值低）。' },
  { key: 'confident', label: '最确定的', hint: '权重最高的记录对。' },
]

export default function ResolutionPage() {
  const datasets = useQuery({ queryKey: ['analysis', 'demos'], queryFn: analysisApi.demos })
  const options = useMemo(() => (datasets.data ?? []).filter(d => d.tool === 'splink'), [datasets.data])
  const [chosen, setChosen] = useState('')
  const [thresholdChoice, setThresholdChoice] = useState<number | null>(null)
  const [tab, setTab] = useState<keyof ResolutionResult['examples']>('borderline')
  const [open, setOpen] = useState<number | null>(null)
  const defaultId = (options.find(d => d.default && d.available) ?? options.find(d => d.available) ?? options[0])?.id ?? ''
  const datasetId = chosen || defaultId
  const active = options.find(d => d.id === datasetId)
  const preset = useQuery({ queryKey: ['analysis', 'preset', datasetId], queryFn: () => analysisApi.resolutionPreset(datasetId), enabled: !!active?.available })
  const threshold = thresholdChoice ?? (preset.data ? Number(preset.data.threshold_match_probability ?? 0.9) : null)

  const run = useMutation({ mutationFn: () => analysisApi.runResolution(datasetId, threshold == null || !preset.data ? undefined : { ...preset.data, threshold_match_probability: threshold }) })
  const result = run.data?.result
  const columns = result ? Object.keys(result.clusters[0]?.records[0] ?? result.examples.borderline[0]?.left ?? {}) : []
  const ev = result?.evaluation

  return <div className="wb-page mx-auto max-w-6xl p-6 pb-16">
    <PageHeader title="实体消歧" subtitle="找出表里指向同一个人（同一个对象）的多条记录：给每一对记录打一个匹配概率，再把概率够高的连成一个实体。"
      credit={<>做法照 <a className="underline" href="https://github.com/moj-analytical-services/splink" target="_blank" rel="noreferrer">Splink</a>（MIT）：Fellegi–Sunter 概率模型，u 用随机配对估计、先验用确定性规则估计、m 用 EM 估计，加词频校正。</>} />

    <Section title="演示数据" hint="默认用 Splink 入门教程的 fake_1000；效果最好的演示是 5 万条历史人物，Splink 官方用它展示去重。">
      <DatasetCards datasets={options} value={datasetId} onChange={id => { setChosen(id); setThresholdChoice(null); run.reset(); setOpen(null) }} badge={d => (d.default ? '默认演示' : '效果最好')} />
      <div className="mt-4 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-3 text-sm text-slate-700">匹配概率阈值
          <input type="range" aria-label="匹配概率阈值" min={0.1} max={0.99} step={0.01} value={threshold ?? 0.5} onChange={e => setThresholdChoice(Number(e.target.value))} className="w-48" />
          <span className="w-12 tabular-nums">{(threshold ?? 0.5).toFixed(2)}</span></label>
        <button type="button" className="wb-button-primary" disabled={run.isPending || !active?.available || threshold == null} onClick={() => run.mutate()}>
          {run.isPending ? (active?.rows && active.rows > 5000 ? '运行中（约 10 秒）…' : '运行中…') : '运行实体消歧'}</button>
        <span className="text-xs text-slate-500">阈值越高，连得越保守：错并少，漏并多。</span>
      </div>
      <div className="mt-2"><ErrorNote error={run.error ?? preset.error} /></div>
    </Section>

    {result && <>
      <Section title="结果" testId="resolution-result" hint={`${num(result.summary.records)} 条记录 · ${result.summary.run_time_ms / 1000} 秒`}>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {ev && <><Stat label="精确率" value={pct(ev.precision)} tone={ev.precision >= 0.95 ? 'good' : 'warn'} hint="连起来的记录对里，确实同一实体的占比" />
            <Stat label="召回率" value={pct(ev.recall)} tone={ev.recall >= 0.8 ? 'good' : 'warn'} hint="真正同一实体的记录对里，被连起来的占比" />
            <Stat label="F1" value={ev.f1.toFixed(3)} /><Stat label="比较前先过滤" value={pct(1 - result.summary.candidate_pairs / Math.max(1, result.summary.total_possible_pairs), 3)} hint={`${num(result.summary.candidate_pairs)} 对进入比较，全部 ${num(result.summary.total_possible_pairs)} 对`} /></>}
          <Stat label="找到的实体" value={num(result.summary.entities_found)} hint={ev ? `真实 ${num(ev.true_entities)} 个` : undefined} />
          <Stat label="最大的一簇" value={num(result.summary.largest_cluster)} hint="条记录" />
          <Stat label="被归并的记录" value={num(result.summary.records_in_clusters)} hint={`${num(result.summary.singletons)} 条没有找到重复`} />
          {ev && <Stat label="分块覆盖率" value={pct(ev.blocking_recall)} hint="真正同一实体的记录对里，进入比较的占比（召回率的上限）" tone={ev.blocking_recall >= 0.8 ? 'good' : 'warn'} />}
        </div>
      </Section>

      <Section title="模型学到了什么" hint={`先验 λ = ${result.model.lambda.toExponential(2)}（随机两条记录是同一实体的概率），用了 ${num(result.model.random_pairs_for_u)} 对随机记录估计 u；每个字段的每个一致程度，权重 = log₂(m/u)。`}>
        <div className="grid gap-3 lg:grid-cols-2">{result.model.comparisons.map(c => <WeightsTable key={c.column} comparison={c} />)}</div>
        <ul className="mt-3 space-y-1 text-xs text-slate-500">{result.model.sessions.map((s, i) => <li key={i}>EM 第 {i + 1} 轮：在“{JSON.stringify(s.rule).replace(/"/g, '')}”相同的 {num(s.pairs)} 对上训练，{s.iterations} 次迭代</li>)}</ul>
      </Section>

      <Section title="匹配权重分布"><Histogram histogram={result.histogram} /></Section>

      <Section title="逐对解释" hint={EXAMPLE_TABS.find(t => t.key === tab)?.hint}>
        <div role="tablist" className="flex flex-wrap gap-1.5">{EXAMPLE_TABS.filter(t => t.key !== 'false_positives' && t.key !== 'false_negatives' || result.evaluation).map(t => <button key={t.key} role="tab" type="button" aria-selected={tab === t.key}
          className={`rounded-lg border px-3 py-1.5 text-sm ${tab === t.key ? 'border-slate-900 bg-slate-900 text-white' : 'bg-white hover:bg-slate-50'}`} onClick={() => setTab(t.key)}>{t.label}（{result.examples[t.key].length}）</button>)}</div>
        {result.examples[tab].length === 0 ? <p className="mt-3 text-sm text-slate-500">这一类一个也没有。</p> : <ul className="mt-3 space-y-3">{result.examples[tab].map((p, i) => <PairCard key={i} pair={p} columns={columns} />)}</ul>}
      </Section>

      <Section title="最大的几簇" hint="每簇是一个被识别出来的实体：同一簇里的记录被认为指向同一个人。">
        <ul className="space-y-2">{result.clusters.slice(0, 12).map(c => <li key={c.cluster_id} className="rounded-lg border border-slate-200 p-3">
          <button type="button" className="flex w-full items-center justify-between text-left text-sm" aria-expanded={open === c.cluster_id} onClick={() => setOpen(open === c.cluster_id ? null : c.cluster_id)}>
            <span className="font-medium text-slate-900">{c.size} 条记录{c.true_entities_inside != null && (c.true_entities_inside === 1 ? <span className="ml-2 wb-status wb-status-success">都是同一实体</span> : <span className="ml-2 wb-status wb-status-warning">混进了 {c.true_entities_inside} 个真实实体</span>)}</span>
            <span className="text-xs text-slate-500">{open === c.cluster_id ? '收起' : '展开'}</span></button>
          {open === c.cluster_id && <div className="mt-2"><RecordTable columns={columns} rows={c.records} />{c.size > c.records.length && <p className="mt-1 text-xs text-slate-500">只显示前 {c.records.length} 条。</p>}</div>}
        </li>)}</ul>
      </Section>
    </>}
  </div>
}
