import { useEffect, useState } from 'react'
import type { CandidateSet, StageRow } from './types'

function useClock(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}

const seconds = (value: number) => value < 10 ? `${value.toFixed(1)} 秒` : value < 120 ? `${Math.round(value)} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`

function duration(row: StageRow, now: number): string {
  if (!row.started_at) return ''
  const end = row.finished_at ?? now / 1000
  return seconds(Math.max(0, end - row.started_at))
}

const mark: Record<StageRow['status'], string> = { pending: '○', running: '◐', done: '●', failed: '✕' }

export default function ProgressPanel({ set, onCancel, cancelling }: { set: CandidateSet; onCancel: () => void; cancelling: boolean }) {
  const generating = set.status === 'generating'
  const now = useClock(generating)
  const progress = set.progress
  const stages = progress.stages ?? []
  const tables = progress.tables ?? { total: 0, done: 0 }
  const started = progress.started_at ?? null
  const elapsed = started ? seconds(Math.max(0, (progress.finished_at ?? now / 1000) - started)) : ''

  const folded = !generating && (set.status === 'review' || set.status === 'published')
  const Wrapper = folded ? 'details' : 'section'
  return <Wrapper className="wb-surface p-4" data-testid="ai-progress" aria-label="生成进度" aria-live="polite">
    {folded && <summary className="cursor-pointer text-sm font-medium">生成过程{elapsed ? `（共 ${elapsed}${progress.llm_calls ? `，${progress.llm_calls} 次模型调用` : ''}）` : ''}</summary>}
    <div className={`flex flex-wrap items-center justify-between gap-3 ${folded ? 'mt-3' : ''}`}>
      <div>
        <p className="wb-eyebrow">{generating ? '正在生成' : '生成记录'}</p>
        <p className="mt-1 text-sm text-slate-600">
          {generating ? '候选还没有写入审阅队列；完成后会自动出现。' : '这次生成的各阶段耗时。'}
          {elapsed && <span className="ml-2 text-slate-500">已用 {elapsed}</span>}
          {progress.llm_calls ? <span className="ml-2 text-slate-500">· 模型调用 {progress.llm_calls} 次</span> : null}
        </p>
      </div>
      {generating && <button type="button" className="wb-button-secondary" disabled={cancelling} onClick={onCancel}>取消生成</button>}
    </div>
    {progress.runner_unreachable && <p role="status" className="mt-2 text-sm text-amber-700">暂时连不上运行器，会自动重试：{progress.runner_unreachable}</p>}
    <ol className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {stages.map(row => <li key={row.key} data-stage={row.key} data-status={row.status}
        className={`rounded-lg border px-3 py-2 text-sm ${row.status === 'running' ? 'border-blue-300 bg-blue-50' : row.status === 'done' ? 'bg-slate-50' : row.status === 'failed' ? 'border-red-300 bg-red-50' : 'opacity-60'}`}>
        <div className="flex items-center justify-between gap-2"><span><span aria-hidden="true" className="mr-1.5">{mark[row.status]}</span>{row.label}</span><span className="text-xs text-slate-500">{duration(row, now)}</span></div>
        {row.key === 'terms' && tables.total > 0 && row.status !== 'pending' && <div className="mt-1.5">
          <div className="h-1.5 overflow-hidden rounded bg-slate-200"><div className="h-full bg-blue-500 transition-all" style={{ width: `${Math.round((tables.done / tables.total) * 100)}%` }} /></div>
          <p className="mt-1 text-xs text-slate-500">{tables.done} / {tables.total} 张表</p>
        </div>}
      </li>)}
    </ol>
    {generating && progress.detail && <p className="mt-3 truncate text-xs text-slate-500" title={progress.detail}>{progress.detail}</p>}
    {set.error && <p role="alert" className="mt-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{set.status === 'cancelled' ? '已取消' : '生成失败'}：{set.error}</p>}
    {(progress.log_tail?.length ?? 0) > 0 && <details className="mt-3">
      <summary className="cursor-pointer text-xs text-slate-500">运行日志（最近 {progress.log_tail?.length} 行）</summary>
      <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-slate-900 p-3 text-[11px] leading-5 text-slate-100">{progress.log_tail?.join('\n')}</pre>
    </details>}
  </Wrapper>
}
