import { useCallback, useEffect, useState } from "react";
import { Check, CircleAlert, Loader2, RefreshCw, X } from "lucide-react";
import { apiClientV2, formatApiError } from "@/api/client";

type JsonObject = Record<string, unknown>;
type SchemaChange = {
  key: string;
  name?: string;
  changed_fields?: string[];
  before?: JsonObject;
  after?: JsonObject;
  property_changes?: JsonObject;
};
type SchemaDiff = {
  digest?: string;
  counts?: Record<string, Record<string, number>>;
  entries?: Record<string, {
    added?: SchemaChange[];
    changed?: SchemaChange[];
    removed?: SchemaChange[];
    omitted?: Record<string, number>;
  }>;
};
type ImpactPreview = {
  digest?: string;
  schema?: {
    current_digest?: string;
    projected_digest?: string;
    changes?: boolean;
  };
  metadata?: {
    current_digest?: string;
    projected_digest?: string;
    changes?: boolean;
    inventory_status?: string;
  };
  changed_entities?: { count?: number; items?: JsonObject[]; omitted?: number };
  dependent_logic_rules?: { count?: number; items?: JsonObject[]; omitted?: number };
  affected_actions?: { count?: number; items?: JsonObject[]; omitted?: number };
  metadata_bound_query_resources?: { status?: string; counts?: JsonObject; resource_names_included?: boolean };
  scenario_impact?: { status?: string; counts?: JsonObject; resource_names_included?: boolean };
};
type ConstructionReviewEvent = {
  event_type: string;
  detail?: JsonObject;
  created_at?: string | null;
};
export type ConstructionReview = {
  id: string;
  draft_id: string;
  dataset_id: string;
  dataset_version_id: string;
  ontology_id?: string | null;
  construction_run_id?: string | null;
  candidate_digest: string;
  status: "pending_review" | "approved" | "rejected" | "changes_requested";
  submitted_by?: string | null;
  reviewed_by?: string | null;
  decision_note?: string | null;
  candidate?: {
    manifest?: JsonObject & { schema_diff?: SchemaDiff; impact_preview?: ImpactPreview };
    mapping_summary?: Record<string, JsonObject[]>;
    lineage?: JsonObject;
  };
  events?: ConstructionReviewEvent[];
  impact?: {
    note?: string;
    preview_kind?: string;
    current_counts?: JsonObject;
    proposed_counts?: JsonObject;
  };
};
type ReviewRun = { id: string; status: string };
type ReviewDecisionResponse<Run extends ReviewRun> = { review: ConstructionReview; run?: Run | null };

const stateLabels: Record<string, string> = {
  pending_review: "等待独立审核",
  approved: "已批准",
  rejected: "已拒绝",
  changes_requested: "要求修改",
};

function display(value: unknown): string {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return value.map(display).join("、") || "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function CountList({ title, counts }: { title: string; counts?: JsonObject }) {
  if (!counts) return null;
  return (
    <div className="rounded border border-gray-100 bg-white p-3">
      <p className="text-xs font-semibold text-gray-700">{title}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {Object.entries(counts).map(([key, value]) => (
          <span key={key} className="rounded bg-slate-50 px-2 py-1 text-[11px] text-slate-600">
            {key}: {display(value)}
          </span>
        ))}
      </div>
    </div>
  );
}

const schemaKindLabels: Record<string, string> = {
  entity_types: "实体类型",
  relationships: "关系",
  logic_rules: "规则",
};
const schemaChangeLabels: Record<string, string> = { added: "新增", changed: "变更", removed: "移除" };

function SchemaDiffPanel({ title, diff }: { title: string; diff?: SchemaDiff | null }) {
  if (!diff?.counts || !diff.entries) return null;
  const details = Object.entries(diff.entries);
  return (
    <details className="mt-3 rounded border border-blue-100 bg-blue-50/40 p-3">
      <summary className="cursor-pointer text-xs font-semibold text-slate-700">{title}</summary>
      <div className="mt-2 space-y-3">
        {details.map(([kind, groups]) => {
          const counts = diff.counts?.[kind] || {};
          return (
            <div key={kind} className="rounded border border-gray-100 bg-white p-2">
              <div className="flex flex-wrap items-center gap-2 text-[11px] font-semibold text-slate-700">
                <span>{schemaKindLabels[kind] || kind}</span>
                {Object.entries(counts).map(([change, count]) => (
                  <span key={change} className="rounded bg-slate-50 px-1.5 py-0.5 font-normal text-slate-500">
                    {schemaChangeLabels[change] || change} {count}
                  </span>
                ))}
              </div>
              {(["added", "changed", "removed"] as const).map((change) => {
                const rows = groups[change] || [];
                if (!rows.length) return null;
                return (
                  <div key={change} className="mt-2 space-y-1">
                    <p className="text-[10px] font-semibold text-slate-500">{schemaChangeLabels[change]}</p>
                    {rows.map((row) => (
                      <details key={`${change}-${row.key}`} className="rounded bg-slate-50 px-2 py-1">
                        <summary className="cursor-pointer break-all text-[10px] text-slate-600">
                          {row.name || row.key}{row.changed_fields?.length ? ` · ${row.changed_fields.join("、")}` : ""}
                        </summary>
                        <div className="mt-1 grid gap-2 md:grid-cols-2">
                          {row.before && <pre className="overflow-auto whitespace-pre-wrap break-words text-[9px] text-slate-500">变更前：{JSON.stringify(row.before, null, 2)}</pre>}
                          {row.after && <pre className="overflow-auto whitespace-pre-wrap break-words text-[9px] text-slate-500">变更后：{JSON.stringify(row.after, null, 2)}</pre>}
                          {row.property_changes && <pre className="overflow-auto whitespace-pre-wrap break-words text-[9px] text-slate-500">属性差异：{JSON.stringify(row.property_changes, null, 2)}</pre>}
                        </div>
                      </details>
                    ))}
                  </div>
                );
              })}
              {Object.values(groups.omitted || {}).some((count) => count > 0) && (
                <p className="mt-2 text-[10px] text-amber-700">部分明细已折叠；总数仍完整计入上方统计。</p>
              )}
            </div>
          );
        })}
      </div>
      {diff.digest && <p className="mt-2 break-all font-mono text-[9px] text-slate-400">Diff SHA-256 · {diff.digest}</p>}
    </details>
  );
}

function ImpactPreviewPanel({ preview }: { preview?: ImpactPreview | null }) {
  if (!preview) return null;
  const changedEntities = preview.changed_entities;
  const dependentRules = preview.dependent_logic_rules;
  const affectedActions = preview.affected_actions;
  const queryCounts = preview.metadata_bound_query_resources?.counts;
  const scenarioCounts = preview.scenario_impact?.counts;
  const resourceLabels: Record<string, string> = {
    object_sets: "Object Set",
    object_views: "对象详情配置",
    query_data_views: "查询数据视图",
  };
  const scenarioLabels: Record<string, string> = {
    scenario_resources: "持久化情景",
    scenario_studies: "情景研究",
    scenario_run_artifacts: "情景运行产物",
    scenario_run_results: "情景运行结果",
    what_if_scenarios: "What-if 固定基线",
    what_if_runs: "What-if 运行记录",
    decision_cases: "关联 DecisionCase",
    agent_context_items: "Agent Session What-if 引用",
    agent_sessions: "关联 Agent Session",
    agent_runs: "关联 Agent Run",
  };
  const reasonLabels: Record<string, string> = {
    linked_entity_schema_changed: "引用的实体结构发生变化",
    linked_logic_rule_changed: "引用的逻辑规则发生变化",
    shares_entity_with_changed_logic_rule: "与变更规则关联同一实体",
  };
  const renderItems = (title: string, section?: { count?: number; items?: JsonObject[]; omitted?: number }) => {
    if (!section || !section.count) return null;
    return (
      <details className="rounded border border-gray-100 bg-white p-2">
        <summary className="cursor-pointer text-[11px] font-semibold text-slate-700">{title} · {section.count}</summary>
        <ul className="mt-2 space-y-1 text-[10px] text-slate-600">
          {(section.items || []).map((item, index) => (
            <li key={`${String(item.id || item.key || index)}`} className="break-all">
              {display(item.name || item.id || item.key)}
              {Array.isArray(item.reasons) && item.reasons.length ? ` · ${item.reasons.map((reason) => reasonLabels[String(reason)] || String(reason)).join("、")}` : item.reason ? ` · ${reasonLabels[String(item.reason)] || display(item.reason)}` : ""}
            </li>
          ))}
        </ul>
        {!!section.omitted && <p className="mt-1 text-[10px] text-amber-700">另有 {section.omitted} 项未展开。</p>}
      </details>
    );
  };
  const metadataChanged = preview.metadata?.changes;
  const schemaChanged = preview.schema?.changes;
  const hasExistingTarget = preview.metadata?.inventory_status !== "no_existing_target";
  return (
    <section className="mt-3 rounded border border-amber-100 bg-amber-50/50 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-semibold text-slate-800">下游影响预览</p>
        {preview.digest && <span className="break-all font-mono text-[9px] text-slate-400">SHA-256 · {preview.digest.slice(0, 16)}</span>}
      </div>
      <p className="mt-1 text-[11px] text-slate-600">
        本体结构{schemaChanged ? "将变化" : "保持一致"} · 查询元数据{metadataChanged ? "将变化" : "保持一致"} · 直接引用按实体、规则 ID 与名称匹配
      </p>
      {!!changedEntities?.count && renderItems("结构变更实体", changedEntities)}
      <div className="mt-2 grid gap-2 md:grid-cols-2">
        {renderItems("关联逻辑规则", dependentRules)}
        {renderItems("关联动作/函数", affectedActions)}
      </div>
      {queryCounts && (metadataChanged || Object.values(queryCounts).some((count) => Number(count) > 0)) && (
        <div className="mt-2 rounded border border-gray-100 bg-white p-2">
          <p className="text-[11px] font-semibold text-slate-700">发布后需要重新校验的元数据绑定资源</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {Object.entries(queryCounts).map(([key, count]) => (
              <span key={key} className="rounded bg-slate-50 px-2 py-1 text-[10px] text-slate-600">
                {resourceLabels[key] || key}: {display(count)}
              </span>
            ))}
          </div>
          <p className="mt-2 text-[10px] text-slate-500">这里只显示计数；不展示查询定义、对象样例或函数代码。</p>
        </div>
      )}
      {hasExistingTarget && scenarioCounts && (schemaChanged || metadataChanged || Object.values(scenarioCounts).some((count) => Number(count) > 0)) && (
        <div className="mt-2 rounded border border-gray-100 bg-white p-2">
          <p className="text-[11px] font-semibold text-slate-700">受结构或查询元数据影响的情景与决策资源</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {Object.entries(scenarioCounts).map(([key, count]) => (
              <span key={key} className="rounded bg-slate-50 px-2 py-1 text-[10px] text-slate-600">
                {scenarioLabels[key] || key}: {display(count)}
              </span>
            ))}
          </div>
          <p className="mt-2 text-[10px] text-slate-500">仅显示聚合数；不展示情景名称、假设、规则覆盖或决策载荷。</p>
        </div>
      )}
      {preview.metadata_bound_query_resources?.status === "no_existing_target" && (
        <p className="mt-2 text-[10px] text-slate-500">新建本体当前没有已发布的下游查询资源。</p>
      )}
    </section>
  );
}

function ReviewCard({
  review,
  onDecision,
  busy,
}: {
  review: ConstructionReview;
  onDecision: (review: ConstructionReview, decision: "approved" | "rejected" | "changes_requested", note: string) => Promise<void>;
  busy: boolean;
}) {
  const [note, setNote] = useState("");
  const mapping = review.candidate?.mapping_summary || {};
  const lineage = review.candidate?.lineage || {};
  const manifest = review.candidate?.manifest || {};
  const schemaDiff = review.candidate?.manifest?.schema_diff;
  const impactPreview = review.candidate?.manifest?.impact_preview;
  const pending = review.status === "pending_review";
  return (
    <article className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-semibold text-slate-900">候选 {review.id.slice(0, 12)}</p>
          <p className="mt-1 text-xs text-slate-500">{stateLabels[review.status] || review.status} · 提交者 {review.submitted_by || "—"}</p>
        </div>
        <span className="rounded bg-slate-100 px-2 py-1 font-mono text-[10px] text-slate-500">{review.candidate_digest.slice(0, 16)}</span>
      </div>
      <div className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
        <p>DatasetVersion <span className="font-mono">{display(manifest.dataset_version_id || review.dataset_version_id)}</span></p>
        <p>数据 checksum <span className="font-mono break-all">{display(manifest.dataset_checksum)}</span></p>
        <p>映射输入摘要 <span className="font-mono break-all">{display(manifest.mapping_task_input_digest || lineage.mapping_attempt_input_digest)}</span></p>
        <p>基准修订 <span className="font-mono">{display(manifest.base_revision_id)}</span></p>
      </div>
      {review.impact?.note && <p className="mt-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">{review.impact.note}</p>}
      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <CountList title="当前修订规模" counts={review.impact?.current_counts} />
        <CountList title="映射候选规模" counts={review.impact?.proposed_counts} />
      </div>
      <SchemaDiffPanel title="查看基准修订与候选映射的结构差异" diff={schemaDiff} />
      <ImpactPreviewPanel preview={impactPreview} />
      <details className="mt-3 rounded border border-gray-100 p-3">
        <summary className="cursor-pointer text-xs font-semibold text-slate-700">查看已确认映射与生成来源</summary>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          {Object.entries(mapping).map(([section, rows]) => (
            <div key={section}>
              <p className="mb-1 text-xs font-semibold text-slate-600">{section} · {rows.length}</p>
              <div className="max-h-48 space-y-1 overflow-auto rounded bg-slate-50 p-2">
                {rows.map((row, index) => <pre key={`${section}-${index}`} className="whitespace-pre-wrap break-words text-[10px] text-slate-600">{JSON.stringify(row, null, 2)}</pre>)}
                {!rows.length && <span className="text-[10px] text-slate-400">无条目</span>}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 grid gap-1 text-[11px] text-slate-500 sm:grid-cols-2">
          {Object.entries(lineage).filter(([key]) => key !== "credential_fingerprint").map(([key, value]) => <p key={key} className="break-all">{key}: {display(value)}</p>)}
        </div>
      </details>
      {pending && (
        <div className="mt-3 border-t border-gray-100 pt-3">
          <textarea className="wb-input min-h-16 w-full text-xs" value={note} onChange={(event) => setNote(event.target.value)} placeholder="拒绝或要求修改时填写原因" />
          <div className="mt-2 flex flex-wrap gap-2">
            <button className="wb-button-primary text-xs" disabled={busy} onClick={() => void onDecision(review, "approved", "")}><Check size={13} />批准并启动构建</button>
            <button className="wb-button-secondary text-xs" disabled={busy || !note.trim()} onClick={() => void onDecision(review, "changes_requested", note.trim())}><RefreshCw size={13} />要求修改</button>
            <button className="wb-button-secondary text-xs text-red-700" disabled={busy || !note.trim()} onClick={() => void onDecision(review, "rejected", note.trim())}><X size={13} />拒绝</button>
          </div>
        </div>
      )}
      {!pending && review.decision_note && <p className="mt-3 text-xs text-slate-500">审核意见：{review.decision_note}</p>}
      {review.events?.filter((event) => event.event_type === "materialized").map((event) => {
        const verification = event.detail?.verification as JsonObject | undefined;
        return (
          <div key={`${event.event_type}-${event.created_at || ""}`} className="mt-3 rounded border border-emerald-100 bg-emerald-50 p-3 text-xs text-emerald-900">
            <p className="font-semibold">实际物化已与候选差异核对一致</p>
            <p className="mt-1 break-all">修订 {display(event.detail?.revision_id)} · 快照 SHA-256 {display(event.detail?.snapshot_hash)}</p>
            <p className="mt-1">结构摘要核验：{display(verification?.status)}</p>
            <SchemaDiffPanel title="查看实际发布的结构差异" diff={event.detail?.schema_diff as SchemaDiff | undefined} />
          </div>
        );
      })}
    </article>
  );
}

export function ConstructionReviewQueue<Run extends ReviewRun = ReviewRun>({
  submittedReview,
  onRun,
  onSubmittedReview,
}: {
  submittedReview: ConstructionReview | null;
  onRun?: (run: Run) => void;
  onSubmittedReview?: (review: ConstructionReview) => void;
}) {
  const [reviews, setReviews] = useState<ConstructionReview[]>([]);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    try {
      const result = await apiClientV2.get<{ reviews?: ConstructionReview[] }>("/construction/reviews");
      setReviews(result?.reviews || []);
      setError("");
    } catch (reason: unknown) {
      setError(formatApiError(reason));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!submittedReview?.id) return;
    let cancelled = false;
    const check = async () => {
      try {
        const current = await apiClientV2.get<ConstructionReview>(`/construction/reviews/${submittedReview.id}`);
        if (cancelled) return;
        onSubmittedReview?.(current);
        if (current.status === "approved" && current.construction_run_id) {
          const run = await apiClientV2.get<Run>(`/construction-runs/${current.construction_run_id}`);
          if (!cancelled) onRun?.(run);
        }
      } catch {
        // The review panel remains usable if a user loses read access later.
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [submittedReview?.id, submittedReview?.status, onRun, onSubmittedReview]);

  const decide = async (
    review: ConstructionReview,
    decision: "approved" | "rejected" | "changes_requested",
    note: string,
  ) => {
    setBusyId(review.id);
    setError("");
    setNotice("");
    try {
      const result = await apiClientV2.post<ReviewDecisionResponse<Run>>(
        `/construction/reviews/${review.id}/decision`,
        { decision, note: note || undefined },
      );
      setNotice(decision === "approved" ? "已批准，构建任务已启动。" : decision === "rejected" ? "候选已拒绝。" : "已要求提交者修改候选。" );
      if (result.run) onRun?.(result.run);
      await refresh();
    } catch (reason: unknown) {
      setError(formatApiError(reason));
    } finally {
      setBusyId("");
    }
  };

  return (
    <section className="wb-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="wb-section-kicker">M2.3 / 独立审核</div>
          <h2 className="wb-section-title mt-1">构建候选审核队列</h2>
        </div>
        <button className="wb-button-secondary text-xs" onClick={() => void refresh()}><RefreshCw size={13} />刷新</button>
      </div>
      {submittedReview && (
        <div className="mt-3 rounded-lg border border-blue-100 bg-blue-50 p-3 text-xs text-blue-900">
          <p className="font-semibold">本次提交：{stateLabels[submittedReview.status] || submittedReview.status}</p>
          <p className="mt-1">候选摘要 {submittedReview.candidate_digest.slice(0, 16)} · DatasetVersion {submittedReview.dataset_version_id}</p>
          {submittedReview.status === "pending_review" && <p className="mt-1">候选已冻结；需另一位具备数据访问权限的 Editor 独立审核。</p>}
          {submittedReview.decision_note && <p className="mt-1">审核意见：{submittedReview.decision_note}</p>}
          {submittedReview.events?.filter((event) => event.event_type === "materialized").map((event) => (
            <div key={`${event.event_type}-${event.created_at || ""}`} className="mt-2 rounded border border-emerald-100 bg-white p-2 text-emerald-900">
              <p className="font-semibold">已发布修订 {display(event.detail?.revision_no)} · ID {display(event.detail?.revision_id)}</p>
              <p className="mt-1 break-all">快照 SHA-256 {display(event.detail?.snapshot_hash)}</p>
              <SchemaDiffPanel title="查看实际物化差异" diff={event.detail?.schema_diff as SchemaDiff | undefined} />
            </div>
          ))}
        </div>
      )}
      {notice && <p className="mt-3 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-3 flex items-start gap-2 text-xs text-red-700"><CircleAlert size={14} className="mt-0.5 shrink-0" />{error}</p>}
      {reviews.length ? (
        <div className="mt-3 space-y-3">{reviews.map((review) => <ReviewCard key={review.id} review={review} busy={busyId === review.id} onDecision={decide} />)}</div>
      ) : !error ? (
        <p className="mt-3 flex items-center gap-2 text-xs text-slate-500"><Loader2 size={13} />当前没有可审核的其他用户候选。</p>
      ) : null}
    </section>
  );
}
