import { useState, useEffect, useRef } from 'react'
import {
  Play, Plus, GitBranch, CheckCircle, Clock, XCircle,
  ChevronDown, ChevronUp, Database, FileText, Layers,
  Scissors, FileJson, Cpu, BarChart3, AlertTriangle
} from 'lucide-react'
import { apiClientV2 } from '@/api/client'
import pipelinesApi, { waitForPipelineRun, type RunDetail as PipelineRunDetail } from '@/api/v2/pipelines'

interface PipelineRun {
  id: string
  status: string
  started_at: string | null
  finished_at: string | null
}

interface Pipeline {
  id: string
  name: string
  route: string
  status: string
}

interface WideTableSplit {
  skipped?: boolean
  executed?: boolean
  suggested?: boolean
  col_count?: number
  tables?: Record<string, number>
  suggestion?: { split_config?: Record<string, unknown> }
}

interface PipelineStatsMeta {
  inferred_schema?: Record<string, string>
  dropped?: number
  rows_before?: number
  rows_after?: number
  wide_table_split?: WideTableSplit
  json_flatten?: { rows_before?: number; rows_after?: number }
  document_to_md?: { processed?: number; strategy?: string }
  md_to_structured?: {
    skipped?: boolean
    success?: number
    processed?: number
    reason?: string
    method?: string
    requested_method?: string | null
    effective_method?: string | null
    fallback_policy?: string | null
    fallback_reason?: string | string[] | null
    status?: string
  }
}

interface RunDetail {
  id: string
  status: string
  dispatch_status?: string | null
  dispatch_attempts?: number
  execution_mode?: string | null
  execution_digest?: string | null
  pipeline_version_id?: string | null
  source_versions?: Array<{
    dataset_id: string | null
    dataset_version_id: string | null
    version_no: number | null
    checksum: string | null
  }>
  llm_model_pin?: {
    requested: boolean
    model_config_id: string | null
    model_config_digest: string | null
    provider: string | null
    model_name: string | null
    prompt_provenance: Record<string, { prompt_id: string; prompt_digest: string }>
    provider_model_artifact_reproducibility: string
  } | null
  stats: {
    rows_in: number
    rows_out: number
    meta: PipelineStatsMeta
    curated_dataset_id: string | null
  } | null
  llm_invocations?: Array<{
    id: string
    attempt_number: number | null
    source_dataset_version_id: string | null
    phase: string | null
    provider: string | null
    model_name: string
    model_config_digest: string | null
    prompt_id: string | null
    prompt_digest: string | null
    request_hash: string | null
    response_hash: string | null
    status: string
    error_code: string | null
    duration_ms: number | null
    provider_model_artifact_reproducibility?: string
    event_chain_status?: 'verified' | 'invalid' | 'unavailable'
    events?: Array<{
      id: string
      sequence: number
      event_type: string
      status: string
      error_code: string | null
      request_hash: string | null
      response_hash: string | null
      previous_digest: string | null
      event_digest: string
      occurred_at: string | null
    }>
  }>
  error_log: string | null
}

// ── 步骤卡片定义 ────────────────────────────────────────────────────────────

interface StepCard {
  key: string
  label: string
  icon: React.ReactNode
  status: 'done' | 'skipped' | 'failed' | 'pending' | 'degraded'
  detail: string
}

function describeExtractionMethod(method?: string | null): string {
  const labels: Record<string, string> = {
    llm_auto: 'LLM 自动提取',
    llm_schema: 'LLM 按字段提取',
    llm_enrich: 'LLM 语义增强',
    rule_based: '规则提取',
    none: '未执行',
    mixed: '多种方式',
  }
  return method
    ? method.split('+').map(part => labels[part] || part).join(' + ')
    : '未执行'
}

function describeExtractionReason(reason?: string | string[] | null): string {
  const labels: Record<string, string> = {
    model_unavailable: '没有可用的模型配置',
    provider_call_failed: '模型服务调用失败',
    empty_response: '模型返回空响应',
    invalid_response: '模型返回了无效 JSON',
    invalid_config: '语义增强字段配置无效',
    no_markdown_text: '没有可提取的 Markdown 文本',
    no_records_to_enrich: '没有可供增强的记录',
  }
  const items = Array.isArray(reason) ? reason : reason ? [reason] : []
  return items.map(item => labels[item] || item).join('；')
}

function buildSteps(route: string, stats: RunDetail['stats'] | null): StepCard[] {
  if (!stats) return []
  const meta = stats.meta || {}

  if (route === 'A') {
    const schema = meta.inferred_schema || {}
    const colCount = Object.keys(schema).length
    const dropped = meta.dropped ?? 0
    const split = meta.wide_table_split

    const steps: StepCard[] = [
      {
        key: 'schema',
        label: 'Schema 推断',
        icon: <Layers size={13} />,
        status: colCount > 0 ? 'done' : 'skipped',
        detail: colCount > 0
          ? `${colCount} 列：${Object.entries(schema).slice(0, 3).map(([k, v]) => `${k}(${v})`).join(', ')}${colCount > 3 ? '…' : ''}`
          : '无 schema 数据',
      },
      {
        key: 'clean',
        label: '数据清洗',
        icon: <CheckCircle size={13} />,
        status: 'done',
        detail: `${meta.rows_before ?? stats.rows_in} → ${meta.rows_after ?? stats.rows_out} 行，去重丢弃 ${dropped} 行`,
      },
    ]

    if (split && !split.skipped) {
      const tables = split.tables || {}
      steps.push({
        key: 'split',
        label: '宽表拆分',
        icon: <Scissors size={13} />,
        status: split.executed ? 'done' : split.suggested ? 'done' : 'skipped',
        detail: split.executed
          ? `拆分为 ${Object.keys(tables).length} 张子表：${Object.entries(tables).map(([n, c]) => `${n}(${c}行)`).join(', ')}`
          : split.suggested
          ? `建议拆分（待用户确认）：${Object.keys(split.suggestion?.split_config || {}).join(', ')}`
          : `跳过（${split.col_count} 列 < 80列阈值）`,
      })
    }

    steps.push({
      key: 'output',
      label: '输出 Curated',
      icon: <Database size={13} />,
      status: stats.curated_dataset_id ? 'done' : 'failed',
      detail: stats.curated_dataset_id
        ? `${stats.rows_out} 行 → 待审核 Curated Dataset (${stats.curated_dataset_id.slice(0, 8)})`
        : `生成失败`,
    })

    return steps
  }

  if (route === 'B') {
    const flatten = meta.json_flatten || {}
    return [
      {
        key: 'parse',
        label: 'JSON/XML 解析',
        icon: <FileJson size={13} />,
        status: 'done',
        detail: '检测嵌套结构，准备 Flatten',
      },
      {
        key: 'flatten',
        label: 'Flatten 摊平',
        icon: <Layers size={13} />,
        status: flatten.rows_after !== undefined ? 'done' : 'skipped',
        detail: flatten.rows_before !== undefined
          ? `${flatten.rows_before} → ${flatten.rows_after} 行（数组 explode）`
          : '未执行',
      },
      {
        key: 'clean',
        label: '数据清洗',
        icon: <CheckCircle size={13} />,
        status: 'done',
        detail: `${meta.rows_before ?? stats.rows_in} → ${meta.rows_after ?? stats.rows_out} 行，去重 ${meta.dropped ?? 0} 行`,
      },
      {
        key: 'output',
        label: '输出 Curated',
        icon: <Database size={13} />,
        status: stats.curated_dataset_id ? 'done' : 'failed',
        detail: stats.curated_dataset_id
          ? `${stats.rows_out} 行 → 待审核 Curated Dataset (${stats.curated_dataset_id.slice(0, 8)})`
          : '生成失败（序列化错误）',
      },
    ]
  }

  if (route === 'C') {
    const doc = meta.document_to_md || {}
    const extract = meta.md_to_structured || {}
    const processedDocuments = doc.processed ?? 0
    const successfulExtractions = extract.success ?? 0
    const extractionStatus = extract.status === 'failed' ? 'failed'
      : extract.status === 'degraded' ? 'degraded'
      : extract.skipped || extract.status === 'skipped' ? 'skipped'
      : extract.status === 'success' ? 'done'
      : successfulExtractions > 0 ? 'done' : 'failed'
    const requestedMethod = extract.requested_method || extract.method || '结构化提取'
    const effectiveMethod = extract.effective_method || extract.method || '未知'
    const displayFallbackReasons = describeExtractionReason(extract.fallback_reason)
    const requestedLabel = describeExtractionMethod(requestedMethod)
    const effectiveLabel = describeExtractionMethod(effectiveMethod)
    const extractionDetail = extract.status === 'degraded'
      ? `请求 ${requestedLabel}，实际使用 ${effectiveLabel}；策略：${extract.fallback_policy || 'rule_based'}；原因：${displayFallbackReasons || 'LLM 不可用'}。输出需人工审核。`
      : extract.status === 'failed'
      ? `请求 ${requestedLabel}，Run 已停止；原因：${displayFallbackReasons || 'LLM 提取失败'}。`
      : extract.status === 'skipped'
      ? `请求 ${requestedLabel}，未执行：${displayFallbackReasons || extract.reason || '没有可提取的文本'}。`
      : `请求 ${requestedLabel}，实际使用 ${effectiveLabel}；成功 ${successfulExtractions}/${extract.processed ?? 0} 条`
    return [
      {
        key: 'doc',
        label: '文档 → Markdown',
        icon: <FileText size={13} />,
        status: processedDocuments > 0 ? 'done' : 'skipped',
        detail: processedDocuments > 0
          ? `策略: ${doc.strategy || 'markitdown'}，处理 ${processedDocuments} 个文档`
          : '未处理（无文档行）',
      },
      {
        key: 'extract',
        label: '结构化提取',
        icon: <Cpu size={13} />,
        status: extractionStatus,
        detail: extract.skipped
          ? `已跳过：${extract.reason || '无 target_schema'}（旧版本）`
          : extract.requested_method || extract.method
          ? extractionDetail
          : '未执行',
      },
      {
        key: 'output',
        label: '输出 Curated',
        icon: <Database size={13} />,
        status: stats.curated_dataset_id ? 'done' : 'failed',
        detail: stats.curated_dataset_id
          ? `${stats.rows_out} 行 → 待审核 Curated Dataset (${stats.curated_dataset_id.slice(0, 8)})`
          : '生成失败',
      },
    ]
  }

  return []
}

// ── 状态样式 ────────────────────────────────────────────────────────────────

const STEP_STATUS_STYLE: Record<string, string> = {
  done:    'bg-green-50 border-green-200 text-green-700',
  degraded:'bg-amber-50 border-amber-200 text-amber-800',
  skipped: 'bg-gray-50 border-gray-200 text-gray-500',
  failed:  'bg-red-50 border-red-200 text-red-600',
  pending: 'bg-blue-50 border-blue-200 text-blue-600',
}

const STEP_STATUS_ICON: Record<string, React.ReactNode> = {
  done:    <CheckCircle size={12} className="text-green-500 flex-shrink-0" />,
  degraded:<AlertTriangle size={12} className="text-amber-600 flex-shrink-0" />,
  skipped: <Clock size={12} className="text-gray-400 flex-shrink-0" />,
  failed:  <XCircle size={12} className="text-red-400 flex-shrink-0" />,
  pending: <Clock size={12} className="text-blue-400 flex-shrink-0" />,
}

const ROUTE_STYLE: Record<string, string> = {
  A: 'bg-blue-50 text-blue-700 border-blue-200',
  B: 'bg-amber-50 text-amber-700 border-amber-200',
  C: 'bg-purple-50 text-purple-700 border-purple-200',
}

const ROUTE_LABEL: Record<string, string> = {
  A: 'Route A · 结构化',
  B: 'Route B · 半结构化',
  C: 'Route C · 非结构化',
}

// ── 主组件 ──────────────────────────────────────────────────────────────────

export default function TransformsTab() {
  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState<string | null>(null)
  const [activeRun, setActiveRun] = useState<{ pipelineId: string; runId: string } | null>(null)
  const runController = useRef<AbortController | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [runDetails, setRunDetails] = useState<Record<string, RunDetail | null>>({})

  const [showCreate, setShowCreate] = useState(false)
  const [datasets, setDatasets] = useState<Array<{id: string; name: string; kind: string}>>([])
  const [createName, setCreateName] = useState('')
  const [createDatasetId, setCreateDatasetId] = useState('')
  const [createRoute, setCreateRoute] = useState<'A'|'B'|'C'>('A')
  const [createError, setCreateError] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    apiClientV2.get<Pipeline[] | { data?: Pipeline[] }>('/pipelines')
      .then(res => setPipelines(Array.isArray(res) ? res : res.data ?? []))
      .catch(() => setPipelines([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => () => runController.current?.abort(), [])

  useEffect(() => {
    if (!activeRun) return
    const controller = new AbortController()
    runController.current = controller
    void waitForPipelineRun(activeRun.runId, {
      signal: controller.signal,
      onUpdate: run => setRunDetails(previous => ({ ...previous, [activeRun.pipelineId]: run as RunDetail })),
    }).then(run => {
      if (!run || controller.signal.aborted) return
      setRunDetails(previous => ({ ...previous, [activeRun.pipelineId]: run as RunDetail }))
      setPipelines(previous => previous.map(pipeline => pipeline.id === activeRun.pipelineId
        ? { ...pipeline, status: run.status } : pipeline))
    }).finally(() => {
      if (!controller.signal.aborted) {
        setRunning(null)
        setActiveRun(null)
      }
    })
    return () => controller.abort()
  }, [activeRun])

  useEffect(() => {
    if (!showCreate) return
    apiClientV2.get<Array<{id: string; name: string; kind: string}>>('/datasets')
      .then(res => setDatasets(Array.isArray(res) ? res.filter(d => d.kind !== 'curated') : []))
      .catch(() => setDatasets([]))
  }, [showCreate])

  const handleCreate = async () => {
    if (!createName.trim()) { setCreateError('请填写管道名称'); return }
    if (!createDatasetId) { setCreateError('请选择数据源'); return }
    setCreating(true)
    try {
      await apiClientV2.post('/pipelines', { name: createName, source_dataset_id: createDatasetId, route: createRoute })
      setShowCreate(false)
      setCreateName(''); setCreateDatasetId(''); setCreateRoute('A'); setCreateError('')
      const res = await apiClientV2.get<Pipeline[] | { data?: Pipeline[] }>('/pipelines')
      setPipelines(Array.isArray(res) ? res : res.data ?? [])
    } catch (e: unknown) {
      const err = e as {detail?: string; message?: string}
      setCreateError(err?.detail || err?.message || '创建失败')
    } finally {
      setCreating(false)
    }
  }

  const loadRunDetail = async (plId: string) => {
    if (runDetails[plId] !== undefined) return
    try {
      const runs = await apiClientV2.get<PipelineRun[] | { data?: PipelineRun[] }>(`/pipelines/${plId}/runs`)
      const runsArr: PipelineRun[] = Array.isArray(runs) ? runs : runs.data ?? []
      const last = runsArr[0]
      if (!last) { setRunDetails(p => ({ ...p, [plId]: null })); return }
      const detail = await pipelinesApi.getRun(last.id) as PipelineRunDetail as RunDetail
      setRunDetails(p => ({ ...p, [plId]: detail }))
      if (['pending', 'running'].includes(detail.status)) {
        setRunning(plId)
        setActiveRun({ pipelineId: plId, runId: last.id })
      }
    } catch {
      setRunDetails(p => ({ ...p, [plId]: null }))
    }
  }

  const toggleExpand = (id: string) => {
    const next = expanded === id ? null : id
    setExpanded(next)
    if (next) loadRunDetail(next)
  }

  const handleRun = async (id: string) => {
    if (running) return
    setRunning(id)
    setRunDetails(previous => {
      const next = { ...previous }
      delete next[id]
      return next
    })
    try {
      const started = await pipelinesApi.run(id)
      setPipelines(prev => prev.map(p => p.id === id ? { ...p, status: 'running' } : p))
      setActiveRun({ pipelineId: id, runId: started.run_id })
    } catch {
      setRunDetails(prev => ({ ...prev, [id]: {
        id: '', status: 'failed', stats: null, error_log: '运行请求未能提交，请检查服务状态后重试。',
      } }))
      setRunning(null)
    }
  }

  if (loading) return <div className="text-gray-400 text-sm p-4">加载中...</div>

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-lg font-semibold">Transform 流水线</h2>
          <p className="text-xs text-gray-400 mt-0.5">三条处理路径：A 结构化 / B 半结构化 / C 非结构化文档</p>
        </div>
        <button
          onClick={() => { setShowCreate(true); setCreateError('') }}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-black text-white text-sm rounded-lg hover:bg-gray-800"
        >
          <Plus size={14} /> 新建流水线
        </button>
      </div>

      {pipelines.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-10 text-center text-gray-400 space-y-2">
          <GitBranch size={28} className="mx-auto opacity-30" />
          <p className="text-sm">暂无流水线</p>
          <p className="text-xs">上传数据集后创建 Transform 流水线，支持结构化 / 半结构化 / 非结构化三条路径</p>
        </div>
      ) : (
        <div className="space-y-2">
          {pipelines.map(pl => {
            const isExpanded = expanded === pl.id
            const detail = runDetails[pl.id]
            const isRunning = running === pl.id

            return (
              <div key={pl.id} className="border rounded-xl overflow-hidden bg-white">
                {/* 流水线标题行 */}
                <div className="p-4 flex items-center gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-sm truncate">{pl.name}</span>
                      {pl.route && (
                        <span className={`text-xs px-1.5 py-0.5 rounded border flex-shrink-0 ${ROUTE_STYLE[pl.route] ?? 'bg-gray-50 text-gray-600 border-gray-200'}`}>
                          {ROUTE_LABEL[pl.route] ?? `Route ${pl.route}`}
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-gray-400 font-mono">{pl.id.slice(0, 8)}</span>
                  </div>

                  <div className="flex items-center gap-2 flex-shrink-0">
                    {detail?.stats && (
                      <span className="text-xs text-gray-500">
                        {detail.stats.rows_in}→{detail.stats.rows_out} 行
                      </span>
                    )}
                    <span className={`text-xs ${detail?.status === 'success' ? 'text-green-600' : detail?.status === 'degraded' ? 'text-amber-700' : detail?.status === 'failed' ? 'text-red-500' : 'text-gray-400'}`}>
                      {detail?.status === 'success' ? '完成' : detail?.status === 'degraded' ? '完成（降级）' : detail?.status === 'failed' ? '失败' : detail?.status === 'pending' ? '排队中' : isRunning ? '处理中' : '未运行'}
                    </span>
                    <button
                      onClick={() => handleRun(pl.id)}
                      disabled={!!isRunning}
                      className="flex items-center gap-1 px-2.5 py-1 text-xs bg-gray-100 rounded-lg hover:bg-gray-200 disabled:opacity-50"
                    >
                      <Play size={11} className={isRunning ? 'animate-pulse' : ''} />
                      {isRunning ? '运行中' : '运行'}
                    </button>
                    <button
                      onClick={() => toggleExpand(pl.id)}
                      className="p-1 rounded hover:bg-gray-100 text-gray-500"
                    >
                      {isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                    </button>
                  </div>
                </div>

                {/* 步骤卡片展开区 */}
                {isExpanded && (
                  <div className="border-t bg-gray-50 px-4 py-3">
                    {!detail ? (
                      <p className="text-xs text-gray-400">加载步骤详情中...</p>
                    ) : !detail.stats ? (
                      <p className="text-xs text-gray-400">
                        {detail.error_log ? `运行失败：${detail.error_log}` : detail.status === 'pending' ? (detail.dispatch_status === 'pending' || detail.dispatch_status === 'dispatching' ? `Run 已持久化，正在派发/自动重试（第 ${detail.dispatch_attempts ?? 0} 次）。` : `Run 已持久化，派发状态：${detail.dispatch_status ?? '等待 worker'}。`) : detail.status === 'running' ? 'Run 正在由服务端 worker 执行。' : '尚未运行，点击「运行」执行流水线'}
                      </p>
                    ) : (
                      <div className="space-y-2">
                        {/* 步骤时间线 */}
                        <div className="flex items-center gap-1 flex-wrap mb-3">
                          {buildSteps(pl.route, detail.stats).map((step, i, arr) => (
                            <div key={step.key} className="flex items-center gap-1">
                              <span className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded border ${STEP_STATUS_STYLE[step.status]}`}>
                                {step.icon}
                                {step.label}
                              </span>
                              {i < arr.length - 1 && (
                                <span className="text-gray-300 text-xs">→</span>
                              )}
                            </div>
                          ))}
                        </div>

                        {/* 步骤详情列表 */}
                        <div className="space-y-1.5">
                          {buildSteps(pl.route, detail.stats).map(step => (
                            <div key={step.key} className={`flex items-start gap-2 text-xs rounded-lg px-3 py-2 border ${STEP_STATUS_STYLE[step.status]}`}>
                              <div className="flex items-center gap-1.5 w-32 flex-shrink-0 font-medium">
                                {STEP_STATUS_ICON[step.status]}
                                {step.label}
                              </div>
                              <span className="text-gray-600 flex-1">{step.detail}</span>
                            </div>
                          ))}
                        </div>

                        {detail.llm_model_pin?.requested && (
                          <div className="rounded-lg border bg-white px-3 py-2 text-[11px] text-gray-600">
                            <div className="font-medium text-gray-700">本次 Run 固定的模型与提示词</div>
                            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                              <span>{detail.llm_model_pin.provider || '模型提供方未知'} · {detail.llm_model_pin.model_name || '模型未配置'}</span>
                              {detail.llm_model_pin.model_config_id && <span>ModelConfig {detail.llm_model_pin.model_config_id.slice(0, 8)}</span>}
                              {detail.llm_model_pin.model_config_digest && <span>配置摘要 {detail.llm_model_pin.model_config_digest.slice(0, 10)}</span>}
                              <span>模型制品可复现性：{detail.llm_model_pin.provider_model_artifact_reproducibility}</span>
                              {Object.values(detail.llm_model_pin.prompt_provenance || {}).map(prompt => (
                                <span key={prompt.prompt_id}>Prompt {prompt.prompt_id} · {prompt.prompt_digest.slice(0, 10)}</span>
                              ))}
                            </div>
                          </div>
                        )}

                        {!!detail.llm_invocations?.length && (
                          <div className="mt-2 rounded-lg border bg-white overflow-hidden">
                            <div className="px-3 py-2 bg-gray-100 text-xs font-medium text-gray-600">
                              LLM 调用证据（仅摘要，不保存提示词或响应正文）
                            </div>
                            <div className="divide-y">
                              {detail.llm_invocations.slice(-8).map(attempt => (
                                <div key={attempt.id} className="px-3 py-2 text-[11px] text-gray-600">
                                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                    <span className="font-medium text-gray-800">尝试 {attempt.attempt_number ?? '?'}</span>
                                    <span>{attempt.provider || '未知提供方'} · {attempt.model_name}</span>
                                    <span>{attempt.phase || '结构化提取'}</span>
                                    <span className={attempt.status === 'completed' ? 'text-green-700' : attempt.status === 'running' ? 'text-amber-700' : 'text-red-600'}>
                                      {attempt.status === 'completed' ? '完成' : attempt.status === 'running' ? (detail.status === 'running' ? '执行中' : '结果未知') : attempt.status === 'invalid_response' ? '响应格式无效' : attempt.status === 'empty_response' ? '空响应' : '失败'}
                                      {attempt.error_code ? ` · ${attempt.error_code}` : ''}
                                    </span>
                                    {attempt.duration_ms != null && <span>{attempt.duration_ms} ms</span>}
                                  </div>
                                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-gray-400">
                                    {attempt.source_dataset_version_id && <span>来源版本 {attempt.source_dataset_version_id.slice(0, 8)}</span>}
                                    {attempt.prompt_id && <span>Prompt {attempt.prompt_id} · {attempt.prompt_digest?.slice(0, 10) || '无摘要'}</span>}
                                    {attempt.model_config_digest && <span>ModelConfig {attempt.model_config_digest.slice(0, 10)}</span>}
                                    {attempt.request_hash && <span>请求 {attempt.request_hash.slice(0, 10)}</span>}
                                    {attempt.response_hash && <span>响应 {attempt.response_hash.slice(0, 10)}</span>}
                                    <span>模型制品可复现性：{attempt.provider_model_artifact_reproducibility || 'unverified'}</span>
                                  </div>
                                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                                    <span className={attempt.event_chain_status === 'verified' ? 'text-green-700' : attempt.event_chain_status === 'invalid' ? 'text-red-600' : 'text-gray-400'}>
                                      {attempt.event_chain_status === 'verified' ? `不可变事件链已校验（${attempt.events?.length || 0} 条）` : attempt.event_chain_status === 'invalid' ? '不可变事件链校验失败' : '此调用没有不可变事件历史'}
                                    </span>
                                    {!!attempt.events?.length && (
                                      <details className="min-w-0">
                                        <summary className="cursor-pointer text-blue-700">查看调用事件</summary>
                                        <div className="mt-1 grid gap-1 rounded bg-gray-50 p-2">
                                          {attempt.events.map(event => (
                                            <div key={event.id} className="flex flex-wrap gap-x-2 gap-y-0.5">
                                              <span>#{event.sequence}</span>
                                              <span>{event.event_type === 'started' ? '调用开始' : event.event_type === 'provider_result' ? 'Provider 返回' : '响应校验失败'}</span>
                                              <span>{event.status}</span>
                                              {event.error_code && <span>{event.error_code}</span>}
                                              {event.occurred_at && <span>{new Date(event.occurred_at).toLocaleString()}</span>}
                                              <span>hash {event.event_digest.slice(0, 12)}</span>
                                            </div>
                                          ))}
                                        </div>
                                      </details>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Schema 详情（Route A）*/}
                        {pl.route === 'A' && detail.stats.meta?.inferred_schema && (
                          <div className="mt-2 border rounded-lg overflow-hidden">
                            <div className="bg-gray-100 px-3 py-1.5 text-xs font-medium text-gray-600 flex items-center gap-1.5">
                              <BarChart3 size={12} /> 推断的列类型
                            </div>
                            <div className="flex flex-wrap gap-1.5 p-2">
                              {Object.entries(detail.stats.meta.inferred_schema).slice(0, 12).map(([col, type]) => (
                                <span key={col} className="text-xs bg-white border rounded px-2 py-0.5">
                                  <span className="font-medium">{col}</span>
                                  <span className="text-gray-400 ml-1">({String(type)})</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    {detail?.execution_digest && (
                      <div className="mt-3 rounded-lg border bg-white px-3 py-2 text-[11px] text-gray-600">
                        <div className="flex flex-wrap gap-x-4 gap-y-1">
                          <span>运行模式：{detail.execution_mode === 'published' ? '已发布版本' : detail.execution_mode === 'preview' ? '草稿预览' : '兼容快照'}</span>
                          {detail.pipeline_version_id && <span>Pipeline 版本：{detail.pipeline_version_id.slice(0, 8)}</span>}
                          <span>输入摘要：{detail.execution_digest.slice(0, 12)}</span>
                        </div>
                        {detail.source_versions?.length ? (
                          <div className="mt-1.5 flex flex-wrap gap-1.5">
                            {detail.source_versions.map(source => (
                              <span key={source.dataset_version_id || source.dataset_id} className="rounded border bg-gray-50 px-1.5 py-0.5">
                                数据源 {source.dataset_id?.slice(0, 8) || '未知'} · v{source.version_no ?? '?'} · {source.checksum ? source.checksum.slice(0, 10) : '无校验和'}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {showCreate && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-lg p-6 w-[440px]">
            <h3 className="font-semibold mb-4">新建管道</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">管道名称 *</label>
                <input
                  value={createName}
                  onChange={e => setCreateName(e.target.value)}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  placeholder="请输入管道名称"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">数据源 *</label>
                <select
                  value={createDatasetId}
                  onChange={e => setCreateDatasetId(e.target.value)}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                >
                  <option value="">请选择数据集</option>
                  {datasets.map(d => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">处理路径 *</label>
                <div className="flex gap-2">
                  {(['A', 'B', 'C'] as const).map(r => (
                    <button
                      key={r}
                      type="button"
                      onClick={() => setCreateRoute(r)}
                      className={`flex-1 text-xs px-3 py-2 rounded-lg border transition-colors ${
                        createRoute === r ? 'bg-black text-white border-black' : 'border-gray-200 text-gray-600 hover:bg-gray-50'
                      }`}
                    >
                      {r === 'A' ? 'A · 结构化' : r === 'B' ? 'B · 半结构化' : 'C · 非结构化'}
                    </button>
                  ))}
                </div>
              </div>
              {createError && <p className="text-xs text-red-500">{createError}</p>}
            </div>
            <div className="flex justify-end gap-3 mt-4">
              <button
                type="button"
                onClick={() => { setShowCreate(false); setCreateError('') }}
                className="px-4 py-2 border rounded-lg text-sm"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleCreate}
                disabled={creating}
                className="px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50"
              >
                {creating ? '创建中...' : '创建'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
