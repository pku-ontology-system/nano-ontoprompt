import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClientV2 } from '@/api/client'
import { useAuthStore } from '@/stores/authStore'
import { AlertTriangle, CheckCircle, Clock, Loader2, Plus, Play, Database, ChevronDown, ChevronRight, Send, ShieldCheck, XCircle, MessageSquareText } from 'lucide-react'
import type {
  MappingDefinitionReview,
  MappingLinkApplyPreview,
  MappingLinkApplyRun,
  MappingLinkDefinitionReview,
  MappingLinkLegacyReconciliationReport,
} from '@/api/v2/curated'

interface Mapping {
  id: string
  curated_dataset_id: string | null
  dataset_name: string | null
  row_count: number | null
  entity_class: string
  field_mapping: Record<string, string>
  status: string
  confidence: number | null
  mapping_digest: string
  can_edit: boolean
  latest_review: MappingDefinitionReview | null
  created_at: string | null
}

interface CuratedDataset {
  id: string
  name: string
  status: string
  row_count: number | null
  quality_score: number | null
  quality_status?: string | null
}

interface ApplyMappingResult {
  run_id: string
  review_id?: string
  mapping_review_id?: string
  dataset_version_id?: string
  status: string
  deduplicated?: boolean
  dispatch_error?: string
}

interface ReviewSession {
  review_id: string
  status: string
  dataset_version_id: string | null
  requested_by: string | null
  decided_by: string | null
}

interface MappingFieldSuggestion {
  column_name: string
  property_name: string
  confidence?: number
}

interface MappingSuggestion {
  entity_class: string
  entity_class_cn?: string
  primary_key_column?: string
  field_mappings?: MappingFieldSuggestion[]
}

interface CuratedPreview {
  rows?: Record<string, unknown>[]
}

interface LinkMapping {
  id: string
  src_dataset_id: string | null
  tgt_dataset_id: string | null
  relation_type: string
  src_key: string
  tgt_key: string
  status: string
  can_edit: boolean
  latest_review: MappingLinkDefinitionReview | null
}

function errorDetail(error: unknown, fallback: string): string {
  if (typeof error !== 'object' || error === null) return fallback
  if ('detail' in error && typeof error.detail === 'string') return error.detail
  if ('detail' in error && typeof error.detail === 'object' && error.detail !== null && 'message' in error.detail) {
    return String(error.detail.message)
  }
  if ('message' in error && typeof error.message === 'string') return error.message
  return fallback
}

function StatusChip({ status }: { status: string }) {
  const cfg: Record<string, string> = {
    applied: 'bg-green-50 border-green-200 text-green-700',
    draft: 'bg-gray-50 border-gray-200 text-gray-500',
    active: 'bg-blue-50 border-blue-200 text-blue-700',
    disabled: 'bg-red-50 border-red-200 text-red-700',
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded border ${cfg[status] ?? cfg.draft}`}>
      {status}
    </span>
  )
}

function MappingRow({ mapping, ontologyId, onApplied }: { mapping: Mapping; ontologyId: string; onApplied: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [working, setWorking] = useState(false)
  const [applyResult, setApplyResult] = useState<ApplyMappingResult | null>(null)
  const [applyError, setApplyError] = useState('')
  const [reviewNote, setReviewNote] = useState('')
  const [editingCandidate, setEditingCandidate] = useState(false)
  const [draftEntityClass, setDraftEntityClass] = useState('')
  const [draftFieldMapping, setDraftFieldMapping] = useState('')
  const candidateReview = mapping.latest_review
  const approvedMappingReview = candidateReview?.status === 'approved'
    && candidateReview.candidate_digest === mapping.mapping_digest
    ? candidateReview
    : null

  const handleApply = async () => {
    if (!approvedMappingReview) {
      setApplyError('当前 Mapping 定义没有匹配的已批准版本，请先提交审核')
      return
    }
    setWorking(true)
    setApplyError('')
    setApplyResult(null)
    try {
      if (!mapping.curated_dataset_id) throw new Error('Mapping 未绑定 Curated Dataset')
      const review = await apiClientV2.get<ReviewSession>(
        `/curated/${mapping.curated_dataset_id}/reviews/latest-approved`
      )
      if (review.status !== 'approved' || !review.dataset_version_id) {
        throw new Error('需要先完成数据集审核，并固定不可变数据版本')
      }
      if (!review.requested_by || !review.decided_by || review.requested_by === review.decided_by) {
        throw new Error('Mapping Apply 需要由不同于申请人的审核者批准数据集')
      }
      const res = await apiClientV2.post<ApplyMappingResult>(
        `/ontologies/${ontologyId}/mappings/${mapping.id}/apply-from-dataset`,
        { review_id: review.review_id, mapping_review_id: approvedMappingReview.review_id },
      )
      setApplyResult(res)
      onApplied()
    } catch (error: unknown) {
      setApplyError(errorDetail(error, '执行失败'))
    } finally {
      setWorking(false)
    }
  }

  const handleSubmitReview = async () => {
    setWorking(true)
    setApplyError('')
    try {
      await apiClientV2.post<MappingDefinitionReview>(
        `/ontologies/${ontologyId}/mappings/${mapping.id}/reviews`,
        {},
      )
      onApplied()
    } catch (error: unknown) {
      setApplyError(errorDetail(error, '提交映射审核失败'))
    } finally {
      setWorking(false)
    }
  }

  const handleStartEditing = () => {
    setDraftEntityClass(mapping.entity_class)
    setDraftFieldMapping(JSON.stringify(mapping.field_mapping || {}, null, 2))
    setExpanded(true)
    setEditingCandidate(true)
    setApplyError('')
  }

  const handleSaveCandidate = async () => {
    let fieldMapping: unknown
    try {
      fieldMapping = JSON.parse(draftFieldMapping)
    } catch {
      setApplyError('字段映射必须是有效的 JSON 对象')
      return
    }
    if (typeof fieldMapping !== 'object' || fieldMapping === null || Array.isArray(fieldMapping)) {
      setApplyError('字段映射必须是 JSON 对象')
      return
    }
    setWorking(true)
    setApplyError('')
    try {
      await apiClientV2.post<MappingDefinitionReview>(
        `/ontologies/${ontologyId}/mappings/${mapping.id}/reviews`,
        {
          candidate: {
            entity_class: draftEntityClass,
            field_mapping: fieldMapping,
            confidence: mapping.confidence,
          },
        },
      )
      setEditingCandidate(false)
      onApplied()
    } catch (error: unknown) {
      setApplyError(errorDetail(error, '保存映射并提交审核失败'))
    } finally {
      setWorking(false)
    }
  }

  const handleDecision = async (decision: 'approved' | 'rejected' | 'changes_requested') => {
    if (!candidateReview) return
    setWorking(true)
    setApplyError('')
    try {
      await apiClientV2.post<MappingDefinitionReview>(
        `/ontologies/${ontologyId}/mappings/${mapping.id}/reviews/${candidateReview.review_id}:decide`,
        { decision, notes: reviewNote },
      )
      setReviewNote('')
      onApplied()
    } catch (error: unknown) {
      setApplyError(errorDetail(error, '记录映射审核决定失败'))
    } finally {
      setWorking(false)
    }
  }

  const reviewStatusLabel: Record<string, string> = {
    pending_review: '待独立审核',
    approved: '已批准并发布',
    rejected: '已拒绝',
    changes_requested: '要求修改',
  }

  const fieldEntries = Object.entries(mapping.field_mapping || {}).filter(
    ([k]) => !k.startsWith('__')
  )

  return (
    <div className="border rounded-xl bg-white overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <button
          onClick={() => setExpanded(e => !e)}
          className="text-gray-400 hover:text-gray-700 flex-shrink-0"
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        <Database size={15} className="text-blue-500 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm truncate">{mapping.entity_class}</span>
            <StatusChip status={mapping.status} />
          </div>
          <p className="text-xs text-gray-400 truncate mt-0.5">
            {mapping.dataset_name ?? mapping.curated_dataset_id?.slice(0, 8)}
            {mapping.row_count != null && ` · ${mapping.row_count.toLocaleString()} 行`}
          </p>
        </div>
        {mapping.can_edit && !editingCandidate && (
          <button
            onClick={handleStartEditing}
            disabled={working}
            className="flex items-center gap-1.5 px-3 py-1.5 border border-blue-200 text-blue-700 rounded-lg text-xs hover:bg-blue-50 disabled:opacity-50 flex-shrink-0"
          >
            {candidateReview?.status === 'changes_requested' ? '修改并重新提交' : '编辑并提交审核'}
          </button>
        )}
        {approvedMappingReview ? (
          <button
            onClick={handleApply}
            disabled={working || mapping.status === 'disabled'}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-black text-white rounded-lg text-xs hover:bg-gray-800 disabled:opacity-50 flex-shrink-0"
          >
            {working ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
            {working ? '提交中...' : '应用已批准版本'}
          </button>
        ) : candidateReview?.status === 'pending_review' ? (
          <span className="text-xs text-amber-700 flex-shrink-0">等待独立审核</span>
        ) : (
          <button
            onClick={handleSubmitReview}
            disabled={working || mapping.status === 'disabled'}
            className="flex items-center gap-1.5 px-3 py-1.5 border border-slate-300 rounded-lg text-xs hover:bg-slate-50 disabled:opacity-50 flex-shrink-0"
          >
            {working ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
            {working ? '提交中...' : candidateReview ? '重新提交映射审核' : '提交映射审核'}
          </button>
        )}
      </div>

      {candidateReview && (
        <div className="mx-4 mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-slate-700">
            <span className="font-medium">映射版本 v{candidateReview.revision_no} · {reviewStatusLabel[candidateReview.status] ?? candidateReview.status}</span>
            <span className="font-mono text-slate-500">digest {candidateReview.candidate_digest.slice(0, 12)}</span>
            <span>提交者：{candidateReview.submitted_by?.slice(0, 8) ?? '未知'}</span>
            {candidateReview.reviewer_id && <span>指派审核人：{candidateReview.reviewer_id.slice(0, 8)}</span>}
            {candidateReview.decided_by && <span>决定人：{candidateReview.decided_by.slice(0, 8)}</span>}
          </div>
          {candidateReview.decision_note && <p className="mt-1 text-slate-600">审核意见：{candidateReview.decision_note}</p>}
          {candidateReview.status === 'pending_review' && candidateReview.can_review && (
            <div className="mt-2 space-y-2">
              <textarea
                value={reviewNote}
                onChange={event => setReviewNote(event.target.value)}
                maxLength={4000}
                rows={2}
                placeholder="填写审核说明（拒绝或要求修改时建议填写）"
                className="w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-xs"
              />
              <div className="flex flex-wrap gap-2">
                <button type="button" onClick={() => handleDecision('approved')} disabled={working} className="inline-flex items-center gap-1 rounded bg-emerald-700 px-2.5 py-1.5 text-white disabled:opacity-50">
                  {working ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />}批准并发布
                </button>
                <button type="button" onClick={() => handleDecision('changes_requested')} disabled={working} className="inline-flex items-center gap-1 rounded border border-amber-300 px-2.5 py-1.5 text-amber-800 disabled:opacity-50">
                  <MessageSquareText size={12} />要求修改
                </button>
                <button type="button" onClick={() => handleDecision('rejected')} disabled={working} className="inline-flex items-center gap-1 rounded border border-red-300 px-2.5 py-1.5 text-red-700 disabled:opacity-50">
                  <XCircle size={12} />拒绝
                </button>
              </div>
            </div>
          )}
          {candidateReview.status === 'pending_review' && !candidateReview.can_review && (
            <p className="mt-1 text-slate-500">提交者不能自行批准；等待另一位有编辑权限的审核者。</p>
          )}
          {candidateReview.events?.length ? (
            <details className="mt-2">
              <summary className="cursor-pointer text-slate-600">审核历史（{candidateReview.events.length} 条）</summary>
              <ul className="mt-1 space-y-1 text-[11px] text-slate-500">
                {candidateReview.events.slice(-5).reverse().map(item => (
                  <li key={`${candidateReview.review_id}-${item.sequence_no}`}>
                    #{item.sequence_no} · {item.event_type} · {item.actor_kind}{item.actor_id ? ` ${item.actor_id.slice(0, 8)}` : ''} · {item.status}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      )}

      {/* Apply result */}
      {applyResult && (
        <div className={`mx-4 mb-3 flex items-center gap-2 text-xs rounded-lg px-3 py-2 border ${
          applyResult.status === 'succeeded' ? 'text-green-700 bg-green-50 border-green-200'
            : applyResult.status === 'failed' ? 'text-red-700 bg-red-50 border-red-200'
              : 'text-slate-700 bg-slate-50 border-slate-200'
        }`}>
          {applyResult.status === 'succeeded' ? <CheckCircle size={13} />
            : applyResult.status === 'failed' ? <AlertTriangle size={13} /> : <Clock size={13} />}
          <span>
            {applyResult.status === 'succeeded'
              ? 'Mapping Apply 已完成'
              : applyResult.status === 'failed'
                ? 'Mapping Apply 运行失败'
                : '已创建持久 MappingApplyRun'}
            {' · 状态：'}{applyResult.status}
            {' · Run：'}{applyResult.run_id.slice(0, 8)}
            {applyResult.dataset_version_id && ` · 数据版本：${applyResult.dataset_version_id.slice(0, 8)}`}
            {applyResult.deduplicated && ' · 已复用相同审核版本的运行记录'}
            {applyResult.dispatch_error && ` · 派发待恢复：${applyResult.dispatch_error}`}
          </span>
        </div>
      )}
      {applyError && (
        <div className="mx-4 mb-3 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
          {applyError}
        </div>
      )}

      {/* Field mapping detail */}
      {expanded && editingCandidate && (
        <div className="border-t mx-4 mb-3 pt-3 space-y-3">
          <label className="block text-xs font-medium text-gray-600">
            实体类型
            <input
              value={draftEntityClass}
              onChange={event => setDraftEntityClass(event.target.value)}
              maxLength={200}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm font-normal"
            />
          </label>
          <label className="block text-xs font-medium text-gray-600">
            字段映射（JSON）
            <textarea
              value={draftFieldMapping}
              onChange={event => setDraftFieldMapping(event.target.value)}
              rows={8}
              spellCheck={false}
              className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 font-mono text-xs"
            />
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleSaveCandidate}
              disabled={working || !draftEntityClass.trim()}
              className="rounded bg-blue-700 px-3 py-1.5 text-xs text-white disabled:opacity-50"
            >
              {working ? '保存并提交中...' : '保存并提交新审核版本'}
            </button>
            <button
              type="button"
              onClick={() => { setEditingCandidate(false); setApplyError('') }}
              disabled={working}
              className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 disabled:opacity-50"
            >
              取消
            </button>
          </div>
        </div>
      )}
      {expanded && fieldEntries.length > 0 && (
        <div className="border-t mx-4 mb-3 pt-3">
          <p className="text-xs font-medium text-gray-500 mb-2">字段映射</p>
          <div className="grid grid-cols-2 gap-1.5">
            {fieldEntries.map(([col, prop]) => (
              <div key={col} className="flex items-center gap-1.5 text-xs bg-gray-50 rounded px-2 py-1">
                <span className="font-mono text-gray-500">{col}</span>
                <span className="text-gray-300">→</span>
                <span className="font-mono text-blue-600">{prop}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function LinkDatasetPanel({ ontologyId, onDone }: { ontologyId: string; onDone: () => void }) {
  const [selectedId, setSelectedId] = useState('')
  const [suggesting, setSuggesting] = useState(false)
  const [suggestion, setSuggestion] = useState<MappingSuggestion | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const { data: datasets = [], isLoading } = useQuery<CuratedDataset[]>({
    queryKey: ['curated-all'],
    queryFn: () => apiClientV2.get<CuratedDataset[]>('/curated'),
  })

  const approvedDatasets = (datasets as CuratedDataset[]).filter(d => d.status === 'approved')
  const selectedDataset = approvedDatasets.find(d => d.id === selectedId)

  const handleSuggest = async () => {
    if (!selectedId) return
    setSuggesting(true)
    setError('')
    setSuggestion(null)
    try {
      const ds = approvedDatasets.find(d => d.id === selectedId)
      // Get preview for column info
      const preview = await apiClientV2.get<CuratedPreview>(`/curated/${selectedId}/preview?limit=5`)
      const previewRows = preview.rows ?? []
      const columns = previewRows.length > 0 ? Object.keys(previewRows[0]) : []
      const res = await apiClientV2.post<MappingSuggestion>(`/ontologies/${ontologyId}/mappings/suggest`, {
        dataset_name: ds?.name ?? '',
        columns,
        sample_rows: previewRows.slice(0, 3),
        ontology_domain: '',
      })
      setSuggestion(res)
    } catch (error: unknown) {
      setError(errorDetail(error, '自动建议失败'))
    } finally {
      setSuggesting(false)
    }
  }

  const handleSave = async () => {
    if (!suggestion) return
    setSaving(true)
    setError('')
    try {
      const fieldMapping: Record<string, string> = {}
      for (const fm of suggestion.field_mappings ?? []) {
        fieldMapping[fm.column_name] = fm.property_name
      }
      if (suggestion.primary_key_column) {
        fieldMapping['__primary_key__'] = suggestion.primary_key_column
      }
      await apiClientV2.post(`/ontologies/${ontologyId}/mappings`, {
        curated_dataset_id: selectedId,
        entity_class: suggestion.entity_class,
        field_mapping: fieldMapping,
        confidence: 0.9,
      })
      onDone()
    } catch (error: unknown) {
      setError(errorDetail(error, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="border rounded-xl bg-white p-4 space-y-4">
      <p className="text-sm font-medium">关联 Curated 数据集</p>

      {isLoading ? (
        <p className="text-xs text-gray-400">加载中...</p>
      ) : approvedDatasets.length === 0 ? (
        <p className="text-xs text-gray-400">暂无已审批的 Curated Dataset。请先在 Pipeline → Curated 中完成审批。</p>
      ) : (
        <>
          <select
            value={selectedId}
            onChange={e => { setSelectedId(e.target.value); setSuggestion(null) }}
            className="w-full border rounded-lg px-3 py-2 text-sm"
          >
            <option value="">选择 Curated Dataset...</option>
            {approvedDatasets.map(d => (
              <option key={d.id} value={d.id}>
                {d.name}{d.row_count != null ? ` (${d.row_count.toLocaleString()} 行)` : ''}{d.quality_status === 'not_evaluated_degraded' ? ' · LLM 降级/质量未判定' : ''}
              </option>
            ))}
          </select>

          {selectedDataset?.quality_status === 'not_evaluated_degraded' && (
            <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              此数据集虽已人工审批，但来源 Pipeline 使用了规则回退，质量未判定；请在生成 Mapping 前检查字段和记录。
            </p>
          )}

          {selectedId && !suggestion && (
            <button
              onClick={handleSuggest}
              disabled={suggesting}
              className="flex items-center gap-1.5 px-4 py-2 border border-black rounded-lg text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              {suggesting ? <Loader2 size={14} className="animate-spin" /> : null}
              {suggesting ? '生成映射中...' : '自动生成 Mapping'}
            </button>
          )}

          {error && <p className="text-xs text-red-500">{error}</p>}

          {suggestion && (
            <div className="space-y-3">
              <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-xs space-y-1">
                <p className="font-medium text-blue-700">实体类型：{suggestion.entity_class}
                  {suggestion.entity_class_cn && <span className="ml-1 text-blue-500">({suggestion.entity_class_cn})</span>}
                </p>
                {suggestion.primary_key_column && (
                  <p className="text-blue-600">主键列：<span className="font-mono">{suggestion.primary_key_column}</span></p>
                )}
              </div>
              <div className="space-y-1">
                <p className="text-xs font-medium text-gray-500">字段映射建议</p>
                {(suggestion.field_mappings ?? []).map(fm => (
                  <div key={fm.column_name} className="flex items-center gap-2 text-xs bg-gray-50 rounded px-2 py-1.5">
                    <span className="font-mono text-gray-600 w-32 truncate">{fm.column_name}</span>
                    <span className="text-gray-300">→</span>
                    <span className="font-mono text-blue-600">{fm.property_name}</span>
                    {fm.confidence != null && (
                      <span className="ml-auto text-gray-300">{Math.round(fm.confidence * 100)}%</span>
                    )}
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="flex items-center gap-1.5 px-4 py-2 bg-black text-white rounded-lg text-sm disabled:opacity-50"
                >
                  {saving ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle size={13} />}
                  {saving ? '保存中...' : '确认并保存 Mapping'}
                </button>
                <button
                  onClick={() => setSuggestion(null)}
                  className="px-3 py-2 text-sm border rounded-lg hover:bg-gray-50"
                >
                  重新生成
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function LinkMappingReviewRow({ link, ontologyId, onUpdated }: { link: LinkMapping; ontologyId: string; onUpdated: () => void }) {
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<MappingLinkApplyPreview | null>(null)
  const [legacyReport, setLegacyReport] = useState<MappingLinkLegacyReconciliationReport | null>(null)
  const [clientRequestId, setClientRequestId] = useState('')
  const [replayReason, setReplayReason] = useState('')
  const [replayRequestId, setReplayRequestId] = useState('')
  const [notes, setNotes] = useState('')
  const [editing, setEditing] = useState(false)
  const [relationType, setRelationType] = useState(link.relation_type)
  const [srcKey, setSrcKey] = useState(link.src_key)
  const [tgtKey, setTgtKey] = useState(link.tgt_key)
  const review = link.latest_review
  const isAdmin = useAuthStore(state => state.user?.role === 'admin')
  const queryClient = useQueryClient()
  const { data: applyRuns = [], refetch: refreshApplyRuns } = useQuery<MappingLinkApplyRun[]>({
    queryKey: ['link-mapping-apply-runs', ontologyId, link.id],
    queryFn: () => apiClientV2.get<MappingLinkApplyRun[]>(`/ontologies/${ontologyId}/link-mappings/${link.id}/apply-runs?limit=10`),
    enabled: review?.status === 'approved',
    refetchInterval: query => query.state.data?.some(run =>
      ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(run.status)
      || run.projection_summary?.manual_replays.some(replay => ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(replay.status)),
    ) ? 2500 : false,
  })
  const activeRun = applyRuns.find(run => ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(run.status))

  const submit = async (withCandidate: boolean) => {
    setWorking(true)
    setError('')
    try {
      await apiClientV2.post<MappingLinkDefinitionReview>(
        `/ontologies/${ontologyId}/link-mappings/${link.id}/reviews`,
        withCandidate ? { candidate: { relation_type: relationType, src_key: srcKey, tgt_key: tgtKey } } : {},
      )
      setEditing(false)
      setPreview(null)
      onUpdated()
    } catch (err: unknown) {
      setError(errorDetail(err, '提交关系映射审核失败'))
    } finally {
      setWorking(false)
    }
  }

  const decide = async (decision: 'approved' | 'rejected' | 'changes_requested') => {
    if (!review) return
    setWorking(true)
    setError('')
    try {
      await apiClientV2.post(
        `/ontologies/${ontologyId}/link-mappings/${link.id}/reviews/${review.review_id}:decide`,
        { decision, notes },
      )
      setNotes('')
      setPreview(null)
      onUpdated()
    } catch (err: unknown) {
      setError(errorDetail(err, '记录关系映射审核决定失败'))
    } finally {
      setWorking(false)
    }
  }

  const previewApply = async () => {
    if (!review || review.status !== 'approved') return
    setWorking(true)
    setError('')
    setPreview(null)
    setClientRequestId('')
    try {
      const result = await apiClientV2.post<MappingLinkApplyPreview>(
        `/ontologies/${ontologyId}/link-mappings/${link.id}/apply-preview`,
        { link_review_id: review.review_id },
      )
      setPreview(result)
    } catch (err: unknown) {
      setError(errorDetail(err, '生成关系变更预览失败'))
    } finally {
      setWorking(false)
    }
  }

  const previewLegacyReconciliation = async () => {
    if (!review || review.status !== 'approved' || !isAdmin) return
    setWorking(true)
    setError('')
    setLegacyReport(null)
    try {
      const result = await apiClientV2.post<{
        preview_digest: string
        legacy_reconciliation: MappingLinkLegacyReconciliationReport
      }>(
        `/ontologies/${ontologyId}/link-mappings/${link.id}:reconciliation-preview`,
        { link_review_id: review.review_id },
      )
      setLegacyReport(result.legacy_reconciliation)
    } catch (err: unknown) {
      setError(errorDetail(err, '生成旧关系只读对账预览失败'))
    } finally {
      setWorking(false)
    }
  }

  const confirmApply = async () => {
    if (!review || !preview) return
    setWorking(true)
    setError('')
    const requestId = clientRequestId || crypto.randomUUID()
    setClientRequestId(requestId)
    try {
      await apiClientV2.post<MappingLinkApplyRun>(
        `/ontologies/${ontologyId}/link-mappings/${link.id}/apply`,
        {
          link_review_id: review.review_id,
          preview_digest: preview.preview_digest,
          client_request_id: requestId,
        },
      )
      setPreview(null)
      setClientRequestId('')
      await refreshApplyRuns()
      onUpdated()
    } catch (err: unknown) {
      setError(errorDetail(err, '提交关系写入任务失败'))
    } finally {
      setWorking(false)
    }
  }

  const replayProjection = async (run: MappingLinkApplyRun) => {
    if (!replayReason.trim()) return
    setWorking(true)
    setError('')
    const requestId = replayRequestId || crypto.randomUUID()
    setReplayRequestId(requestId)
    try {
      await apiClientV2.post<{ status: string; in_progress?: boolean }>(
        `/ontologies/${ontologyId}/link-mappings/${link.id}/apply-runs/${run.run_id}:reconcile-projection`,
        { reason: replayReason.trim(), client_request_id: requestId, batch_size: 100 },
      )
      setReplayReason('')
      setReplayRequestId('')
      await refreshApplyRuns()
      onUpdated()
    } catch (err: unknown) {
      setError(errorDetail(err, '关系图投影人工重放失败'))
    } finally {
      setWorking(false)
    }
  }

  const runStatusLabel: Record<string, string> = {
    pending: '等待派发',
    dispatching: '正在派发',
    queued: '队列中',
    running: '执行中',
    retry_wait: '等待重试',
    succeeded: '已完成',
    failed: '失败',
  }

  const replayStatusLabel: Record<string, string> = {
    pending: '人工重放待派发',
    dispatching: '人工重放正在派发',
    queued: '人工重放排队中',
    running: '人工重放执行中',
    retry_wait: '人工重放等待恢复',
    completed: '人工重放完成',
    partial: '人工重放部分完成',
    failed: '人工重放失败',
  }

  const legacyClassificationLabel: Record<string, string> = {
    exact_pair_legacy_mapping: '目标边、旧映射标记和实体类型均匹配',
    mapping_signature_and_types: '旧映射标记与实体类型匹配',
    mapping_signature_without_endpoint_confirmation: '有旧映射标记，端点尚未确认',
    current_dataset_endpoint_overlap: '端点与当前数据版本重合',
    same_entity_types_unverified: '实体类型相同，来源尚未确认',
    incomplete_endpoint_evidence: '端点证据不足',
  }

  const statusLabel: Record<string, string> = {
    pending_review: '待独立审核',
    approved: '定义已批准',
    rejected: '已拒绝',
    changes_requested: '要求修改',
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-slate-800">{link.relation_type}</span>
        <span className="font-mono text-slate-500">{link.src_key} → {link.tgt_key}</span>
        <span className="text-slate-500">{link.status}</span>
        {review && <span className="rounded border px-2 py-0.5 text-slate-600">v{review.revision_no} · {statusLabel[review.status] ?? review.status}</span>}
        {link.can_edit && !editing && (
          <button
            type="button"
            onClick={() => { setEditing(true); setRelationType(link.relation_type); setSrcKey(link.src_key); setTgtKey(link.tgt_key); setError('') }}
            className="ml-auto rounded border border-blue-200 px-2 py-1 text-blue-700 hover:bg-blue-50"
          >
            {review?.status === 'changes_requested' ? '修改并重新提交' : '编辑关系定义'}
          </button>
        )}
        {!review && !editing && (
          <button type="button" onClick={() => void submit(false)} disabled={working} className="ml-auto rounded border px-2 py-1 hover:bg-slate-50 disabled:opacity-50">
            {working ? '提交中…' : '提交独立审核'}
          </button>
        )}
      </div>

      {review && (
        <div className="rounded bg-slate-50 px-2.5 py-2 text-[11px] text-slate-600">
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            <span>提交者：{review.submitted_by?.slice(0, 8) ?? '未知'}</span>
            {review.reviewer_id && <span>指派审核人：{review.reviewer_id.slice(0, 8)}</span>}
            {review.decided_by && <span>决定人：{review.decided_by.slice(0, 8)}</span>}
            <span className="font-mono">digest {review.candidate_digest.slice(0, 12)}</span>
          </div>
          {review.decision_note && <p className="mt-1">审核意见：{review.decision_note}</p>}
          {review.status === 'pending_review' && review.can_review && (
            <div className="mt-2 space-y-2">
              <textarea value={notes} onChange={event => setNotes(event.target.value)} maxLength={4000} rows={2} placeholder="审核说明" className="w-full rounded border bg-white px-2 py-1.5" />
              <div className="flex flex-wrap gap-2">
                <button type="button" onClick={() => void decide('approved')} disabled={working} className="rounded bg-emerald-700 px-2.5 py-1.5 text-white disabled:opacity-50">批准定义</button>
                <button type="button" onClick={() => void decide('changes_requested')} disabled={working} className="rounded border border-amber-300 px-2.5 py-1.5 text-amber-800 disabled:opacity-50">要求修改</button>
                <button type="button" onClick={() => void decide('rejected')} disabled={working} className="rounded border border-red-300 px-2.5 py-1.5 text-red-700 disabled:opacity-50">拒绝</button>
              </div>
            </div>
          )}
          {review.status === 'pending_review' && !review.can_review && <p className="mt-1">提交者不能自行批准；等待有权限的独立审核者。</p>}
          {review.events?.length ? (
            <details className="mt-2">
              <summary className="cursor-pointer">审核历史（{review.events.length} 条）</summary>
              <ul className="mt-1 space-y-1">
                {review.events.slice(-5).reverse().map(item => <li key={`${review.review_id}-${item.sequence_no}`}>#{item.sequence_no} · {item.event_type} · {item.actor_kind}{item.actor_id ? ` ${item.actor_id.slice(0, 8)}` : ''}</li>)}
              </ul>
            </details>
          ) : null}
        </div>
      )}

      {editing && (
        <div className="grid gap-2 border-t pt-2 sm:grid-cols-3">
          <label className="text-[11px] text-slate-600">关系类型<input value={relationType} onChange={event => setRelationType(event.target.value)} maxLength={100} className="mt-1 w-full rounded border px-2 py-1.5 text-xs" /></label>
          <label className="text-[11px] text-slate-600">来源匹配字段<input value={srcKey} onChange={event => setSrcKey(event.target.value)} maxLength={100} className="mt-1 w-full rounded border px-2 py-1.5 text-xs" /></label>
          <label className="text-[11px] text-slate-600">目标匹配字段<input value={tgtKey} onChange={event => setTgtKey(event.target.value)} maxLength={100} className="mt-1 w-full rounded border px-2 py-1.5 text-xs" /></label>
          <div className="sm:col-span-3 flex gap-2">
            <button type="button" onClick={() => void submit(true)} disabled={working || !relationType.trim() || !srcKey.trim() || !tgtKey.trim()} className="rounded bg-blue-700 px-3 py-1.5 text-xs text-white disabled:opacity-50">保存并提交新版本</button>
            <button type="button" onClick={() => { setEditing(false); setError('') }} disabled={working} className="rounded border px-3 py-1.5 text-xs">取消</button>
          </div>
        </div>
      )}
      {review?.status === 'approved' && (
        <div className="space-y-2 rounded border border-emerald-200 bg-emerald-50/60 px-2.5 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-[11px] text-emerald-900">关系定义已独立批准。请先完成两端实体映射审核与 Apply，再预览关系边变化。</p>
            {isAdmin && (
              <button type="button" onClick={() => void previewLegacyReconciliation()} disabled={working} className="rounded border border-amber-300 bg-white px-2.5 py-1.5 text-xs text-amber-900 disabled:opacity-50">
                {working ? '处理中…' : '只读检查旧关系'}
              </button>
            )}
            <button type="button" onClick={() => void previewApply()} disabled={working || Boolean(activeRun)} className="ml-auto rounded border border-emerald-300 bg-white px-2.5 py-1.5 text-xs text-emerald-800 disabled:opacity-50">
              {working ? '处理中…' : '预览关系变更'}
            </button>
          </div>
          {legacyReport && (
            <div className="rounded border border-amber-200 bg-amber-50 p-2.5 text-[11px] text-amber-950">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-medium">
                <span>只读旧关系对账 · 扫描 {legacyReport.scanned_relation_count}/{legacyReport.scan_limit}</span>
                <span>候选 {legacyReport.candidate_count}</span>
                {legacyReport.scan_truncated && <span className="text-red-700">扫描达到上限，结果不完整</span>}
                {legacyReport.candidate_list_truncated && <span>候选清单仅显示前 {legacyReport.candidate_list_limit} 条</span>}
              </div>
              <p className="mt-1">只读检查，不会变更 PostgreSQL Relation 或图数据库。证据摘要：<span className="font-mono">{legacyReport.evidence_digest.slice(0, 16)}</span></p>
              {legacyReport.candidate_relations.length > 0 ? (
                <details className="mt-2">
                  <summary className="cursor-pointer">查看候选边（已列 {legacyReport.candidate_relations.length} 条，共 {legacyReport.candidate_count} 条）</summary>
                  <ul className="mt-1 max-h-48 space-y-1 overflow-y-auto">
                    {legacyReport.candidate_relations.map(candidate => (
                      <li key={candidate.relation_id} className="break-all">
                        <span className="font-mono">{candidate.relation_id.slice(0, 12)}</span>
                        {' · '}{legacyClassificationLabel[candidate.classification] ?? candidate.classification}
                        {' · '}{candidate.source_entity_type ?? '未知'} → {candidate.target_entity_type ?? '未知'}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : <p className="mt-1">本次有界扫描未发现候选旧关系。</p>}
            </div>
          )}
          {preview && (
            <div className="rounded border border-emerald-200 bg-white p-2.5 text-[11px] text-slate-700">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                <span>匹配关系：{preview.summary.matched_relation_count}</span>
                <span className="text-emerald-700">新增：{preview.summary.added_relation_count}</span>
                <span className="text-red-700">移除：{preview.summary.removed_relation_count}</span>
                <span>不变：{preview.summary.unchanged_relation_count}</span>
                <span>基数：{preview.summary.cardinality}</span>
              </div>
              <div className="mt-1 text-slate-500">
                固定来源版本 <span className="font-mono">{preview.source_dataset_version_id.slice(0, 12)}</span>
                {' · '}目标版本 <span className="font-mono">{preview.target_dataset_version_id.slice(0, 12)}</span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button type="button" onClick={() => void confirmApply()} disabled={working} className="rounded bg-emerald-700 px-2.5 py-1.5 text-white disabled:opacity-50">
                  {working ? '提交中…' : '确认并执行关系写入'}
                </button>
                <span className="text-slate-500">提交后会先保存关系事实和投影记录，再由后台写入图数据库。</span>
              </div>
            </div>
          )}
          {applyRuns.length > 0 && (
            <div className="space-y-1.5">
              {applyRuns.slice(0, 3).map(run => (
                <div key={run.run_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-2.5 py-2 text-[11px]">
                  <span className={run.status === 'succeeded' ? 'font-medium text-emerald-700' : run.status === 'failed' ? 'font-medium text-red-700' : 'font-medium text-blue-700'}>
                    {run.status === 'succeeded' && run.projection_summary &&
                      run.projection_summary.state_counts.projected === run.projection_summary.change_count &&
                      run.projection_summary.pending_changes === 0 && run.projection_summary.dead_letter_changes === 0
                      ? '关系事实与图投影已完成'
                      : run.status === 'failed' && run.projection_summary && run.projection_summary.change_count > 0 &&
                        run.projection_summary.state_counts.projected === run.projection_summary.change_count &&
                        run.projection_summary.pending_changes === 0 && run.projection_summary.dead_letter_changes === 0
                        ? 'ApplyRun 保留失败终态；其关系投影 outbox 已完成'
                        : runStatusLabel[run.status] ?? run.status}
                  </span>
                  {run.preview_summary && <span>关系边 {run.preview_summary.matched_relation_count}</span>}
                  <span className="font-mono text-slate-400">Run {run.run_id.slice(0, 8)}</span>
                  {run.error_message && <span className="text-red-700">{run.error_message}</span>}
                  {run.attempt_history_valid === false && <span className="text-amber-700">执行历史校验异常</span>}
                  {run.projection_summary && (
                    <div className="w-full border-t border-slate-100 pt-1 text-slate-600">
                      <span>图投影：待处理 {run.projection_summary.pending_changes} · 终态隔离 {run.projection_summary.dead_letter_changes}</span>
                      <span className="ml-3">自动尝试 {run.projection_summary.automatic_attempts} · 人工尝试 {run.projection_summary.manual_attempts}</span>
                      {run.projection_summary.attempt_history_valid === false && <span className="ml-3 text-amber-700">投影尝试摘要链校验异常</span>}
                      {run.projection_summary.attempt_history_truncated && <span className="ml-3 text-slate-400">仅显示最近投影事件</span>}
                    </div>
                  )}
                  {run.projection_summary?.attempt_events.length ? (
                    <details className="w-full text-slate-500">
                      <summary className="cursor-pointer">投影尝试历史（最近 {run.projection_summary.attempt_events.length} 条）</summary>
                      <ul className="mt-1 space-y-1">
                        {run.projection_summary.attempt_events.slice(-8).reverse().map(event => (
                          <li key={event.event_id}>
                            {event.attempt_kind === 'manual' ? '人工' : '自动'} #{event.attempt_number} · {event.event_type}
                            {' · '}{event.actor_kind} {event.actor_id.slice(0, 8)}
                            {event.outcome ? ` · ${event.outcome}` : ''}
                            {event.reason ? ` · 原因：${event.reason}` : ''}
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                  {run.projection_summary?.manual_replays.map(replay => (
                    <div key={replay.request_id} className="w-full border-t border-slate-100 pt-1 text-slate-600">
                      <span className={['failed', 'partial'].includes(replay.status) ? 'text-amber-700' : replay.status === 'completed' ? 'text-emerald-700' : 'text-blue-700'}>
                        {replayStatusLabel[replay.status] ?? replay.status}
                      </span>
                      <span className="ml-2">{replay.projected_count}/{replay.selected_count} 关系边 · 派发 {replay.dispatch_attempts ?? 0} 次</span>
                      {replay.error_code && <span className="ml-2 text-amber-700">{replay.error_code}</span>}
                      <span className="ml-2 text-slate-400">原因：{replay.reason}</span>
                    </div>
                  ))}
                  {isAdmin && run.projection_summary && (run.projection_summary.pending_changes > 0 || run.projection_summary.dead_letter_changes > 0) && (
                    <div className="flex w-full flex-wrap items-center gap-2 border-t border-slate-100 pt-1">
                      <input
                        value={replayReason}
                        onChange={event => { setReplayReason(event.target.value); setReplayRequestId('') }}
                        maxLength={2000}
                        placeholder="填写人工重放原因"
                        className="min-w-64 flex-1 rounded border px-2 py-1.5"
                      />
                      <button
                        type="button"
                        onClick={() => void replayProjection(run)}
                        disabled={working || !replayReason.trim() || run.projection_summary.manual_replays.some(replay => ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(replay.status))}
                        className="rounded border border-amber-300 px-2.5 py-1.5 text-amber-800 disabled:opacity-50"
                      >
                        {working ? '提交中…' : run.projection_summary.manual_replays.some(replay => ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(replay.status)) ? '已有恢复任务运行' : '管理员人工重放'}
                      </button>
                      <span className="text-slate-400">任务由后台处理；每条关系最多 5 次独立人工尝试，不会重置自动预算。</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  )
}

function LinkMappingsPanel({ ontologyId, onUpdated }: { ontologyId: string; onUpdated: () => void }) {
  const [srcDatasetId, setSrcDatasetId] = useState('')
  const [tgtDatasetId, setTgtDatasetId] = useState('')
  const [relationType, setRelationType] = useState('')
  const [srcKey, setSrcKey] = useState('')
  const [tgtKey, setTgtKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const { data: datasets = [] } = useQuery<CuratedDataset[]>({
    queryKey: ['curated-all'],
    queryFn: () => apiClientV2.get<CuratedDataset[]>('/curated'),
  })
  const { data: links = [], isLoading } = useQuery<LinkMapping[]>({
    queryKey: ['link-mappings', ontologyId],
    queryFn: () => apiClientV2.get<LinkMapping[]>(`/ontologies/${ontologyId}/link-mappings`),
  })
  const approvedDatasets = datasets.filter(dataset => dataset.status === 'approved')
  const srcPreview = useQuery<CuratedPreview>({
    queryKey: ['link-mapping-preview', srcDatasetId],
    queryFn: () => apiClientV2.get<CuratedPreview>(`/curated/${srcDatasetId}/preview?limit=1`),
    enabled: Boolean(srcDatasetId),
  })
  const tgtPreview = useQuery<CuratedPreview>({
    queryKey: ['link-mapping-preview', tgtDatasetId],
    queryFn: () => apiClientV2.get<CuratedPreview>(`/curated/${tgtDatasetId}/preview?limit=1`),
    enabled: Boolean(tgtDatasetId),
  })
  const srcColumns = Object.keys(srcPreview.data?.rows?.[0] ?? {})
  const tgtColumns = Object.keys(tgtPreview.data?.rows?.[0] ?? {})

  const createLinkMapping = async () => {
    setSaving(true)
    setError('')
    try {
      await apiClientV2.post(`/ontologies/${ontologyId}/link-mappings`, {
        src_dataset_id: srcDatasetId,
        tgt_dataset_id: tgtDatasetId,
        relation_type: relationType,
        src_key: srcKey,
        tgt_key: tgtKey,
      })
      setRelationType('')
      setSrcKey('')
      setTgtKey('')
      onUpdated()
    } catch (err: unknown) {
      setError(errorDetail(err, '创建关系映射失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div>
        <h3 className="text-sm font-medium text-slate-800">关系映射</h3>
        <p className="mt-1 text-xs text-slate-500">固定来源与目标字段，提交独立审核并保留版本历史。</p>
      </div>
      <div className="grid gap-2 rounded-lg border bg-white p-3 sm:grid-cols-2 lg:grid-cols-5">
        <label className="text-[11px] text-slate-600">来源数据集<select value={srcDatasetId} onChange={event => { setSrcDatasetId(event.target.value); setSrcKey('') }} className="mt-1 w-full rounded border px-2 py-1.5 text-xs"><option value="">选择来源</option>{approvedDatasets.map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}</select></label>
        <label className="text-[11px] text-slate-600">来源字段<select value={srcKey} onChange={event => setSrcKey(event.target.value)} disabled={!srcDatasetId} className="mt-1 w-full rounded border px-2 py-1.5 text-xs"><option value="">选择字段</option>{srcColumns.map(column => <option key={column}>{column}</option>)}</select></label>
        <label className="text-[11px] text-slate-600">目标数据集<select value={tgtDatasetId} onChange={event => { setTgtDatasetId(event.target.value); setTgtKey('') }} className="mt-1 w-full rounded border px-2 py-1.5 text-xs"><option value="">选择目标</option>{approvedDatasets.map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}</select></label>
        <label className="text-[11px] text-slate-600">目标字段<select value={tgtKey} onChange={event => setTgtKey(event.target.value)} disabled={!tgtDatasetId} className="mt-1 w-full rounded border px-2 py-1.5 text-xs"><option value="">选择字段</option>{tgtColumns.map(column => <option key={column}>{column}</option>)}</select></label>
        <label className="text-[11px] text-slate-600">关系类型<input value={relationType} onChange={event => setRelationType(event.target.value)} maxLength={100} placeholder="例如 SUPPLIES" className="mt-1 w-full rounded border px-2 py-1.5 text-xs" /></label>
        <div className="sm:col-span-2 lg:col-span-5 flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => void createLinkMapping()} disabled={saving || !srcDatasetId || !tgtDatasetId || !srcKey || !tgtKey || !relationType.trim()} className="rounded bg-slate-900 px-3 py-1.5 text-xs text-white disabled:opacity-50">{saving ? '创建中…' : '创建关系映射草稿'}</button>
          {error && <span className="text-xs text-red-600">{error}</span>}
        </div>
      </div>
      {isLoading ? <p className="text-xs text-slate-400">加载关系映射…</p> : links.length === 0 ? <p className="text-xs text-slate-400">尚无关系映射。</p> : (
        <div className="space-y-2">{links.map(link => <LinkMappingReviewRow key={link.id} link={link} ontologyId={ontologyId} onUpdated={onUpdated} />)}</div>
      )}
    </section>
  )
}

export default function CuratedDatasetsTab({ ontologyId }: { ontologyId: string }) {
  const qc = useQueryClient()
  const [showLink, setShowLink] = useState(false)

  const { data: mappings = [], isLoading } = useQuery<Mapping[]>({
    queryKey: ['mappings', ontologyId],
    queryFn: () => apiClientV2.get<Mapping[]>(`/ontologies/${ontologyId}/mappings`),
  })

  const handleApplied = () => {
    qc.invalidateQueries({ queryKey: ['mappings', ontologyId] })
    qc.invalidateQueries({ queryKey: ['link-mappings', ontologyId] })
    qc.invalidateQueries({ queryKey: ['entities', ontologyId] })
    qc.invalidateQueries({ queryKey: ['stats'] })
  }

  const handleLinkDone = () => {
    setShowLink(false)
    qc.invalidateQueries({ queryKey: ['mappings', ontologyId] })
  }

  const handleLinkMappingsUpdated = () => {
    qc.invalidateQueries({ queryKey: ['link-mappings', ontologyId] })
  }

  if (isLoading) return <div className="text-gray-400 text-sm py-8 text-center">加载中...</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          关联已审批的 Curated Dataset，配置字段映射后即可构建本体实体。
        </p>
        <button
          onClick={() => setShowLink(v => !v)}
          className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm hover:bg-gray-50"
        >
          <Plus size={14} />
          关联数据集
        </button>
      </div>

      {showLink && (
        <LinkDatasetPanel ontologyId={ontologyId} onDone={handleLinkDone} />
      )}

      {(mappings as Mapping[]).length === 0 && !showLink ? (
        <div className="border-2 border-dashed rounded-xl py-16 text-center text-gray-400">
          <Database size={32} className="mx-auto mb-3 opacity-30" />
          <p className="text-sm">尚未关联任何 Curated Dataset</p>
          <p className="text-xs mt-1">点击右上角"关联数据集"开始配置 Mapping</p>
        </div>
      ) : (
        <div className="space-y-2">
          {(mappings as Mapping[]).map(m => (
            <MappingRow key={m.id} mapping={m} ontologyId={ontologyId} onApplied={handleApplied} />
          ))}
        </div>
      )}
      <LinkMappingsPanel ontologyId={ontologyId} onUpdated={handleLinkMappingsUpdated} />
    </div>
  )
}
