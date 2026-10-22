import { useEffect, useState } from 'react';
import { apiClientV2, getApiErrorStatus } from '@/api/client';
import { useAuthStore } from '@/stores/authStore';
import type { ObjectPanelRecord } from './ObjectPanel';

type ActionDefinition = {
  id: string;
  name: string;
  target_entity_type?: string | null;
  status: string;
  enabled: boolean;
  parameters: { name: string; type?: string; required?: boolean; object_type?: string; choices?: string[] }[];
  effects?: { op: string }[];
  side_effects?: unknown[] | null;
  backed_by_function?: string | null;
};
type Preview = { preview_digest: string; preview: { diff: { op: string; property: string; before: unknown; after: unknown }[]; target: { object_id: string }; reversible: boolean } };
type DecisionCase = { id: string; status: string; title: string; preview_digest?: string; reviewed_preview?: Preview['preview']; action_run_id?: string; projection_status?: string; review_comment?: string };
type ActionRun = { id: string; status: string; action_type_id: string; decision_case_id?: string | null; projection_status?: string | null; side_effect_results?: { op: string }[] | null };

function message(error: unknown): string {
  const status = getApiErrorStatus(error);
  const fallback: Record<number, string> = {
    403: '没有执行此操作的权限',
    409: '对象已变化，请刷新后重新预览',
    410: '操作上下文已过期，请重新打开对象',
    503: '操作服务暂不可用，请稍后重试',
  };
  if (error && typeof error === 'object' && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    const detail = response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
      const record = detail as { message?: string; error?: string };
      return record.message || record.error || '操作未通过校验';
    }
  }
  if (error && typeof error === 'object') {
    const payload = error as { detail?: unknown; message?: unknown; error?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
    if (payload.detail && typeof payload.detail === 'object') {
      const detail = payload.detail as { message?: unknown; error?: unknown };
      if (typeof detail.message === 'string') return detail.message;
      if (typeof detail.error === 'string') return detail.error;
    }
    if (typeof payload.message === 'string') return payload.message;
    if (typeof payload.error === 'string') return payload.error;
  }
  if (status && fallback[status]) return fallback[status];
  return error instanceof Error ? error.message : '操作未通过校验';
}

export default function ObjectActionRunner({ ontologyId, object, onClose, onCommitted }: {
  ontologyId: string;
  object: ObjectPanelRecord;
  onClose: () => void;
  onCommitted?: (message?: string) => void;
}) {
  const [actions, setActions] = useState<ActionDefinition[]>([]);
  const [selected, setSelected] = useState('');
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reason, setReason] = useState('');
  const [decisionCase, setDecisionCase] = useState<DecisionCase | null>(null);
  const [cases, setCases] = useState<DecisionCase[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [runs, setRuns] = useState<ActionRun[]>([]);
  const action = actions.find((item) => item.id === selected);
  const role = useAuthStore(state => state.user?.role);
  const canWrite = role === 'admin' || role === 'editor';
  const casesBase = `/ontologies/${ontologyId}/decision-cases`;

  useEffect(() => {
    let active = true;
    apiClientV2.get<ActionDefinition[]>(`/ontologies/${ontologyId}/actions`).then((result) => {
      if (!active) return;
      setActions(result.filter((item) => item.enabled && item.status === 'published' &&
        (!item.target_entity_type || item.target_entity_type === object.object_type) &&
        !item.backed_by_function && !item.side_effects?.some(effect =>
          typeof effect !== 'object' || effect === null || (effect as { type?: string }).type !== 'review_note') &&
        !!item.effects?.length && item.effects.every(effect => effect.op === 'set_property' || effect.op === 'unset_property')));
    }).catch((cause: unknown) => { if (active) setError(message(cause)); });
    return () => { active = false; };
  }, [ontologyId, object.object_type]);

  async function refreshCases() {
    const result = await apiClientV2.get<{ cases: DecisionCase[] }>(casesBase,
      { params: { target_object_id: object.object_id, limit: 20 } });
    setCases(result.cases);
    if (decisionCase) {
      const updated = result.cases.find(item => item.id === decisionCase.id);
      if (updated) setDecisionCase(updated);
    }
  }
  useEffect(() => {
    let active = true;
    apiClientV2.get<{ cases: DecisionCase[] }>(casesBase,
      { params: { target_object_id: object.object_id, limit: 20 } })
      .then(result => { if (active) setCases(result.cases); }).catch(() => {});
    return () => { active = false; };
  }, [casesBase, object.object_id]);

  async function refreshRuns() {
    const items = await apiClientV2.get<ActionRun[]>(`/ontologies/${ontologyId}/action-runs`, { params: { target_object_id: object.object_id, limit: 10 } });
    setRuns(items);
  }
  useEffect(() => {
    let active = true;
    apiClientV2.get<ActionRun[]>(`/ontologies/${ontologyId}/action-runs`, { params: { target_object_id: object.object_id, limit: 10 } })
      .then(items => { if (active) setRuns(items); }).catch(() => {});
    return () => { active = false; };
  }, [ontologyId, object.object_id]);

  const targetParameter = action?.parameters?.find(item => item.type === 'object' &&
    (!item.object_type || item.object_type === object.object_type));
  function updateParameter(name: string, type: string | undefined, raw: string) {
    setDraft(previous => ({ ...previous, [name]: raw }));
    setPreview(null);
    const kind = (type || 'string').toLowerCase();
    let value: unknown = raw;
    let issue = '';
    if (raw === '') value = undefined;
    else if (kind === 'boolean') value = raw === 'true';
    else if (['number', 'integer', 'decimal'].includes(kind)) {
      value = Number(raw);
      if (!Number.isFinite(value) || (kind === 'integer' && !Number.isInteger(value))) issue = '请输入有效数字';
    } else if (['object', 'struct', 'array'].includes(kind)) {
      try {
        value = JSON.parse(raw);
        if ((kind === 'array' && !Array.isArray(value)) || (kind !== 'array' && (value === null || Array.isArray(value) || typeof value !== 'object'))) issue = 'JSON 类型与参数定义不一致';
      } catch { issue = '请输入有效 JSON'; }
    }
    setInvalid(previous => ({ ...previous, [name]: issue }));
    setValues(previous => ({ ...previous, [name]: value }));
  }
  async function showPreview() {
    if (!action) return;
    if (Object.values(invalid).some(Boolean)) { setError('请先修正参数格式'); return; }
    if (!reason.trim()) { setError('请填写此次操作的理由'); return; }
    setBusy(true); setError(''); setSuccess(''); setPreview(null);
    try {
      const target = await apiClientV2.get<{ etag: string; object_type: string }>(
        `${casesBase}/target-state/${encodeURIComponent(object.object_id)}`);
      const parameters = { ...values };
      if (targetParameter && parameters[targetParameter.name] === undefined) {
        parameters[targetParameter.name] = { object_type: target.object_type, object_id: object.object_id };
      }
      if (action.parameters.some(item => item.name === 'reason' && item.type === 'string') &&
          parameters.reason === undefined) parameters.reason = reason.trim();
      const created = await apiClientV2.post<DecisionCase>(casesBase, {
        kind: 'general', title: action.name, action_type_id: action.id,
        target_object_id: object.object_id, target_object_type: target.object_type,
        parameters, evidence_refs: [], reason: reason.trim(),
        context: { source: 'object_explorer' }, expected_etag: target.etag,
        client_request_id: crypto.randomUUID(),
      });
      setDecisionCase(created);
      const result = await apiClientV2.post<Preview>(`${casesBase}/${created.id}:preview`, {});
      setPreview(result);
      await refreshCases();
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }
  async function submit() {
    if (!decisionCase || !preview) return;
    setBusy(true); setError('');
    try {
      const submitted = await apiClientV2.post<DecisionCase>(
        `${casesBase}/${decisionCase.id}:submit`, { preview_digest: preview.preview_digest });
      setDecisionCase(submitted);
      setPreview(null);
      setSuccess('已提交审核，正式对象尚未修改。');
      await refreshCases();
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }
  async function review(decision: 'approved' | 'rejected' | 'changes_requested') {
    if (!decisionCase) return;
    setBusy(true); setError('');
    try {
      const updated = await apiClientV2.post<DecisionCase>(`${casesBase}/${decisionCase.id}:review`,
        { decision, comment: decision === 'approved' ? '已核对预览和人工理由' : '请补充或修正理由与证据' });
      setDecisionCase(updated);
      setSuccess(decision === 'approved' ? '审核已批准，尚待执行。' : '审核结果已记录，正式对象未修改。');
      await refreshCases();
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }
  async function execute() {
    if (!decisionCase) return;
    setBusy(true); setError('');
    try {
      const result = await apiClientV2.post<{ case: DecisionCase }>(`${casesBase}/${decisionCase.id}:execute`, {});
      setDecisionCase(result.case);
      setSuccess('审核写回已完成，可从事项和 Action Run 回看。');
      onCommitted?.('审核写回已完成。');
      await Promise.all([refreshCases(), refreshRuns()]);
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }
  async function revert(runId: string) {
    setBusy(true); setError(''); setSuccess('');
    try {
      await apiClientV2.post(`/ontologies/${ontologyId}/action-runs/${runId}:revert`, {});
      setSuccess('属性编辑已回退。'); onCommitted?.('属性编辑已回退。');
      await Promise.all([refreshRuns(), refreshCases()]);
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  }

  return <div className="fixed inset-0 z-50 grid place-items-center bg-slate-900/50 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section role="dialog" aria-modal="true" aria-label="执行对象操作" className="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl">
      <div className="flex items-start justify-between gap-3"><div><h2 className="text-base font-semibold">对象操作审核</h2><p className="text-xs text-slate-500">{object.object_type} · {object.object_id} · 正式数据</p></div><button type="button" onClick={onClose} aria-label="关闭操作">×</button></div>
      <p className="mt-2 text-xs text-slate-600">先预览并提交事项，审核批准后再执行本地写回。</p>
      <label className="mt-4 block text-sm">操作
        <select className="mt-1 w-full rounded border p-2" value={selected} onChange={(event) => { setSelected(event.target.value); setValues({}); setDraft({}); setInvalid({}); setPreview(null); setDecisionCase(null); setError(''); }}>
          <option value="">选择已发布操作</option>{actions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      {action?.parameters?.filter(parameter => parameter.name !== targetParameter?.name && parameter.name !== 'reason').map((parameter) => <label key={parameter.name} className="mt-3 block text-sm">{parameter.name}{parameter.required && ' *'}
        {parameter.choices?.length ? <select className="mt-1 w-full rounded border p-2" aria-label={parameter.name} value={draft[parameter.name] || ''} onChange={event => updateParameter(parameter.name, parameter.type, event.target.value)}><option value="">选择</option>{parameter.choices.map(choice => <option key={choice} value={choice}>{choice}</option>)}</select>
          : parameter.type === 'boolean' ? <select className="mt-1 w-full rounded border p-2" aria-label={parameter.name} value={draft[parameter.name] || ''} onChange={event => updateParameter(parameter.name, parameter.type, event.target.value)}><option value="">选择</option><option value="true">true</option><option value="false">false</option></select>
          : ['object', 'struct', 'array'].includes(parameter.type || '') ? <textarea className="mt-1 w-full rounded border p-2 font-mono text-xs" aria-label={parameter.name} placeholder={parameter.type === 'array' ? '[]' : '{}'} value={draft[parameter.name] || ''} onChange={event => updateParameter(parameter.name, parameter.type, event.target.value)} />
          : <input className="mt-1 w-full rounded border p-2" aria-label={parameter.name} value={draft[parameter.name] || ''} onChange={event => updateParameter(parameter.name, parameter.type, event.target.value)} />}
        {invalid[parameter.name] && <span className="mt-1 block text-xs text-red-700">{invalid[parameter.name]}</span>}
      </label>)}
      {action && <label className="mt-3 block text-sm">操作理由 *<textarea className="mt-1 min-h-20 w-full rounded border p-2" value={reason} maxLength={4000} onChange={event => { setReason(event.target.value); setPreview(null); }} placeholder="说明判断依据；无来源证据时作为人工例外理由记录" /></label>}
      {!actions.length && !error && <p className="mt-3 text-xs text-slate-500">此对象类型没有可用的已发布操作。</p>}
      {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
      {success && <p role="status" className="mt-3 text-sm text-green-700">{success}</p>}
      {preview && <div className="mt-4 rounded border bg-slate-50 p-3 text-xs"><p className="font-medium">预计修改 {preview.preview.diff.length} 个字段 · {preview.preview.target.object_id}</p><ul className="mt-2 space-y-1">{preview.preview.diff.map((edit, index) => <li key={index}>{edit.op} · {edit.property}: {String(edit.before ?? '∅')} → {String(edit.after ?? '∅')}</li>)}</ul><p className="mt-2 font-mono text-[10px] text-slate-500">预览摘要 {preview.preview_digest.slice(0, 16)}</p></div>}
      {!!cases.length && <div className="mt-4 border-t pt-3"><h3 className="text-xs font-medium">关联决策事项</h3><div className="mt-2 flex flex-wrap gap-1">{cases.slice(0, 8).map(item => <button type="button" key={item.id} className={`rounded border px-2 py-1 text-[11px] ${decisionCase?.id === item.id ? 'border-blue-600 bg-blue-50 text-blue-700' : 'border-slate-200'}`} onClick={() => { setDecisionCase(item); setPreview(null); setError(''); }}>{item.status} · {item.id.slice(0, 8)}</button>)}</div></div>}
      {decisionCase && <div className="mt-3 rounded border border-blue-100 bg-blue-50/50 p-3 text-xs"><p className="font-medium">{decisionCase.title} · {decisionCase.status}</p><p className="mt-1 break-all font-mono text-[10px]">Case {decisionCase.id}</p>{decisionCase.review_comment && <p className="mt-1">审核意见：{decisionCase.review_comment}</p>}{!preview && decisionCase.reviewed_preview && <ul className="mt-2 space-y-1">{decisionCase.reviewed_preview.diff.map((edit, index) => <li key={index}>{edit.property}: {String(edit.before ?? '∅')} → {String(edit.after ?? '∅')}</li>)}</ul>}{decisionCase.action_run_id && <p className="mt-2 break-all font-mono text-[10px]">Run {decisionCase.action_run_id} · 图投影 {decisionCase.projection_status ?? '待同步'}</p>}</div>}
      {!!runs.length && <div className="mt-4 max-h-32 overflow-auto border-t pt-3"><h3 className="text-xs font-medium">最近的对象操作</h3>{runs.map(run => <div key={run.id} className="mt-2 flex items-center justify-between text-xs"><span>{run.action_type_id} · {run.status}</span>{run.status === 'completed' && !!run.side_effect_results?.length && run.side_effect_results.every(item => item.op === 'set_property' || item.op === 'unset_property') && <button type="button" disabled={busy} onClick={() => void revert(run.id)} className="text-blue-700">回退属性编辑</button>}</div>)}</div>}
      <div className="mt-5 flex flex-wrap justify-end gap-2"><button type="button" className="rounded border px-3 py-2 text-sm" onClick={onClose}>关闭</button><button type="button" className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={!selected || !canWrite || !reason.trim() || busy} onClick={() => void showPreview()}>创建事项并预览</button>{decisionCase?.status === 'draft' && <button type="button" className="rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={!preview || busy} onClick={() => void submit()}>提交审核</button>}{role === 'admin' && decisionCase?.status === 'ready_for_review' && <><button type="button" className="rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy} onClick={() => void review('approved')}>批准</button><button type="button" className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy} onClick={() => void review('rejected')}>驳回</button><button type="button" className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy} onClick={() => void review('changes_requested')}>退回</button></>}{canWrite && decisionCase?.status === 'approved' && <button type="button" className="rounded bg-blue-700 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy} onClick={() => void execute()}>执行审核写回</button>}</div>
    </section>
  </div>;
}
