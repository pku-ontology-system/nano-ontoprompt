/* eslint-disable @typescript-eslint/no-explicit-any */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Clock3, X } from "lucide-react";
import { apiClientV2 } from "@/api/client";

type Change = { id: string; target_kind: string; operation: string; target_id?: string; before?: any; after?: any; validation?: any; base_revision_id?: string; result_revision_id?: string; created_at?: string; note?: string };
type RestorePreview = { target_revision: { revision_no: number }; expected_current_revision_id: string | null; preview_digest: string; can_restore: boolean; blockers: Array<{ entity_id: string; name: string }>; diff: Record<string, { added: number; removed: number; changed: number }>; preserves_instance_rows: boolean };

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = error.detail;
    if (typeof detail === "string") return detail;
    if (typeof detail === "object" && detail !== null && "message" in detail) return String(detail.message);
  }
  return "恢复操作失败，请重试";
}

export default function ChangeHistoryDrawer({ ontologyId, onClose }: { ontologyId: string; onClose: () => void }) {
  const { data, isLoading } = useQuery({ queryKey: ["ontology-changes", ontologyId], queryFn: () => apiClientV2.get<{ changes: Change[] }>(`/ontologies/${ontologyId}/changes`) });
  const [restorePreview, setRestorePreview] = useState<RestorePreview | null>(null);
  const [restoreRevisionId, setRestoreRevisionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const previewRestore = async (revisionId?: string) => {
    if (!revisionId) return;
    setBusy(true); setError(""); setRestorePreview(null); setRestoreRevisionId(revisionId);
    try {
      setRestorePreview(await apiClientV2.post<RestorePreview>(`/ontologies/${ontologyId}/revisions/${revisionId}:preview-restore`));
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };
  const confirmRestore = async () => {
    if (!restorePreview?.can_restore || !restoreRevisionId) return;
    setBusy(true); setError("");
    try {
      await apiClientV2.post(`/ontologies/${ontologyId}/revisions/${restoreRevisionId}/restore`, {
        expected_current_revision_id: restorePreview.expected_current_revision_id,
        preview_digest: restorePreview.preview_digest,
      });
      window.location.reload();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };
  return <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[420px] flex-col border-l border-slate-200 bg-white shadow-xl" data-testid="change-history-drawer">
    <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3"><div className="flex items-center gap-2"><Clock3 size={16} /><h2 className="text-sm font-semibold text-slate-900">修改记录</h2></div><button onClick={onClose} className="rounded p-1 text-slate-500 hover:bg-slate-100"><X size={16} /></button></div>
    <div className="flex-1 overflow-y-auto p-4">
      {error && <p role="alert" className="mb-3 rounded bg-red-50 p-2 text-xs text-red-700">{error}</p>}
      {restorePreview && <section className="mb-4 rounded-lg border border-blue-200 bg-blue-50 p-3" data-testid="change-history-restore-preview">
        <strong className="text-xs text-slate-800">恢复预览 · r{restorePreview.target_revision.revision_no}</strong>
        <p className="mt-1 text-[11px] text-slate-600">将创建新的当前修订；实例行会保留。</p>
        <div className="mt-2 space-y-1">{Object.entries(restorePreview.diff).map(([kind, counts]) => <p key={kind} className="text-[11px] text-slate-700">{kind}：新增 {counts.added} · 删除 {counts.removed} · 修改 {counts.changed}</p>)}</div>
        {restorePreview.blockers.length > 0 && <p className="mt-2 text-[11px] text-amber-800">当前不能恢复：会移除仍有关联实例的类型 {restorePreview.blockers.map((item) => item.name).join("、")}</p>}
        {!restorePreview.can_restore && restorePreview.blockers.length === 0 && <p className="mt-2 text-[11px] text-slate-600">该版本已经是当前修订，无需恢复。</p>}
        <div className="mt-3 flex gap-2"><button type="button" disabled={busy} onClick={() => { setRestorePreview(null); setError(""); }} className="rounded-md border border-slate-300 px-2.5 py-1.5 text-[11px] text-slate-700">取消</button><button type="button" disabled={busy || !restorePreview.can_restore} onClick={() => void confirmRestore()} className="rounded-md bg-blue-700 px-2.5 py-1.5 text-[11px] text-white disabled:opacity-50">{busy ? "处理中…" : "确认恢复"}</button></div>
      </section>}
      {isLoading ? <p className="text-xs text-slate-500">正在读取记录</p> : <div className="space-y-3">{(data?.changes || []).map((change) => <article key={change.id} className="rounded-lg border border-slate-200 p-3"><div className="flex items-center justify-between gap-2"><strong className="text-xs text-slate-800">{change.operation === "add" ? "新增" : change.operation === "update" ? "修改" : change.operation === "restore" ? "恢复" : "删除"} · {change.target_kind}</strong><span className="text-[10px] text-slate-400">{change.created_at ? new Date(change.created_at).toLocaleString() : "—"}</span></div><p className="mt-1 break-all text-[11px] text-slate-500">对象 {change.target_id || "—"} · 修订 {change.result_revision_id || "—"}</p>{change.note && <p className="mt-2 text-xs text-slate-600">{change.note}</p>}<details className="mt-2"><summary className="cursor-pointer text-[11px] text-slate-500">查看差异</summary><pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-[10px] text-slate-600">{JSON.stringify({ before: change.before, after: change.after, validation: change.validation }, null, 2)}</pre></details>{change.base_revision_id && <button disabled={busy} onClick={() => void previewRestore(change.base_revision_id)} className="mt-3 rounded-md border border-slate-300 px-2.5 py-1.5 text-[11px] text-slate-700 hover:border-slate-600 disabled:opacity-50">{busy && restoreRevisionId === change.base_revision_id ? "正在预览…" : "预览恢复"}</button>}</article>)}{!data?.changes?.length && <p className="py-8 text-center text-xs text-slate-400">暂无修改记录</p>}</div>}
    </div>
  </aside>;
}
