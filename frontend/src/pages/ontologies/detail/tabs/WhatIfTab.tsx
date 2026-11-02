/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Play, RotateCcw } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { apiClientV2 } from "@/api/client";

type Node = { id: string; entity_type?: string; event_seq?: string | number | null; properties?: Record<string, any> };
type VectorIndexJob = { id: string; status: "queued" | "running" | "completed" | "failed" | "pruned"; total_facts: number; indexed_facts: number; error_code?: string | null; error_message?: string | null };
type PendingVectorIndex = { status: "indexing"; message?: string; job: VectorIndexJob };
type VectorEvidence = { query: string; query_digest: string; index_revision: string; replay_id: string; episode_id: string; dataset_version_id?: string | null; snapshot_id?: string | null; snapshot_hash?: string | null; schema_revision_id: string; schema_revision_hash: string; permission_digest: string; as_of_ordinal: string; time_kind?: string; time_value?: string | number | null; time_zone?: string | null; result_count: number; use_as_authoritative_fact: false; results: Array<{ kind: "property" | "relation"; fact_id: string; subject_id: string; predicate: string; object_id?: string; value?: any; score: number; source_event_id?: string | null; evidence_ref_id?: string | null; valid_from_ordinal: string; valid_to_ordinal?: string | null }> };
type ScenarioContext = { contract_version: "1.0" | "1.1"; source_adapter_id?: string | null; source_adapter_version?: string | null; source_mapping_digest?: string | null };
type Context = { scenario_context?: ScenarioContext; base_revision_id?: string; dataset_version_id?: string | null; target_instance_id?: string; episode_id?: string; at?: string | number; mode?: string; vector_evidence?: VectorEvidence | null; provenance?: { source_kind?: string; source_id?: string | null; source_mode?: string | null; source_adapter_id?: string | null; source_adapter_version?: string | null; source_mapping_digest?: string | null; replay_id?: string | null; source_event_id?: string | null; source_event_ordinal?: string | number | null; source_sequence?: number | null; source_row_id?: string | null; entity_key?: string | null; source_payload_hash?: string | null; source_snapshot_id?: string | null; source_snapshot_hash?: string | null; source_snapshot_through_ordinal?: string | number | null; source_snapshot_event_count?: number | null; schema_revision_id?: string | null; schema_revision_hash?: string; rule_set_digest?: string; vector_index_revision?: string | null; vector_query_digest?: string | null; vector_result_count?: number | null; dataset_version_id?: string | null; time_kind?: string; time_value?: string | number | null; time_zone?: string | null; watermark_ordinal?: string | number | null; watermark_sequence?: number | null; context_digest?: string; node_count?: number; edge_count?: number; authorized_properties_by_object?: Record<string, string[]>; property_types_by_object?: Record<string, Record<string, string>> }; nodes: Node[]; edges: Array<{ id?: string; source: string; target: string; type: string }>; rules?: any[]; limits?: Record<string, number> };
type Run = { id: string; status: string; stage: string; progress: number; error?: string; baseline?: { facts?: string[]; derived?: Array<{ fact?: string }> }; diff?: { added?: string[]; removed?: string[]; unchanged?: string[]; proofs?: any[] }; engine?: string; engine_version?: string };
type RelationMode = "none" | "add" | "remove";

const stageNames: Record<string, string> = { prepare_baseline: "准备基线", baseline_reasoning: "基线推理", apply_assumption: "应用假设", scenario_reasoning: "情景推理", generate_diff: "生成差异", completed: "完成" };

type DiffStatus = "added" | "removed" | "unchanged";

function factForEdge(edge: { source: string; target: string; type: string }) {
  return `${String(edge.type || "RELATED").replace(/[^A-Za-z0-9_:-]/g, "_").toUpperCase()}(${edge.source}, ${edge.target})`;
}

function factParts(fact: string): { predicate: string; source: string; target: string } | null {
  const match = /^([A-Z][A-Z0-9_]*)\\((.*), (.*)\\)$/.exec(fact);
  if (!match) return null;
  return { predicate: match[1], source: match[2], target: match[3] };
}

function nodeText(node: Node) {
  const props = node.properties || {};
  return String(props.name || props.label || props.equipment_id || props.episode_id || props.value || node.id).slice(0, 24);
}

function ScenarioGraph({ context, diff }: { context: Context; diff: NonNullable<Run["diff"]> }) {
  const added = new Set(diff.added || []);
  const removed = new Set(diff.removed || []);
  const edgeRows = new Map<string, { source: string; target: string; predicate: string; status: DiffStatus }>();
  (context.edges || []).forEach((edge) => {
    const predicate = String(edge.type || "RELATED").replace(/[^A-Za-z0-9_:-]/g, "_").toUpperCase();
    const row = { source: edge.source, target: edge.target, predicate, status: (added.has(factForEdge(edge)) ? "added" : removed.has(factForEdge(edge)) ? "removed" : "unchanged") as DiffStatus };
    edgeRows.set(`${row.predicate}|${row.source}|${row.target}`, row);
  });
  // A newly inferred relation has no source edge in the baseline context. Add
  // it to the same bounded canvas so the green scenario result is visible.
  [...added, ...removed].forEach((fact) => {
    const parts = factParts(fact);
    // Property/type support facts remain in the textual diff; the canvas is
    // reserved for actual binary relationships between instance nodes.
    if (!parts || parts.predicate.startsWith("PROP_") || parts.predicate.startsWith("TYPE_") || (!parts.source && !parts.target)) return;
    const key = `${parts.predicate}|${parts.source}|${parts.target}`;
    if (!edgeRows.has(key)) edgeRows.set(key, { ...parts, status: added.has(fact) ? "added" : "removed" });
  });
  const candidateIds = new Set<string>();
  edgeRows.forEach((edge) => { candidateIds.add(edge.source); candidateIds.add(edge.target); });
  const sourceNodes = [...(context.nodes || [])].filter((node) => candidateIds.has(node.id));
  const known = new Set(sourceNodes.map((node) => node.id));
  candidateIds.forEach((id) => {
    if (!known.has(id)) sourceNodes.push({ id, entity_type: id.startsWith("scenario:") ? "ToolCondition" : "实体", properties: { value: id.startsWith("scenario:") ? id.split(":").slice(-1)[0] : id } });
  });
  const nodes = sourceNodes
    .slice(0, 32)
    .sort((left, right) => left.id.localeCompare(right.id));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = [...edgeRows.values()]
    .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .sort((left, right) => `${left.predicate}|${left.source}|${left.target}`.localeCompare(`${right.predicate}|${right.source}|${right.target}`))
    .slice(0, 56);
  const columns = Math.min(5, Math.max(2, Math.ceil(Math.sqrt(nodes.length || 1))));
  const rows = Math.max(1, Math.ceil(nodes.length / columns));
  const canvasWidth = 1040;
  const canvasHeight = Math.max(440, rows * 138 + 70);
  const positions = new Map(nodes.map((node, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    return [node.id, { x: 115 + column * ((canvasWidth - 230) / Math.max(columns - 1, 1)), y: 72 + row * 138 }];
  }));
  const edgeOffsets = new Map<string, number>();
  edges.forEach((edge, index) => {
    const pair = edges.filter(item => [item.source, item.target].sort().join('|') === [edge.source, edge.target].sort().join('|'));
    const pairIndex = pair.findIndex(item => item === edge);
    const centered = pairIndex - (pair.length - 1) / 2;
    edgeOffsets.set(`${edge.predicate}|${edge.source}|${edge.target}`, (Math.abs(centered) * 34 + 24) * (index % 2 === 0 ? 1 : -1));
  });
  const color: Record<DiffStatus, string> = { added: "#059669", removed: "#dc2626", unchanged: "#94a3b8" };
  return <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3" data-testid="what-if-graph">
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <span className="text-xs font-medium text-slate-700">情景数据模型</span>
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-slate-500"><span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-emerald-600" />新增</span><span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-red-600" />消失</span><span><i className="mr-1 inline-block h-2 w-2 rounded-full bg-slate-400" />未变化</span></div>
    </div>
    <div className="overflow-x-auto rounded-md border border-slate-200 bg-white">
      <svg viewBox={`0 0 ${canvasWidth} ${canvasHeight}`} className="h-auto min-h-[300px] min-w-[900px] w-full" role="img" aria-label="情景数据模型关系差异">
        <defs><marker id="what-if-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#64748b" /></marker></defs>
        {edges.map((edge) => {
          const start = positions.get(edge.source); const end = positions.get(edge.target);
          if (!start || !end) return null;
          const stroke = color[edge.status];
          const offset = edgeOffsets.get(`${edge.predicate}|${edge.source}|${edge.target}`) || 24;
          const controlX = (start.x + end.x) / 2;
          const controlY = (start.y + end.y) / 2 - offset;
          const labelX = (start.x + 2 * controlX + end.x) / 4;
          const labelY = (start.y + 2 * controlY + end.y) / 4;
          const label = edge.predicate.slice(0, 22);
          return <g key={`${edge.predicate}-${edge.source}-${edge.target}`}><path d={`M ${start.x} ${start.y} Q ${controlX} ${controlY} ${end.x} ${end.y}`} fill="none" stroke={stroke} strokeWidth={edge.status === "unchanged" ? 1.5 : 2.6} strokeDasharray={edge.status === "removed" ? "6 4" : undefined} markerEnd="url(#what-if-arrow)" opacity={edge.status === "unchanged" ? 0.7 : 0.96} /><rect x={labelX - label.length * 3.2 - 5} y={labelY - 10} width={label.length * 6.4 + 10} height="18" rx="5" fill="#ffffff" fillOpacity="0.94" stroke="#e2e8f0" /><text x={labelX} y={labelY + 3} textAnchor="middle" className="fill-slate-600 text-[9px]">{label}</text></g>;
        })}
        {nodes.map((node) => { const point = positions.get(node.id); if (!point) return null; const isTarget = node.id === context.target_instance_id; const title = nodeText(node).slice(0, 18); const kind = String(node.entity_type || "实体").slice(0, 18); return <g key={node.id}><rect x={point.x - 58} y={point.y - 29} width="116" height="58" rx="12" fill={isTarget ? "#1e293b" : "#0f766e"} stroke={isTarget ? "#f59e0b" : "#ffffff"} strokeWidth={isTarget ? 3 : 2} /><text x={point.x} y={point.y - 2} textAnchor="middle" className="fill-white text-[10px] font-medium">{title}</text><text x={point.x} y={point.y + 16} textAnchor="middle" className="fill-white/75 text-[9px]">{kind}</text></g>; })}
      </svg>
    </div>
    {!edges.length && <p className="mt-2 text-[11px] text-slate-400">当前范围没有可绘制的关系</p>}
  </div>;
}

export default function WhatIfTab({ ontologyId }: { ontologyId: string }) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [context, setContext] = useState<Context | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [evidenceQuery, setEvidenceQuery] = useState("");
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState("");
  const [evidenceIndexJob, setEvidenceIndexJob] = useState<VectorIndexJob | null>(null);
  const [step, setStep] = useState(1);
  const [scenarioId, setScenarioId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [scenarioPropertySelection, setScenarioPropertySelection] = useState("ctx_tool_condition");
  const [scenarioValue, setScenarioValue] = useState("worn");
  const [relationMode, setRelationMode] = useState<RelationMode>("none");
  const [relationSource, setRelationSource] = useState("");
  const [relationTarget, setRelationTarget] = useState("");
  const [relationType, setRelationType] = useState("RELATED");
  const [relationToRemove, setRelationToRemove] = useState("");
  const [disabledRuleIds, setDisabledRuleIds] = useState<string[]>([]);
  const [temporaryRuleEnabled, setTemporaryRuleEnabled] = useState(true);
  const targetId = params.get("target_instance_id") || params.get("target") || "";
  useEffect(() => {
    let cancelled = false;
    if (!targetId) {
      return () => { cancelled = true; };
    }
    // The fetch lifecycle owns these two visible state flags.
    setLoading(true); setError(""); // eslint-disable-line react-hooks/set-state-in-effect
    apiClientV2.get<Context>(`/ontologies/${ontologyId}/what-if/context`, { params: { target_instance_id: targetId || undefined, episode_id: params.get("episode_id") || undefined, at: params.get("at") || undefined, replay_id: params.get("replay_id") || undefined, mode: params.get("mode") || "cumulative" } }).then((result) => { if (!cancelled) { setContext(result); setEvidenceQuery(result.vector_evidence?.query || ""); setEvidenceIndexJob(null); setEvidenceError(""); setStep(1); } }).catch((reason: any) => { if (!cancelled) setError(String(reason?.detail?.message || reason?.message || "无法读取推演基线")); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [ontologyId, targetId, params]);
  useEffect(() => {
    if (!run || !scenarioId || !["queued", "running"].includes(run.status)) return;
    const timer = window.setInterval(() => { void apiClientV2.get<Run>(`/ontologies/${ontologyId}/what-if/runs/${run.id}`).then(setRun).catch(() => undefined); }, 900);
    return () => window.clearInterval(timer);
  }, [ontologyId, run, scenarioId]);
  useEffect(() => {
    const jobId = evidenceIndexJob?.id;
    if (!jobId || !["queued", "running", "completed"].includes(evidenceIndexJob?.status || "")) return;
    let cancelled = false;
    let polling = false;
    const refreshEvidence = async () => {
      try {
        const result = await apiClientV2.get<Context | PendingVectorIndex>(`/ontologies/${ontologyId}/what-if/context`, { params: { target_instance_id: targetId, episode_id: params.get("episode_id") || undefined, at: params.get("at") || undefined, replay_id: params.get("replay_id") || undefined, mode: params.get("mode") || "cumulative", evidence_query: evidenceQuery.trim() } });
        if (cancelled) return;
        if ((result as PendingVectorIndex).status === "indexing") {
          const pending = result as PendingVectorIndex;
          if (pending.job.status === "failed") {
            setEvidenceIndexJob(null);
            setEvidenceError(pending.job.error_message || "后台向量索引构建失败，请稍后重试");
          } else {
            setEvidenceIndexJob(pending.job);
          }
          return;
        }
        const contextResult = result as Context;
        setContext(contextResult);
        setEvidenceQuery(contextResult.vector_evidence?.query || evidenceQuery.trim());
        setEvidenceError("");
        setEvidenceIndexJob(null);
      } catch (reason: any) {
        if (!cancelled) setEvidenceError(String(reason?.detail?.message || reason?.message || "索引完成后读取证据失败"));
      }
    };
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const job = await apiClientV2.get<VectorIndexJob>(`/ontologies/${ontologyId}/what-if/vector-index-jobs/${jobId}`);
        if (cancelled) return;
        setEvidenceIndexJob(job);
        if (job.status === "completed") {
          await refreshEvidence();
        } else if (job.status === "failed" || job.status === "pruned") {
          setEvidenceIndexJob(null);
          setEvidenceError(job.error_message || (job.status === "pruned" ? "历史向量索引已清理，请重新检索" : "后台向量索引构建失败，请稍后重试"));
        }
      } catch {
        // Keep the persisted job visible; a later poll can recover after a transient API failure.
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [evidenceIndexJob?.id, evidenceIndexJob?.status, ontologyId, targetId, params, evidenceQuery]);
  const target = useMemo(() => context?.nodes.find((node) => node.id === (context.target_instance_id || targetId)) || context?.nodes[0], [context, targetId]);
  const scenarioDerivedFacts = useMemo(() => new Set((run?.diff?.proofs || []).map((proof: any) => String(proof.fact || ""))), [run]);
  const baselineDerivedFacts = useMemo(() => new Set((run?.baseline?.derived || []).map((proof) => String(proof.fact || ""))), [run]);
  const allowedTargetProperties = target ? (context?.provenance?.authorized_properties_by_object?.[target.id] || []) : [];
  const scenarioProperty = allowedTargetProperties.includes(scenarioPropertySelection)
    ? scenarioPropertySelection
    : allowedTargetProperties.includes("ctx_tool_condition")
      ? "ctx_tool_condition"
      : allowedTargetProperties[0] || "";
  const currentValue = scenarioProperty ? target?.properties?.[scenarioProperty] : undefined;
  const scenarioPropertyType = scenarioProperty
    ? context?.provenance?.property_types_by_object?.[target?.id || ""]?.[scenarioProperty]
    : undefined;
  const numericScenarioProperty = ["integer", "int", "decimal", "float", "number"].includes(String(scenarioPropertyType || "").toLowerCase()) || typeof currentValue === "number";
  const booleanScenarioProperty = ["boolean", "bool"].includes(String(scenarioPropertyType || "").toLowerCase()) || typeof currentValue === "boolean";
  const isFactoryNetSource = context?.provenance?.source_id === "factorynet_cnc"
    || Boolean(target?.id.startsWith("FactoryNet:Observation:"));
  useEffect(() => {
    if (isFactoryNetSource && ["ctx_tool_condition", "tool_condition"].includes(scenarioProperty)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setScenarioValue(String(currentValue ?? "") === "worn" ? "unworn" : "worn");
    } else {
      setScenarioValue(currentValue == null ? "" : String(currentValue));
    }
  }, [context?.provenance?.source_event_id, currentValue, isFactoryNetSource, scenarioProperty, target?.id]);
  const lineageMissing = [
    !context?.dataset_version_id && !context?.provenance?.dataset_version_id && "数据集版本",
    !context?.provenance?.source_kind && "来源类型",
    !context?.provenance?.source_id && "来源标识",
    !context?.provenance?.source_mode && "接入模式",
    !context?.provenance?.source_adapter_id && "TemporalSourceAdapter",
    !context?.provenance?.source_adapter_version && "适配器版本",
    !context?.provenance?.source_mapping_digest && "来源映射摘要",
    !context?.provenance?.replay_id && "回放编号",
    !context?.provenance?.source_event_id && "来源事件",
    context?.provenance?.source_event_ordinal == null && "来源事件序号",
    context?.provenance?.source_sequence == null && "来源顺序号",
    !context?.provenance?.source_payload_hash && "来源事件摘要",
    !context?.provenance?.time_kind && "时间类型",
    context?.provenance?.time_kind === "event_time" && !context?.provenance?.time_zone && "事件时区",
    !context?.provenance?.source_snapshot_id && "来源快照",
    !context?.provenance?.source_snapshot_hash && "快照摘要",
    !context?.provenance?.schema_revision_id && "Schema 修订",
    !context?.provenance?.schema_revision_hash && "Schema 修订哈希",
    !context?.provenance?.rule_set_digest && "规则集摘要",
  ].filter((item): item is string => Boolean(item));
  const relationOptions = useMemo(() => (context?.edges || []).filter((edge) => edge.source && edge.target).slice(0, 80), [context]);
  const searchEvidence = async () => {
    const query = evidenceQuery.trim();
    if (!query) {
      setEvidenceError("请输入证据检索词");
      return;
    }
    setEvidenceLoading(true);
    setEvidenceError("");
    try {
      const result = await apiClientV2.get<Context | PendingVectorIndex>(`/ontologies/${ontologyId}/what-if/context`, { params: { target_instance_id: targetId, episode_id: params.get("episode_id") || undefined, at: params.get("at") || undefined, replay_id: params.get("replay_id") || undefined, mode: params.get("mode") || "cumulative", evidence_query: query } });
      if ((result as PendingVectorIndex).status === "indexing") {
        const pending = result as PendingVectorIndex;
        if (pending.job.status === "failed") {
          setEvidenceIndexJob(null);
          setEvidenceError(pending.job.error_message || "后台向量索引构建失败，请稍后重试");
          return;
        }
        setEvidenceIndexJob(pending.job);
        setEvidenceError("");
        return;
      }
      const contextResult = result as Context;
      setEvidenceIndexJob(null);
      setContext(contextResult);
      setEvidenceQuery(contextResult.vector_evidence?.query || query);
    } catch (reason: any) {
      setEvidenceError(String(reason?.detail?.message || reason?.message || "证据检索失败"));
    } finally {
      setEvidenceLoading(false);
    }
  };
  const temporaryRule = {
    id: "factorynet-phase-tool-state",
    conditions: [
      { kind: "relationship", predicate: "IN_PHASE", source: "?observation", target: "?phase" },
      { kind: "relationship", predicate: "HAS_TOOL_CONDITION", source: "?observation", target: "?condition" },
    ],
    effect: { kind: "relationship", predicate: "PHASE_TOOL_STATE", source: "?phase", target: "?condition" },
  };
  const runScenario = async () => {
    if (!context || !target) return;
    if (evidenceIndexJob) {
      setError("向量索引仍在后台构建，完成后请重新检索再运行推演");
      return;
    }
    setError("");
    try {
      const assumptions: Array<Record<string, any>> = [];
      if (scenarioProperty) {
        let nextValue: any = scenarioValue;
        if (numericScenarioProperty) {
          nextValue = Number(scenarioValue);
          if (!Number.isFinite(nextValue)) {
            setError("情景值必须是有限数值");
            return;
          }
          if (["integer", "int"].includes(String(scenarioPropertyType || "").toLowerCase()) && !Number.isInteger(nextValue)) {
            setError("情景值必须是整数");
            return;
          }
        } else if (booleanScenarioProperty) {
          nextValue = scenarioValue === "true";
        }
        assumptions.push({ kind: "set_property", instance_id: target.id, property: scenarioProperty, value: nextValue });
      }
      if (relationMode === "add") {
        if (!relationSource || !relationTarget || !relationType.trim()) {
          setError("请补充要新增的关系起点、终点和名称");
          return;
        }
        assumptions.push({ kind: "add_relation", source: relationSource, target: relationTarget, type: relationType.trim() });
      }
      if (relationMode === "remove") {
        const selectedRelation = relationOptions.find((edge) => `${edge.source}|${edge.type || "RELATED"}|${edge.target}` === relationToRemove);
        if (!selectedRelation) {
          setError("请选择要移除的现有关系");
          return;
        }
        assumptions.push({ kind: "remove_relation", source: selectedRelation.source, target: selectedRelation.target, type: selectedRelation.type || "RELATED" });
      }
      const scenario = await apiClientV2.post<any>(`/ontologies/${ontologyId}/what-if/scenarios`, { name: isFactoryNetSource ? "FactoryNet 刀具状态情景" : `${target.entity_type || "时序对象"} 情景推演`, baseline: context, assumptions, rule_overrides: { disabled: disabledRuleIds, temporary: isFactoryNetSource && temporaryRuleEnabled ? [temporaryRule] : [] } });
      setScenarioId(scenario.id);
      const started = await apiClientV2.post<Run>(`/ontologies/${ontologyId}/what-if/scenarios/${scenario.id}/runs`);
      setRun(started); setStep(3);
    } catch (reason: any) { setError(String(reason?.detail?.message || reason?.message || "推演启动失败")); }
  };
  if (!targetId) return <div className="rounded-xl border border-slate-200 bg-white p-8 text-center"><h2 className="text-sm font-semibold text-slate-900">先选择一个基线对象</h2><p className="mx-auto mt-2 max-w-md text-xs leading-5 text-slate-500">情景推演固定在选定时序对象及其可见邻域。FactoryNet 仍需选择 Observation；其他 TemporalReplay 来源可选择其映射对象。</p><button onClick={() => navigate(`/ontologies/${ontologyId}?tab=data_model`)} className="mt-5 rounded-md bg-slate-800 px-4 py-2 text-xs text-white">返回数据模型</button></div>;
  if (loading) return <div className="flex min-h-[420px] items-center justify-center text-sm text-slate-500"><Loader2 size={17} className="mr-2 animate-spin" />正在准备推演基线</div>;
  if (error && !context) return <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm text-red-700"><AlertTriangle size={16} className="mr-2 inline" />{error}</div>;
  if (!context || !target) return <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">请先在时序数据模型中选择一个对象</div>;
  return <div className="space-y-4" data-testid="what-if-tab">
    <section className="rounded-xl border border-slate-200 bg-white p-4"><div className="flex flex-wrap items-center gap-2">{["选择基线", "设置假设", "运行推演", "查看差异"].map((label, index) => <div key={label} className={`flex items-center gap-2 text-xs ${step === index + 1 ? "font-semibold text-slate-900" : step > index + 1 ? "text-emerald-700" : "text-slate-400"}`}><span className={`grid h-6 w-6 place-items-center rounded-full border ${step > index + 1 ? "border-emerald-600 bg-emerald-50" : step === index + 1 ? "border-slate-800 bg-slate-800 text-white" : "border-slate-200"}`}>{step > index + 1 ? <CheckCircle2 size={13} /> : index + 1}</span>{label}{index < 3 && <span className="mx-1 text-slate-300">/</span>}</div>)}</div></section>
    {step === 1 && <section className="rounded-xl border border-slate-200 bg-white p-5"><h2 className="text-sm font-semibold text-slate-900">基线对象</h2><div className="mt-3 grid gap-3 md:grid-cols-4 text-xs"><div><span className="text-slate-500">{target.entity_type || "对象"}</span><p className="mt-1 break-all font-mono text-slate-800">{target.id}</p></div><div><span className="text-slate-500">episode_id</span><p className="mt-1 font-mono text-slate-800">{context.episode_id || target.properties?.episode_id || "—"}</p></div><div><span className="text-slate-500">{context.provenance?.time_kind === "event_time" ? "事件时间" : "Ordinal"}</span><p className="mt-1 break-all font-mono text-slate-800">{context.provenance?.time_kind === "event_time" ? context.provenance?.time_value ?? context.at ?? "—" : context.at ?? target.event_seq ?? "—"}</p></div><div><span className="text-slate-500">邻域</span><p className="mt-1 text-slate-800">{context.nodes.length} 个节点 / {context.edges.length} 条关系</p></div></div><div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><strong className="text-slate-700">固定基线血缘</strong><span className="font-mono text-[10px] text-slate-500">SHA-256 {context.provenance?.context_digest?.slice(0, 16) || "未生成"}…</span></div><div className="mt-2 grid gap-2 sm:grid-cols-3 lg:grid-cols-4"><div><span className="text-slate-500">本体修订</span><p className="mt-1 break-all font-mono text-slate-700">{context.base_revision_id || "—"}</p></div><div><span className="text-slate-500">数据集版本</span><p className="mt-1 break-all font-mono text-slate-700">{context.dataset_version_id || context.provenance?.dataset_version_id || "未绑定"}</p></div><div><span className="text-slate-500">来源类型</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_kind || "未绑定"}</p></div><div><span className="text-slate-500">回放编号</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.replay_id || "未绑定"}</p></div><div><span className="text-slate-500">来源适配器 / 版本 / 映射摘要</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_adapter_id || "未绑定"} v{context.provenance?.source_adapter_version || "—"} / {context.provenance?.source_mapping_digest?.slice(0, 16) || "未绑定"}…</p></div><div><span className="text-slate-500">来源事件 / 序号</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_event_id || "未绑定"} / {context.provenance?.source_event_ordinal ?? "未绑定"}</p></div><div><span className="text-slate-500">来源快照 / SHA-256</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_snapshot_id || "未绑定"} / {context.provenance?.source_snapshot_hash?.slice(0, 16) || "未绑定"}…</p></div><div><span className="text-slate-500">来源标识 / 接入模式</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_id || "未绑定"} / {context.provenance?.source_mode || "未绑定"}</p></div><div><span className="text-slate-500">时间类型 / 时区</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.time_kind || "—"} / {context.provenance?.time_zone || "未记录"}</p></div><div><span className="text-slate-500">来源行 / 顺序</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_row_id || "—"} / {context.provenance?.source_sequence ?? "—"}</p></div><div><span className="text-slate-500">事件载荷摘要</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.source_payload_hash?.slice(0, 16) || "未绑定"}…</p></div><div><span className="text-slate-500">事件时间 / 水位</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.time_value ?? "—"} / {context.provenance?.watermark_ordinal ?? "—"} ({context.provenance?.watermark_sequence ?? "—"})</p></div><div><span className="text-slate-500">Schema 修订 / SHA-256</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.schema_revision_id || "未绑定"} / {context.provenance?.schema_revision_hash?.slice(0, 16) || "未绑定"}…</p></div><div><span className="text-slate-500">规则集 SHA-256</span><p className="mt-1 break-all font-mono text-slate-700">{context.provenance?.rule_set_digest?.slice(0, 16) || "未绑定"}…</p></div></div>{lineageMissing.length > 0 && <p className="mt-2 text-[11px] leading-5 text-amber-800">血缘信息不完整，缺少：{lineageMissing.join("、")}。SHA-256 摘要可用于发现快照内容变化，但不能补足缺失的来源标识。</p>}</div><button onClick={() => setStep(2)} className="mt-5 rounded-md bg-slate-800 px-4 py-2 text-xs text-white">确认基线</button></section>}
    {step === 1 && <section className="rounded-xl border border-slate-200 bg-white p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-sm font-semibold text-slate-900">同上下文向量证据</h2><p className="mt-1 max-w-3xl text-xs leading-5 text-slate-500">只作候选证据，不作为事实权威或规则输入。检索固定到当前回放、as-of 时间、数据集/快照、本体修订和权限摘要；返回前由 PostgreSQL 复核有效区间，并按对象、属性和关系权限裁剪。</p></div><span className="rounded-full border border-slate-200 px-2 py-1 text-[10px] text-slate-500">最多同步索引 25,000 条事实；更大的回放由后台分批构建</span></div><div className="mt-3 flex flex-wrap gap-2"><input aria-label="时序证据检索词" maxLength={1000} className="min-w-[240px] flex-1 rounded-md border border-slate-300 px-3 py-2 text-xs" value={evidenceQuery} onChange={(event) => setEvidenceQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void searchEvidence(); }} placeholder="输入与当前对象/时间相关的证据描述" /><button type="button" disabled={evidenceLoading || Boolean(evidenceIndexJob)} onClick={() => void searchEvidence()} className="inline-flex items-center gap-1 rounded-md bg-slate-800 px-3 py-2 text-xs text-white disabled:opacity-50">{evidenceLoading && <Loader2 size={13} className="animate-spin" />}{evidenceIndexJob ? "索引中" : evidenceLoading ? "检索中" : context.vector_evidence ? "重新检索" : "检索证据"}</button></div>{evidenceIndexJob && <p role="status" className="mt-2 text-[11px] text-blue-700">后台索引：{evidenceIndexJob.status === "queued" ? "排队中" : "构建中"}，已处理 {evidenceIndexJob.indexed_facts.toLocaleString()} / {evidenceIndexJob.total_facts.toLocaleString()} 条事实。</p>}{context.vector_evidence && evidenceQuery.trim() !== context.vector_evidence.query && <p role="status" className="mt-2 text-[11px] text-amber-700">输入已修改，当前结果仍对应上次检索并固定在基线。重新检索以更新。</p>}{!context.vector_evidence && evidenceQuery.trim() && !evidenceIndexJob && <p role="status" className="mt-2 text-[11px] text-slate-500">当前输入还未检索，未进入推演基线。</p>}{evidenceError && <p role="alert" className="mt-2 text-xs text-red-700"><AlertTriangle size={13} className="mr-1 inline" />{evidenceError}</p>}{context.vector_evidence && <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3"><div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-slate-500"><span>索引修订 <code className="break-all">{context.vector_evidence.index_revision}</code></span><span>查询 SHA-256 {context.vector_evidence.query_digest.slice(0, 16)}… · {context.vector_evidence.result_count} 条授权证据</span></div><p className="mt-1 text-[10px] text-slate-500">固定时间 {context.vector_evidence.time_kind === "event_time" ? context.vector_evidence.time_value : context.vector_evidence.as_of_ordinal} · Schema {context.vector_evidence.schema_revision_hash.slice(0, 12)}… · ACL {context.vector_evidence.permission_digest.slice(0, 12)}… · 结果已进入基线摘要</p>{context.vector_evidence.results.length > 0 ? <div className="mt-3 space-y-2">{context.vector_evidence.results.map((item) => { const value = item.kind === "relation" ? `${item.predicate} · ${item.subject_id} → ${item.object_id}` : `${item.predicate} = ${typeof item.value === "string" ? item.value : JSON.stringify(item.value)}`; return <div key={item.fact_id} className="rounded-md border border-slate-200 bg-white px-3 py-2 text-[11px]"><div className="flex flex-wrap items-start justify-between gap-2"><p className="break-all text-slate-700">{value}</p><span className="shrink-0 text-slate-400">相似度 {item.score.toFixed(3)}</span></div><p className="mt-1 break-all text-[10px] text-slate-400">事实 {item.fact_id} · 事件 {item.source_event_id || "—"} · EvidenceRef {item.evidence_ref_id || "—"} · 有效区间 {item.valid_from_ordinal} → {item.valid_to_ordinal ?? "当前"}</p></div>; })}</div> : <p className="mt-3 text-xs text-slate-400">当前时间和权限范围内没有可展示的匹配事实。</p>}</div>}</section>}
    {step === 2 && <section className="rounded-xl border border-slate-200 bg-white p-5"><h2 className="text-sm font-semibold text-slate-900">设置假设</h2><p className="mt-1 text-xs text-slate-500">修改只作为本次推演的模拟输入，不代表现场状态；正式本体和业务数据不会被改写。</p><div className="mt-4 grid max-w-xl gap-3 md:grid-cols-2">
      {allowedTargetProperties.length > 0 ? <label className="text-xs text-slate-600">模拟属性<select className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-xs" value={scenarioProperty} onChange={(event) => setScenarioPropertySelection(event.target.value)}>{allowedTargetProperties.map((property) => <option key={property} value={property}>{property}</option>)}</select></label> : <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">当前对象没有可用于情景覆盖的已授权属性；仍可基于现有本体规则进行推演。</div>}
      <label className="text-xs text-slate-600">当前值<input className="mt-1 w-full rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs" value={String(currentValue ?? "未设置")} readOnly /></label>
      <label className="text-xs text-slate-600">情景值{booleanScenarioProperty ? <select className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-xs" value={scenarioValue} onChange={(event) => setScenarioValue(event.target.value)}><option value="true">true</option><option value="false">false</option></select> : <input className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-xs" type={numericScenarioProperty ? "number" : "text"} step={String(scenarioPropertyType || "").toLowerCase().includes("int") ? 1 : "any"} value={scenarioValue} onChange={(event) => setScenarioValue(event.target.value)} />}</label>
    </div>{context.nodes.length > 1 && <div className="mt-5 rounded-lg border border-slate-200 bg-slate-50 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs font-medium text-slate-700">实例关系（可选）</span><select aria-label="关系假设类型" className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[11px]" value={relationMode} onChange={(event) => setRelationMode(event.target.value as RelationMode)}><option value="none">不调整关系</option><option value="add">新增关系</option><option value="remove">移除关系</option></select></div>{relationMode === "add" && <div className="mt-3 grid gap-2 md:grid-cols-3"><select aria-label="新增关系起点" className="rounded-md border border-slate-300 bg-white px-2 py-2 text-[11px]" value={relationSource} onChange={(event) => setRelationSource(event.target.value)}><option value="">起点实例</option>{context.nodes.map((node) => <option key={node.id} value={node.id}>{nodeText(node)}</option>)}</select><select aria-label="新增关系终点" className="rounded-md border border-slate-300 bg-white px-2 py-2 text-[11px]" value={relationTarget} onChange={(event) => setRelationTarget(event.target.value)}><option value="">终点实例</option>{context.nodes.map((node) => <option key={node.id} value={node.id}>{nodeText(node)}</option>)}</select><input aria-label="新增关系名称" className="rounded-md border border-slate-300 bg-white px-2 py-2 text-[11px]" value={relationType} onChange={(event) => setRelationType(event.target.value)} placeholder="关系名称" /></div>}{relationMode === "remove" && <select aria-label="选择要移除的关系" className="mt-3 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-[11px]" value={relationToRemove} onChange={(event) => setRelationToRemove(event.target.value)}><option value="">选择现有关系</option>{relationOptions.map((edge) => { const key = `${edge.source}|${edge.type || "RELATED"}|${edge.target}`; return <option key={key} value={key}>{String(edge.type || "RELATED")} · {nodeText(context.nodes.find((node) => node.id === edge.source) || { id: edge.source })} → {nodeText(context.nodes.find((node) => node.id === edge.target) || { id: edge.target })}</option>; })}</select>}</div>}{isFactoryNetSource && <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3"><label className="flex items-center gap-2 text-xs text-slate-700"><input type="checkbox" checked={temporaryRuleEnabled} onChange={(event) => setTemporaryRuleEnabled(event.target.checked)} />启用情景规则：IN_PHASE 且 HAS_TOOL_CONDITION → PHASE_TOOL_STATE</label></div>}{(context.rules || []).length > 0 && <div className="mt-3 border-t border-slate-100 pt-3"><p className="text-[11px] text-slate-500">停用当前本体规则（仅本情景）</p><div className="mt-2 flex flex-wrap gap-3">{(context.rules || []).map((rule: any) => <label key={rule.id} className="flex items-center gap-1.5 text-[11px] text-slate-600"><input type="checkbox" checked={disabledRuleIds.includes(String(rule.id))} onChange={(event) => setDisabledRuleIds((current) => event.target.checked ? [...new Set([...current, String(rule.id)])] : current.filter((id) => id !== String(rule.id)))} />{rule.name_cn || rule.name || rule.id}</label>)}</div></div>}<div className="mt-5 flex gap-2"><button onClick={() => setStep(1)} className="rounded-md border border-slate-300 px-4 py-2 text-xs">返回</button><button onClick={() => setStep(3)} className="rounded-md bg-slate-800 px-4 py-2 text-xs text-white">确认假设</button></div></section>}
    {step >= 3 && <section className="rounded-xl border border-slate-200 bg-white p-5"><div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="text-sm font-semibold text-slate-900">运行推演</h2><p className="mt-1 text-xs text-slate-500">基线和情景固定在修订 {context.base_revision_id || "—"}；运行前复核快照摘要，结果不会写回正式本体。</p><p className="mt-1 break-all font-mono text-[10px] text-slate-400">SHA-256 {context.provenance?.context_digest || "legacy context"}</p></div>{!run && <button disabled={Boolean(evidenceIndexJob)} onClick={() => void runScenario()} className="inline-flex items-center gap-1 rounded-md bg-slate-800 px-4 py-2 text-xs text-white disabled:opacity-50"><Play size={13} />{evidenceIndexJob ? "等待索引完成" : "开始推演"}</button>}</div>{run && <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-4"><div className="flex items-center gap-2 text-xs text-slate-700">{["queued", "running"].includes(run.status) && <Loader2 size={15} className="animate-spin" />}{run.status === "completed" && <CheckCircle2 size={15} className="text-emerald-700" />}{run.status === "failed" && <AlertTriangle size={15} className="text-red-600" />}<strong>{run.status === "queued" ? "排队中" : run.status === "running" ? (stageNames[run.stage] || "处理中") : run.status === "completed" ? "推演完成" : "推演失败"}</strong><span className="ml-auto tabular-nums">{run.progress}%</span></div><div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-200"><div className="h-full rounded-full bg-slate-700 transition-all" style={{ width: `${run.progress}%` }} /></div>{run.error && <p className="mt-2 text-xs text-red-700">{run.error}</p>}</div>}{run?.status === "completed" && <button onClick={() => setStep(4)} className="mt-4 inline-flex items-center gap-1 rounded-md border border-slate-300 px-4 py-2 text-xs"><RotateCcw size={13} />查看差异</button>}</section>}
    {step === 4 && run?.diff && <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]"><div className="rounded-xl border border-slate-200 bg-white p-5"><div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold text-slate-900">本体关系差异</h2><div className="flex items-center gap-2"><span className="text-[11px] text-slate-500">新增 {run.diff.added?.length || 0} · 消失 {run.diff.removed?.length || 0} · 未变化 {run.diff.unchanged?.length || 0}</span><button onClick={() => { setRun(null); setScenarioId(""); setStep(2); }} className="rounded-md border border-slate-300 px-2.5 py-1.5 text-[11px] text-slate-700 hover:border-slate-600">再次配置</button></div></div><div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-900"><strong>情景模拟 · 未审批</strong> 来源事实来自所选固定基线，规则推断可追溯到规则和前提；情景假设只改变本次计算。当前结果不会写回正式数据，也没有批准业务操作。</div><ScenarioGraph context={context} diff={run.diff} /><div className="mt-4 space-y-2">{(run.diff.added || []).map((fact) => { const derived = scenarioDerivedFacts.has(fact); return <div key={`a-${fact}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800"><span className="break-all font-mono">+ {fact}</span><span className="shrink-0 rounded bg-white/80 px-2 py-1 text-[10px]">{derived ? "规则推断" : "情景假设"}</span></div>; })}{(run.diff.removed || []).map((fact) => <div key={`r-${fact}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"><span className="break-all font-mono">− {fact}</span><span className="shrink-0 rounded bg-white/80 px-2 py-1 text-[10px]">{baselineDerivedFacts.has(fact) ? "基线推断因假设变化而消失" : "情景假设移除"}</span></div>)}{(run.diff.unchanged || []).slice(0, 24).map((fact) => <div key={`u-${fact}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 px-3 py-2 text-xs text-slate-600"><span className="break-all font-mono">= {fact}</span><span className="shrink-0 rounded bg-slate-50 px-2 py-1 text-[10px]">{baselineDerivedFacts.has(fact) ? "基线推断" : "来源事实"}</span></div>)}</div></div><aside className="rounded-xl border border-slate-200 bg-white p-5"><h2 className="text-sm font-semibold text-slate-900">推导与证据</h2><p className="mt-2 text-xs text-slate-500">引擎：{run.engine || "—"}</p><p className="mt-1 break-all text-[10px] text-slate-400">版本：{run.engine_version || "—"}</p><div className="mt-4 space-y-2">{(run.diff.proofs || []).map((proof: any) => <div key={proof.fact} className="rounded-md border border-slate-200 bg-slate-50 p-2 text-[11px]"><p className="font-mono text-slate-800">{proof.fact}</p><p className="mt-1 text-slate-500">规则 {proof.rule_id}</p><p className="mt-1 text-slate-500">前提：{(proof.premises || []).join("；")}</p>{Array.isArray(proof.evidence) && proof.evidence.length > 0 && <p className="mt-1 break-words text-slate-500">证据：{proof.evidence.slice(0, 3).map((item: any) => String(item.evidence_ref_id || item.source_file || item.source_row || item.source_sample_id || item.source || "来源记录")).join("；")}</p>}</div>)}{!(run.diff.proofs || []).length && <p className="text-xs text-slate-400">本次没有新增可执行规则结论</p>}</div></aside></section>}
  </div>;
}
