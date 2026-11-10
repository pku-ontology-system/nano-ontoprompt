import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Images, Merge, Radio, Table2, Upload } from 'lucide-react'
import { formatApiError } from '@/api/client'
import { importApi, type ImportBundle, type ImportJob, type UploadPreview } from '@/api/imports'
import { ImportProgress } from './ImportProgress'
import { useImportJob } from './useImportJob'
import { ReplayPanel } from './ReplayPanel'

const KIND: Record<ImportBundle['kind'], { label: string; icon: typeof Table2 }> = {
  relational: { label: '关系型多表', icon: Table2 }, timeseries: { label: '时序数据', icon: Activity }, events: { label: '动态事件流', icon: Radio },
  multimodal: { label: '多模态', icon: Images }, multisource: { label: '多来源合并', icon: Merge },
}
const MEDIA_LABEL: Record<string, string> = { image: '图片', audio: '音频', video: '视频', pdf: 'PDF', document: '文档', text: '文本', pointcloud: '点云/三维', other: '其他' }
const num = (n: number) => n.toLocaleString('zh-CN')

function BundleCard({ bundle, busy, onImport }: { bundle: ImportBundle; busy: boolean; onImport: (replace: boolean) => void }) {
  const kind = KIND[bundle.kind] ?? KIND.relational
  const Icon = kind.icon
  const imported = bundle.imported
  return <li data-testid={`bundle-${bundle.id}`} data-imported={!!imported} className="wb-surface flex flex-col p-4">
    <div className="flex items-start gap-3">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-slate-700"><Icon size={19} strokeWidth={1.7} /></span>
      <div className="min-w-0 flex-1">
        <h3 className="text-sm font-semibold text-slate-900">{bundle.title}</h3>
        <p className="mt-0.5 text-xs text-slate-500">{kind.label} · {bundle.domain}</p>
      </div>
      {imported && <span className="wb-status wb-status-success">已导入</span>}
    </div>
    <p className="mt-2 text-sm leading-6 text-slate-600">{bundle.summary}</p>
    <ul className="mt-2 flex flex-wrap gap-1.5">{bundle.tags.map(t => <li key={t} className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">{t}</li>)}</ul>
    <ul className="mt-3 list-disc space-y-0.5 pl-5 text-xs text-slate-600">{bundle.shows.map(s => <li key={s}>{s}</li>)}</ul>
    <p className="mt-3 text-[11px] leading-5 text-slate-500">数据来源：{bundle.sources.map((s, i) => <span key={s.id}>{i > 0 ? '；' : ''}{s.url ? <a className="underline" href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}{s.license ? `（${s.license}）` : ''}</span>)}</p>
    <div className="mt-auto pt-4">
      {imported ? <>
        <p className="text-xs text-slate-600">已导入为“{imported.name}”，{num(imported.objects)} 个对象。</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <Link to={`/ontologies/${imported.ontology_id}?tab=overview`} className="wb-button-secondary">本体总览</Link>
          <Link to={`/ontologies/${imported.ontology_id}?tab=data_model`} className="wb-button-secondary">数据模型</Link>
          <Link to={`/analysis/lineage?source=ontology&ontology=${imported.ontology_id}`} className="wb-button-secondary">数据血缘</Link>
          <button type="button" className="wb-button-secondary" disabled={busy} onClick={() => onImport(true)}>重新导入</button>
        </div>
        {bundle.replay && <div className="mt-3"><ReplayPanel ontologyId={imported.ontology_id} /></div>}
      </> : <button type="button" className="wb-button-primary" disabled={busy} data-testid={`import-${bundle.id}`} onClick={() => onImport(false)}>导入这份数据</button>}
    </div>
  </li>
}

function UploadPanel({ onStart, busy }: { onStart: (job: ImportJob) => void; busy: boolean }) {
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [name, setName] = useState('')
  const [stepwise, setStepwise] = useState(false)
  const [table, setTable] = useState('')
  const [order, setOrder] = useState('')
  const [initial, setInitial] = useState(200)
  const upload = useMutation({ mutationFn: (files: File[]) => importApi.upload(files), onSuccess: data => { setName(data.title); setStepwise(false); setTable(''); setOrder('') } })
  const preview: UploadPreview | undefined = upload.data
  const tables = preview?.preview.tables.filter(t => t.kind === 'entity') ?? []
  const chosen = tables.find(t => t.name === (table || tables[0]?.name))
  const orderColumn = order || chosen?.columns.find(c => ['timestamp', 'date'].includes(c.type))?.name || chosen?.columns[0]?.name || ''
  const start = useMutation({
    mutationFn: (replace: boolean) => importApi.importFiles(preview!.upload_id, { ontology_name: name.trim(), replace, replay: stepwise && chosen ? { table: chosen.name, order_by: orderColumn, initial, batch: initial } : null }),
    onSuccess: onStart,
  })
  const pick = (list: FileList | null) => { if (list && list.length) upload.mutate(Array.from(list)) }
  return <div>
    <div role="button" tabIndex={0} aria-label="选择或拖入文件" data-testid="dropzone" onClick={() => input.current?.click()} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') input.current?.click() }}
      onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files) }}
      className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border-2 border-dashed px-4 py-8 text-center ${dragging ? 'border-slate-700 bg-slate-50' : 'border-slate-300 hover:bg-slate-50'}`}>
      <Upload size={22} className="text-slate-500" />
      <p className="text-sm font-medium text-slate-800">{upload.isPending ? '上传并分析中…' : '把文件拖到这里，或点击选择'}</p>
      <p className="text-xs text-slate-500">任何类型都可以：CSV、Excel、JSON、Parquet 会读成表；图片、音频、视频、PDF、文档等会记成文件资产；ZIP 会先展开。</p>
      <input ref={input} type="file" multiple hidden aria-label="选择文件" data-testid="file-input" onChange={e => { pick(e.target.files); e.target.value = '' }} />
    </div>
    {upload.error && <p role="alert" className="wb-alert wb-alert-danger mt-3">{formatApiError(upload.error)}</p>}
    {preview && <div className="mt-4 space-y-3" data-testid="upload-preview">
      <div className="grid gap-3 md:grid-cols-2">
        {preview.preview.tables.map(t => <div key={t.name} className="rounded-lg border border-slate-200 p-3 text-sm">
          <p className="font-medium text-slate-900">{t.label || t.name} <span className="ml-1 text-xs font-normal text-slate-500">→ 对象类型 {t.type_name} · {num(t.rows)} 行 · {t.columns.length} 列</span></p>
          <p className="mt-1 text-xs text-slate-500">{t.kind === 'asset' ? '文件资产' : t.primary_key.length ? `主键 ${t.primary_key.map(k => t.columns.find(c => c.name === k)?.label || k).join(' + ')}` : '没有找到唯一的键，按行号编号'}</p>
          <p className="mt-1 truncate text-[11px] text-slate-500" title={t.columns.map(c => `${c.label || c.name}（${c.name}）:${c.type}`).join('，')}>{t.columns.slice(0, 8).map(c => c.label || c.name).join('，')}{t.columns.length > 8 ? ' …' : ''}</p>
        </div>)}
      </div>
      {Object.keys(preview.preview.assets).length > 0 && <p className="text-sm text-slate-700">文件资产：{Object.entries(preview.preview.assets).map(([k, v]) => `${MEDIA_LABEL[k] ?? k} ${v} 个`).join('，')}</p>}
      {preview.preview.links.length > 0 && <div><p className="text-xs font-medium text-slate-500">识别出的关系（按名字和取值推断）</p>
        <ul className="mt-1 space-y-0.5 text-xs text-slate-700">{preview.preview.links.map(l => <li key={l.name}><span className="font-mono">{l.from}</span> → <span className="font-mono">{l.to}</span>{l.coverage != null ? `（${Math.round(l.coverage * 100)}% 的值找得到）` : ''}</li>)}</ul></div>}
      <div className="flex flex-wrap items-end gap-4">
        <label className="text-sm text-slate-700">本体名称
          <input aria-label="本体名称" value={name} onChange={e => setName(e.target.value)} className="wb-input mt-1 w-72" maxLength={200} /></label>
        {tables.length > 0 && <label className="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={stepwise} onChange={e => setStepwise(e.target.checked)} />把一张表当事件流，按顺序逐步载入</label>}
      </div>
      {stepwise && chosen && <div className="flex flex-wrap items-end gap-4 text-sm text-slate-700">
        <label>事件表<select aria-label="事件表" className="wb-input mt-1 block w-auto" value={chosen.name} onChange={e => { setTable(e.target.value); setOrder('') }}>{tables.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}</select></label>
        <label>按哪一列排序<select aria-label="排序列" className="wb-input mt-1 block w-auto" value={orderColumn} onChange={e => setOrder(e.target.value)}>{chosen.columns.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}</select></label>
        <label>每批条数<input type="number" aria-label="每批条数" min={1} max={5000} value={initial} onChange={e => setInitial(Math.max(1, Number(e.target.value) || 1))} className="wb-input mt-1 block w-24" /></label>
      </div>}
      {preview.preview.notes.length > 0 && <details><summary className="cursor-pointer text-xs text-slate-500">分析时的说明（{preview.preview.notes.length} 条）</summary><ul className="mt-1 list-disc pl-5 text-xs text-slate-600">{preview.preview.notes.map((n, i) => <li key={i}>{n}</li>)}</ul></details>}
      <div className="flex items-center gap-3"><button type="button" className="wb-button-primary" disabled={!name.trim() || start.isPending || busy} data-testid="start-upload-import" onClick={() => start.mutate(false)}>开始导入</button>
        {start.error && <span role="alert" className="text-sm text-red-700">{formatApiError(start.error)}</span>}</div>
    </div>}
  </div>
}

export default function DataSourcesPage({ embedded = false }: { embedded?: boolean }) {
  const queryClient = useQueryClient()
  const bundles = useQuery({ queryKey: ['import-bundles'], queryFn: importApi.bundles })
  const [jobId, setJobId] = useState<string | null>(null)
  const job = useImportJob(jobId)
  const [lastBundle, setLastBundle] = useState<{ id: string } | null>(null)
  const running = job.data ? ['queued', 'running'].includes(job.data.status) : false
  const refreshed = useRef<string | null>(null)
  useEffect(() => {
    if (job.data?.status === 'done' && refreshed.current !== job.data.id) {
      refreshed.current = job.data.id
      void queryClient.invalidateQueries({ queryKey: ['import-bundles'] })
      void queryClient.invalidateQueries({ queryKey: ['ontologies'] })
      void queryClient.invalidateQueries({ queryKey: ['platform-overview'] })
    }
  }, [job.data, queryClient])
  const start = useMutation({ mutationFn: ({ id, replace }: { id: string; replace: boolean }) => importApi.importBundle(id, replace), onSuccess: (data, vars) => { setLastBundle({ id: vars.id }); setJobId(data.id) } })
  const begin = (job_: ImportJob) => { setLastBundle(null); setJobId(job_.id) }
  return <div className={embedded ? 'space-y-6' : 'wb-page mx-auto max-w-6xl space-y-6 p-6 pb-16'}>
    {!embedded && <header>
      <p className="wb-eyebrow">数据接入</p>
      <h1 className="wb-page-title mt-1">数据源</h1>
      <p className="wb-page-subtitle">把数据导入成本体：表、事件流、图片和文档都可以。导入时会识别类型、主键和关系，发布成一个新的本体，导入后在本体总览、对象类型、数据模型和数据分析里都能看到。</p>
    </header>}

    {jobId && job.data && <ImportProgress job={job.data} onReplace={lastBundle ? () => start.mutate({ id: lastBundle.id, replace: true }) : undefined} />}
    {start.error && <p role="alert" className="wb-alert wb-alert-danger">{formatApiError(start.error)}</p>}

    <section className="wb-surface p-5" data-testid="bundles">
      <h2 className="wb-section-title">演示数据集</h2>
      <p className="mt-0.5 text-sm text-slate-500">来自不同来源、不同类型的数据：一键导入，建成可以直接分析的本体。</p>
      <ul className="mt-4 grid items-start gap-4 lg:grid-cols-2">{(bundles.data ?? []).map(b => <BundleCard key={b.id} bundle={b} busy={running || start.isPending} onImport={replace => start.mutate({ id: b.id, replace })} />)}</ul>
      {bundles.isLoading && <p className="mt-3 text-sm text-slate-500">读取中…</p>}
      {bundles.error && <p role="alert" className="wb-alert wb-alert-danger mt-3">{formatApiError(bundles.error)}</p>}
    </section>

    <section className="wb-surface p-5" data-testid="upload">
      <h2 className="wb-section-title">导入你自己的数据</h2>
      <p className="mt-0.5 text-sm text-slate-500">没有固定的格式要求。读完先给你看识别出了什么，确认后才写入。</p>
      <div className="mt-4"><UploadPanel onStart={begin} busy={running} /></div>
    </section>

    <details className="wb-surface p-5">
      <summary className="cursor-pointer text-sm font-medium text-slate-800">专用格式的导入器</summary>
      <p className="mt-2 text-xs text-slate-500">下面三个是为某种固定格式做的旧导入器，只在你的数据正好是这种格式时用；一般情况请用上面的导入。</p>
      <ul className="mt-2 space-y-1 text-sm"><li><Link className="underline" to="/data/temporal">时序数据构建</Link>（FactoryNet 的序列格式）</li><li><Link className="underline" to="/data/multimodal">多模态构建</Link>（RGB-D、掩码、点云样本包）</li>
        <li><Link className="underline" to="/data/dynamic?new=1">动态数据演示</Link>（FactoryNet 逐事件构建）</li></ul>
    </details>
  </div>
}
