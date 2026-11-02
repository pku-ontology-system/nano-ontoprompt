import { useCallback, useRef, useState, useEffect, type ComponentType } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  ReactFlow, MiniMap, Controls, Background,
  useNodesState, useEdgesState, addEdge, ReactFlowProvider,
  type Connection, type Node, type Edge,
  type NodeTypes, type OnNodesChange, type OnEdgesChange,
  MarkerType,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Save, Play, CheckCircle, X, ArrowLeft, Loader2, AlertTriangle, Pencil, ChevronLeft, ChevronRight, CalendarClock } from 'lucide-react'
import pipelinesApi, { waitForPipelineRun, type Pipeline, type RunDetail, type ValidateResult, type PipelineSchedule, type PipelineScheduleRunItem } from '@/api/v2/pipelines'
import servicePrincipalsApi, { type ServicePrincipal } from '@/api/v2/servicePrincipals'
import ConnectorNode from './nodes/ConnectorNode'
import StorageNode from './nodes/StorageNode'
import TransformNode from './nodes/TransformNode'
import OutputNode from './nodes/OutputNode'
import ConnectorInspector from './inspectors/ConnectorInspector'
import StorageInspector from './inspectors/StorageInspector'
import TransformInspector from './inspectors/TransformInspector'
import OutputInspector from './inspectors/OutputInspector'

const nodeTypes: NodeTypes = {
  connector: ConnectorNode, storage: StorageNode,
  transform: TransformNode, output: OutputNode,
}

const NODE_DEFAULTS: Record<string, { label: string; color: string; config: Record<string, unknown> }> = {
  connector: { label: '连接器', color: '#3B82F6', config: { source_type: 'file', config_values: {} } },
  storage: { label: '存储器', color: '#10B981', config: { storage_mode: 'auto' } },
  transform: { label: '转换器', color: '#F59E0B', config: { path: 'auto', steps: [] } },
  output: { label: '输出', color: '#8B5CF6', config: { dataset_type: 'curated_dataset', primary_key: [] } },
}

const TOOLS = [
  { type: 'connector', label: '连接器', desc: '数据源连接' },
  { type: 'storage', label: '存储器', desc: '原始数据存储' },
  { type: 'transform', label: '转换器', desc: '数据转换' },
  { type: 'output', label: '输出', desc: '输出 Curated Dataset' },
]

const TYPE_ORDER: Record<string, number> = { connector: 0, storage: 1, transform: 2, output: 3 }

interface DefinitionNode {
  id: string
  type: string
  position?: { x: number; y: number }
  label?: string
  config?: Record<string, unknown>
}

interface DefinitionEdge {
  id: string
  source: string
  target: string
}

interface PipelineNodeData extends Record<string, unknown> {
  label: string
  config: Record<string, unknown>
  status?: string
  statusReason?: string
}

type PipelineFlowNode = Node<PipelineNodeData>

interface RunStats {
  curatedIds: string[]
  curatedId: string
  nodeStatus: Record<string, string>
  nodeStatusReason: Record<string, string>
}

function readRunStats(stats: Record<string, unknown> | null | undefined): RunStats {
  const curatedIds = Array.isArray(stats?.curated_dataset_ids)
    ? stats.curated_dataset_ids.filter((value): value is string => typeof value === 'string')
    : []
  const nodeStatus = typeof stats?.node_status === 'object' && stats.node_status !== null
    ? stats.node_status as Record<string, string>
    : {}
  const nodeStatusReason = typeof stats?.node_status_reason === 'object' && stats.node_status_reason !== null
    ? stats.node_status_reason as Record<string, string>
    : {}
  return {
    curatedIds,
    curatedId: typeof stats?.curated_dataset_id === 'string' ? stats.curated_dataset_id : curatedIds[0] || '',
    nodeStatus,
    nodeStatusReason,
  }
}

function layoutDefinitionNodes(rawNodes: DefinitionNode[]): PipelineFlowNode[] {
  const used = new Set<string>()
  const hasOverlap = rawNodes.some(n => {
    const p = n.position
    if (!p) return true
    const key = `${Math.round(p.x)}:${Math.round(p.y)}`
    if (used.has(key)) return true
    used.add(key)
    return false
  })
  const sorted = [...rawNodes].sort((a, b) => (TYPE_ORDER[a.type] ?? 99) - (TYPE_ORDER[b.type] ?? 99))
  return rawNodes.map((n, index) => {
    const layoutIndex = sorted.findIndex(s => s.id === n.id)
    return {
      id: n.id,
      type: n.type,
      position: hasOverlap ? { x: 120 + layoutIndex * 240, y: 180 + (index % 2) * 20 } : n.position || { x: 120, y: 180 },
      data: { label: n.label || '', config: n.config || {} },
    }
  })
}

interface SelectedNodeData {
  id: string; type: string; label: string; config: Record<string, unknown>
}

export default function PipelineBuilderPage() {
  const { pipelineId } = useParams<{ pipelineId: string }>()
  const navigate = useNavigate()
  const reactFlowWrapper = useRef<HTMLDivElement>(null)
  const inspectorRef = useRef<HTMLDivElement>(null)
  const reactFlowInstanceRef = useRef<{ screenToFlowPosition: (position: { x: number; y: number }) => { x: number; y: number } } | null>(null)

  const [nodes, setNodes, onNodesChange] = useNodesState<PipelineFlowNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedNode, setSelectedNode] = useState<SelectedNodeData | null>(null)
  const [pipeline, setPipeline] = useState<Pipeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [runStatus, setRunStatus] = useState('')
  const [runMessage, setRunMessage] = useState('')
  const [runEvidence, setRunEvidence] = useState<RunDetail | null>(null)
  const [validation, setValidation] = useState<ValidateResult | null>(null)
  const [saveStatus, setSaveStatus] = useState<'saved' | 'unsaved' | 'saving'>('saved')
  const [inspectorWidth, setInspectorWidth] = useState(288)
  const [toolbarCollapsed, setToolbarCollapsed] = useState(false)
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [schedule, setSchedule] = useState<PipelineSchedule | null>(null)
  const [scheduleRuns, setScheduleRuns] = useState<PipelineScheduleRunItem[]>([])
  const [publishedVersions, setPublishedVersions] = useState<Array<{ id: string; version: number }>>([])
  const [scheduleVersionId, setScheduleVersionId] = useState('')
  const [scheduleServicePrincipalId, setScheduleServicePrincipalId] = useState('')
  const [schedulePrincipals, setSchedulePrincipals] = useState<ServicePrincipal[]>([])
  const [scheduleLoading, setScheduleLoading] = useState(false)
  const [scheduleSaving, setScheduleSaving] = useState(false)
  const [scheduleError, setScheduleError] = useState('')
  const [scheduleCron, setScheduleCron] = useState('0 8 * * 1-5')
  const [scheduleTimezone, setScheduleTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
  const [scheduleEnabled, setScheduleEnabled] = useState(true)
  const [scheduleMisfire, setScheduleMisfire] = useState<'skip' | 'run_once'>('skip')
  const [scheduleGrace, setScheduleGrace] = useState(300)
  const [scheduleOverlap, setScheduleOverlap] = useState<'forbid' | 'allow'>('forbid')
  const isDraggingPanel = useRef(false)

  const handlePanelResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault(); isDraggingPanel.current = true
    document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'
  }, [])
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (!isDraggingPanel.current) return
      setInspectorWidth(Math.max(240, Math.min(600, window.innerWidth - e.clientX - 48)))
    }
    const u = () => { isDraggingPanel.current = false; document.body.style.cursor = ''; document.body.style.userSelect = '' }
    window.addEventListener('mousemove', h); window.addEventListener('mouseup', u)
    return () => { window.removeEventListener('mousemove', h); window.removeEventListener('mouseup', u) }
  }, [])

  useEffect(() => {
    if (!pipelineId) return
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActiveRunId(null)
    setRunning(false)
    setRunEvidence(null)
    void Promise.resolve().then(() => setLoading(true))
    Promise.all([pipelinesApi.get(pipelineId), pipelinesApi.runs(pipelineId)])
      .then(([pl, runs]) => {
        setPipeline(pl)
        const lastRun = Array.isArray(runs) && runs.length > 0 ? runs[0] : null
        if (lastRun) {
          if (['pending', 'running'].includes(lastRun.status)) {
            setActiveRunId(lastRun.id)
            setRunStatus(lastRun.status)
            setRunning(true)
          }
          pipelinesApi.getRun(lastRun.id).catch(() => null).then(detail => {
            if (!detail) return
            setRunEvidence(detail)
            const { nodeStatus, nodeStatusReason, curatedIds, curatedId } = readRunStats(detail.stats)
            const fallbackStatus = detail.status === 'degraded' ? 'degraded'
              : detail.status === 'success' ? 'success'
              : detail.status === 'failed' ? 'failed' : detail.status === 'running' ? 'running' : 'idle'
            setNodes(nds => nds.map(n => {
              const data: PipelineNodeData = {
                ...n.data,
                status: nodeStatus[n.id] || fallbackStatus,
                statusReason: nodeStatusReason[n.id] || undefined,
              }
              if (n.type === 'output' && curatedId) {
                data.config = { ...data.config, curated_dataset_id: curatedId, curated_dataset_ids: curatedIds }
              }
              return { ...n, data }
            }))
          })
        }
        const def = pl.definition || { nodes: [], edges: [] }
        setNodes(layoutDefinitionNodes((def.nodes || []) as DefinitionNode[]))
        setEdges(((def.edges || []) as DefinitionEdge[]).map(e => ({
          id: e.id, source: e.source, target: e.target, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed },
        })))
      }).catch(() => navigate('/data/pipelines')).finally(() => setLoading(false))
  }, [navigate, pipelineId, setEdges, setNodes])

  const saveDefinition = useCallback(async (): Promise<boolean> => {
    if (!pipelineId) return false
    setSaving(true); setSaveStatus('saving')
    try {
      const updated = await pipelinesApi.update(pipelineId, { definition: { nodes: nodes.map(n => ({
        id: n.id, type: n.type, position: n.position, label: n.data.label || '', config: n.data.config || {},
      })), edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target })) } })
      setPipeline(updated)
      setSaveStatus('saved')
      return true
    } catch { setSaveStatus('unsaved'); return false }
    finally { setSaving(false) }
  }, [pipelineId, nodes, edges])

  useEffect(() => { const h = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveDefinition() } }; window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h) }, [saveDefinition])
  useEffect(() => { if (!loading) void Promise.resolve().then(() => setSaveStatus('unsaved')) }, [nodes, edges, loading])

  const applyRunDetail = useCallback((detail: RunDetail) => {
    setRunEvidence(detail)
    const { nodeStatus, nodeStatusReason, curatedIds, curatedId } = readRunStats(detail.stats)
    setRunStatus(detail.status)
    if (detail.status === 'failed') setRunMessage(detail.error_log || '流水线运行失败')
    else if (detail.status === 'degraded') setRunMessage('流水线已生成待审核输出，但 LLM 结构化提取按已配置策略回退到规则提取；此 Run 不计为 LLM 提取成功。')
    else if (detail.status === 'success') setRunMessage('')
    else if (detail.status === 'pending') {
      const attempts = detail.dispatch_attempts ?? 0
      setRunMessage(detail.dispatch_status === 'pending' || detail.dispatch_status === 'dispatching'
        ? `Run 已保存，正在派发或自动重试（第 ${attempts} 次）。`
        : 'Run 已保存，等待 worker 接收。')
    }
    const fallbackStatus = detail.status === 'degraded' ? 'degraded'
      : detail.status === 'success' ? 'success'
      : detail.status === 'failed' ? 'failed' : 'pending'
    setNodes(nds => nds.map(n => {
      const data: PipelineNodeData = {
        ...n.data,
        status: nodeStatus[n.id] || fallbackStatus,
        statusReason: nodeStatusReason[n.id] || undefined,
      }
      if (n.type === 'output' && curatedId) {
        data.config = { ...data.config, curated_dataset_id: curatedId, curated_dataset_ids: curatedIds }
      }
      return { ...n, data }
    }))
    setSelectedNode(prev => {
      if (!prev || prev.type !== 'output' || !curatedId) return prev
      return { ...prev, config: { ...prev.config, curated_dataset_id: curatedId, curated_dataset_ids: curatedIds } }
    })
  }, [setNodes])

  useEffect(() => {
    if (!activeRunId) return
    const controller = new AbortController()
    void waitForPipelineRun(activeRunId, {
      signal: controller.signal,
      onUpdate: applyRunDetail,
      onPollError: () => setRunMessage('暂时无法读取运行状态；服务端仍保留此 Run，页面会继续重试。'),
    }).then(async detail => {
      if (!detail || controller.signal.aborted) return
      applyRunDetail(detail)
      if (pipelineId) setPipeline(await pipelinesApi.get(pipelineId))
    }).catch(() => {
      if (!controller.signal.aborted) setRunMessage('运行结束状态读取失败，可刷新页面从服务端 Run 记录恢复。')
    }).finally(() => {
      if (!controller.signal.aborted) {
        setRunning(false)
        setActiveRunId(null)
      }
    })
    return () => controller.abort()
  }, [activeRunId, pipelineId, applyRunDetail])

  const handleRun = async () => {
    if (!pipelineId || running) return
    setRunning(true)
    setRunMessage('')
    setRunEvidence(null)
    setNodes(nds => nds.map(n => ({ ...n, data: { ...n.data, status: 'pending', statusReason: undefined } })))
    try {
      const saved = await saveDefinition()
      if (!saved) {
        setRunMessage('Pipeline 定义保存失败，已停止运行；请检查修改后重试。')
        setRunning(false)
        return
      }
      const run = await pipelinesApi.run(pipelineId)
      setRunStatus(run.status)
      setActiveRunId(run.run_id)
    } catch {
      setRunMessage('运行请求未能提交；请检查服务状态后重试。')
      setRunning(false)
      setNodes(nds => nds.map(n => ({ ...n, data: { ...n.data, status: 'failed' } })))
    }
  }

  const handleValidate = async () => { if (!pipelineId) return; try { setValidation(await pipelinesApi.validate(pipelineId)) } catch { setValidation({ valid: false, errors: [], warnings: [{ node_id: '', severity: 'error', message: '校验失败' }] }) } }
  const handlePublish = async () => { if (!pipelineId) return; try { const r = await pipelinesApi.publish(pipelineId); setPipeline(await pipelinesApi.get(pipelineId)); alert(`已发布 v${r.version}`) } catch (error: unknown) { alert(typeof error === 'object' && error !== null && 'detail' in error && typeof error.detail === 'string' ? error.detail : '发布失败') } }

  const onDragStart = useCallback((event: React.DragEvent, nodeType: string) => { event.dataTransfer.setData('application/reactflow', nodeType); event.dataTransfer.effectAllowed = 'move' }, [])
  const onDrop = useCallback((event: React.DragEvent) => {
    event.preventDefault(); const type = event.dataTransfer.getData('application/reactflow')
    if (!type || !NODE_DEFAULTS[type]) return
    const pos = reactFlowInstanceRef.current?.screenToFlowPosition({ x: event.clientX, y: event.clientY }) || { x: event.clientX - 150, y: event.clientY - 40 }
    const d = NODE_DEFAULTS[type]
    setNodes(nds => nds.concat({ id: `${type}_${Date.now()}`, type, position: pos, data: { label: `${d.label}_${nodes.length + 1}`, config: { ...d.config } } }))
  }, [nodes, setNodes])
  const onDragOver = useCallback((event: React.DragEvent) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }, [])
  const onConnect = useCallback((conn: Connection) => { setEdges(eds => addEdge({ ...conn, id: `edge_${Date.now()}`, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed } }, eds)) }, [setEdges])
  const onNodeClick = useCallback((_: React.MouseEvent, node: PipelineFlowNode) => { setSelectedNode({ id: node.id, type: node.type || '', label: node.data.label || '', config: node.data.config || {} }) }, [])
  const onPaneClick = useCallback(() => { setSelectedNode(null); setValidation(null) }, [])
  const updateNodeData = useCallback((nodeId: string, data: Pick<SelectedNodeData, 'config' | 'label'>) => {
    setNodes(nds => nds.map(n => n.id === nodeId ? { ...n, data: { ...n.data, ...data } } : n))
    setSelectedNode(prev => prev && prev.id === nodeId ? { ...prev, ...data } : prev)
  }, [setNodes])

  const openSchedule = async () => {
    if (!pipelineId) return
    setScheduleOpen(true)
    setScheduleLoading(true)
    setScheduleError('')
    try {
      const [result, runs, versions, principals] = await Promise.all([
        pipelinesApi.getSchedule(pipelineId),
        pipelinesApi.scheduleRuns(pipelineId, 12),
        pipelinesApi.versions(pipelineId),
        servicePrincipalsApi.list({ pipeline_id: pipelineId }),
      ])
      const current = result.schedule
      const published = versions.filter(version => version.status === 'published')
      setSchedule(current)
      setScheduleRuns(runs)
      setPublishedVersions(published)
      setSchedulePrincipals(principals.items)
      const currentPrincipalIsAvailable = principals.items.some(item => item.id === current?.service_principal_id)
      setScheduleServicePrincipalId(currentPrincipalIsAvailable ? current?.service_principal_id || '' : principals.items[0]?.id || '')
      setScheduleVersionId(current?.pipeline_version_id || published[0]?.id || '')
      if (current) {
        setScheduleCron(current.cron_expression)
        setScheduleTimezone(current.timezone)
        setScheduleEnabled(current.enabled)
        setScheduleMisfire(current.misfire_policy)
        setScheduleGrace(current.misfire_grace_seconds)
        setScheduleOverlap(current.overlap_policy)
      }
    } catch {
      setScheduleError('无法读取 Schedule；请确认服务端数据库已完成对应迁移。')
    } finally {
      setScheduleLoading(false)
    }
  }

  const refreshScheduleRuns = async () => {
    if (!pipelineId) return
    try { setScheduleRuns(await pipelinesApi.scheduleRuns(pipelineId, 12)) } catch { /* keep the last readable history */ }
  }

  const saveSchedule = async () => {
    if (!pipelineId) return
    setScheduleSaving(true)
    setScheduleError('')
    try {
      const result = await pipelinesApi.upsertSchedule(pipelineId, {
        cron_expression: scheduleCron,
        timezone: scheduleTimezone,
        enabled: scheduleEnabled,
        ...(scheduleVersionId ? { pipeline_version_id: scheduleVersionId } : {}),
        misfire_policy: scheduleMisfire,
        misfire_grace_seconds: scheduleGrace,
        overlap_policy: scheduleOverlap,
        service_principal_id: scheduleServicePrincipalId,
        ...(schedule ? { expected_revision: schedule.revision } : {}),
      })
      setSchedule(result.schedule)
      await refreshScheduleRuns()
    } catch {
      setScheduleError('保存失败：请确认 cron、已发布版本和服务主体授权均有效；被停用或撤权的主体不能继续调度。')
    } finally {
      setScheduleSaving(false)
    }
  }

  const disableSchedule = async () => {
    if (!pipelineId) return
    setScheduleSaving(true)
    setScheduleError('')
    try {
      const result = await pipelinesApi.disableSchedule(pipelineId)
      if (result.schedule) setSchedule(result.schedule)
      setScheduleEnabled(false)
      await refreshScheduleRuns()
    } catch {
      setScheduleError('停用失败；请刷新后重试。')
    } finally {
      setScheduleSaving(false)
    }
  }

  const cancelScheduleRun = async (scheduleRunId: string) => {
    if (!pipelineId) return
    setScheduleError('')
    try {
      await pipelinesApi.cancelScheduleRun(pipelineId, scheduleRunId)
      await refreshScheduleRuns()
    } catch {
      setScheduleError('取消失败；正在运行的 PipelineRun 不能中断。')
    }
  }

  const openSchedulePipelineRun = async (runId: string) => {
    try {
      const detail = await pipelinesApi.getRun(runId)
      setRunEvidence(detail)
      setRunStatus(detail.status)
      setRunMessage(`已加载历史 PipelineRun：${detail.status}`)
      setScheduleOpen(false)
    } catch {
      setScheduleError('无法读取关联的 PipelineRun 详情。')
    }
  }

  if (loading) return <div className="text-gray-400 text-sm p-8 text-center">加载 Pipeline...</div>
  if (!pipeline) return <div className="text-gray-400 text-sm p-8 text-center">Pipeline 未找到</div>

  return (
    <ReactFlowProvider>
      <div className="h-[calc(100vh-5rem)] flex flex-col -m-6">
        <div className="flex items-center gap-3 px-4 py-2 bg-white border-b shrink-0">
          <button onClick={() => navigate(-1)} className="p-1.5 rounded hover:bg-gray-100 text-gray-500"><ArrowLeft size={16} /></button>
          <span className="font-semibold text-sm">{pipeline.name}</span>
          <span className={`text-xs px-1.5 py-0.5 rounded border ${pipeline.status === 'published' ? 'bg-green-50 text-green-600 border-green-200' : pipeline.status === 'failed' ? 'bg-red-50 text-red-600 border-red-200' : 'bg-gray-100 text-gray-600 border-gray-200'}`}>{pipeline.status}</span>
          <span className="text-xs text-gray-400">v{pipeline.version || 1}</span>
          <span className="text-xs text-gray-400 font-mono">{pipeline.branch || 'main'}</span>
          {saveStatus === 'saving' && <span className="text-xs text-amber-500 ml-2">保存中...</span>}
          {saveStatus === 'saved' && <span className="text-xs text-green-500 ml-2">已保存</span>}
          {saveStatus === 'unsaved' && <span className="text-xs text-gray-400 ml-2">未保存</span>}
          <div className="flex-1" />
          <button onClick={saveDefinition} disabled={saving} className="flex items-center gap-1 px-3 py-1.5 text-xs border rounded-lg hover:bg-gray-50"><Save size={13} />保存</button>
          <button onClick={handleValidate} className="flex items-center gap-1 px-3 py-1.5 text-xs border rounded-lg hover:bg-gray-50"><CheckCircle size={13} />校验</button>
          <button onClick={handleRun} disabled={running || saving} className="flex items-center gap-1 px-3 py-1.5 text-xs bg-gray-800 text-white rounded-lg hover:bg-black disabled:opacity-50">{running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}{running ? (runStatus === 'pending' ? '排队中' : '运行中') : '运行'}</button>
          <button onClick={() => void openSchedule()} className="flex items-center gap-1 px-3 py-1.5 text-xs border rounded-lg hover:bg-gray-50"><CalendarClock size={13} />定时</button>
          <button onClick={handlePublish} className="flex items-center gap-1 px-3 py-1.5 text-xs bg-black text-white rounded-lg hover:bg-gray-800">发布</button>
        </div>
        {scheduleOpen && <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="Pipeline 定时运行">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between px-5 py-4 border-b">
              <div><h2 className="font-semibold text-sm">Pipeline 定时运行</h2><p className="text-xs text-gray-500 mt-1">每次触发固定到一个已发布版本，并生成可追踪的 PipelineRun。</p></div>
              <button onClick={() => setScheduleOpen(false)} className="p-1.5 rounded hover:bg-gray-100 text-gray-500"><X size={16} /></button>
            </div>
            {scheduleLoading ? <div className="p-8 text-center text-sm text-gray-400">读取 Schedule...</div> : <div className="p-5 space-y-4">
              {scheduleError && <p role="alert" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{scheduleError}</p>}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="text-xs text-gray-600">Cron（五段式）
                  <input value={scheduleCron} onChange={e => setScheduleCron(e.target.value)} placeholder="0 8 * * 1-5" className="mt-1 w-full border rounded-lg px-3 py-2 text-sm font-mono" />
                </label>
                <label className="text-xs text-gray-600">时区
                  <input value={scheduleTimezone} onChange={e => setScheduleTimezone(e.target.value)} placeholder="Europe/London" className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
                </label>
                <label className="text-xs text-gray-600">错过触发
                  <select value={scheduleMisfire} onChange={e => setScheduleMisfire(e.target.value as 'skip' | 'run_once')} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm">
                    <option value="skip">超过宽限期跳过</option><option value="run_once">错过的触发合并补跑一次</option>
                  </select>
                </label>
                <label className="text-xs text-gray-600">并发策略
                  <select value={scheduleOverlap} onChange={e => setScheduleOverlap(e.target.value as 'forbid' | 'allow')} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm">
                    <option value="forbid">前一个未结束时跳过</option><option value="allow">允许并行</option>
                  </select>
                </label>
                <label className="text-xs text-gray-600">补跑宽限期（秒）
                  <input type="number" min={0} max={604800} value={scheduleGrace} onChange={e => setScheduleGrace(Number(e.target.value))} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" />
                </label>
                <label className="text-xs text-gray-600">固定的已发布版本
                  <select value={scheduleVersionId} onChange={e => setScheduleVersionId(e.target.value)} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" disabled={publishedVersions.length === 0}>
                    {publishedVersions.length === 0 && <option value="">尚无已发布版本</option>}
                    {publishedVersions.map(version => <option key={version.id} value={version.id}>v{version.version} · {version.id.slice(0, 8)}</option>)}
                  </select>
                </label>
                <label className="text-xs text-gray-600">执行服务主体
                  <select value={scheduleServicePrincipalId} onChange={e => setScheduleServicePrincipalId(e.target.value)} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" disabled={schedulePrincipals.length === 0}>
                    {schedulePrincipals.length === 0 && <option value="">无已授权主体，联系管理员授权</option>}
                    {schedulePrincipals.map(principal => <option key={principal.id} value={principal.id}>{principal.name} · {principal.kind === 'system' ? '系统' : '服务'} · r{principal.revision}</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-xs text-gray-700 self-end pb-2">
                  <input type="checkbox" checked={scheduleEnabled} onChange={e => setScheduleEnabled(e.target.checked)} />启用计划
                </label>
              </div>
              {schedule && <div className="rounded-lg bg-gray-50 border p-3 text-xs text-gray-600 space-y-1">
                <p>当前 revision：<span className="font-mono">r{schedule.revision}</span> · 固定版本：<span className="font-mono">{schedule.pipeline_version_id.slice(0, 8)}</span></p>
                <p>执行主体：<span className="font-mono">{schedule.service_principal_id}</span></p>
                <p>下次触发：{schedule.next_fire_at ? new Date(schedule.next_fire_at).toLocaleString() : '未启用'}</p>
              </div>}
              <div className="flex gap-2 justify-end">
                {schedule?.enabled && <button disabled={scheduleSaving} onClick={() => void disableSchedule()} className="px-3 py-2 border rounded-lg text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50">停用并保留历史</button>}
                <button disabled={scheduleSaving || !scheduleServicePrincipalId} onClick={() => void saveSchedule()} className="px-4 py-2 bg-black text-white rounded-lg text-xs hover:bg-gray-800 disabled:opacity-50">{scheduleSaving ? '保存中...' : '保存 Schedule'}</button>
              </div>
              <div className="border-t pt-3">
                <div className="flex items-center justify-between mb-2"><h3 className="text-xs font-medium text-gray-700">最近触发记录</h3><button onClick={() => void refreshScheduleRuns()} className="text-xs text-gray-500 hover:text-black">刷新</button></div>
                {scheduleRuns.length === 0 ? <p className="text-xs text-gray-400 py-3">暂无 Schedule Run</p> : <div className="space-y-2">
                  {scheduleRuns.map(item => <div key={item.id} className="flex items-center gap-2 rounded-lg border px-3 py-2 text-xs">
                    <span className="w-20 text-gray-600">{item.status}</span><span className="flex-1 text-gray-500">{new Date(item.scheduled_for).toLocaleString()} · {item.local_slot}</span>
                    {item.pipeline_run_id && <button onClick={() => void openSchedulePipelineRun(item.pipeline_run_id!)} className="text-blue-600 hover:underline">Run 详情</button>}
                    {['pending', 'dispatching', 'queued'].includes(item.status) && <button onClick={() => void cancelScheduleRun(item.id)} className="text-red-600 hover:underline">取消</button>}
                  </div>)}
                </div>}
              </div>
            </div>}
          </div>
        </div>}
        {validation && !validation.valid && (<div className="bg-red-50 border-b border-red-200 px-4 py-2 shrink-0"><AlertTriangle size={12} className="inline mr-1 text-red-600" /><span className="text-xs text-red-600">校验未通过: {validation.errors.map(e => e.message).join('; ')}</span></div>)}
        {runMessage && <div role="status" className={`border-b px-4 py-2 shrink-0 text-xs ${runStatus === 'failed' || runMessage.includes('失败') ? 'bg-red-50 border-red-200 text-red-700' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>{runMessage}</div>}
        {runEvidence?.execution_digest && <div className="border-b bg-slate-50 px-4 py-1.5 shrink-0 text-[11px] text-slate-600">
          输入快照：{runEvidence.execution_mode === 'published' ? '已发布版本' : runEvidence.execution_mode === 'preview' ? '草稿预览' : '兼容快照'} · {runEvidence.source_versions?.length || 0} 个来源版本 · 摘要 {runEvidence.execution_digest.slice(0, 12)}
        </div>}
        <div className="flex flex-1 overflow-hidden">
          <div className={`${toolbarCollapsed ? "w-10" : "w-48"} bg-gray-50 border-r p-1 space-y-1 shrink-0 transition-all duration-200 relative`}>
            <button onClick={() => setToolbarCollapsed(!toolbarCollapsed)}
              className="absolute -right-2 top-2 w-4 h-4 bg-white border rounded-full flex items-center justify-center text-gray-400 hover:text-black z-10">
              {toolbarCollapsed ? <ChevronRight size={10} /> : <ChevronLeft size={10} />}
            </button>
            {!toolbarCollapsed && <p className="text-xs font-medium text-gray-500 px-2 py-1">节点工具</p>}
            {TOOLS.map(t => (
              <div key={t.type} draggable onDragStart={e => onDragStart(e, t.type)}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs cursor-grab active:cursor-grabbing hover:bg-white border border-transparent hover:border-gray-200 ${toolbarCollapsed ? "justify-center px-1" : ""}`} title={t.label}>
                <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: NODE_DEFAULTS[t.type]?.color }} />
                {!toolbarCollapsed && <div><p className="font-medium">{t.label}</p><p className="text-gray-400">{t.desc}</p></div>}
              </div>
            ))}
          </div>
          <div ref={reactFlowWrapper} className="flex-1" onDrop={onDrop} onDragOver={onDragOver}>
            <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange as OnNodesChange} onEdgesChange={onEdgesChange as OnEdgesChange} onConnect={onConnect} onNodeClick={onNodeClick} onPaneClick={onPaneClick} nodeTypes={nodeTypes} fitView deleteKeyCode={['Backspace', 'Delete']} snapToGrid snapGrid={[15, 15]} onInit={(inst) => { reactFlowInstanceRef.current = inst }}>
              <Controls /><MiniMap nodeStrokeWidth={3} nodeColor={n => NODE_DEFAULTS[n.type || '']?.color || '#666'} style={{ width: 150, height: 100 }} /><Background color="#f0f0f0" gap={15} />
            </ReactFlow>
          </div>
          <div ref={inspectorRef} className="relative bg-white border-l overflow-y-auto shrink-0" style={{ width: inspectorWidth }}>
            <div onMouseDown={handlePanelResizeStart} className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize hover:bg-blue-400 active:bg-blue-500 transition-colors z-10 group"><div className="absolute left-0.5 top-1/2 -translate-y-1/2 w-0.5 h-8 bg-gray-300 rounded-full group-hover:bg-white" /></div>
            {selectedNode ? (<NodeInspector nodeData={selectedNode} onUpdate={(data) => updateNodeData(selectedNode.id, data)} onClose={() => setSelectedNode(null)} pipelineId={pipelineId} />) : (<div className="p-4 text-center text-gray-400 text-xs mt-8">点击节点查看配置</div>)}
          </div>
        </div>
      </div>
    </ReactFlowProvider>
  )
}

/** ── NodeInspector: 编辑/确认/取消 三态 ── */
function NodeInspector({ nodeData, onUpdate, onClose, pipelineId }: { nodeData: SelectedNodeData; onUpdate: (data: Pick<SelectedNodeData, 'config' | 'label'>) => void; onClose: () => void; pipelineId?: string }) {
  const [editing, setEditing] = useState(false)
  const [localConfig, setLocalConfig] = useState<Record<string, unknown>>(nodeData.config || {})
  const [localLabel, setLocalLabel] = useState(nodeData.label)

  useEffect(() => {
    if (editing) return
    void Promise.resolve().then(() => {
      setLocalConfig(nodeData.config || {})
      setLocalLabel(nodeData.label)
    })
  }, [nodeData.id, nodeData.config, nodeData.label, editing])

  const handleChange = (key: string, value: unknown) => { setLocalConfig(p => ({ ...p, [key]: value })) }

  const handleConfirm = () => {
    onUpdate({ config: localConfig, label: localLabel })
    setEditing(false)
  }
  const handleCancel = () => {
    setLocalConfig(nodeData.config || {})
    setLocalLabel(nodeData.label)
    setEditing(false)
  }

  type InspectorProps = { config: Record<string, unknown>; onChange: (key: string, value: unknown) => void; readOnly?: boolean; pipelineId?: string }
  const Inspectors: Record<string, ComponentType<InspectorProps>> = {
    connector: ConnectorInspector as ComponentType<InspectorProps>,
    storage: StorageInspector as ComponentType<InspectorProps>,
    transform: TransformInspector as ComponentType<InspectorProps>,
    output: OutputInspector as ComponentType<InspectorProps>,
  }
  const InspectorComponent = Inspectors[nodeData.type]

  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: NODE_DEFAULTS[nodeData.type]?.color || '#666' }} /><span className="font-medium text-sm">{nodeData.label}</span></div>
        <button onClick={onClose} className="text-gray-400 hover:text-black"><X size={14} /></button>
      </div>
      <div className="space-y-3">
        <div>
          <label className="text-xs text-gray-500 mb-1 block">节点名称</label>
          {editing ? (
            <input value={localLabel} onChange={e => setLocalLabel(e.target.value)} className="w-full border rounded-lg px-3 py-1.5 text-sm" />
          ) : (
            <p className="text-sm text-gray-700 bg-gray-50 rounded-lg px-3 py-1.5">{localLabel}</p>
          )}
        </div>
        {InspectorComponent && <InspectorComponent config={localConfig} onChange={handleChange} readOnly={!editing} pipelineId={pipelineId} />}
      </div>
      <div className="flex gap-2 mt-4">
        {editing ? (<>
          <button onClick={handleCancel} className="flex-1 px-3 py-2 border rounded-lg text-sm text-gray-600 hover:bg-gray-50">取消</button>
          <button onClick={handleConfirm} className="flex-1 px-3 py-2 bg-black text-white rounded-lg text-sm hover:bg-gray-800">确认</button>
        </>) : (
          <button onClick={() => setEditing(true)} className="w-full flex items-center justify-center gap-1.5 px-3 py-2 border rounded-lg text-sm text-gray-700 hover:bg-gray-50"><Pencil size={13} />编辑</button>
        )}
      </div>
    </div>
  )
}
