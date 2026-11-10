import { Link } from 'react-router-dom'
import type { ColumnImpact } from '@/api/analysis'
import { LAYER_STYLE, refLink } from './format'

const ORDER = ['object', 'rule', 'alert', 'decision', 'analysis']

function Chip({ layer }: { layer: string | null }) {
  const style = layer ? LAYER_STYLE[layer] : undefined
  return <span className="rounded px-1.5 py-0.5 text-[11px] ring-1" style={{ background: style?.fill ?? '#f8fafc', color: '#0f172a', borderColor: style?.stroke ?? '#cbd5e1', ['--tw-ring-color' as string]: style?.stroke ?? '#cbd5e1' }}>{style?.label ?? '其他'}</span>
}

/** What a change to one column reaches: the derived columns and the jobs between, with the rules and alerts named first. */
export function ColumnImpactView({ impact, dataset }: { impact: ColumnImpact; dataset: string }) {
  const rules = impact.jobs.filter(j => j.layer === 'rule')
  const datasetsOf = (layer: string) => new Set(impact.columns.filter(c => c.layer === layer).map(c => c.dataset)).size
  const summary = [rules.length && `${rules.length} 条规则`, datasetsOf('alert') && `${datasetsOf('alert')} 组告警`, datasetsOf('decision') && `${datasetsOf('decision')} 组处置单`,
    datasetsOf('object') && `${datasetsOf('object')} 个对象类型`, datasetsOf('analysis') && `${datasetsOf('analysis')} 个分析结果`].filter(Boolean)
  return <section data-testid="column-impact" className="mt-3 rounded-lg border border-slate-200 bg-slate-50/70 p-3">
    <p className="text-sm text-slate-900">改 <code className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">{dataset}.{impact.field}</code>{impact.columns.length === 0 ? ' 不会影响别的列。' : <> 会影响 <b>{impact.columns.length}</b> 个下游列、<b>{impact.jobs.length}</b> 个作业{summary.length > 0 && <>：{summary.join('、')}</>}。</>}</p>
    {impact.columns.length === 0 && <p className="mt-1 text-xs text-slate-500">列血缘只记录了生产者写明的输入列：这一列没有被任何下游引用，或者引用它的作业没有写明。</p>}
    {impact.columns.length > 0 && <ol className="mt-2 space-y-1.5">{[...impact.columns].sort((a, b) => a.distance - b.distance || ORDER.indexOf(a.layer ?? '') - ORDER.indexOf(b.layer ?? '')).map(c => {
      const link = refLink(c.ref)
      return <li key={`${c.dataset}.${c.field}`} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <Chip layer={c.layer} /><span className="font-mono text-slate-900">{c.label}.{c.field}</span>
        <span className="text-slate-500">第 {c.distance} 跳 · 由 {c.derived_from.field} 得来{c.transformation ? `（${c.transformation}）` : ''}</span>
        {link && <Link to={link.to} className="underline">{link.label}</Link>}</li>
    })}</ol>}
    {impact.jobs.length > 0 && <div className="mt-3"><p className="text-xs font-medium text-slate-500">中间经过的作业</p>
      <ul className="mt-1 space-y-1">{impact.jobs.map(j => { const link = refLink(j.ref); return <li key={j.id} className="flex flex-wrap items-center gap-x-2 text-xs"><Chip layer={j.layer} /><span className="font-mono">{j.label}</span>
        <span className="text-slate-500">{j.description}</span>{link && <Link to={link.to} className="underline">{link.label}</Link>}</li> })}</ul></div>}
  </section>
}
