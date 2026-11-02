import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClientV2 } from "@/api/client";
import { modelApi } from "@/api/ontologies";
import { useAuthStore } from "@/stores/authStore";

type AgentSession = {
  id: string;
  title: string;
  status: "idle" | "running" | "paused" | "waiting_for_review" | "cancelled" | string;
  version: number;
  access_role?: "owner" | "viewer" | string;
  metadata_revision_id?: string | null;
  permission_digest?: string | null;
  pending_case_id?: string | null;
  agent_definition_id?: string | null;
  agent_definition_version_id?: string | null;
  agent_definition_digest?: string | null;
};
type AgentDefinitionVersion = { id: string; version: number; snapshot_digest: string; published_at: string | null };
type AgentDefinition = {
  id: string;
  name: string;
  description: string;
  status: "draft" | "published" | "withdrawn" | string;
  draft_revision: number;
  published_version: number | null;
  draft_config?: { mode?: AgentMode; tool_names?: string[]; model_config_id?: string | null };
  versions: AgentDefinitionVersion[];
  can_manage?: boolean;
};
type AgentDefinitionList = { definitions: AgentDefinition[] };

type ContextItem = {
  id: string;
  kind: string;
  ref_id: string | null;
  source_version: string | null;
  temporal?: { replay_id?: string; dataset_version_id?: string | null; schema_revision_id?: string | null; event_ordinal?: number | null; event_time?: string | null; evidence_ref_id?: string; object_ref_id?: string } | null;
  state: "current" | "changed" | "unavailable" | string;
};

type AgentEvent = {
  id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  trace_id: string;
  created_at: string | null;
};

type SessionDetail = AgentSession & { context_items: ContextItem[] };
type SessionList = { sessions: AgentSession[] };
type EventList = { events: AgentEvent[] };
type Participant = {
  user_id: string;
  email: string;
  username: string;
  role: "viewer" | string;
  active: boolean;
  granted_by: string | null;
  created_at: string | null;
  revoked_at: string | null;
};
type ParticipantList = { participants: Participant[] };
type ContextKind = "object" | "evidence" | "scenario" | "what_if" | "query" | "temporal_event";
type AgentMode = "deterministic" | "local";
type LocalModelProbe = Awaited<ReturnType<typeof modelApi.localProbe>>;
type SourceRef = { kind: string; ref_id: string; source_version: string | null; key?: string };
type ActionParameter = { name: string; type?: string; required?: boolean; object_type?: string; choices?: string[] };
type ActionDefinition = {
  id: string;
  name: string;
  description?: string | null;
  target_entity_type?: string | null;
  status: string;
  enabled: boolean;
  parameters: ActionParameter[];
  effects?: { action?: string; op?: string }[];
  side_effects?: unknown[] | null;
  backed_by_function?: string | null;
};
type TargetState = { object_type: string; etag: string; properties: Record<string, unknown> };
type PreviewDiff = { op: string; property: string; before: unknown; after: unknown };
type ActionPreview = { preview_digest: string; preview: { diff: PreviewDiff[]; target: { object_id: string }; reversible: boolean } };
type DecisionCaseRecord = {
  id: string;
  status: string;
  title: string;
  action_type_id: string;
  target: { object_id: string; object_type: string };
  created_by: string;
  reviewed_by?: string | null;
  review_comment?: string | null;
  reason: string;
  context?: Record<string, unknown>;
  preview_digest?: string | null;
  reviewed_preview?: ActionPreview["preview"] | null;
  action_run_id?: string | null;
  projection_status?: string | null;
};

type TemporalActionContext = {
  ref_id?: string;
  source_version?: string;
  temporal?: {
    replay_id?: string;
    dataset_version_id?: string | null;
    schema_revision_id?: string | null;
    event_id?: string;
    event_ordinal?: number | null;
    evidence_ref_id?: string;
    evidence_content_hash?: string;
    object_ref_id?: string;
  };
};

function temporalActionRefs(context: Record<string, unknown> | undefined): TemporalActionContext[] {
  const refs = context?.temporal_events;
  if (!Array.isArray(refs)) return [];
  return refs.filter((item): item is TemporalActionContext => Boolean(item && typeof item === "object"));
}

const panel = "rounded-xl border border-[#E0CFCF] bg-white p-4 shadow-sm";
const input = "w-full rounded-lg border border-[#D5D5D5] bg-white px-3 py-2 text-sm text-[#1A1A1A] outline-none focus:ring-2 focus:ring-[#C41230]/20";
const primary = "inline-flex items-center justify-center rounded-lg bg-[#C41230] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50";
const secondary = "inline-flex items-center justify-center rounded-lg border border-[#D5D5D5] bg-white px-3 py-2 text-sm text-[#1A1A1A] hover:bg-[#F5F0F0] disabled:cursor-not-allowed disabled:opacity-50";

function errorText(error: unknown): string {
  if (typeof error === "object" && error !== null) {
    const value = error as { detail?: unknown; message?: string };
    if (typeof value.detail === "string") return value.detail;
    if (typeof value.detail === "object" && value.detail !== null && "message" in value.detail) {
      const message = (value.detail as { message?: unknown }).message;
      if (typeof message === "string") return message;
    }
    if (typeof value.message === "string") return value.message;
  }
  return "请求失败，请刷新会话状态后重试。";
}

function statusLabel(status: string): string {
  return ({ idle: "可继续", queued: "排队中", running: "处理中", cancelling: "取消中", paused: "已暂停", waiting_for_review: "等待审核", cancelled: "已取消" } as Record<string, string>)[status] || status;
}

function caseStatusLabel(status: string): string {
  return ({ draft: "草稿，待预览", changes_requested: "需要修改", ready_for_review: "待审核", approved: "已批准，待执行", rejected: "已驳回", completed: "已执行", failed: "执行失败", reverted: "已撤回" } as Record<string, string>)[status] || status;
}

function displayValue(value: unknown): string {
  if (value === undefined) return "未设置";
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function supportsReviewedWrite(action: ActionDefinition): boolean {
  const externalEffects = (action.side_effects || []).some((effect) =>
    !effect || typeof effect !== "object" || (effect as { type?: string }).type !== "review_note");
  return action.enabled && action.status === "published" && !action.backed_by_function
    && !externalEffects && Array.isArray(action.effects) && action.effects.length > 0
    && action.effects.every((effect) => ["set_property", "unset_property"].includes(effect.action || effect.op || ""));
}

function buildActionParameters(action: ActionDefinition, drafts: Record<string, string>, target: {
  object_id: string; object_type: string;
}, reason: string): Record<string, unknown> {
  const parameters: Record<string, unknown> = {};
  const targetParameter = action.parameters?.find((parameter) => parameter.type === "object"
    && (!parameter.object_type || parameter.object_type === target.object_type));
  for (const parameter of action.parameters || []) {
    if (parameter === targetParameter) {
      parameters[parameter.name] = { object_type: target.object_type, object_id: target.object_id };
      continue;
    }
    const raw = parameter.name === "reason" && !drafts[parameter.name]?.trim()
      ? reason.trim() : drafts[parameter.name] ?? "";
    if (!raw.trim()) {
      if (parameter.required) throw new Error(`请填写必填参数：${parameter.name}`);
      continue;
    }
    const kind = (parameter.type || "string").toLowerCase();
    if (kind === "boolean") {
      if (raw !== "true" && raw !== "false") throw new Error(`参数 ${parameter.name} 必须选择 true 或 false`);
      parameters[parameter.name] = raw === "true";
    } else if (["number", "integer", "decimal"].includes(kind)) {
      const number = Number(raw);
      if (!Number.isFinite(number) || (kind === "integer" && !Number.isInteger(number))) {
        throw new Error(`参数 ${parameter.name} 不是有效的${kind === "integer" ? "整数" : "数字"}`);
      }
      parameters[parameter.name] = number;
    } else if (["object", "struct", "array"].includes(kind)) {
      let value: unknown;
      try { value = JSON.parse(raw); } catch { throw new Error(`参数 ${parameter.name} 需要有效 JSON`); }
      if ((kind === "array" && !Array.isArray(value))
          || (kind !== "array" && (value === null || typeof value !== "object" || Array.isArray(value)))) {
        throw new Error(`参数 ${parameter.name} 的 JSON 类型与 Action 定义不符`);
      }
      parameters[parameter.name] = value;
    } else {
      parameters[parameter.name] = raw;
    }
  }
  if (action.parameters?.some((parameter) => parameter.name === "reason" && parameter.type === "string")
      && parameters.reason === undefined) parameters.reason = reason.trim();
  return parameters;
}

function eventContent(event: AgentEvent): { title: string; body: string } {
  const p = event.payload || {};
  if (event.event_type === "user_input") return { title: "你", body: String(p.task || "") };
  if (event.event_type === "answer") {
    const lineage = p.model_lineage && typeof p.model_lineage === "object"
      ? p.model_lineage as Record<string, unknown> : null;
    const artifact = lineage?.artifact_status === "verified" ? "模型制品摘要已核验" : "模型制品摘要未能核验";
    const mode = p.mode === "local"
      ? `本地模型草案 · ${String(p.model_name || "Ollama")}${lineage ? ` · ${artifact}` : ""}`
      : "确定性整理";
    const uncertainty = typeof p.uncertainty === "string" && p.uncertainty ? `\n不确定性：${p.uncertainty}` : "";
    return { title: `Agent · ${mode}`, body: `${String(p.answer || "")}${uncertainty}` };
  }
  if (event.event_type === "tool_call") return { title: "工具调用", body: `调用 ${String(p.tool || "工具")} · 输入摘要 ${String(p.input_digest || "—")}` };
  if (event.event_type === "tool_result") return { title: "工具结果", body: `读取 ${String(p.object_count ?? p.count ?? 0)} 个对象，引用 ${String(p.source_count ?? 0)} 条` };
  if (event.event_type === "turn_started") return { title: "运行已持久化", body: `本轮开始处理 · ${String(p.mode || "deterministic")}` };
  if (event.event_type === "turn_queued") {
    const lineage = p.model_lineage && typeof p.model_lineage === "object"
      ? p.model_lineage as Record<string, unknown> : null;
    const model = lineage ? ` · ${String(lineage.model_name || "本地模型")} · 制品摘要${lineage.artifact_status === "verified" ? "已固定" : "未能核验"}` : "";
    const registry = typeof p.tool_registry_digest === "string"
      ? ` · 工具目录摘要 ${p.tool_registry_digest.slice(0, 12)}` : " · 旧版工具版本记录";
    return { title: "任务已持久化", body: `本轮已进入后台队列；页面可刷新，服务会继续追踪运行状态${model}${registry}。` };
  }
  if (event.event_type === "cancel_requested") return { title: "已请求取消", body: "取消状态已保存；worker 确认停止或执行租约过期后，会话才会标记为已取消。" };
  if (event.event_type === "cancelled") return { title: "运行已取消", body: "后台已确认本轮停止，未保存模型回答。" };
  if (event.event_type === "turn_recovery_scheduled") return { title: "原请求已恢复排队", body: "检测到运行超时；系统已核对上下文、权限与工具版本，并将原请求重新入队。Agent 推理不直接写入业务数据。" };
  if (event.event_type === "turn_dispatch_deferred") return { title: "等待运行队列", body: "任务已保存在数据库，当前 worker 队列不可用；系统会定期重新投递。" };
  if (event.event_type === "turn_interrupted") return { title: "运行已中断", body: "原请求未能安全恢复；会话已解除运行状态，可刷新上下文后重新发送。" };
  if (event.event_type === "error") return { title: "Agent 运行未完成", body: `${String(p.code || "error")} · ${String(p.message || "请检查本地模型状态后重试。")}` };
  if (event.event_type === "approval_wait") return { title: "等待人工审核", body: `DecisionCase ${String(p.case_id || "")} · ${String(p.case_status || "待处理")}` };
  if (event.event_type === "session_created") return { title: "会话已创建", body: String(p.title || "") };
  if (event.event_type === "context_added") return { title: "已加入上下文", body: `${String(p.kind || "引用")} · ${String(p.source_version || "版本未知")}` };
  if (event.event_type === "context_removed") return { title: "已移除上下文", body: String(p.kind || "引用") };
  if (event.event_type === "context_refreshed") return { title: "上下文已刷新", body: `本体修订 ${String(p.metadata_revision_id || "未知")}` };
  return { title: event.event_type, body: "" };
}

function eventReferences(event: AgentEvent): SourceRef[] {
  const refs = event.payload?.source_refs;
  if (!Array.isArray(refs)) return [];
  return refs.filter((item): item is SourceRef => Boolean(
    item && typeof item === "object"
    && typeof (item as SourceRef).kind === "string"
    && typeof (item as SourceRef).ref_id === "string",
  ));
}

export default function AgentSessionsPanel({ ontologyId, handoff, onHandoffConsumed }: {
  ontologyId: string;
  handoff?: { title: string; message: string; candidateObjectIds: string[]; contextKind?: ContextKind; contextRef?: string };
  onHandoffConsumed?: () => void;
}) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState("");
  const [title, setTitle] = useState(handoff?.title || "");
  const [selectedDefinitionVersionId, setSelectedDefinitionVersionId] = useState("");
  const [definitionName, setDefinitionName] = useState("");
  const [definitionDescription, setDefinitionDescription] = useState("");
  const [definitionMode, setDefinitionMode] = useState<AgentMode>("deterministic");
  const [definitionToolNames, setDefinitionToolNames] = useState<string[]>(["read_object", "read_context_ref", "propose_action"]);
  const [editingDefinitionId, setEditingDefinitionId] = useState("");
  const [participantEmail, setParticipantEmail] = useState("");
  const [contextKind, setContextKind] = useState<ContextKind>(handoff?.contextKind || "object");
  const [contextRef, setContextRef] = useState(handoff?.contextRef || handoff?.candidateObjectIds[0] || "");
  const [task, setTask] = useState(handoff?.message || "");
  const [handoffPending, setHandoffPending] = useState(Boolean(handoff));
  const [mode, setMode] = useState<AgentMode>("deterministic");
  const [busy, setBusy] = useState(false);
  const [turnPending, setTurnPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [proposalTargetId, setProposalTargetId] = useState("");
  const [proposalActionTypeId, setProposalActionTypeId] = useState("");
  const [proposalTitle, setProposalTitle] = useState("");
  const [proposalReason, setProposalReason] = useState("");
  const [proposalParameterDraft, setProposalParameterDraft] = useState<Record<string, string>>({});
  const [proposalEvidenceRefs, setProposalEvidenceRefs] = useState<string[]>([]);
  const [proposalRequestId, setProposalRequestId] = useState("");
  const [focusedCaseId, setFocusedCaseId] = useState("");
  const [showCasePreview, setShowCasePreview] = useState(false);
  const currentUser = useAuthStore((state) => state.user);

  const sessionsQuery = useQuery({
    queryKey: ["agent-sessions", ontologyId],
    queryFn: () => apiClientV2.get<SessionList>(`/ontologies/${ontologyId}/agent/sessions`),
    refetchInterval: (query) => query.state.data?.sessions.some((item) => ["queued", "running", "cancelling"].includes(item.status)) ? 2000 : false,
  });
  const definitionsQuery = useQuery({
    queryKey: ["agent-definitions", ontologyId],
    queryFn: () => apiClientV2.get<AgentDefinitionList>(`/ontologies/${ontologyId}/agent/definitions`),
  });
  const localModelQuery = useQuery<LocalModelProbe>({
    queryKey: ["local-model-probe"],
    queryFn: () => modelApi.localProbe(),
    refetchInterval: 15000,
  });
  const detailQuery = useQuery({
    queryKey: ["agent-session", ontologyId, selectedId],
    enabled: Boolean(selectedId),
    queryFn: async () => {
      const session = await apiClientV2.get<SessionDetail>(`/ontologies/${ontologyId}/agent/sessions/${selectedId}`);
      try {
        const eventList = await apiClientV2.get<EventList>(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/events?limit=200`);
        return { session, events: eventList.events || [], eventsError: "" };
      } catch (cause) {
        return { session, events: [], eventsError: errorText(cause) };
      }
    },
    refetchInterval: (query) => turnPending || ["queued", "running", "cancelling"].includes(query.state.data?.session.status || "") ? 2000 : false,
  });

  const sessions = sessionsQuery.data?.sessions || [];
  const definitions = definitionsQuery.data?.definitions || [];
  const canManageDefinitions = ["admin", "editor"].includes(currentUser?.role || "");
  const detail = detailQuery.data?.session;
  const canManageSession = detail?.id === selectedId && detail?.access_role === "owner";
  const events = detailQuery.data?.events || [];
  const eventsError = detailQuery.data?.eventsError || "";
  const canProposeAction = canManageSession && ["admin", "editor"].includes(currentUser?.role || "");
  const objectContextItems = (detail?.context_items || []).filter((item) =>
    item.kind === "object" && item.state === "current" && Boolean(item.ref_id));
  const evidenceContextItems = (detail?.context_items || []).filter((item) =>
    item.kind === "evidence" && item.state === "current" && Boolean(item.ref_id));
  const participantsQuery = useQuery({
    queryKey: ["agent-session-participants", ontologyId, selectedId],
    enabled: Boolean(selectedId && canManageSession),
    queryFn: () => apiClientV2.get<ParticipantList>(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/participants`),
  });
  const actionTypesQuery = useQuery<ActionDefinition[]>({
    queryKey: ["agent-action-types", ontologyId],
    enabled: Boolean(canProposeAction),
    queryFn: () => apiClientV2.get<ActionDefinition[]>(`/ontologies/${ontologyId}/actions`),
    staleTime: 30_000,
  });
  const targetStateQuery = useQuery<TargetState>({
    queryKey: ["agent-action-target", ontologyId, proposalTargetId],
    enabled: Boolean(canProposeAction && proposalTargetId),
    queryFn: () => apiClientV2.get<TargetState>(
      `/ontologies/${ontologyId}/decision-cases/target-state/${encodeURIComponent(proposalTargetId)}`),
  });
  const latestApprovalEvent = [...events].reverse().find((event) => event.event_type === "approval_wait");
  const eventCaseId = typeof latestApprovalEvent?.payload.case_id === "string"
    ? latestApprovalEvent.payload.case_id : "";
  const activeCaseId = detail?.pending_case_id || focusedCaseId || eventCaseId;
  const decisionCaseQuery = useQuery<DecisionCaseRecord>({
    queryKey: ["agent-decision-case", ontologyId, activeCaseId],
    enabled: Boolean(activeCaseId),
    queryFn: () => apiClientV2.get<DecisionCaseRecord>(
      `/ontologies/${ontologyId}/decision-cases/${activeCaseId}`),
  });
  const decisionCase = decisionCaseQuery.data;
  const decisionTemporalRefs = temporalActionRefs(decisionCase?.context);
  const previewQuery = useQuery<ActionPreview>({
    queryKey: ["agent-decision-preview", ontologyId, activeCaseId, decisionCase?.status],
    enabled: Boolean(activeCaseId && decisionCase && ["draft", "changes_requested"].includes(decisionCase.status)),
    queryFn: () => apiClientV2.post<ActionPreview>(
      `/ontologies/${ontologyId}/decision-cases/${activeCaseId}:preview`, {}),
    retry: false,
  });
  const compatibleActionTypes = (actionTypesQuery.data || []).filter((action) =>
    supportsReviewedWrite(action) && (!action.target_entity_type
      || action.target_entity_type === targetStateQuery.data?.object_type));
  const selectedAction = compatibleActionTypes.find((action) => action.id === proposalActionTypeId);
  const reviewedPreview = decisionCase && !["draft", "changes_requested"].includes(decisionCase.status)
    && decisionCase.preview_digest && decisionCase.reviewed_preview
    ? { preview_digest: decisionCase.preview_digest, preview: decisionCase.reviewed_preview }
    : previewQuery.data;

  useEffect(() => {
    if (!selectedId && sessions.length) setSelectedId(sessions[0].id);
    if (selectedId && !sessions.some((item) => item.id === selectedId)) setSelectedId("");
  }, [sessions, selectedId]);

  useEffect(() => {
    setFocusedCaseId("");
    setProposalActionTypeId("");
    setProposalParameterDraft({});
    setProposalEvidenceRefs([]);
    setProposalRequestId("");
    setShowCasePreview(false);
  }, [selectedId]);

  useEffect(() => {
    const objectRefs = (detail?.context_items || []).filter((item) =>
      item.kind === "object" && item.state === "current" && Boolean(item.ref_id));
    if (objectRefs.some((item) => item.ref_id === proposalTargetId)) return;
    setProposalTargetId(objectRefs[0]?.ref_id || "");
    setProposalActionTypeId("");
    setProposalParameterDraft({});
    setProposalEvidenceRefs([]);
    setProposalRequestId("");
  }, [detail?.context_items, proposalTargetId]);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["agent-sessions", ontologyId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-definitions", ontologyId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session", ontologyId, selectedId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-session-participants", ontologyId, selectedId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-decision-case", ontologyId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-decision-preview", ontologyId] }),
    ]);
  };

  const execute = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      await invalidate();
    } catch (cause) {
      setError(errorText(cause));
      await invalidate().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  const proposeAction = () => execute(async () => {
    if (!detail || !canProposeAction || detail.status !== "idle" || !selectedAction
        || !targetStateQuery.data || !proposalReason.trim()) return;
    const clientRequestId = proposalRequestId || (typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
    setProposalRequestId(clientRequestId);
    const parameters = buildActionParameters(selectedAction, proposalParameterDraft, {
      object_id: proposalTargetId,
      object_type: targetStateQuery.data.object_type,
    }, proposalReason);
    const result = await apiClientV2.post<{ case_id: string; case_status: string }>(
      `/ontologies/${ontologyId}/agent/sessions/${selectedId}:propose-action`, {
        expected_version: detail.version,
        action_type_id: selectedAction.id,
        target_object_id: proposalTargetId,
        parameters,
        evidence_refs: proposalEvidenceRefs,
        title: proposalTitle.trim() || selectedAction.name,
        reason: proposalReason.trim(),
        client_request_id: clientRequestId,
      });
    setFocusedCaseId(result.case_id);
    setShowCasePreview(true);
    setProposalRequestId("");
    setNotice(`DecisionCase ${result.case_id.slice(0, 8)} 已建立。Agent 不会直接写入对象；请先检查预览并提交审核。`);
  });

  const submitDecisionCase = () => execute(async () => {
    if (!decisionCase || !reviewedPreview) return;
    await apiClientV2.post(`/ontologies/${ontologyId}/decision-cases/${decisionCase.id}:submit`, {
      preview_digest: reviewedPreview.preview_digest,
    });
    setShowCasePreview(true);
    setNotice("预览摘要已提交审核；正式对象尚未修改。等待另一位审核人处理。");
  });

  const reviewDecisionCase = (decision: "approved" | "rejected" | "changes_requested") => execute(async () => {
    if (!decisionCase) return;
    await apiClientV2.post(`/ontologies/${ontologyId}/decision-cases/${decisionCase.id}:review`, {
      decision,
      comment: decision === "approved" ? "已核对 Agent 提议、对象状态和预览差异" : "请补充或修正提议与证据",
    });
    setShowCasePreview(true);
    setNotice(decision === "approved" ? "审核已批准；对象尚未修改，下一步需由授权执行人确认写回。"
      : decision === "rejected" ? "审核已驳回；正式对象未修改。" : "已退回修改；正式对象未修改。");
  });

  const executeDecisionCase = () => execute(async () => {
    if (!decisionCase) return;
    const result = await apiClientV2.post<{ case: DecisionCaseRecord; run: { id: string; status: string } }>(
      `/ontologies/${ontologyId}/decision-cases/${decisionCase.id}:execute`, {});
    setShowCasePreview(true);
    setNotice(`审核写回已完成 · ActionRun ${result.run.id} · ${result.run.status}。返回会话后检查审批状态以恢复 Agent。`);
  });

  const updateProposalParameter = (name: string, value: string) => {
    setProposalParameterDraft((current) => ({ ...current, [name]: value }));
    setProposalRequestId("");
  };

  const editDefinition = (definition: AgentDefinition) => {
    setEditingDefinitionId(definition.id);
    setDefinitionName(definition.name);
    setDefinitionDescription(definition.description || "");
    setDefinitionMode(definition.draft_config?.mode || "deterministic");
    setDefinitionToolNames(definition.draft_config?.tool_names || ["read_object", "read_context_ref", "propose_action"]);
  };

  const resetDefinitionDraft = () => {
    setEditingDefinitionId("");
    setDefinitionName("");
    setDefinitionDescription("");
    setDefinitionMode("deterministic");
    setDefinitionToolNames(["read_object", "read_context_ref", "propose_action"]);
  };

  const saveDefinitionDraft = () => execute(async () => {
    if (!definitionName.trim()) return;
    const body = {
      name: definitionName.trim(),
      description: definitionDescription.trim(),
      mode: definitionMode,
      tool_names: definitionToolNames,
    };
    if (editingDefinitionId) {
      const current = definitions.find((item) => item.id === editingDefinitionId);
      if (!current) throw new Error("Agent Definition 已不在列表中，请刷新后重试。");
      await apiClientV2.put(`/ontologies/${ontologyId}/agent/definitions/${editingDefinitionId}`, {
        ...body,
        expected_revision: current.draft_revision,
      });
      setNotice("Agent Definition 草稿已保存；已发布版本仍按原摘要运行。");
    } else {
      const created = await apiClientV2.post<AgentDefinition>(`/ontologies/${ontologyId}/agent/definitions`, body);
      setEditingDefinitionId(created.id);
      setNotice("Agent Definition 草稿已创建。检查工具和运行模式后，可以发布版本供新会话固定。");
    }
  });

  const publishDefinition = (definition: AgentDefinition) => execute(async () => {
    await apiClientV2.post(`/ontologies/${ontologyId}/agent/definitions/${definition.id}:publish`, {
      expected_revision: definition.draft_revision,
    });
    setNotice(`Agent Definition「${definition.name}」已发布新版本；运行将固定该版本摘要。`);
  });

  const changeDefinitionLifecycle = (definition: AgentDefinition, action: "withdraw" | "reactivate") => execute(async () => {
    await apiClientV2.post(`/ontologies/${ontologyId}/agent/definitions/${definition.id}:${action}`, {});
    setNotice(action === "withdraw" ? `Agent Definition「${definition.name}」已撤回，固定它的会话将停止新运行。`
      : `Agent Definition「${definition.name}」已恢复发布。`);
  });

  const createSession = () => execute(async () => {
    const created = await apiClientV2.post<AgentSession>(`/ontologies/${ontologyId}/agent/sessions`, {
      title: title.trim() || `工作会话 ${new Date().toLocaleString()}`,
      agent_definition_version_id: selectedDefinitionVersionId || null,
    });
    await sessionsQuery.refetch();
    setSelectedId(created.id);
    setTitle("");
    setSelectedDefinitionVersionId("");
    if (handoffPending) {
      setHandoffPending(false);
      onHandoffConsumed?.();
      setNotice(handoff?.contextKind === "temporal_event"
        ? "已创建专属持久会话。时序事件 ID 已预填；请先加入并核验固定版本、对象权限与证据，再手动发送草稿问题。"
        : "已创建专属持久会话。旧对象 ID 仅作为候选；请先加入并核验固定上下文，再检查草稿消息后手动发送。");
    } else {
      setNotice("会话已保存；刷新或重新进入后可继续查看事件与上下文。");
    }
  });

  const addContext = () => execute(async () => {
    if (!detail || !contextRef.trim()) return;
    await apiClientV2.post(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/context`, {
      expected_version: detail.version,
      kind: contextKind,
      ref_id: contextRef.trim(),
    });
    setContextRef("");
    setNotice("引用已加入。运行前会重新检查对象、版本和权限。");
  });

  const removeContext = (item: ContextItem) => execute(async () => {
    if (!detail) return;
    await apiClientV2.delete(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/context/${item.id}?expected_version=${detail.version}`);
    setNotice("引用已从当前会话上下文移除；事件记录仍保留。");
  });

  const refreshContext = () => execute(async () => {
    if (!detail) return;
    await apiClientV2.post(`/ontologies/${ontologyId}/agent/sessions/${selectedId}:refresh-context`, {
      expected_version: detail.version,
    });
    setNotice("会话已绑定当前本体修订和权限摘要。");
  });

  const sendTurn = () => execute(async () => {
    if (!detail || !task.trim() || handoffPending) return;
    const clientRequestId = typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const pendingTurn = apiClientV2.post<{ status: string; replayed?: boolean; dispatch_pending?: boolean }>(`/ontologies/${ontologyId}/agent/sessions/${selectedId}:turn`, {
      expected_version: detail.version,
      task: task.trim(),
      client_request_id: clientRequestId,
      mode,
    });
    setTurnPending(true);
    try {
      await queryClient.invalidateQueries({ queryKey: ["agent-session", ontologyId, selectedId] }).catch(() => undefined);
      const result = await pendingTurn;
      if (result.dispatch_pending) {
        setNotice("本轮和上下文已持久化；worker 队列暂不可用，系统会定期重新投递。");
      } else if (result.status === "queued") {
        setNotice(mode === "local"
          ? "本轮已持久化并进入后台队列，由本地 Ollama 处理；写入仍须人工审核。"
          : "本轮已持久化并进入后台队列；写入仍须人工审核。");
      } else {
        setNotice(result.replayed
          ? `请求已存在，当前状态：${statusLabel(result.status)}。`
          : `本轮状态：${statusLabel(result.status)}。`);
      }
    } finally {
      setTurnPending(false);
    }
    setTask("");
  });

  const changeStatus = (action: "pause" | "unpause" | "cancel") => execute(async () => {
    if (!detail || !canManageSession) return;
    const result = await apiClientV2.post<{ status?: string }>(`/ontologies/${ontologyId}/agent/sessions/${selectedId}:${action}`, {
      expected_version: detail.version,
    });
    setNotice(action === "pause" ? "会话已暂停。" : action === "cancel"
      ? result.status === "cancelling" ? "取消请求已保存；正在等待当前 worker 确认停止。" : "会话已取消。"
      : "会话已恢复。" );
  });

  const grantParticipant = () => execute(async () => {
    if (!detail || !canManageSession || !participantEmail.trim()) return;
    await apiClientV2.post(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/participants`, {
      email: participantEmail.trim(),
    });
    setParticipantEmail("");
    setNotice("已授予只读访问；受邀者的本体数据权限不会因此扩大。");
  });

  const revokeParticipant = (participant: Participant) => execute(async () => {
    if (!canManageSession) return;
    await apiClientV2.delete(`/ontologies/${ontologyId}/agent/sessions/${selectedId}/participants/${participant.user_id}`);
    setNotice(`已撤销 ${participant.email} 的访问。`);
  });

  const checkApproval = () => execute(async () => {
    if (!detail) return;
    const result = await apiClientV2.post<{ waiting: boolean; queued?: boolean; dispatch_pending?: boolean; running?: boolean; cancelling?: boolean; cancelled?: boolean; interrupted?: boolean; recovered?: boolean; case_status?: string }>(
      `/ontologies/${ontologyId}/agent/sessions/${selectedId}:resume`,
      { expected_version: detail.version },
    );
    setNotice(result.cancelled
      ? "运行租约已过期且取消请求已确认；会话已安全收敛为已取消。"
      : result.cancelling
      ? "取消请求仍在等待 worker 确认；会话会继续刷新状态。"
      : result.recovered
      ? result.dispatch_pending
        ? "检测到过期运行；原请求已安全恢复并持久入队，worker 队列不可用时系统会继续重投。"
        : "检测到过期运行；原请求已核对上下文、权限和工具版本并重新入队。"
      : result.interrupted
      ? "检测到过期运行，但原请求已无法安全恢复；会话已解除运行状态，请刷新上下文后重新发送。"
      : result.queued
        ? result.dispatch_pending
          ? "任务仍在持久队列中；worker 队列不可用，系统会继续重试投递。"
          : "任务仍在队列中，已重新投递并等待 worker。"
      : result.running
        ? "本轮仍在运行中；后台通过持久租约检测 worker，租约过期后会自动恢复或收敛。"
        : result.waiting
          ? `审批尚未结束：${result.case_status || "等待审核"}。`
          : "审批结果已同步，会话已恢复。");
  });

  return (
    <div className="space-y-4">
      <section className={panel}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#816C72]">AGENT SESSION</p>
            <h2 className="mt-1 text-lg font-semibold text-[#1A1A1A]">持久会话</h2>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-[#6B6B6B]">会话、上下文引用、事件和检查点保存在服务端；每次读取会重新验证当前权限。</p>
          </div>
          <span className="rounded-md border border-[#E0CFCF] bg-[#F5F0F0] px-2.5 py-1 text-xs text-[#64131F]">
            {localModelQuery.data?.ready ? "可选本地模型 · 不调用云端模型" : "本地模型未就绪 · 可用确定性整理"}
          </span>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <input aria-label="新会话标题" className={input} maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="新会话标题（可留空自动命名）" />
          <button type="button" className={primary} disabled={busy} onClick={createSession}>新建会话</button>
        </div>
        <div className="mt-3 rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] px-3 py-2 text-xs leading-5 text-[#6B6B6B]">
          Agent 轮次会整理当前授权上下文。Action 提议先生成只读预览，再由人工审核并授权执行；推理过程不会直接写入业务对象。
        </div>
        {handoffPending && <div role="status" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-950">
          {handoff?.contextKind === "temporal_event"
            ? "时序观测和问题已放入草稿，尚未发送。请先创建专属会话，再加入事件引用；服务会核验对象权限、固定数据版本和证据，草稿不会自动提交或写回。"
            : "旧助手的任务和建议已放入草稿，尚未发送。请先创建专属会话；候选对象需要重新加入并通过当前权限校验，草稿不会自动提交或写回。"}
        </div>}
      </section>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[280px_minmax(0,1fr)]">
        <aside className={`${panel} min-w-0`}>
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-[#1A1A1A]">会话列表</h3>
            <button type="button" className={secondary} disabled={sessionsQuery.isFetching} onClick={() => void sessionsQuery.refetch()}>刷新</button>
          </div>
          {sessionsQuery.isPending && <p role="status" className="py-6 text-center text-sm text-[#6B6B6B]">正在加载会话</p>}
          {sessionsQuery.isError && <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{errorText(sessionsQuery.error)}</p>}
          {!sessionsQuery.isPending && !sessionsQuery.isError && sessions.length === 0 && (
            <div className="mt-3 rounded-lg border border-dashed border-[#E0CFCF] bg-[#F9F5F5] p-4 text-sm text-[#6B6B6B]">还没有会话。新建后可固定对象或证据引用，并查看事件历史。</div>
          )}
          <div className="mt-3 space-y-2">
            {sessions.map((session) => (
              <button key={session.id} type="button" onClick={() => setSelectedId(session.id)} aria-current={session.id === selectedId ? "true" : undefined}
                className={`w-full rounded-lg border p-3 text-left transition ${session.id === selectedId ? "border-[#8B1A2B] bg-[#F5F0F0]" : "border-[#E0CFCF] bg-white hover:bg-[#F9F5F5]"}`}>
                <span className="block truncate text-sm font-medium text-[#1A1A1A]">{session.title}</span>
                <span className="mt-1 flex items-center justify-between gap-2 text-xs text-[#6B6B6B]"><span>{session.access_role === "viewer" ? "共享只读" : statusLabel(session.status)}</span><span>v{session.version}</span></span>
              </button>
            ))}
          </div>
        </aside>

        <section className={`${panel} min-w-0`}>
          {!selectedId && <div className="rounded-lg border border-dashed border-[#E0CFCF] bg-[#F9F5F5] p-8 text-center text-sm text-[#6B6B6B]">选择或新建一个会话以继续。</div>}
          {selectedId && detailQuery.isPending && <p role="status" className="py-8 text-center text-sm text-[#6B6B6B]">正在恢复会话</p>}
          {selectedId && detailQuery.isError && <div role="alert" className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900"><p>{errorText(detailQuery.error)}</p><button type="button" className={`${secondary} mt-3`} onClick={() => void detailQuery.refetch()}>重新读取</button></div>}
          {detail && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[#E0CFCF] pb-3">
                <div className="min-w-0">
                  <h3 className="truncate text-base font-semibold text-[#1A1A1A]">{detail.title}</h3>
                  <p className="mt-1 break-all text-xs text-[#6B6B6B]">状态：{statusLabel(detail.status)} · 本体修订：{detail.metadata_revision_id || "未设置"} · 权限摘要：{detail.permission_digest?.slice(0, 12) || "未设置"}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {canManageSession && <button type="button" className={secondary} disabled={busy || !["idle", "paused"].includes(detail.status)} onClick={refreshContext}>刷新上下文</button>}
                  {canManageSession && detail.status === "idle" && <button type="button" className={secondary} disabled={busy} onClick={() => changeStatus("pause")}>暂停</button>}
                  {canManageSession && detail.status === "paused" && <button type="button" className={secondary} disabled={busy} onClick={() => changeStatus("unpause")}>继续</button>}
                  {canManageSession && detail.status === "waiting_for_review" && <button type="button" className={secondary} disabled={busy} onClick={checkApproval}>检查审批状态</button>}
                  {canManageSession && ["queued", "running", "cancelling"].includes(detail.status) && <button type="button" className={secondary} disabled={busy} onClick={checkApproval}>检查运行 / 恢复中断</button>}
                  {canManageSession && ["idle", "paused", "queued", "running"].includes(detail.status) && <button type="button" className="inline-flex items-center rounded-lg border border-red-200 bg-white px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50" disabled={busy} onClick={() => changeStatus("cancel")}>{detail.status === "running" ? "请求取消运行" : "取消会话"}</button>}
                </div>
              </div>

              {!canManageSession && <div className="rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-900">你以只读参与者身份查看此会话。上下文、运行和会话控制仍由所有者管理；若权限范围发生变化，读取会自动暂停。</div>}

              {canManageSession && <section className="rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h4 className="text-sm font-semibold text-[#1A1A1A]">共享参与者</h4>
                    <p className="mt-1 text-xs leading-5 text-[#6B6B6B]">仅允许当前数据权限与所有者一致的账户；共享者只读，撤权立即生效。</p>
                  </div>
                </div>
                <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                  <input aria-label="参与者账户邮箱" className={input} type="email" maxLength={200} value={participantEmail} onChange={(event) => setParticipantEmail(event.target.value)} placeholder="输入已有账户邮箱" />
                  <button type="button" className={secondary} disabled={busy || !participantEmail.trim()} onClick={grantParticipant}>授予只读访问</button>
                </div>
                {participantsQuery.isError && <p role="alert" className="mt-2 text-xs text-red-700">{errorText(participantsQuery.error)}</p>}
                <div className="mt-2 space-y-2">
                  {(participantsQuery.data?.participants || []).map((participant) => <div key={participant.user_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[#E0CFCF] bg-white px-3 py-2 text-xs">
                    <div className="min-w-0"><span className="font-medium text-[#1A1A1A]">{participant.username}</span><span className="ml-2 break-all text-[#6B6B6B]">{participant.email}</span><span className="ml-2 text-[#816C72]">{participant.active ? "只读中" : "已撤权"}</span></div>
                    {participant.active && <button type="button" className="rounded-md px-2 py-1 text-red-700 hover:bg-red-50 disabled:opacity-50" disabled={busy} onClick={() => revokeParticipant(participant)}>撤销</button>}
                  </div>)}
                  {!participantsQuery.isPending && !participantsQuery.isError && !participantsQuery.data?.participants.length && <p className="text-xs text-[#6B6B6B]">目前没有参与者。</p>}
                </div>
              </section>}

              {activeCaseId && <section className="rounded-lg border border-[#D8BFC3] bg-white p-4 shadow-sm">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#816C72]">REVIEWED ACTION</p>
                    <h4 className="mt-1 text-sm font-semibold text-[#1A1A1A]">Action 提议与审核</h4>
                    {decisionCase && <p className="mt-1 break-all text-xs text-[#6B6B6B]">{decisionCase.title} · {caseStatusLabel(decisionCase.status)}</p>}
                  </div>
                  {decisionCase && <button type="button" className={secondary} onClick={() => setShowCasePreview((value) => !value)}>
                    {showCasePreview ? "收起详情" : "查看预览与处理"}
                  </button>}
                </div>
                {decisionCaseQuery.isPending && <p role="status" className="mt-3 text-sm text-[#6B6B6B]">正在读取 DecisionCase…</p>}
                {decisionCaseQuery.isError && <div role="alert" className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">DecisionCase 暂不可见：{errorText(decisionCaseQuery.error)}。请确认你仍有此事项的访问权限。</div>}
                {decisionCase && showCasePreview && <div className="mt-3 space-y-3 border-t border-[#E0CFCF] pt-3">
                  <div className="grid gap-2 text-xs sm:grid-cols-2">
                    <p className="break-all"><span className="text-[#6B6B6B]">目标对象：</span>{decisionCase.target.object_type} · {decisionCase.target.object_id}</p>
                    <p className="break-all"><span className="text-[#6B6B6B]">Action：</span>{decisionCase.action_type_id}</p>
                    <p className="sm:col-span-2"><span className="text-[#6B6B6B]">业务理由：</span>{decisionCase.reason}</p>
                  </div>
                  {decisionTemporalRefs.length > 0 && <div className="rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] p-3 text-xs">
                    <strong className="text-[#64131F]">固定时序依据 · {decisionTemporalRefs.length} 条</strong>
                    <div className="mt-2 space-y-2">{decisionTemporalRefs.map((item, index) => <div key={`${item.temporal?.event_id || item.ref_id || index}`} className="break-all rounded-md border border-[#E0CFCF] bg-white p-2">
                      <p>回放 {item.temporal?.replay_id || "—"} · ordinal {item.temporal?.event_ordinal ?? "—"}</p>
                      <p className="mt-1 text-[#6B6B6B]">事件 {item.temporal?.event_id || item.ref_id || "—"} · 对象 {item.temporal?.object_ref_id || "—"}</p>
                      <p className="mt-1 text-[#6B6B6B]">数据版本 {item.temporal?.dataset_version_id || "—"} · Schema {item.temporal?.schema_revision_id || "—"}</p>
                      <p className="mt-1 text-[#6B6B6B]">证据 {item.temporal?.evidence_ref_id || "—"} · SHA-256 {item.temporal?.evidence_content_hash?.slice(0, 16) || "—"}</p>
                    </div>)}</div>
                  </div>}
                  {decisionCase.review_comment && <p className="rounded-md bg-[#F9F5F5] p-2 text-xs text-[#64131F]">审核意见：{decisionCase.review_comment}</p>}
                  {previewQuery.isPending && <p role="status" className="text-xs text-[#6B6B6B]">正在计算只读预览…</p>}
                  {previewQuery.isError && <p role="alert" className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">预览暂不可用：{errorText(previewQuery.error)}。对象状态或权限可能已变化，请重新检查事项。</p>}
                  {reviewedPreview && <div className="rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <strong className="text-xs text-[#64131F]">写回预览 · {reviewedPreview.preview.diff.length} 项变化</strong>
                      <span className="break-all font-mono text-[10px] text-[#6B6B6B]">摘要 {reviewedPreview.preview_digest.slice(0, 16)}…</span>
                    </div>
                    {reviewedPreview.preview.diff.length > 0 ? <div className="mt-2 space-y-2">
                      {reviewedPreview.preview.diff.map((item, index) => <div key={`${item.property}:${index}`} className="grid gap-1 rounded-md border border-[#E0CFCF] bg-white p-2 text-xs sm:grid-cols-[minmax(100px,0.55fr)_minmax(0,1.45fr)]">
                        <span className="font-medium text-[#1A1A1A]">{item.property} · {item.op}</span>
                        <span className="break-all text-[#6B6B6B]">{displayValue(item.before)} <span className="px-1 text-[#C41230]">→</span> <strong className="text-[#1A1A1A]">{displayValue(item.after)}</strong></span>
                      </div>)}
                    </div> : <p className="mt-2 text-xs text-[#6B6B6B]">当前预览没有对象属性变化。</p>}
                    <p className="mt-2 text-[11px] text-[#6B6B6B]">{reviewedPreview.preview.reversible ? "该 Action 支持撤销。" : "该 Action 不支持自动撤销。"} 预览只说明拟写入内容，提交/审核阶段不会修改对象。</p>
                  </div>}
                  {decisionCase.status === "draft" || decisionCase.status === "changes_requested" ? <div className="flex flex-wrap items-center gap-2">
                    <button type="button" className={primary} disabled={busy || !reviewedPreview || !["admin", "editor"].includes(currentUser?.role || "")} onClick={submitDecisionCase}>提交审核</button>
                    <span className="text-xs text-[#6B6B6B]">提交后需要管理员审核，通过后才可执行。</span>
                  </div> : null}
                  {decisionCase.status === "ready_for_review" && <div className="space-y-2">
                    {decisionCase.created_by === currentUser?.id
                      ? <p className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">创建人不能审核自己的提议，请由另一位管理员处理。</p>
                      : currentUser?.role === "admin"
                        ? <div className="flex flex-wrap gap-2">
                          <button type="button" className={primary} disabled={busy} onClick={() => reviewDecisionCase("approved")}>批准</button>
                          <button type="button" className={secondary} disabled={busy} onClick={() => reviewDecisionCase("changes_requested")}>退回修改</button>
                          <button type="button" className="rounded-lg border border-red-200 bg-white px-3 py-2 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50" disabled={busy} onClick={() => reviewDecisionCase("rejected")}>驳回</button>
                        </div>
                        : <p className="text-xs text-[#6B6B6B]">等待管理员审核。审核人必须与提议创建人不同。</p>}
                  </div>}
                  {decisionCase.status === "approved" && <div className="flex flex-wrap items-center gap-2">
                    <button type="button" className={primary} disabled={busy || !["admin", "editor"].includes(currentUser?.role || "")} onClick={executeDecisionCase}>执行已批准 Action</button>
                    <span className="text-xs text-[#6B6B6B]">只有执行此步骤才会写回正式对象。</span>
                  </div>}
                  {decisionCase.status === "completed" && <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-900">
                    ActionRun：<span className="break-all font-mono">{decisionCase.action_run_id || "未返回"}</span> · 投影状态：{decisionCase.projection_status || "待同步"}
                    <p className="mt-1">需要恢复 Agent 会话时，使用上方“检查审批状态”同步最终结果。</p>
                  </div>}
                </div>}
              </section>}

              <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(240px,0.8fr)_minmax(0,1.2fr)]">
                <div className="min-w-0 space-y-3">
                  <div className="rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] p-3">
                    <h4 className="text-sm font-semibold text-[#1A1A1A]">固定上下文</h4>
                    <div className="mt-2 grid gap-2 sm:grid-cols-[120px_minmax(0,1fr)]">
                      <select aria-label="引用类型" className={input} disabled={!canManageSession} value={contextKind} onChange={(event) => setContextKind(event.target.value as ContextKind)}>
                        <option value="object">业务对象</option><option value="evidence">证据</option><option value="temporal_event">时序事件</option><option value="scenario">情景资源</option><option value="what_if">What-if 推演情景</option><option value="query">查询视图</option>
                      </select>
                      <input aria-label="引用 ID" className={input} disabled={!canManageSession} value={contextRef} onChange={(event) => setContextRef(event.target.value)} placeholder={contextKind === "temporal_event" ? "粘贴已提交的事件 ID" : contextKind === "what_if" ? "粘贴 What-if 情景 ID" : "粘贴对象或资源 ID"} />
                    </div>
                    <button type="button" className={`${primary} mt-2 w-full`} disabled={!canManageSession || busy || !detail || !contextRef.trim() || !["idle", "paused"].includes(detail.status)} onClick={addContext}>加入引用</button>
                  </div>
                  <div className="space-y-2">
                    {(detail.context_items || []).map((item) => (
                      <div key={item.id} className="flex min-w-0 items-start justify-between gap-2 rounded-lg border border-[#E0CFCF] bg-white p-3">
                        <div className="min-w-0">
                          <p className="text-xs font-medium text-[#1A1A1A]">{({ object: "业务对象", evidence: "证据", temporal_event: "时序事件", scenario: "情景资源", what_if: "What-if 推演情景", query: "查询视图" } as Record<string, string>)[item.kind] || item.kind} · {item.state === "current" ? "版本有效" : item.state === "changed" ? "版本已变化" : "当前不可用"}</p>
                          <p className="mt-1 break-all font-mono text-[11px] text-[#6B6B6B]">{item.ref_id || "权限变化或资源不可用"}</p>
                          <p className="mt-1 break-all text-[11px] text-[#816C72]">版本 {item.source_version || "—"}</p>
                          {item.kind === "temporal_event" && item.temporal && <p className="mt-1 break-all text-[11px] text-[#816C72]">回放 {item.temporal.replay_id || "—"} · 时间 {item.temporal.event_ordinal ?? item.temporal.event_time ?? "—"} · 证据 {item.temporal.evidence_ref_id || "—"}</p>}
                        </div>
                        {canManageSession && <button type="button" className="shrink-0 rounded-md px-2 py-1 text-xs text-[#64131F] hover:bg-[#F5F0F0] disabled:opacity-50" disabled={busy || !["idle", "paused"].includes(detail.status)} onClick={() => removeContext(item)}>移除</button>}
                      </div>
                    ))}
                    {!detail.context_items?.length && <p className="rounded-lg border border-dashed border-[#E0CFCF] p-3 text-sm text-[#6B6B6B]">尚未固定对象或证据。</p>}
                  </div>

                  {canManageSession && <section className="rounded-lg border border-[#D8BFC3] bg-white p-3">
                    <div>
                      <h4 className="text-sm font-semibold text-[#1A1A1A]">Agent Action 提议</h4>
                      <p className="mt-1 text-xs leading-5 text-[#6B6B6B]">只可对当前会话固定的对象提议已发布的本地属性 Action。外部副作用 Action 不在此入口开放。</p>
                    </div>
                    {!canProposeAction && <p className="mt-3 rounded-md bg-[#F9F5F5] p-2 text-xs text-[#6B6B6B]">当前账户角色不能发起写入提议。</p>}
                    {canProposeAction && <div className="mt-3 space-y-3">
                      {objectContextItems.length === 0
                        ? <p className="rounded-md border border-dashed border-[#E0CFCF] p-3 text-xs text-[#6B6B6B]">先在上方固定一个当前有效的业务对象。</p>
                        : <>
                          <label className="block text-xs font-medium text-[#6B6B6B]">目标对象
                            <select aria-label="Action 目标对象" className={`${input} mt-1`} value={proposalTargetId} onChange={(event) => {
                              setProposalTargetId(event.target.value); setProposalActionTypeId(""); setProposalParameterDraft({}); setProposalRequestId("");
                            }}>
                              {objectContextItems.map((item) => <option key={item.id} value={item.ref_id || ""}>{item.ref_id}</option>)}
                            </select>
                          </label>
                          {targetStateQuery.isPending && <p role="status" className="text-xs text-[#6B6B6B]">正在读取对象类型和当前状态…</p>}
                          {targetStateQuery.isError && <p role="alert" className="text-xs text-red-700">对象当前状态读取失败：{errorText(targetStateQuery.error)}</p>}
                          {targetStateQuery.data && <>
                            <label className="block text-xs font-medium text-[#6B6B6B]">可审核 Action
                              <select aria-label="Agent Action 类型" className={`${input} mt-1`} value={proposalActionTypeId} onChange={(event) => {
                                setProposalActionTypeId(event.target.value); setProposalParameterDraft({}); setProposalRequestId("");
                              }}>
                                <option value="">选择 Action</option>
                                {compatibleActionTypes.map((action) => <option key={action.id} value={action.id}>{action.name}</option>)}
                              </select>
                            </label>
                            {actionTypesQuery.isError && <p role="alert" className="text-xs text-red-700">Action 列表读取失败：{errorText(actionTypesQuery.error)}</p>}
                            {!actionTypesQuery.isPending && !actionTypesQuery.isError && !compatibleActionTypes.length && <p className="rounded-md bg-amber-50 p-2 text-xs text-amber-900">该对象类型目前没有可通过审核执行的本地属性 Action。</p>}
                            {selectedAction && <div className="space-y-2 rounded-md bg-[#F9F5F5] p-2">
                              {selectedAction.parameters.filter((parameter) => !(parameter.type === "object"
                                && (!parameter.object_type || parameter.object_type === targetStateQuery.data?.object_type)))
                                .filter((parameter) => parameter.name !== "reason")
                                .map((parameter) => <label key={parameter.name} className="block text-xs font-medium text-[#6B6B6B]">
                                  {parameter.name}{parameter.required ? " · 必填" : " · 可选"}
                                  {parameter.choices?.length ? <select className={`${input} mt-1`} required={parameter.required} value={proposalParameterDraft[parameter.name] || ""} onChange={(event) => updateProposalParameter(parameter.name, event.target.value)}>
                                    <option value="">选择值</option>{parameter.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                                  </select> : parameter.type === "boolean" ? <select className={`${input} mt-1`} required={parameter.required} value={proposalParameterDraft[parameter.name] || ""} onChange={(event) => updateProposalParameter(parameter.name, event.target.value)}>
                                    <option value="">选择值</option><option value="true">是</option><option value="false">否</option>
                                  </select> : ["object", "struct", "array"].includes((parameter.type || "").toLowerCase())
                                    ? <textarea className={`${input} mt-1 font-mono`} rows={3} required={parameter.required} value={proposalParameterDraft[parameter.name] || ""} onChange={(event) => updateProposalParameter(parameter.name, event.target.value)} placeholder={parameter.type === "array" ? "JSON 数组" : "JSON 对象"} />
                                    : <input className={`${input} mt-1`} type={["number", "integer", "decimal"].includes((parameter.type || "").toLowerCase()) ? "number" : "text"} step={parameter.type === "integer" ? "1" : "any"} required={parameter.required} value={proposalParameterDraft[parameter.name] || ""} onChange={(event) => updateProposalParameter(parameter.name, event.target.value)} />}
                                </label>)}
                            </div>}
                          </>}
                          <input aria-label="提议标题" className={input} maxLength={200} value={proposalTitle} onChange={(event) => { setProposalTitle(event.target.value); setProposalRequestId(""); }} placeholder="提议标题（可留空使用 Action 名称）" />
                          <label className="block text-xs font-medium text-[#6B6B6B]">业务理由
                            <textarea aria-label="Action 业务理由" className={`${input} mt-1 resize-y`} rows={3} maxLength={4000} value={proposalReason} onChange={(event) => { setProposalReason(event.target.value); setProposalRequestId(""); }} placeholder="说明为什么需要执行该 Action" />
                          </label>
                          {evidenceContextItems.length > 0 && <fieldset className="space-y-1">
                            <legend className="text-xs font-medium text-[#6B6B6B]">关联当前证据（可选）</legend>
                            {evidenceContextItems.map((item) => <label key={item.id} className="flex min-w-0 items-start gap-2 rounded-md border border-[#E0CFCF] bg-[#F9F5F5] p-2 text-[11px] text-[#1A1A1A]">
                              <input type="checkbox" checked={proposalEvidenceRefs.includes(item.ref_id || "")} onChange={(event) => {
                                const ref = item.ref_id || "";
                                setProposalEvidenceRefs((current) => event.target.checked ? [...current, ref] : current.filter((value) => value !== ref));
                                setProposalRequestId("");
                              }} />
                              <span className="break-all">{item.ref_id}</span>
                            </label>)}
                          </fieldset>}
                          <button type="button" className={`${primary} w-full`} disabled={!canProposeAction || busy || detail.status !== "idle" || !proposalTargetId || !selectedAction || !targetStateQuery.data || targetStateQuery.isFetching || !proposalReason.trim()} onClick={proposeAction}>
                            {busy ? "正在建立审核事项…" : "生成预览并建立审核事项"}
                          </button>
                          {detail.status !== "idle" && <p className="text-[11px] text-[#816C72]">会话状态为“{statusLabel(detail.status)}”，需恢复到可继续后才能发起提议。</p>}
                        </>}
                    </div>}
                  </section>}
                </div>

                <div className="min-w-0">
                  <div className="max-h-[420px] space-y-3 overflow-y-auto rounded-lg border border-[#E0CFCF] bg-[#F9F5F5] p-3" aria-live="polite">
                    {detailQuery.isFetching && <p className="text-xs text-[#6B6B6B]">正在同步持久事件…</p>}
                    {eventsError && <p role="alert" className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">事件暂不可见：{eventsError}。若权限刚发生变化，请刷新上下文。</p>}
                    {events.map((event) => {
                      const content = eventContent(event);
                      const citations = eventReferences(event);
                      const modelLineage = event.payload?.model_lineage && typeof event.payload.model_lineage === "object"
                        ? event.payload.model_lineage as Record<string, unknown> : null;
                      const isUser = event.event_type === "user_input";
                      return <article key={event.id} className={`max-w-[92%] rounded-lg border p-3 text-sm ${isUser ? "ml-auto border-[#E0CFCF] bg-white" : "border-[#E0CFCF] bg-white"}`}>
                        <div className="flex items-center justify-between gap-3"><strong className="text-xs text-[#64131F]">{content.title}</strong><span className="text-[10px] text-[#816C72]">#{event.sequence}</span></div>
                        {content.body && <p className="mt-1 whitespace-pre-wrap break-words leading-6 text-[#1A1A1A]">{content.body}</p>}
                        {citations.length > 0 && <div className="mt-2 rounded-md bg-[#F9F5F5] p-2 text-[10px] leading-5 text-[#6B6B6B]">
                          <strong className="mr-1 text-[#64131F]">服务端校验的引用</strong>
                          {citations.map((item) => <span key={`${item.kind}:${item.ref_id}`} className="mr-2 inline-block break-all">
                            {item.kind}:{item.ref_id} · {item.source_version || "版本未知"}
                          </span>)}
                        </div>}
                        {modelLineage && <details className="mt-2 text-[10px] text-[#6B6B6B]"><summary className="cursor-pointer">模型运行血缘（无凭据）</summary>
                          <p className="mt-1 break-all">ModelConfig ID: {String(modelLineage.model_config_id || "未知")}</p>
                          <p className="mt-1 break-all">配置指纹: {String(modelLineage.model_config_fingerprint || "未知")}</p>
                          <p className="mt-1 break-all">模型制品: {String(modelLineage.model_name || "未知")} · {modelLineage.artifact_status === "verified" ? "摘要已核验" : "摘要未能核验"}</p>
                          {typeof modelLineage.artifact_digest === "string" && <p className="mt-1 break-all">制品摘要: {modelLineage.artifact_digest}</p>}
                        </details>}
                        <details className="mt-2 text-[10px] text-[#6B6B6B]"><summary className="cursor-pointer">事件追踪</summary><p className="mt-1 break-all">Trace: {event.trace_id}</p><p>{event.created_at || "时间未知"}</p></details>
                      </article>;
                    })}
                    {!events.length && <p className="py-8 text-center text-sm text-[#6B6B6B]">会话事件会显示在这里。</p>}
                  </div>
                  <div className="mt-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label className="flex items-center gap-2 text-xs text-[#6B6B6B]">
                        推理模式
                        <select aria-label="Agent 推理模式" disabled={!canManageSession} className={`${input} w-auto min-w-[230px]`} value={mode} onChange={(event) => setMode(event.target.value as AgentMode)}>
                          <option value="deterministic">确定性来源整理（无模型）</option>
                          <option value="local" disabled={!localModelQuery.data?.ready}>本地 Ollama · qwen3.5:0.8b{localModelQuery.data?.ready ? "" : "（未就绪）"}</option>
                        </select>
                      </label>
                      {!localModelQuery.data?.ready && <span className="text-[11px] text-[#816C72]">
                        {localModelQuery.isPending ? "正在检查本地模型…" : localModelQuery.isError ? errorText(localModelQuery.error) : localModelQuery.data?.error || "请在模型与审查页启动本地 Ollama。"}
                      </span>}
                    </div>
                    <textarea aria-label="会话消息" className={`${input} resize-y`} rows={3} maxLength={4000} disabled={!canManageSession || busy || handoffPending || detail.status !== "idle"} value={task} onChange={(event) => setTask(event.target.value)} placeholder={canManageSession ? "基于当前固定上下文提出问题" : "只读参与者不能发起 Agent 轮次"} />
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-xs text-[#6B6B6B]">{task.length}/4000 字符 · 请求会持久化并使用幂等 ID</span>
                      <button type="button" className={primary} disabled={!canManageSession || busy || handoffPending || detail.status !== "idle" || task.trim().length < 1} onClick={sendTurn}>{handoffPending ? "先新建专属会话" : busy ? "处理中…" : "发送"}</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </section>
      </div>

      {error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</p>}
      {notice && <p role="status" className="rounded-lg border border-[#E0CFCF] bg-[#F5F0F0] p-3 text-sm text-[#64131F]">{notice}</p>}
    </div>
  );
}
