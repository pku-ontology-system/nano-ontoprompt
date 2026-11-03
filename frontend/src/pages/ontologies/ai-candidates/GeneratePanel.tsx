import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import type { CandidateSet, RunnerInfo } from './types'

type Props = { ontologyId: string; runner: RunnerInfo | undefined; loading: boolean; busy: boolean; onCreated: (set: CandidateSet) => void }

export default function GeneratePanel({ ontologyId, runner, loading, busy, onCreated }: Props) {
  const base = `/ontologies/${ontologyId}/ai-candidates`
  const [pickedSource, setPickedSource] = useState('')
  const [pickedModel, setPickedModel] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [example, setExample] = useState('')

  const sources = runner?.sources ?? []
  const models = runner?.models ?? []
  const source = sources.find(row => row.id === pickedSource) ?? sources[0]
  const model = models.find(row => row.id === pickedModel) ?? models.find(row => row.available) ?? models[0]
  const cloud = model?.locality === 'cloud'
  const blocked = cloud && source?.privacy_level === 'private'
  const exampleId = example || runner?.examples[0]?.id || ''

  const generate = useMutation({
    mutationFn: () => apiClientV2.post<CandidateSet>(`${base}:generate`, { source_id: source!.id, model_id: model!.id, acknowledge_cloud: acknowledged }),
    onSuccess: onCreated,
  })
  const importExample = useMutation({
    mutationFn: () => apiClientV2.post<CandidateSet>(`${base}:import`, { example_id: exampleId }),
    onSuccess: onCreated,
  })
  const importFile = useMutation({
    mutationFn: async (file: File) => apiClientV2.post<CandidateSet>(`${base}:import`, { document: await file.text(), title: file.name }),
    onSuccess: onCreated,
  })
  const error = generate.error ?? importExample.error ?? importFile.error

  const canGenerate = Boolean(runner?.reachable && source && model?.available && !blocked && (!cloud || acknowledged) && !busy && !generate.isPending)

  return <section className="wb-surface p-4" data-testid="ai-generate-panel" aria-label="生成候选">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p className="wb-eyebrow">一键生成</p>
        <h2 className="mt-1 text-base font-semibold">用 Auto Ontology 从关系库生成候选</h2>
        <p className="mt-1 max-w-2xl text-sm text-slate-600">读取登记的关系库，生成对象类型、列属性和外键候选。结果不会直接改动本体，需要逐条审阅后才能发布。</p>
      </div>
      {runner?.reachable && <p className="text-xs text-slate-500" data-testid="runner-status">运行器在线 · Auto Ontology {runner.health?.auto_ontology.version}（{runner.health?.auto_ontology.commit}）</p>}
    </div>

    {loading && <p role="status" className="mt-3 text-sm text-slate-500">正在检查 Auto Ontology 运行器…</p>}
    {!loading && runner && !runner.reachable && <p role="status" className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-800" data-testid="runner-offline">
      {runner.reason ?? '运行器不可用'}。仍然可以导入 Auto Ontology 的导出文件，或使用内置样例。</p>}

    {runner?.reachable && <div className="mt-4 grid gap-3 md:grid-cols-2">
      <label className="text-sm font-medium">来源库
        <select className="wb-input mt-1" aria-label="来源库" value={source?.id ?? ''} onChange={event => setPickedSource(event.target.value)}>
          {sources.map(row => <option key={row.id} value={row.id}>{row.label}{row.privacy_level === 'private' ? '（私有）' : ''}</option>)}
        </select>
      </label>
      <label className="text-sm font-medium">生成模型
        <select className="wb-input mt-1" aria-label="生成模型" value={model?.id ?? ''} onChange={event => { setPickedModel(event.target.value); setAcknowledged(false) }}>
          {models.map(row => <option key={row.id} value={row.id} disabled={!row.available}>{row.label} · {row.locality === 'cloud' ? '云端' : '本机'}{row.available ? '' : '（不可用）'}</option>)}
        </select>
      </label>
      {source && <div className="md:col-span-2" aria-label="将读取的表">
        <p className="text-xs text-slate-500">将读取 {source.tables.length} 张表（只读）：</p>
        <ul className="mt-1 flex flex-wrap gap-1.5">{source.tables.map(table => <li key={table.name} className="rounded-md border bg-slate-50 px-2 py-0.5 text-xs" title={table.columns.join('、')}>{table.name}<span className="text-slate-400"> · {table.columns.length} 列</span></li>)}</ul>
      </div>}
      {cloud && model && <div className="md:col-span-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm" data-testid="cloud-notice">
        <p className="font-medium text-amber-900">“{model.label}”是云端模型，这次运行会把以下内容发送到云端：</p>
        <ul className="mt-1 list-disc pl-5 text-amber-900">{model.outbound.map(item => <li key={item}>{item}</li>)}</ul>
        <p className="mt-1 text-xs text-amber-800">连接串和模型密钥不会离开本机运行器；私有来源会被拒绝使用云端模型。</p>
        {blocked
          ? <p className="mt-2 font-medium text-red-700" role="alert">来源库“{source?.label}”是私有来源，不能使用云端模型，请改用本机模型。</p>
          : <label className="mt-2 flex items-center gap-2 font-medium"><input type="checkbox" aria-label="确认发送到云端" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />我确认可以把上述内容发送到“{model.label}”</label>}
      </div>}
      <div className="md:col-span-2">
        <button type="button" className="wb-button-primary" disabled={!canGenerate} onClick={() => generate.mutate()}>{generate.isPending ? '正在启动…' : '用 Auto Ontology 生成'}</button>
        {busy && <span className="ml-3 text-xs text-slate-500">有一次生成正在运行，请等它结束。</span>}
      </div>
    </div>}

    <div className="mt-5 border-t pt-4">
      <p className="text-sm font-medium">或者导入已有的导出文件</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select className="wb-input w-auto min-w-72" aria-label="内置样例" value={exampleId} onChange={event => setExample(event.target.value)}>
          {(runner?.examples ?? []).map(row => <option key={row.id} value={row.id}>{row.label}</option>)}
        </select>
        <button type="button" className="wb-button-secondary" disabled={!exampleId || importExample.isPending} onClick={() => importExample.mutate()}>导入样例</button>
        <label className="wb-button-secondary cursor-pointer">选择 YAML 文件
          <input type="file" accept=".yaml,.yml" className="sr-only" aria-label="选择导出文件" disabled={importFile.isPending}
            onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) importFile.mutate(file) }} />
        </label>
      </div>
    </div>
    {error && <p role="alert" className="mt-3 text-sm text-red-700">{formatApiError(error)}</p>}
  </section>
}
