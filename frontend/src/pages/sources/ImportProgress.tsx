import { Link } from 'react-router-dom'
import { CheckCircle2, Circle, Loader2, XCircle } from 'lucide-react'
import type { ImportJob } from '@/api/imports'
import { ReplayPanel } from './ReplayPanel'

const num = (n: number) => n.toLocaleString('zh-CN')

function StepIcon({ status }: { status: string }) {
  if (status === 'done') return <CheckCircle2 size={16} className="text-emerald-600" aria-label="完成" />
  if (status === 'running') return <Loader2 size={16} className="animate-spin text-blue-600" aria-label="进行中" />
  if (status === 'failed') return <XCircle size={16} className="text-red-600" aria-label="失败" />
  return <Circle size={16} className="text-slate-300" aria-label="等待" />
}

export function ImportProgress({ job, onReplace }: { job: ImportJob; onReplace?: () => void }) {
  const result = job.result
  const links = result && !result.conflict_ontology_id ? [
    { to: `/ontologies/${result.ontology_id}?tab=overview`, label: '本体总览' },
    { to: `/ontologies/${result.ontology_id}?tab=entities`, label: '对象类型' },
    { to: `/ontologies/${result.ontology_id}?tab=data_model`, label: '数据模型' },
    { to: `/analysis/health?source=ontology&ontology=${result.ontology_id}`, label: '数据健康' },
    { to: `/analysis/resolution?source=ontology&ontology=${result.ontology_id}`, label: '实体消歧' },
    { to: `/analysis/lineage?source=ontology&ontology=${result.ontology_id}`, label: '数据血缘' },
    { to: `/analysis/pipeline?source=ontology&ontology=${result.ontology_id}`, label: '流水线' },
  ] : []
  return <section aria-label="导入进度" data-testid="import-progress" data-status={job.status} className="wb-surface p-4">
    <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold text-slate-900">{job.title}</h3>
      <span className={`wb-status ${job.status === 'done' ? 'wb-status-success' : job.status === 'failed' ? 'wb-status-danger' : 'wb-status-warning'}`}>{{ queued: '排队中', running: '导入中', done: '导入完成', failed: '导入失败' }[job.status]}</span></div>
    <ol className="mt-3 space-y-1.5">{job.steps.map(step => <li key={step.key} data-testid={`import-step-${step.key}`} data-status={step.status} className="flex items-start gap-2 text-sm">
      <span className="mt-0.5 shrink-0"><StepIcon status={step.status} /></span>
      <span className="min-w-0 flex-1"><span className={step.status === 'pending' ? 'text-slate-400' : 'text-slate-900'}>{step.title}</span>
        {step.detail && <span className="ml-2 text-xs text-slate-500">{step.detail}</span>}</span>
      {step.seconds != null && <span className="shrink-0 text-xs tabular-nums text-slate-400">{step.seconds} 秒</span>}</li>)}</ol>
    {job.error && <p role="alert" className="wb-alert wb-alert-danger mt-3">{job.error}{result?.conflict_ontology_id && <> <Link className="underline" to={`/ontologies/${result.conflict_ontology_id}?tab=overview`}>查看已有的本体</Link>{onReplace && <> · <button type="button" className="underline" onClick={onReplace}>替换它（删除后重新导入）</button></>}</>}</p>}
    {result && !result.conflict_ontology_id && <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50/60 p-3" data-testid="import-result">
      <p className="text-sm text-emerald-900">已建成本体“<b>{result.ontology_name}</b>”：{result.types} 个对象类型、{result.links} 个关系、{num(result.objects)} 个对象、{num(result.link_instances)} 条关系实例。</p>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-emerald-900">{Object.entries(result.counts).sort((a, b) => b[1] - a[1]).map(([name, count]) => <li key={name}><span className="font-mono">{name}</span> {num(count)}</li>)}</ul>
      <p className="mt-3 flex flex-wrap gap-2 text-xs">{links.map(l => <Link key={l.label} to={l.to} className="rounded-md bg-white px-2.5 py-1 text-slate-800 ring-1 ring-emerald-200 hover:bg-emerald-50">{l.label}</Link>)}</p>
      {result.replay && <div className="mt-3"><ReplayPanel ontologyId={result.ontology_id} /></div>}
    </div>}
    {job.log.length > 0 && <details className="mt-3"><summary className="cursor-pointer text-xs text-slate-500">运行记录（{job.log.length} 行）</summary><pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-900 p-3 text-[11px] leading-5 text-slate-100">{job.log.join('\n')}</pre></details>}
  </section>
}
