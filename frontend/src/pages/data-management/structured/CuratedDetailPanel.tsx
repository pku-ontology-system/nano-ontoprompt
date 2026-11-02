import { useEffect, useRef, useState } from 'react'
import {
  X, CheckCircle, AlertTriangle, Clock,
  Save, Trash2, Loader2, Pencil,
} from 'lucide-react'
import curatedApi, {
  curatedReviewsBlockingDeleteCount,
  type CuratedRowEdit,
  type MappingApplyRun,
  type MappingRowProjectionAttempt,
  type ReviewSession,
} from '@/api/v2/curated'
import ConfirmDialog from '@/components/ConfirmDialog'
import { useAuthStore } from '@/stores/authStore'
import { formatApiError } from '@/api/client'

interface Props {
  datasetId: string
  datasetName: string
  datasetStatus: string
  pipelineName: string
  onClose: () => void
  onStatusChange: (id: string, status: string) => void
  onDeleted: (id: string) => void
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: '待审核',
  pending: '待独立审核',
  in_review: '审核中',
  approved:       '已审核',
  rejected:       '已拒绝',
}

const STATUS_STYLE: Record<string, string> = {
  pending_review: 'bg-yellow-50 text-yellow-700 border-yellow-200',
  pending:        'bg-amber-50 text-amber-700 border-amber-200',
  in_review:      'bg-amber-50 text-amber-700 border-amber-200',
  approved:       'bg-green-50 text-green-700 border-green-200',
  rejected:       'bg-red-50 text-red-600 border-red-200',
}

const STATUS_ICON = (status: string) => {
  if (status === 'approved') return <CheckCircle size={13} className="text-green-500" />
  if (status === 'rejected') return <AlertTriangle size={13} className="text-red-400" />
  return <Clock size={13} className="text-yellow-400" />
}

const MAPPING_RUN_LABEL: Record<string, string> = {
  pending: '等待投递',
  dispatching: '正在投递',
  queued: '队列中',
  running: '映射处理中',
  retry_wait: '等待重试',
  succeeded: '映射完成',
  failed: '映射失败',
}

type CellKey = `${number}::${string}`

const isScalarEditable = (value: unknown): boolean =>
  value === null
  || typeof value === 'string'
  || typeof value === 'boolean'
  || (typeof value === 'number' && Number.isFinite(value))

export default function CuratedDetailPanel({
  datasetId, datasetName, datasetStatus, pipelineName,
  onClose, onStatusChange, onDeleted,
}: Props) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [cols, setCols] = useState<string[]>([])
  const [rowTokens, setRowTokens] = useState<string[]>([])
  const [protectedFields, setProtectedFields] = useState<Set<string>>(new Set(['id', '__pk__']))
  const [previewVersionId, setPreviewVersionId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [reviewEditPreviewWarning, setReviewEditPreviewWarning] = useState('')
  const [status, setStatus] = useState(datasetStatus)
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [reviewAudit, setReviewAudit] = useState<ReviewSession | null>(null)
  const [reviewEdits, setReviewEdits] = useState<CuratedRowEdit[]>([])
  const [reviewEditTotal, setReviewEditTotal] = useState(0)
  const [editAuditExpanded, setEditAuditExpanded] = useState(false)
  const [runReviewId, setRunReviewId] = useState<string | null>(null)
  const [mappingRuns, setMappingRuns] = useState<MappingApplyRun[]>([])
  const currentUserRole = useAuthStore(state => state.user?.role)
  const isAdmin = currentUserRole === 'admin'
  const currentUserId = useAuthStore(state => state.user?.id ?? null)
  const [projectionRecoveryRunId, setProjectionRecoveryRunId] = useState<string | null>(null)
  const [projectionReason, setProjectionReason] = useState('')
  const [projectionRequestId, setProjectionRequestId] = useState('')
  const [projectionRequestLocked, setProjectionRequestLocked] = useState(false)
  const [projectionBusy, setProjectionBusy] = useState(false)
  const [projectionError, setProjectionError] = useState('')
  const [projectionNotice, setProjectionNotice] = useState('')
  const [projectionHistoryRunId, setProjectionHistoryRunId] = useState<string | null>(null)
  const [attemptHistoryRunId, setAttemptHistoryRunId] = useState<string | null>(null)

  const [editingCell, setEditingCell] = useState<CellKey | null>(null)
  const [pendingEdits, setPendingEdits] = useState<Map<CellKey, { old: string; val: string }>>(new Map())
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const editInputRef = useRef<HTMLInputElement>(null)

  const [approving, setApproving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [cascadeDeleteCount, setCascadeDeleteCount] = useState<number | null>(null)
  const [deleteError, setDeleteError] = useState('')

  const startsNewReview = Boolean(
    reviewAudit?.status === 'rejected'
    || (reviewAudit?.status === 'approved'
      && Boolean(previewVersionId)
      && reviewAudit.dataset_version_id !== previewVersionId),
  )
  const canEditRows = (isAdmin || currentUserRole === 'editor')
    && Boolean(previewVersionId)
    && (!reviewAudit || startsNewReview || (
      reviewAudit.status === 'pending'
      && Boolean(reviewAudit.dataset_version_id)
      && reviewAudit.dataset_version_id === previewVersionId
      && (isAdmin
        || reviewAudit.requested_by === currentUserId
        || reviewAudit.reviewer_id === currentUserId)
    ))

  useEffect(() => {
    let active = true
    setLoading(true)
    setLoadError('')
    setRows([])
    setCols([])
    setRowTokens([])
    setProtectedFields(new Set(['id', '__pk__']))
    setPreviewVersionId(null)
    setReviewEditPreviewWarning('')
    setReviewId(null)
    setReviewAudit(null)
    setRunReviewId(null)
    setMappingRuns([])
    setReviewEdits([])
    setReviewEditTotal(0)
    setPendingEdits(new Map())
    setEditingCell(null)
    setSaveMsg('')

    const loadReviewAndPreview = async () => {
      try {
        const review = await curatedApi.latestReview(datasetId).catch(() => null)
        if (!active) return
        setReviewAudit(review)
        setReviewId(review?.status === 'pending' ? review.review_id : null)
        setRunReviewId(review?.status === 'approved' ? review.review_id : null)
        setMappingRuns([])
        let preview: Awaited<ReturnType<typeof curatedApi.preview>>
        if (review?.status === 'pending' && review.dataset_version_id) {
          preview = await curatedApi.preview(datasetId, 500, {
            versionId: review.dataset_version_id,
            reviewId: review.review_id,
          })
        } else {
          preview = await curatedApi.preview(datasetId, 500)
          if (review?.status === 'approved'
            && review.dataset_version_id
            && preview.dataset_version_id === review.dataset_version_id) {
            preview = await curatedApi.preview(datasetId, 500, {
              versionId: review.dataset_version_id,
              reviewId: review.review_id,
            })
          }
        }
        if (active) {
          if (preview.error) {
            setLoadError(preview.error)
          } else {
            const rowArr = Array.isArray(preview.rows) ? preview.rows : []
            setRows(rowArr)
            setCols(rowArr.length > 0 ? Object.keys(rowArr[0]) : [])
            setRowTokens(Array.isArray(preview.row_tokens) ? preview.row_tokens : [])
            setProtectedFields(new Set(['id', '__pk__', ...(preview.protected_fields ?? [])]))
            setPreviewVersionId(preview.dataset_version_id ?? null)
            if (review && !review.dataset_version_id) {
              setReviewEditPreviewWarning('此历史审核没有固定数据版本，当前以只读方式显示最新数据。')
            } else if (review?.status === 'pending'
              && preview.latest_dataset_version_id
              && preview.latest_dataset_version_id !== review.dataset_version_id) {
              setReviewEditPreviewWarning('审核继续固定在发起时的数据版本；数据集已有更新版本，本次批准仍只作用于已固定版本。')
            } else if (review?.status !== 'pending'
              && review?.dataset_version_id
              && preview.dataset_version_id !== review.dataset_version_id) {
              setReviewEditPreviewWarning('数据集已有新版本；当前显示最新数据，需要为该版本发起新的审核。')
            } else if ((preview.review_edits?.outside_preview ?? 0) > 0) {
              setReviewEditPreviewWarning(
                `${preview.review_edits?.outside_preview} 条已保存修改在当前预览范围之外；完整审核应用时会校验整个固定版本。`,
              )
            }
          }
        }

        if (review) {
          const audit = await curatedApi.reviewEdits(review.review_id).catch(() => null)
          if (active) {
            setReviewEdits(audit?.edits ?? [])
            setReviewEditTotal(audit?.total ?? 0)
          }
        } else if (active) {
          setReviewAudit(null)
          setReviewId(null)
          setReviewEdits([])
          setReviewEditTotal(0)
        }
      } catch {
        if (active) setLoadError('数据加载失败或审核版本已不可用，请刷新后重试。')
      } finally {
        if (active) setLoading(false)
      }
    }

    void loadReviewAndPreview()
    return () => { active = false }
  }, [datasetId])

  useEffect(() => {
    if (editingCell && canEditRows) editInputRef.current?.focus()
  }, [editingCell, canEditRows])

  useEffect(() => {
    if (!runReviewId) return
    let active = true
    let timer: number | undefined

    const refreshRuns = async () => {
      try {
        const result = await curatedApi.mappingRuns(runReviewId)
        if (!active) return
        setMappingRuns(result.runs)
        const stillActive = result.runs.some(run =>
          ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(run.status)
          || ['pending', 'projecting', 'mixed'].includes(run.projection?.status ?? ''),
        )
        if (stillActive) timer = window.setTimeout(refreshRuns, 2500)
      } catch {
        if (active) timer = window.setTimeout(refreshRuns, 5000)
      }
    }

    void refreshRuns()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [runReviewId])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape' && !editingCell) onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, editingCell])

  const cellVal = (rowIdx: number, col: string) => {
    const key: CellKey = `${rowIdx}::${col}`
    return pendingEdits.get(key)?.val ?? String(rows[rowIdx]?.[col] ?? '')
  }

  const startEdit = (rowIdx: number, col: string) => {
    const value = rows[rowIdx]?.[col]
    if (!canEditRows || !rowTokens[rowIdx] || protectedFields.has(col) || !isScalarEditable(value)) return
    setEditingCell(`${rowIdx}::${col}`)
    setSaveMsg('')
  }

  const commitEdit = (rowIdx: number, col: string, newVal: string) => {
    const key: CellKey = `${rowIdx}::${col}`
    const oldVal = String(rows[rowIdx]?.[col] ?? '')
    if (newVal === oldVal) {
      setPendingEdits(prev => { const m = new Map(prev); m.delete(key); return m })
    } else {
      setPendingEdits(prev => new Map(prev).set(key, { old: oldVal, val: newVal }))
    }
    setEditingCell(null)
  }

  const persistPendingEdits = async () => {
    if (pendingEdits.size === 0) return { reviewId, count: 0 }
    if (!previewVersionId) throw new Error('审核编辑必须绑定固定数据版本')
    const resumableReviewId = reviewAudit?.status === 'pending' ? reviewAudit.review_id : null
    const session = reviewId || resumableReviewId
      ? null
      : await curatedApi.startReview(datasetId, previewVersionId)
    const activeReviewId = reviewId ?? resumableReviewId ?? session?.review_id
    if (!activeReviewId) throw new Error('审核会话创建失败')
    if (session) {
      setReviewAudit(session)
      setStatus('pending')
      onStatusChange(datasetId, 'pending')
    }
    const edits = Array.from(pendingEdits.entries()).map(([key, { old: oldVal, val: newVal }]) => {
      const [rowIdxStr, col] = key.split('::')
      const rowIdx = Number(rowIdxStr)
      const rowToken = rowTokens[rowIdx]
      if (!rowToken) throw new Error('缺少审核行身份，请重新加载数据预览')
      return { row_pk: rowToken, field_name: col, old_value: oldVal, new_value: newVal }
    })
    const saved = await curatedApi.saveEdits(activeReviewId, edits)
    setReviewId(activeReviewId)
    setReviewEdits(current => {
      const byId = new Map(current.map(edit => [edit.id, edit]))
      saved.edits.forEach(edit => byId.set(edit.id, edit))
      return Array.from(byId.values()).sort((a, b) =>
        (a.edited_at ?? '').localeCompare(b.edited_at ?? ''),
      )
    })
    setReviewEditTotal(current => current + saved.saved)
    setRows(prev => {
      const updated = prev.map(r => ({ ...r }))
      pendingEdits.forEach(({ val }, key) => {
        const [rowIdxStr, col] = key.split('::')
        updated[Number(rowIdxStr)][col] = val
      })
      return updated
    })
    setPendingEdits(new Map())
    return { reviewId: activeReviewId, count: edits.length }
  }

  const handleSaveEdits = async () => {
    if (pendingEdits.size === 0) return
    setSaving(true)
    setSaveMsg('')
    try {
      const result = await persistPendingEdits()
      setSaveMsg(`已保存 ${result.count} 处修改`)
    } catch {
      setSaveMsg('数据可能已更新，请刷新后重试')
    } finally {
      setSaving(false)
    }
  }

  const handleApprove = async () => {
    setApproving(true)
    try {
      const edits = await persistPendingEdits()
      if (!edits.reviewId) {
        const review = await curatedApi.startReview(datasetId, previewVersionId)
        setReviewAudit(review)
        setReviewId(review.review_id)
        setStatus('pending')
        onStatusChange(datasetId, 'pending')
        setSaveMsg('审核申请已创建，请由不同用户完成批准。')
        return
      }
      if (reviewAudit?.requested_by && reviewAudit.requested_by === currentUserId) {
        setSaveMsg('审核申请已保存，请等待独立审核者批准。')
        return
      }
      const approval = await curatedApi.approveReview(edits.reviewId)
      setReviewAudit(approval)
      setRunReviewId(approval.review_id)
      setMappingRuns([])
      setReviewId(null)
      setStatus('approved')
      onStatusChange(datasetId, 'approved')
    } catch (error: unknown) {
      const detail = typeof error === 'object' && error !== null && 'detail' in error
        ? (error as { detail?: unknown }).detail
        : null
      const message = typeof detail === 'string'
        ? detail
        : typeof detail === 'object' && detail !== null && 'message' in detail
          ? String((detail as { message: unknown }).message)
          : error instanceof Error ? error.message : '审核操作失败'
      setSaveMsg(message)
    } finally { setApproving(false) }
  }

  const handleReject = async () => {
    setApproving(true)
    try {
      const review = reviewId
        ? await curatedApi.rejectReview(reviewId)
        : await curatedApi.reject(datasetId)
      setReviewAudit(review)
      setReviewId(null)
      setStatus('rejected')
      onStatusChange(datasetId, 'rejected')
    } finally { setApproving(false) }
  }

  const handleDelete = async () => {
    const cascade = cascadeDeleteCount !== null
    setDeleting(true)
    try {
      await curatedApi.delete(datasetId, { cascade })
      onDeleted(datasetId)
    } catch (error) {
      const reviewCount = curatedReviewsBlockingDeleteCount(error)
      if (!cascade && reviewCount !== null) {
        setCascadeDeleteCount(reviewCount)
        setDeleteError('')
      } else {
        setDeleteError(formatApiError(error, '删除失败，请稍后重试。'))
      }
    } finally {
      setDeleting(false)
    }
  }

  const openProjectionRecovery = (run: MappingApplyRun) => {
    const storageKey = `project4:mapping-projection:${run.run_id}`
    let stored: { reason?: string; client_request_id?: string } | null = null
    try { stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null') }
    catch { stored = null }
    setProjectionRecoveryRunId(run.run_id)
    setProjectionReason(stored?.reason ?? '')
    setProjectionRequestId(stored?.client_request_id ?? crypto.randomUUID())
    setProjectionRequestLocked(!!stored?.client_request_id)
    setProjectionError('')
    setProjectionNotice('')
  }

  const submitProjectionRecovery = async (run: MappingApplyRun) => {
    if (!runReviewId || !projectionReason.trim() || !projectionRequestId || projectionBusy) return
    const payload = { reason: projectionReason.trim(), client_request_id: projectionRequestId }
    sessionStorage.setItem(`project4:mapping-projection:${run.run_id}`, JSON.stringify(payload))
    setProjectionRequestLocked(true)
    setProjectionBusy(true)
    setProjectionError('')
    setProjectionNotice('')
    try {
      const result = await curatedApi.reconcileMappingProjection(runReviewId, run.run_id, payload)
      setMappingRuns(current => current.map(item => item.run_id === run.run_id
        ? { ...item, projection: result.projection }
        : item))
      setProjectionNotice(result.result.replayed
        ? '已按同一请求编号返回历史结果；若要再次尝试，请新建请求编号。'
        : `人工重放完成：投影 ${result.result.projected} 项，待处理 ${result.result.pending} 项，终态失败 ${result.result.dead_letter} 项。`)
    } catch {
      setProjectionError('请求结果暂不可确认。请用同一请求编号重试，避免重复创建重放。')
    } finally {
      setProjectionBusy(false)
    }
  }

  const startNewProjectionRequest = (run: MappingApplyRun) => {
    sessionStorage.removeItem(`project4:mapping-projection:${run.run_id}`)
    setProjectionReason('')
    setProjectionRequestId(crypto.randomUUID())
    setProjectionRequestLocked(false)
    setProjectionError('')
    setProjectionNotice('')
  }

  const projectionLabel = (status?: string) => ({
    not_applicable: '无行投影',
    pending: '等待图投影',
    projecting: '图投影中',
    projected: '图投影完成',
    dead_letter: '自动重试耗尽',
    mixed: '部分待处理',
  }[status ?? ''] ?? '图投影状态未知')

  const projectionAttemptLabel = (attempt: MappingRowProjectionAttempt) => {
    const kind = attempt.attempt_kind === 'manual' ? `人工重放 #${attempt.attempt_number}` : `自动重试 #${attempt.attempt_number}`
    const outcome = ({
      projected: '成功', retry_wait: '失败，等待重试', dead_letter: '失败，已终止自动重试',
      outcome_unknown: '结果未知，已安排恢复', running: '进行中',
    } as Record<string, string>)[attempt.status] ?? attempt.status
    return `${kind} · 对象 ${attempt.object_id.slice(0, 12)} · ${outcome}`
  }

  const hasPending = pendingEdits.size > 0
  const editRowLabel = (rowPk: string) => {
    if (!rowPk.startsWith('review-row-v1:')) return rowPk
    const rowIndex = Number(rowPk.split(':')[2])
    return Number.isInteger(rowIndex) && rowIndex >= 0 ? `第 ${rowIndex + 1} 行` : '审核行'
  }
  const auditActor = (actorId: string | null, fallback: string) => actorId
    ? <code title={actorId} className="font-mono text-slate-700">{actorId.slice(0, 8)}</code>
    : <span className="text-slate-400">{fallback}</span>

  return (
    <>
      {/* Full-screen modal overlay */}
      <div
        className="fixed inset-0 bg-black/50 z-40 flex items-center justify-center p-6"
        onClick={onClose}
      >
        {/* Modal window */}
        <div
          className="bg-white rounded-xl shadow-2xl z-50 flex flex-col w-full max-w-5xl"
          style={{ maxHeight: 'calc(100vh - 3rem)' }}
          onClick={e => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-start justify-between px-6 py-4 border-b shrink-0">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="font-semibold text-base truncate">{datasetName}</h2>
                <span className={`inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border ${STATUS_STYLE[status] || 'bg-gray-100 text-gray-600 border-gray-200'}`}>
                  {STATUS_ICON(status)}
                  {STATUS_LABEL[status] || status}
                </span>
              </div>
              <p className="text-xs text-gray-400 mt-0.5">来自管道：{pipelineName}</p>
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded hover:bg-gray-100 text-gray-400 hover:text-black ml-4 shrink-0"
            >
              <X size={16} />
            </button>
          </div>

          {/* Action bar */}
          <div className="flex items-center gap-2 px-6 py-3 border-b bg-gray-50 shrink-0 flex-wrap">
            {(status !== 'approved' || startsNewReview) && (
              <button
                onClick={handleApprove}
                disabled={approving || !previewVersionId || Boolean(reviewAudit?.status === 'pending' && (!reviewAudit.dataset_version_id || !isAdmin || reviewAudit.requested_by === currentUserId))}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
              >
                {approving ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle size={12} />}
                {reviewAudit?.status === 'pending' && reviewAudit.requested_by === currentUserId
                  ? '等待独立审核'
                  : reviewAudit?.status === 'pending'
                    ? (isAdmin ? '批准' : '等待审核者')
                    : reviewAudit?.status === 'rejected'
                      ? '重新提交审核'
                      : startsNewReview ? '发起新版本审核' : '发起审核'}
              </button>
            )}
            {status !== 'rejected' && (
              <button
                onClick={handleReject}
                disabled={approving}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs border border-red-200 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-50"
              >
                {approving ? <Loader2 size={12} className="animate-spin" /> : <AlertTriangle size={12} />}
                拒绝
              </button>
            )}

            <div className="flex-1" />

            {hasPending && (
              <span className="text-xs text-amber-600 flex items-center gap-1">
                <Pencil size={11} /> {pendingEdits.size} 处未保存
              </span>
            )}
            {saveMsg && (
              <span className={`text-xs ${saveMsg.includes('失败') ? 'text-red-500' : 'text-green-600'}`}>
                {saveMsg}
              </span>
            )}
            <button
              onClick={handleSaveEdits}
              disabled={!hasPending || saving || !canEditRows}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs border rounded-lg hover:bg-gray-100 disabled:opacity-40"
            >
              {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
              保存编辑
            </button>
            <button
              onClick={() => {
                setCascadeDeleteCount(null)
                setDeleteError('')
                setShowDeleteConfirm(true)
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs border border-red-200 text-red-500 rounded-lg hover:bg-red-50"
            >
              <Trash2 size={12} /> 删除
            </button>
          </div>

          {reviewAudit && (
            <div className="flex flex-wrap gap-x-6 gap-y-1 px-6 py-2 border-b bg-white text-[11px] text-slate-600" aria-label="审核身份与数据版本">
              <span>请求人：{auditActor(reviewAudit.requested_by, '历史未记录')}</span>
              <span>指派审核人：{auditActor(reviewAudit.reviewer_id, '未单独指派')}</span>
              <span>决定人：{auditActor(reviewAudit.decided_by, reviewAudit.status === 'pending' ? '尚未决定' : '历史未记录')}</span>
              <span>审核数据版本：{reviewAudit.dataset_version_id
                ? <code className="font-mono text-slate-700">{reviewAudit.dataset_version_id}</code>
                : <span className="text-amber-700">未固定版本</span>}</span>
            </div>
          )}

          {reviewEditPreviewWarning && (
            <div className="border-b bg-amber-50 px-6 py-2 text-[11px] text-amber-800">
              {reviewEditPreviewWarning}
            </div>
          )}

          {reviewAudit && (
            <section className="border-b bg-slate-50 px-6 py-2.5" aria-label="行级修改审计">
              <button
                type="button"
                className="text-[11px] font-medium text-slate-700 hover:text-red-800 disabled:text-slate-400"
                aria-expanded={editAuditExpanded}
                onClick={() => setEditAuditExpanded(value => !value)}
                disabled={reviewEdits.length === 0}
              >
                {reviewEdits.length === 0
                  ? '行级修改审计：暂无记录'
                  : `${editAuditExpanded ? '收起' : '查看'}最近 ${Math.min(reviewEdits.length, 5)} 条行级修改（共 ${reviewEditTotal} 条；最多加载 100 条）`}
              </button>
              {editAuditExpanded && reviewEdits.length > 0 && (
                <ol className="mt-2 max-h-36 space-y-1 overflow-y-auto text-[11px] text-slate-600">
                  {reviewEdits.slice(-5).reverse().map(edit => (
                    <li key={edit.id} className="flex flex-wrap gap-x-3 gap-y-0.5 rounded border border-slate-200 bg-white px-2 py-1.5">
                      <span>{editRowLabel(edit.row_pk)} · 字段 {edit.field_name}</span>
                      <span className="break-all">{edit.old_value ?? '∅'} → {edit.new_value ?? '∅'}</span>
                      <span>编辑人：{auditActor(edit.edited_by, '历史未记录')}</span>
                      {edit.edited_at && <time dateTime={edit.edited_at}>{new Date(edit.edited_at).toLocaleString()}</time>}
                    </li>
                  ))}
                </ol>
              )}
            </section>
          )}

          {mappingRuns.length > 0 && (
            <div className="px-6 py-3 border-b bg-slate-50 space-y-2">
              <div className="text-xs font-medium text-slate-700">本体映射运行</div>
              {mappingRuns.map(run => {
                const isActive = ['pending', 'dispatching', 'queued', 'running', 'retry_wait'].includes(run.status)
                const projection = run.projection
                const canReconcile = isAdmin && !!projection && (projection.pending > 0 || projection.dead_letter > 0)
                return (
                  <div key={run.run_id} className="rounded border border-slate-200 bg-white p-2.5 text-xs">
                    <div className="flex items-start gap-2">
                      {isActive || projection?.status === 'projecting'
                        ? <Loader2 size={13} className="mt-0.5 animate-spin text-blue-500" />
                        : run.status === 'succeeded' && ['projected', 'not_applicable'].includes(projection?.status ?? '')
                          ? <CheckCircle size={13} className="mt-0.5 text-green-600" />
                          : <AlertTriangle size={13} className="mt-0.5 text-amber-600" />}
                      <div className="min-w-0 flex-1">
                        <div className="text-slate-700">
                          {run.mapping_id.slice(0, 8)} · SQL 应用：{MAPPING_RUN_LABEL[run.status] ?? run.status}
                          <span className="ml-2 text-slate-500">图投影：{projectionLabel(projection?.status)}</span>
                          <span className="ml-2 text-slate-400">数据版本 {run.dataset_version_id}</span>
                        </div>
                        <div className="mt-1 text-[11px] text-slate-500 break-all">
                          映射审核版本 {run.mapping_review_id ? run.mapping_review_id.slice(0, 8) : '旧记录未固定'}
                          {' · 发起者：'}{run.request_actor_kind === 'user'
                            ? `用户 ${run.request_actor_id?.slice(0, 8) ?? '未知'}`
                            : run.request_actor_kind === 'system'
                              ? `系统 ${run.request_actor_id ?? '未知'}`
                              : '历史未记录'}
                          {' · 最近执行 worker：'}{run.worker_actor_id ?? '尚未记录'}
                        </div>
                        {projection && projection.status !== 'not_applicable' && (
                          <div className="mt-1 text-[11px] text-slate-500">
                            对象 {projection.row_count} · 已投影 {projection.projected} · 待处理 {projection.pending} · 终态失败 {projection.dead_letter}
                            · 自动尝试 {projection.automatic_attempts}/{projection.max_automatic_attempts} · 人工重放 {projection.manual_attempts}
                          </div>
                        )}
                        {run.error_message && (
                          <div className="mt-1 text-red-600 break-words">SQL 应用错误：{run.error_message}</div>
                        )}
                        {run.execution_attempts > 0 && run.attempt_history_state === 'legacy_untracked' && (
                          <div className="mt-1 text-[11px] text-slate-500">旧运行没有逐次执行事件记录</div>
                        )}
                        {run.attempt_events?.length > 0 && (
                          <div className="mt-2">
                            <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                              <span>
                                执行尝试历史：{run.execution_attempts} 次 · {run.attempt_history_state === 'in_progress' ? '执行中' : run.attempt_history_state === 'complete' ? '链校验通过' : '链校验异常'}
                              </span>
                              {run.attempt_history_valid === false && <span className="text-red-600">事件摘要链不匹配</span>}
                              <button type="button" className="text-blue-700 underline" onClick={() => setAttemptHistoryRunId(current => current === run.run_id ? null : run.run_id)}>
                                {attemptHistoryRunId === run.run_id ? '收起' : '查看'}
                              </button>
                            </div>
                            {attemptHistoryRunId === run.run_id && (
                              <ol className="mt-1 space-y-1 text-[11px] text-slate-600">
                                {run.attempt_events.map(item => (
                                  <li key={`${run.run_id}-${item.sequence_no}`} className="break-all">
                                    #{item.sequence_no} · 第 {item.attempt_number} 次 · {item.event_type === 'attempt_started' ? '开始' : item.event_type === 'attempt_succeeded' ? '成功' : item.event_type === 'attempt_failed' ? '失败' : '租约过期，结果未知'}
                                    {' · '}{item.actor_kind} {item.actor_id}
                                    {item.celery_task_id && ` · Task ${item.celery_task_id.slice(0, 8)}`}
                                    {item.error_code && ` · 错误码 ${item.error_code}`}
                                    {item.occurred_at && ` · ${new Date(item.occurred_at).toLocaleString()}`}
                                  </li>
                                ))}
                              </ol>
                            )}
                          </div>
                        )}
                        {canReconcile && projectionRecoveryRunId !== run.run_id && (
                          <button type="button" className="mt-2 rounded border border-amber-300 px-2 py-1 text-[11px] text-amber-800 hover:bg-amber-50" onClick={() => openProjectionRecovery(run)}>
                            管理员人工恢复图投影
                          </button>
                        )}
                        {projection?.attempts.length ? (
                          <div className="mt-2">
                            <button type="button" className="text-[11px] text-blue-700 underline" onClick={() => setProjectionHistoryRunId(current => current === run.run_id ? null : run.run_id)}>
                              {projectionHistoryRunId === run.run_id ? '收起投影尝试记录' : `查看最近 ${Math.min(projection.attempts.length, 5)} 条投影尝试`}
                            </button>
                            {projectionHistoryRunId === run.run_id && (
                              <ul className="mt-1 space-y-1 text-[11px] text-slate-600">
                                {projection.attempts.slice(0, 5).map(attempt => (
                                  <li key={attempt.attempt_id} className="break-words">
                                    {projectionAttemptLabel(attempt)} · {attempt.actor_kind}{attempt.actor_id ? ` ${attempt.actor_id.slice(0, 8)}` : ''}
                                    {attempt.reason ? ` · 原因：${attempt.reason}` : ''}
                                    {attempt.error_message ? ` · ${attempt.error_message}` : ''}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>
                        ) : null}
                        {projectionRecoveryRunId === run.run_id && (
                          <div className="mt-2 rounded border border-amber-200 bg-amber-50/60 p-2.5">
                            <p className="font-medium text-slate-700">人工重放不会清零自动重试计数，每次尝试都会留下操作者和理由。</p>
                            <label className="mt-2 block text-[11px] text-slate-700">
                              重放原因
                              <textarea className="mt-1 min-h-16 w-full rounded border border-slate-300 bg-white p-2 text-xs" value={projectionReason} maxLength={4000} readOnly={projectionRequestLocked} onChange={event => setProjectionReason(event.target.value)} placeholder="说明已经核对或修复的事项" />
                            </label>
                            <p className="mt-1 break-all font-mono text-[10px] text-slate-500">请求编号 {projectionRequestId}</p>
                            {projectionError && <p className="mt-1 text-[11px] text-red-700">{projectionError}</p>}
                            {projectionNotice && <p className="mt-1 text-[11px] text-emerald-700">{projectionNotice}</p>}
                            <div className="mt-2 flex flex-wrap gap-2">
                              <button type="button" className="wb-button-primary" disabled={projectionBusy || !projectionReason.trim()} onClick={() => void submitProjectionRecovery(run)}>
                                {projectionBusy ? '正在投影…' : projectionRequestLocked ? '用同一请求编号重试' : '提交人工重放'}
                              </button>
                              {projectionRequestLocked && <button type="button" className="wb-button-secondary" disabled={projectionBusy} onClick={() => startNewProjectionRequest(run)}>新建请求编号</button>}
                              <button type="button" className="wb-button-secondary" disabled={projectionBusy} onClick={() => setProjectionRecoveryRunId(null)}>关闭</button>
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* Data table */}
          <div className="flex-1 overflow-auto">
            {loading ? (
              <div className="flex items-center justify-center h-48 text-gray-400 text-sm gap-2">
                <Loader2 size={16} className="animate-spin" /> 加载中...
              </div>
            ) : loadError ? (
              <div className="p-6 text-sm text-red-400">{loadError}</div>
            ) : rows.length === 0 ? (
              <div className="p-8 text-center text-sm text-gray-400">暂无数据行</div>
            ) : (
              <>
                <p className="px-6 py-2 text-xs text-gray-400 bg-gray-50 border-b shrink-0">
                  共 {rows.length} 行 · {canEditRows ? '双击文本或数值单元格可编辑' : '当前版本只读'}
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs min-w-max">
                    <thead className="bg-gray-50 border-b sticky top-0">
                      <tr>
                        <th className="px-4 py-2 text-gray-400 font-normal text-left w-12">#</th>
                        {cols.map(col => (
                          <th key={col} className="px-4 py-2 text-left font-medium text-gray-600 whitespace-nowrap">
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, rowIdx) => (
                        <tr key={rowIdx} className="border-b hover:bg-blue-50/30 transition-colors">
                          <td className="px-4 py-2 text-gray-300 tabular-nums select-none">{rowIdx + 1}</td>
                          {cols.map(col => {
                            const key: CellKey = `${rowIdx}::${col}`
                            const isEditing = editingCell === key
                            const isModified = pendingEdits.has(key)
                            const val = cellVal(rowIdx, col)
                            const cellEditable = canEditRows
                              && Boolean(rowTokens[rowIdx])
                              && !protectedFields.has(col)
                              && isScalarEditable(row[col])

                            return (
                              <td
                                key={col}
                                className={`px-4 py-2 max-w-[220px] ${isModified ? 'bg-amber-50' : ''} ${cellEditable ? 'cursor-text' : ''}`}
                                onDoubleClick={() => { if (cellEditable) startEdit(rowIdx, col) }}
                              >
                                {isEditing ? (
                                  <input
                                    ref={editInputRef}
                                    defaultValue={val}
                                    onBlur={e => commitEdit(rowIdx, col, e.target.value)}
                                    onKeyDown={e => {
                                      if (e.key === 'Enter') commitEdit(rowIdx, col, e.currentTarget.value)
                                      if (e.key === 'Escape') setEditingCell(null)
                                    }}
                                    className="w-full border border-blue-400 rounded px-1.5 py-0.5 outline-none bg-white text-xs min-w-[80px]"
                                    onClick={e => e.stopPropagation()}
                                  />
                                ) : (
                                  <span
                                    className={`block truncate ${cellEditable ? 'cursor-text' : 'cursor-default'} ${isModified ? 'text-amber-700 font-medium' : 'text-gray-700'}`}
                                    title={val}
                                  >
                                    {val || <span className="text-gray-300">—</span>}
                                  </span>
                                )}
                              </td>
                            )
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={showDeleteConfirm}
        title={cascadeDeleteCount === null ? '删除数据集' : '同时删除审核历史？'}
        message={[
          cascadeDeleteCount === null
            ? `确认删除「${datasetName}」？此操作不可撤销，数据将永久删除。`
            : `该数据集关联 ${cascadeDeleteCount} 条审核记录。继续将同时删除审核记录及其行编辑历史，且无法恢复。确认继续吗？`,
          deleteError,
        ].filter(Boolean).join(' ')}
        confirmLabel={deleting ? '删除中...' : cascadeDeleteCount === null ? '确认删除' : '同时删除审核历史'}
        onConfirm={handleDelete}
        onCancel={() => {
          setShowDeleteConfirm(false)
          setCascadeDeleteCount(null)
          setDeleteError('')
        }}
      />
    </>
  )
}
