import { useQuery } from '@tanstack/react-query'
import { analysisApi, type DuplicatePair } from '@/api/analysis'
import { ErrorNote, Section } from './shared'

const VERDICT: Record<string, { label: string; style: string }> = { likely: { label: '很可能重复', style: 'wb-status-danger' }, possible: { label: '可能重复', style: 'wb-status-warning' } }

function Pair({ pair }: { pair: DuplicatePair }) {
  const verdict = VERDICT[pair.verdict]
  const o = pair.property_overlap
  return <li data-testid="duplicate-pair" className="rounded-lg border border-slate-200 p-3">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="text-sm font-semibold text-slate-900">{pair.a} ≈ {pair.b}</span>
      {verdict && <span className={`wb-status ${verdict.style}`}>{verdict.label}</span>}
      <span className="text-xs tabular-nums text-slate-500">相似度 {(pair.score * 100).toFixed(0)}%</span>
      <span className="ml-auto text-xs text-slate-500">{pair.a} {pair.objects.a ?? '?'} 个对象 · {pair.b} {pair.objects.b ?? '?'} 个对象</span>
    </div>
    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full bg-red-500" style={{ width: `${Math.round(pair.score * 100)}%` }} /></div>
    <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-slate-700">{pair.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
    <details className="mt-2"><summary className="cursor-pointer text-xs text-slate-600">看属性怎么对上的（共有 {o.shared.length} 个）</summary>
      <div className="mt-2 grid gap-3 text-xs md:grid-cols-3">
        <div><p className="font-medium text-slate-500">共有</p><ul className="mt-1 space-y-0.5">{o.shared.map(s => <li key={s.a} className="font-mono">{s.a === s.b ? s.a : `${s.a} ≈ ${s.b}`}</li>)}</ul></div>
        <div><p className="font-medium text-slate-500">只在 {pair.a}</p><p className="mt-1 break-all font-mono">{o.only_a.join(', ') || '—'}</p></div>
        <div><p className="font-medium text-slate-500">只在 {pair.b}</p><p className="mt-1 break-all font-mono">{o.only_b.join(', ') || '—'}</p></div>
      </div>
      <p className="mt-2 text-xs text-slate-500">得分 = 0.35×名字相似 {pair.name_similarity} + 0.35×属性重合 {o.score} + 0.30×同一来源表 {pair.shared_tables.length ? 1 : 0}</p>
    </details>
    <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900">{pair.suggestion}</p>
  </li>
}

/** Object types that look like one thing described twice (the same table built twice, a hand-made type next to an Auto Ontology one). */
export function DuplicateTypes({ ontologyId }: { ontologyId: string }) {
  const report = useQuery({ queryKey: ['analysis', 'duplicates', ontologyId], queryFn: () => analysisApi.duplicateTypes(ontologyId), enabled: !!ontologyId })
  const pairs = report.data?.pairs ?? []
  return <Section title="重复的对象类型" testId="duplicate-types"
    hint="先看类型：Auto Ontology 按表建类型，手工建的类型又可能和它撞车。按名字、共有的属性、是否来自同一张表打分，列出可能是同一个东西建了两遍的类型。这里只提示，不替你合并。">
    {report.isLoading ? <p className="text-sm text-slate-500">比较中…</p> : pairs.length === 0
      ? <p className="text-sm text-slate-600">比较了 {report.data?.types_compared ?? 0} 个对象类型，没有发现重复的。</p>
      : <ul className="space-y-3">{pairs.map(p => <Pair key={`${p.a}|${p.b}`} pair={p} />)}</ul>}
    <div className="mt-2"><ErrorNote error={report.error} /></div>
  </Section>
}
