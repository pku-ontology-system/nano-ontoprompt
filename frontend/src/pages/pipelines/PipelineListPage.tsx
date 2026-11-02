import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus, Search, Play, GitBranch, Trash2,
  ExternalLink, X, Loader2
} from 'lucide-react'
import pipelinesApi, { waitForPipelineRun } from '@/api/v2/pipelines'
import type { Pipeline } from '@/api/v2/pipelines'
import { DOMAINS } from '@/types/ontology'

const STATUS_STYLE: Record<string, string> = {
  draft:     'bg-gray-100 text-gray-600 border-gray-200',
  editing:   'bg-blue-50 text-blue-600 border-blue-200',
  running:   'bg-amber-50 text-amber-600 border-amber-200',
  failed:    'bg-red-50 text-red-600 border-red-200',
  published: 'bg-green-50 text-green-600 border-green-200',
  pending:   'bg-amber-50 text-amber-600 border-amber-200',
  success:   'bg-green-50 text-green-600 border-green-200',
  degraded:  'bg-amber-50 text-amber-700 border-amber-200',
}

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿', editing: '编辑中', running: '运行中',
  pending: '排队中', failed: '失败', success: '完成', degraded: '完成（降级）', published: '已发布',
}

export default function PipelineListPage() {
  const navigate = useNavigate()
  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [filterDomain, setFilterDomain] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [runningIds, setRunningIds] = useState<Record<string, string>>({})
  const [runStatuses, setRunStatuses] = useState<Record<string, string>>({})
  const [runError, setRunError] = useState('')
  const runControllers = useRef(new Map<string, AbortController>())
  const monitoredRunIds = useRef(new Set<string>())

  const [showCreate, setShowCreate] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    pipelinesApi.list({ search, domain: filterDomain, status: filterStatus })
      .then(res => setPipelines(Array.isArray(res) ? res : []))
      .catch(() => setPipelines([]))
      .finally(() => setLoading(false))
  }, [search, filterDomain, filterStatus])

  useEffect(() => { Promise.resolve().then(load) }, [load])

  const watchRun = useCallback((pipelineId: string, runId: string) => {
    if (monitoredRunIds.current.has(runId)) return
    const controller = new AbortController()
    monitoredRunIds.current.add(runId)
    runControllers.current.set(runId, controller)
    setRunningIds(previous => ({ ...previous, [pipelineId]: 'pending' }))
    void waitForPipelineRun(runId, {
      signal: controller.signal,
      onUpdate: run => {
        setRunStatuses(previous => ({ ...previous, [pipelineId]: run.status }))
        setRunningIds(previous => ({ ...previous, [pipelineId]: run.status }))
      },
      onPollError: () => setRunError('暂时无法读取运行状态；Run 仍保存在服务端，页面会继续重试。'),
    }).then(async finished => {
      if (finished && !controller.signal.aborted) await load()
    }).catch(() => {
      if (!controller.signal.aborted) setRunError('运行状态读取失败，可刷新后从服务端 Run 记录恢复。')
    }).finally(() => {
      runControllers.current.delete(runId)
      monitoredRunIds.current.delete(runId)
      if (!controller.signal.aborted) {
        setRunningIds(previous => {
          const next = { ...previous }
          delete next[pipelineId]
          return next
        })
      }
    })
  }, [load])

  useEffect(() => {
    let cancelled = false
    for (const pipeline of pipelines) {
      if (!['pending', 'running'].includes(pipeline.last_run_status || '')) continue
      void pipelinesApi.runs(pipeline.id).then(runs => {
        const latest = Array.isArray(runs) ? runs[0] : null
        if (!cancelled && latest && ['pending', 'running'].includes(latest.status)) {
          watchRun(pipeline.id, latest.id)
        }
      }).catch(() => undefined)
    }
    return () => { cancelled = true }
  }, [pipelines, watchRun])

  useEffect(() => () => {
    for (const controller of runControllers.current.values()) controller.abort()
    runControllers.current.clear()
    monitoredRunIds.current.clear()
  }, [])

  const handleRun = async (pipelineId: string) => {
    if (runningIds[pipelineId]) return
    setRunError('')
    setRunStatuses(previous => ({ ...previous, [pipelineId]: 'pending' }))
    try {
      const started = await pipelinesApi.run(pipelineId)
      setRunStatuses(previous => ({ ...previous, [pipelineId]: started.status }))
      watchRun(pipelineId, started.run_id)
    } catch {
      setRunError('运行请求未能提交，请检查 Pipeline 定义和数据源版本后重试。')
    }
  }

  const handleDelete = async (pl: Pipeline) => {
    if (!window.confirm(`确认删除 Pipeline「${pl.name}」？删除后不会删除已生成的 Curated Dataset。`)) return
    await pipelinesApi.delete(pl.id)
    load()
  }

  const domains = [...new Set(pipelines.map(p => p.domain || '通用').filter(Boolean))]

  const filtered = pipelines.filter(p => {
    if (filterDomain && p.domain !== filterDomain) return false
    if (filterStatus && p.status !== filterStatus) return false
    if (search) {
      const q = search.toLowerCase()
      return p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q)
    }
    return true
  })

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-lg font-semibold">Pipeline 列表</h2>
          <p className="text-xs text-gray-400 mt-0.5">管理数据管道，从数据接入到输出 Curated Dataset</p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-black text-white text-sm rounded-lg hover:bg-gray-800"
        >
          <Plus size={14} /> 新建 Pipeline
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3 items-center">
        <div className="relative flex-1 max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="搜索名称 / ID..."
            className="w-full pl-8 pr-3 py-1.5 border rounded-lg text-sm"
          />
          {search && (
            <button onClick={() => setSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-black">
              <X size={12} />
            </button>
          )}
        </div>
        <select
          value={filterDomain}
          onChange={e => setFilterDomain(e.target.value)}
          className="border rounded-lg px-3 py-1.5 text-sm"
        >
          <option value="">全部领域</option>
          {domains.map(d => <option key={d} value={d}>{d}</option>)}
        </select>
        <select
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
          className="border rounded-lg px-3 py-1.5 text-sm"
        >
          <option value="">全部状态</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <button onClick={load} className="text-xs text-gray-500 hover:text-black px-2 py-1">重置</button>
      </div>

      {runError && <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{runError}</p>}

      {/* List */}
      {loading ? (
        <div className="text-gray-400 text-sm p-8 text-center">加载中...</div>
      ) : filtered.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-12 text-center text-gray-400 space-y-2">
          <GitBranch size={32} className="mx-auto opacity-30" />
          <p className="text-sm font-medium">{search || filterDomain ? '没有匹配的 Pipeline' : '暂无 Pipeline'}</p>
          <p className="text-xs">点击「新建 Pipeline」创建数据管道</p>
        </div>
      ) : (
        <div className="border rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">ID</th>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">名称</th>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">领域</th>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">状态</th>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">版本</th>
                <th className="text-left px-4 py-2.5 font-medium text-gray-600 text-xs">分支</th>
                <th className="text-right px-4 py-2.5 font-medium text-gray-600 text-xs">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map(pl => {
                const visibleStatus = runStatuses[pl.id] || pl.last_run_status || pl.status
                const isRunning = Boolean(runningIds[pl.id])
                return (
                <tr key={pl.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 font-mono text-xs text-gray-400 hover:text-black" title={pl.id}>
                    {pl.id.slice(0, 8)}
                  </td>
                  <td className="px-4 py-3 font-medium">{pl.name}</td>
                  <td className="px-4 py-3 text-xs text-gray-500">{pl.domain || '通用'}</td>
                  <td className="px-4 py-3">
                    <span title={`管线生命周期状态：${STATUS_LABEL[pl.status] || pl.status}`} className={`text-xs px-1.5 py-0.5 rounded border ${STATUS_STYLE[visibleStatus] || STATUS_STYLE.draft}`}>
                      {STATUS_LABEL[visibleStatus] || visibleStatus}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">v{pl.version || 1}</td>
                  <td className="px-4 py-3 text-xs text-gray-500 font-mono">{pl.branch || 'main'}</td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex gap-1 justify-end">
                      <button
                        onClick={() => navigate(`/data/pipelines/${pl.id}`)}
                        className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-black transition-colors"
                        title="编辑"
                      >
                        <ExternalLink size={14} />
                      </button>
                      <button
                        onClick={() => void handleRun(pl.id)}
                        disabled={isRunning}
                        className="p-1.5 rounded hover:bg-gray-100 text-gray-500 hover:text-black transition-colors disabled:opacity-50"
                        title={isRunning ? (runningIds[pl.id] === 'pending' ? '排队中' : '运行中') : '运行'}
                      >
                        {isRunning ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                      </button>
                      <button
                        onClick={() => handleDelete(pl)}
                        className="p-1.5 rounded hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors"
                        title="删除"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Create Modal */}
      {showCreate && (
        <PipelineCreateModal
          onClose={() => setShowCreate(false)}
          onCreated={(pl) => {
            setShowCreate(false)
            navigate(`/data/pipelines/${pl.id}`)
          }}
        />
      )}
    </div>
  )
}

/** Pipeline 创建弹窗 */
function PipelineCreateModal({
  onClose, onCreated,
}: {
  onClose: () => void
  onCreated: (pl: Pipeline) => void
}) {
  const [name, setName] = useState('')
  const [domain, setDomain] = useState(DOMAINS[0])
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleCreate = async () => {
    if (!name.trim()) { setError('请填写 Pipeline 名称'); return }
    setSaving(true)
    setError('')
    try {
      const pl = await pipelinesApi.create({
        name: name.trim(),
        domain,
        description,
        definition: { nodes: [], edges: [] },
      })
      onCreated(pl)
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setError(err?.detail || err?.message || '创建失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-lg p-6 w-[420px]" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-4">
          <h3 className="font-semibold">新建 Pipeline</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-black">
            <X size={16} />
          </button>
        </div>
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Pipeline 名称 *</label>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm"
              placeholder="例：供应链数据清洗"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">业务领域</label>
            <select
              value={domain}
              onChange={e => setDomain(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm"
            >
              {DOMAINS.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">描述</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm"
              rows={3}
              placeholder="Pipeline 用途说明"
            />
          </div>
          {error && <p className="text-red-500 text-xs">{error}</p>}
        </div>
        <div className="flex justify-end gap-3 mt-4">
          <button onClick={onClose} className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={handleCreate}
            disabled={saving}
            className="flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50"
          >
            {saving && <Loader2 size={13} className="animate-spin" />}
            {saving ? '创建中...' : '创建'}
          </button>
        </div>
      </div>
    </div>
  )
}
