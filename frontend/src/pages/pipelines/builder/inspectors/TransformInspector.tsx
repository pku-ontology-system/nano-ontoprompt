import { useState, useMemo, useEffect } from 'react'
import { Plus, Trash2, Eye } from 'lucide-react'
import { apiClientV2 } from '@/api/client'

const AVAILABLE_OPS = [
  { op: 'rename_columns', label: '重命名列', path: 'A', enabled: true }, { op: 'drop_nulls', label: '删除空值行', path: 'A', enabled: true },
  { op: 'fill_nulls', label: '填充空值', path: 'A', enabled: true }, { op: 'drop_duplicates', label: '去重', path: 'A', enabled: true },
  { op: 'normalize_dates', label: '日期标准化', path: 'A', enabled: true }, { op: 'select_columns', label: '选择列', path: 'A', enabled: true },
  { op: 'filter_rows', label: '过滤行', path: 'A', enabled: true }, { op: 'sort', label: '排序', path: 'A', enabled: true },
  { op: 'join', label: 'Join 关联', path: 'A', enabled: false }, { op: 'aggregate', label: 'Aggregate 聚合', path: 'A', enabled: false },
  { op: 'group_by', label: 'Group By 分组', path: 'A', enabled: false }, { op: 'pivot', label: 'Pivot 透视', path: 'A', enabled: false },
  { op: 'detect_wide_table', label: '检测宽表', path: 'WIDE', enabled: true }, { op: 'suggest_split', label: '建议拆分', path: 'WIDE', enabled: true },
  { op: 'apply_split', label: '执行拆分', path: 'WIDE', enabled: true },
  { op: 'parse_json', label: '解析 JSON', path: 'B', enabled: true }, { op: 'parse_xml', label: '解析 XML', path: 'B', enabled: true },
  { op: 'flatten_json', label: 'JSON Flatten', path: 'B', enabled: true }, { op: 'explode_array', label: '数组 Explode', path: 'B', enabled: true },
  { op: 'document_to_markdown', label: '文档转 Markdown', path: 'C', enabled: true }, { op: 'ocr_extract', label: 'OCR 文字提取', path: 'C', enabled: true },
  { op: 'vlm_extract', label: 'VLM 视觉提取', path: 'C', enabled: true }, { op: 'llm_structurize', label: 'LLM 结构化', path: 'C', enabled: true },
  { op: 'llm_enrich', label: 'LLM 语义增强', path: 'C', enabled: true },
]

const PATH_OPS_MAP: Record<string, typeof AVAILABLE_OPS> = {
  auto: AVAILABLE_OPS, structured: AVAILABLE_OPS.filter(o => o.path === 'A'), semi_structured: AVAILABLE_OPS.filter(o => o.path === 'B'),
  unstructured: AVAILABLE_OPS.filter(o => o.path === 'C'), wide_table: AVAILABLE_OPS.filter(o => o.path === 'WIDE'),
}

function describeFallbackReason(reason?: string | string[] | null): string {
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

export default function TransformInspector({ config, onChange, readOnly = false, pipelineId }: { config: Record<string, unknown>; onChange: (key: string, value: unknown) => void; nodeId?: string; readOnly?: boolean; pipelineId?: string }) {
  const currentPath = String(config.path || 'auto')
  const steps = (config.steps || []) as Array<{ op: string; params?: Record<string, unknown> }>
  const [showCatalog, setShowCatalog] = useState(false)
  const [previewMap, setPreviewMap] = useState<Record<number, { loading: boolean; data?: Record<string, unknown>[]; error?: string }>>({})
  const filteredOps = useMemo(() => PATH_OPS_MAP[currentPath] || AVAILABLE_OPS, [currentPath])

  // Runtime stats (hooks must be at top level)
  const [runStats, setRunStats] = useState<{
    rows_in: number
    rows_out: number
    status: string
    error_log?: string | null
    extraction?: { requested_method?: string | null; effective_method?: string | null; fallback_policy?: string | null; fallback_reason?: string | string[] | null }
  } | null>(null);
  const extractionReason = describeFallbackReason(runStats?.extraction?.fallback_reason)
  useEffect(() => {
    if (!pipelineId || !readOnly) return;
    apiClientV2.get<Array<{ id: string }>>('/pipelines/' + pipelineId + '/runs').then((runs) => {
      const last = Array.isArray(runs) && runs.length > 0 ? runs[0] : null;
      if (last) apiClientV2.get<{
        status: string
        error_log?: string | null
        stats?: {
          rows_in: number
          rows_out: number
          meta?: { md_to_structured?: { requested_method?: string | null; effective_method?: string | null; fallback_policy?: string | null; fallback_reason?: string | string[] | null } }
        }
      }>('/pipelines/runs/' + last.id).then((d) => {
        setRunStats({
          rows_in: d.stats?.rows_in || 0,
          rows_out: d.stats?.rows_out || 0,
          status: d.status,
          error_log: d.error_log,
          extraction: d.stats?.meta?.md_to_structured,
        })
      }).catch(() => {});
    }).catch(() => {});
  }, [pipelineId, readOnly]);

  if (readOnly) {
    return (
      <div className="space-y-3">
        <div className="bg-amber-50 border border-amber-100 rounded-lg p-3 text-xs">
          <p className="text-amber-700 font-medium mb-1">转换配置</p>
          <p className="text-amber-600">路径: {currentPath === 'auto' ? '自动检测' : currentPath === 'structured' ? 'Path A · 结构化' : currentPath === 'semi_structured' ? 'Path B · 半结构化' : currentPath === 'unstructured' ? 'Path C · 非结构化' : '宽表拆分'}</p>
          <p className="text-amber-600">引擎: {String(config.engine || 'pandas')}</p>
          <p className="text-amber-600">步骤: {steps.length} 个</p>
          {steps.length > 0 && (<div className="mt-1 space-y-0.5">{steps.map((s, i) => (<p key={i} className="text-amber-500">{i + 1}. {AVAILABLE_OPS.find(o => o.op === s.op)?.label || s.op}{['llm_structurize', 'llm_enrich'].includes(s.op) ? ` · LLM 失败处理：${s.params?.fallback_policy === 'rule_based' ? '允许规则回退并标记降级' : '停止 Run'}` : ''}</p>))}</div>)}
        </div>
        {runStats && (
          <div className="bg-white border rounded-lg p-3 text-xs">
            <p className="font-medium text-gray-700 mb-2">运行时执行结果</p>
            <p className="text-gray-500">输入行数: {runStats.rows_in}</p>
            <p className="text-gray-500">输出行数: {runStats.rows_out}</p>
            {runStats.status === 'degraded' ? (
              <p className="text-amber-700 mt-1">已完成但状态为降级：请求 {runStats.extraction?.requested_method || 'LLM'}，实际使用 {runStats.extraction?.effective_method || '规则提取'}；输出需人工审核。</p>
            ) : runStats.status === 'failed' ? (
              <p className="text-red-600 mt-1">运行失败：{runStats.error_log || extractionReason || '请查看 Run 详情。'}</p>
            ) : (
              <p className="text-green-600 mt-1">运行状态：{runStats.status}</p>
            )}
            {runStats.extraction?.requested_method && (
              <p className="text-gray-500 mt-1">LLM 请求: {runStats.extraction.requested_method}；实际: {runStats.extraction.effective_method || '无'}；失败策略: {runStats.extraction.fallback_policy === 'rule_based' ? '规则回退并降级' : '失败关闭'}{extractionReason ? `；原因: ${extractionReason}` : ''}</p>
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <div><label className="text-xs text-gray-500 mb-1 block">处理路径</label>
        <select value={currentPath} onChange={e => { const np = e.target.value; onChange('path', np); const v = PATH_OPS_MAP[np] || AVAILABLE_OPS; const vs = new Set(v.map(o => o.op)); onChange('steps', steps.filter(s => vs.has(s.op))) }} className="w-full border rounded-lg px-3 py-1.5 text-sm"><option value="auto">自动检测</option><option value="structured">Path A · 结构化</option><option value="semi_structured">Path B · 半结构化</option><option value="unstructured">Path C · 非结构化</option><option value="wide_table">宽表拆分</option></select></div>
      <div><label className="text-xs text-gray-500 mb-1 block">引擎</label>
        <select value={String(config.engine || 'pandas')} onChange={e => onChange('engine', e.target.value)} className="w-full border rounded-lg px-3 py-1.5 text-sm">
          <option value="pandas">pandas</option>{currentPath === 'A' && <option value="duckdb">DuckDB</option>}{(currentPath === 'C' || currentPath === 'auto') && (<><option value="llm">LLM</option><option value="vlm">VLM</option><option value="ocr">OCR</option></>)}</select></div>
      <div><div className="flex items-center justify-between mb-1"><label className="text-xs text-gray-500">处理步骤</label><button onClick={() => setShowCatalog(!showCatalog)} className="flex items-center gap-0.5 text-xs text-blue-500"><Plus size={11} />添加</button></div>
        {showCatalog && (<div className="border rounded-lg p-2 mb-2 max-h-40 overflow-y-auto space-y-0.5">{filteredOps.map(op => op.enabled ? (<button key={op.op} onClick={() => { onChange('steps', [...steps, { op: op.op, params: {} }]); setShowCatalog(false) }} className="w-full text-left text-xs px-2 py-1 rounded hover:bg-gray-50 flex items-center gap-2"><span className="text-gray-300 text-[10px]">{op.path}</span><span className="font-medium">{op.label}</span></button>) : (<div key={op.op} className="w-full text-left text-xs px-2 py-1 rounded flex items-center gap-2 opacity-50" title="即将推出"><span className="text-gray-300 text-[10px]">{op.path}</span><span className="text-gray-400">{op.label}</span><span className="ml-auto text-[9px] text-gray-400 border-dashed border rounded px-1">即将推出</span></div>))}</div>)}
        {steps.length === 0 ? <p className="text-xs text-gray-400 italic">暂无步骤</p> : (<div className="space-y-1.5">{steps.map((step, i) => (
          <div key={i} className="border rounded-lg p-2 text-xs space-y-1">
            {previewMap[i] && <p role="status">{previewMap[i].loading ? 'Loading…' : previewMap[i].error || JSON.stringify(previewMap[i].data)}</p>}
            <div className="flex items-center justify-between">
              <span className="font-medium">{i + 1}. {AVAILABLE_OPS.find(o => o.op === step.op)?.label || step.op}</span>
              <div className="flex gap-0.5">
                <button onClick={async () => { setPreviewMap(p => ({ ...p, [i]: { loading: true } })); try { const r = await apiClientV2.post<{ preview: Record<string, unknown>[]; error?: string }>('/pipelines/preview-step', { op: step.op, params: step.params || {}, sample_data: [{ col: 's1' }, { col: 's2' }] }); setPreviewMap(p => ({ ...p, [i]: { loading: false, data: r.preview || [], error: r.error } })) } catch { setPreviewMap(p => ({ ...p, [i]: { loading: false, error: '失败' } })) } }} className="text-gray-400 hover:text-blue-500"><Eye size={11} /></button>
                <button onClick={() => onChange('steps', steps.filter((_, j) => j !== i))} className="text-gray-400 hover:text-red-500"><Trash2 size={11} /></button>
              </div>
            </div>
            {['llm_structurize', 'llm_enrich'].includes(step.op) && (
              <label className="block space-y-1 pt-1">
                <span className="text-gray-500">LLM 失败处理</span>
                <select
                  value={String(step.params?.fallback_policy || 'fail_closed')}
                  onChange={event => onChange('steps', steps.map((current, index) => index === i
                    ? { ...current, params: { ...(current.params || {}), fallback_policy: event.target.value } }
                    : current))}
                  className="w-full border rounded px-2 py-1 bg-white"
                >
                  <option value="fail_closed">停止 Run 并显示原因</option>
                  <option value="rule_based">允许规则回退，并将 Run 标为降级</option>
                </select>
                <span className="block text-[10px] text-gray-400">规则回退结果进入待审核候选，不计为 LLM 提取成功。</span>
              </label>
            )}
          </div>
        ))}</div>)}
      </div>
    </>
  )
}
