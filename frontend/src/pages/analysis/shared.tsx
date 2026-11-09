import type { ReactNode } from 'react'
import { formatApiError } from '@/api/client'
import type { DemoDataset } from '@/api/analysis'
import { cell, num, size } from './format'

export function Stat({ label, value, hint, tone = 'plain' }: { label: string; value: ReactNode; hint?: ReactNode; tone?: 'plain' | 'good' | 'bad' | 'warn' }) {
  const color = { plain: 'text-slate-900', good: 'text-emerald-700', bad: 'text-red-700', warn: 'text-amber-700' }[tone]
  return <div className="wb-surface px-4 py-3">
    <p className="text-xs text-slate-500">{label}</p>
    <p className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</p>
    {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
  </div>
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  return <p role="alert" className="wb-alert wb-alert-danger">{formatApiError(error)}</p>
}

export function SourceNote({ dataset }: { dataset: DemoDataset }) {
  return <p className="text-xs text-slate-500">
    来自 <a className="underline" href={dataset.source.url} target="_blank" rel="noreferrer">{dataset.source.project}</a>（{dataset.source.license}）· 文件 {dataset.file}
    {dataset.bytes ? ` · ${size(dataset.bytes)}` : ''}{dataset.rows ? ` · ${num(dataset.rows)} 行` : ''}
  </p>
}

/** The demo datasets of one tool as selectable cards: which one is the project's default, which shows it at its best. */
export function DatasetCards({ datasets, value, onChange, badge }: { datasets: DemoDataset[]; value: string; onChange: (id: string) => void; badge?: (d: DemoDataset) => string | null }) {
  return <div role="radiogroup" aria-label="演示数据" className="grid gap-3 md:grid-cols-2">
    {datasets.map(d => {
      const selected = d.id === value
      const tag = badge?.(d)
      return <button key={d.id} type="button" role="radio" aria-checked={selected} disabled={!d.available} data-testid={`dataset-${d.id}`} onClick={() => onChange(d.id)}
        className={`wb-choice-card text-left disabled:opacity-50 ${selected ? 'ring-2 ring-[var(--pku-red-action)]' : ''}`}>
        <span className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-slate-900">{d.title}</span>
          {tag && <span className="wb-status wb-status-success">{tag}</span>}
          {!d.available && <span className="wb-status wb-status-warning">{d.problem ?? '文件还没放进来'}</span>}
        </span>
        <span className="mt-1.5 block text-sm text-slate-600">{d.description}</span>
        <span className="mt-2 block"><SourceNote dataset={d} /></span>
      </button>
    })}
  </div>
}

/** A bar that grows right for positive values and left for negative ones. */
export function Diverging({ value, max, label }: { value: number; max: number; label?: string }) {
  const width = Math.min(50, (Math.abs(value) / Math.max(max, 1e-9)) * 50)
  return <div className="relative h-4 w-full" role="img" aria-label={label ?? `${value}`}>
    <div className="absolute inset-y-0 left-1/2 w-px bg-slate-300" />
    <div className={`absolute inset-y-0.5 rounded-sm ${value >= 0 ? 'bg-emerald-500' : 'bg-rose-500'}`} style={value >= 0 ? { left: '50%', width: `${width}%` } : { right: '50%', width: `${width}%` }} />
  </div>
}

export function PageHeader({ title, subtitle, credit }: { title: string; subtitle: string; credit?: ReactNode }) {
  return <header>
    <p className="wb-eyebrow">数据分析</p>
    <h1 className="wb-page-title mt-1">{title}</h1>
    <p className="wb-page-subtitle">{subtitle}</p>
    {credit && <p className="mt-1 text-xs text-slate-500">{credit}</p>}
  </header>
}

export function Section({ title, hint, children, testId }: { title: string; hint?: ReactNode; children: ReactNode; testId?: string }) {
  return <section className="wb-surface p-5" data-testid={testId}>
    <h2 className="wb-section-title">{title}</h2>
    {hint && <p className="mt-0.5 text-sm text-slate-500">{hint}</p>}
    <div className="mt-4">{children}</div>
  </section>
}

export function RecordTable({ rows, columns }: { rows: Record<string, unknown>[]; columns?: string[] }) {
  const cols = columns ?? Object.keys(rows[0] ?? {})
  return <div className="overflow-x-auto"><table className="w-full text-left text-xs">
    <thead><tr className="text-slate-500">{cols.map(c => <th key={c} className="whitespace-nowrap px-2 py-1 font-medium">{c}</th>)}</tr></thead>
    <tbody>{rows.map((row, i) => <tr key={i} className="border-t border-slate-100">{cols.map(c => <td key={c} className="whitespace-nowrap px-2 py-1 text-slate-800">{cell(row[c])}</td>)}</tr>)}</tbody>
  </table></div>
}
