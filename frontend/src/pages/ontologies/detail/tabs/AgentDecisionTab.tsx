import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { apiClientV2 } from "@/api/client";
import AgentSessionsPanel from "./AgentSessionsPanel";

type AgentObject = { id: string; label?: string; summary?: string; relevance?: unknown; properties?: Record<string, unknown> };
type Conclusion = { conclusion?: string; human_explanation?: string; relevance?: unknown; limitation?: unknown; rule_id?: string; premises?: unknown; bindings?: unknown };
type Rule = { id: string; explanation?: string; status?: string; technical_rule?: string };
type ReasoningRun = { id?: string; run_id?: string; created_at?: string; status?: string; derived_fact_count?: number | null; authorization_status?: "current" | "stale_or_unpinned" };
type Evaluation = { rule_id: string; name?: string; status?: string; explanation?: string; objects?: Array<{ alternatives?: Array<{ checks?: Array<{ field?: string; missing?: boolean; actual?: unknown; expected?: unknown; passed?: boolean }> }> }> };
type Proposal = { outcome?: string; reasoning?: string; decision_rule_evaluation?: { rule_count: number; matches: unknown[]; evaluated?: Evaluation[] }; evidence?: Conclusion[]; basis?: Conclusion[]; ontology_revision_id?: string };
type ContextDraft = { objects: AgentObject[]; reasoning_run?: ReasoningRun; conclusions: Conclusion[]; rules: Rule[]; notice?: string };
type DraftResponse = { context: ContextDraft; run_id: string };
type ProposalResponse = { proposal: Proposal };
type SessionHandoff = { title: string; message: string; candidateObjectIds: string[]; contextKind?: "temporal_event"; contextRef?: string };

const humanize = (value: string) => value.replace(/^FactoryNet:/, "").replace(/[:_/-]+/g, " ").replace(/\b(Phase|ProcessPhase)\s+/i, "工艺阶段 ").replace(/\b(ToolCondition)\s+/i, "刀具状态 ").trim();
const conclusionText = (item: unknown) => typeof item === "string" ? item : (item as Conclusion | null)?.conclusion || "";
const readableConclusion = (item: unknown) => {
  const text = conclusionText(item); const match = text.match(/^([A-Z_]+)\((.*)\)$/);
  return match ? `${humanize(match[1])}：${match[2].split(",").map((part: string) => humanize(part.trim())).join(" → ")}` : text;
};

function LegacyAgentDecisionTab({ ontologyId, onUsePersistentSession }: { ontologyId: string; onUsePersistentSession: (handoff: SessionHandoff) => void }) {
  const [task, setTask] = useState("哪些 CNC 设备因为刀具状态异常需要安排人工检查？");
  const [runs, setRuns] = useState<ReasoningRun[]>([]); const [draft, setDraft] = useState<ContextDraft | null>(null);
  const [draftRunId, setDraftRunId] = useState(""); const [selectedObjects, setSelectedObjects] = useState<string[]>([]);
  const [selectedConclusions, setSelectedConclusions] = useState<string[]>([]); const [agentRunId, setAgentRunId] = useState("");
  const [proposal, setProposal] = useState<Proposal | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  useEffect(() => { apiClientV2.get<ReasoningRun[]>(`/ontologies/${ontologyId}/reasoning/runs`).then(items => setRuns(items || [])).catch((e: unknown) => { const error = e as { message?: string }; setError(error.message || "加载推理运行失败"); }); }, [ontologyId]);
  const execute = async (fn: () => Promise<void>) => { setBusy(true); setError(""); setNotice(""); try { await fn(); } catch (e: unknown) { const error = e as { detail?: string; message?: string }; setError(error.detail || error.message || "请求失败"); } finally { setBusy(false); } };
  const selectionSummary = useMemo(() => {
    const objects = (draft?.objects || []).filter(item => selectedObjects.includes(item.id));
    const conclusions = (draft?.conclusions || []).filter(item => selectedConclusions.includes(conclusionText(item)));
    if (!objects.length) return "尚未选择业务对象。";
    const phases = [...new Set(objects.map(item => item.properties?.ctx_process_phase).filter(Boolean))] as string[];
    if (!phases.length) return `已选择 ${objects.length} 个对象和 ${conclusions.length} 条结论。它们通过推理证据相连；是否足以回答问题，还需要核对规则条件。`;
    const explanations = phases.map(phase => phase === "Repositioning" ? "重新定位：调整加工位置的阶段" : /^Layer (\d+) (Up|Down)$/.test(phase) ? phase.replace(/^Layer (\d+) (Up|Down)$/, (_, layer, direction) => `第 ${layer} 层${direction === "Up" ? "向上" : "向下"}加工：区分加工层次和行进方向的步骤名称`) : phase);
    const states = objects.map(item => item.properties?.ctx_tool_condition);
    const condition = states.every(state => state === "unworn") ? "这些记录的刀具状态都标为“未磨损”。它们不能支持“已发现刀具磨损”的判断，但也不能证明设备不存在其他异常，或其他时段同样正常。" : "刀具状态请逐条核对；状态标签本身不等于是否需要检查的结论。";
    return `本次选出的 ${objects.length} 条记录，能帮助核对加工步骤与刀具状态，而不是因为它们已经被判定异常才入选。涉及的步骤是：${explanations.join("；")}。这些是工艺步骤名称，不是故障等级。当前选中 ${conclusions.length} 条规则结论，用于把记录中的状态对应到加工阶段。${condition}`;
  }, [draft, selectedObjects, selectedConclusions]);
  const createDraft = () => execute(async () => { const out = await apiClientV2.post<DraftResponse>(`/ontologies/${ontologyId}/agent/context-drafts`, { task }); const context = out.context; setAgentRunId(out.run_id); setDraft(context); setDraftRunId(context.reasoning_run?.id || ""); setSelectedObjects((context.objects || []).map((item) => item.id)); setSelectedConclusions((context.conclusions || []).map(conclusionText).filter(Boolean)); setProposal(null); setNotice("上下文草案已生成。当前采用关键词与证明匹配，请审核后批准。"); });
  const changeRun = (runId: string) => execute(async () => {
    setProposal(null);
    const context = await apiClientV2.post<ContextDraft>(`/ontologies/${ontologyId}/agent/context-preview`, { object_ids: selectedObjects, reasoning_run_id: runId });
    setDraftRunId(runId); setDraft(context); setSelectedConclusions(context.conclusions.map(conclusionText));
  });
  const approveContext = () => execute(async () => { await apiClientV2.patch(`/ontologies/${ontologyId}/agent/runs/${agentRunId}/context`, { object_ids: selectedObjects, reasoning_run_id: draftRunId, conclusions: selectedConclusions }); const out = await apiClientV2.post<ProposalResponse>(`/ontologies/${ontologyId}/agent/runs/${agentRunId}/generate`, {}); setProposal(out.proposal); setNotice("上下文已批准，Agent 已生成建议。需要继续处理时，请转入持久会话重新核验。"); });
  const continueInPersistentSession = () => {
    const candidateObjectIds = selectedObjects.slice(0, 12);
    const selectedProofs = selectedConclusions.slice(0, 12).map((item) => item.slice(0, 240));
    const transferLines = [
      "请在此持久会话中重新核验以下旧版助手线索。候选内容不代表已获授权、已固定上下文或已批准写入。",
      `原始问题：${task.trim().slice(0, 900) || "未提供"}`,
      candidateObjectIds.length ? `候选对象 ID（需重新固定并校验权限）：${candidateObjectIds.join("、")}` : "旧助手没有选中对象。",
      selectedProofs.length ? `候选推理结论（仅供复核）：${selectedProofs.join("；")}` : "旧助手没有选中推理结论。",
      proposal ? `旧助手建议（仅供复核）：${String(proposal.outcome || "").slice(0, 500)}；理由：${String(proposal.reasoning || "").slice(0, 500)}` : "旧助手尚未生成建议。",
      "请先确认可访问的对象和证据，再提出需要人工审核的 DecisionCase；不得把这些线索直接当作 Action Preview 或写回授权。",
    ];
    const fullMessage = transferLines.join("\n");
    const message = fullMessage.length <= 3800 ? fullMessage : `${fullMessage.slice(0, 3750)}\n[旧助手线索已截断；请返回原页面核对]`;
    onUsePersistentSession({
      title: `延续：${task.trim().slice(0, 140) || "旧版 Agent 任务"}`,
      message,
      candidateObjectIds,
    });
  };
  return <div className="space-y-4">
    <div role="note" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950">
      <p className="max-w-3xl leading-6">兼容入口：此助手仍可生成旧版建议，但不能确认或写入决策。请转入持久会话重新核验上下文；需要业务写回时，必须经 DecisionCase、预览、独立审核和 ActionRun。</p>
      <button type="button" className="shrink-0 rounded-md border border-amber-300 bg-white px-3 py-2 text-sm font-medium hover:bg-amber-100" onClick={continueInPersistentSession}>转入持久会话复核</button>
    </div>
    <section className="rounded border bg-white p-5"><h2 className="text-xl font-semibold">Agent 决策助手</h2><p className="mt-2 text-sm text-slate-600">用自然语言提问，系统先匹配对象、命中规则和推理证据；你批准上下文后，它才生成待复核建议。</p><textarea aria-label="自然语言问题" className="mt-4 w-full rounded border p-3 text-sm" rows={3} disabled={busy} value={task} onChange={(e) => { setTask(e.target.value); setDraft(null); setProposal(null); }} /><button disabled={busy || task.trim().length < 5} className="mt-3 rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50" onClick={createDraft}>分析问题并选择上下文</button></section>
    {draft && <section className="rounded border border-violet-200 bg-violet-50/30 p-5"><h2 className="font-semibold">Agent 上下文草案</h2><p className="mt-1 text-sm text-slate-600">请审核对象 → 命中规则 → 推理运行 → 实际结论。</p><div className="mt-4 space-y-4">
      <div className="rounded border border-violet-200 bg-white p-4 text-sm"><h3 className="font-semibold">这些材料为什么值得看？</h3><p className="mt-2 leading-7 text-slate-700">{selectionSummary}</p></div><div><h3 className="text-sm font-medium">相关业务对象 / 证明引用的观测</h3>{draft.objects.length === 0 && <p className="mt-1 text-sm text-amber-700">未自动找到对象，请重新提问。</p>}<div className="mt-2 grid gap-2 md:grid-cols-2">{draft.objects.map((item) => <label key={item.id} className="rounded border bg-white p-3 text-sm"><input type="checkbox" disabled={busy} className="mr-2" checked={selectedObjects.includes(item.id)} onChange={(e) => { setProposal(null); setSelectedObjects((old) => e.target.checked ? [...old, item.id] : old.filter((id) => id !== item.id)); }} /><strong>{item.label || item.id}</strong><p className="mt-1 text-xs text-slate-700">{item.summary}</p><details className="mt-2 text-xs text-slate-500"><summary>查看技术详情</summary><pre className="mt-1 whitespace-pre-wrap">{JSON.stringify({ matching_reason: item.relevance, properties: item.properties }, null, 2)}</pre></details></label>)}</div></div>
      <div className="rounded border bg-white p-3 text-sm"><h3 className="font-medium">命中规则</h3>{(draft.rules || []).map((rule) => <div key={rule.id} className="mt-2"><p className="leading-7">{rule.explanation}</p><details className="mt-1 text-xs text-slate-500"><summary>查看技术详情</summary><p>{rule.id} · {rule.status}</p><pre className="mt-1 whitespace-pre-wrap">{rule.technical_rule}</pre></details></div>)}</div>
      <div><h3 className="text-sm font-medium">推理运行</h3><select aria-label="上下文推理运行" className="mt-2 w-full rounded border bg-white p-2 text-sm" disabled={busy} value={draftRunId} onChange={(e) => changeRun(e.target.value)}><option value="">请选择推理运行</option>{runs.map((run) => <option key={run.run_id} value={run.run_id} disabled={run.authorization_status !== "current"}>{run.created_at} · {run.status} · {run.authorization_status === "current" ? `${run.derived_fact_count ?? "—"} 条结论` : "权限已变化，需重跑"}</option>)}</select></div>
      <div><h3 className="text-sm font-medium">推理结论</h3><div className="mt-2 space-y-2">{(draft.conclusions || []).map((item) => { const value = conclusionText(item); return <label key={value} className="block rounded border bg-white p-3 text-sm"><input type="checkbox" disabled={busy} className="mr-2" checked={selectedConclusions.includes(value)} onChange={(e) => { setProposal(null); setSelectedConclusions((old) => e.target.checked ? [...old, value] : old.filter((v) => v !== value)); }} /><strong>{item.human_explanation || readableConclusion(item)}</strong><details className="mt-2 ml-6 text-xs text-slate-500"><summary>查看技术详情（供审计使用）</summary><p className="mt-1">系统原始结论：{value}</p><pre className="mt-1 whitespace-pre-wrap">{JSON.stringify({ relevance: item.relevance, limitation: item.limitation, rule_id: item.rule_id, premises: item.premises, bindings: item.bindings }, null, 2)}</pre></details></label>; })}</div></div>
    </div><button disabled={busy || !agentRunId || !draftRunId || !selectedConclusions.length} className="mt-4 rounded bg-violet-700 px-4 py-2 text-sm text-white disabled:opacity-50" onClick={approveContext}>批准上下文并生成建议</button></section>}
    {error && <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</p>}{notice && <p role="status" className="rounded border border-violet-200 bg-violet-50 p-3 text-sm text-violet-800">{notice}</p>}
    {proposal && <section className="rounded border bg-white p-5"><h2 className="font-semibold">决策建议（仅供复核）</h2><p className="mt-2"><strong>建议：</strong>{proposal.outcome}</p><p className="mt-2 text-sm">{proposal.reasoning}</p><div className="mt-3 rounded bg-slate-50 p-3 text-sm"><strong>为什么这样建议：</strong><p className="mt-1">{proposal.reasoning}</p>{proposal.decision_rule_evaluation && <div className="mt-3 border-t pt-3"><strong>决策规则核对</strong><p className="mt-1">检查了 {proposal.decision_rule_evaluation.rule_count} 条已发布规则；命中 {proposal.decision_rule_evaluation.matches.length} 条。</p>{(proposal.decision_rule_evaluation.evaluated || []).map((item) => <div key={item.rule_id} className="mt-2 rounded border bg-white p-2"><strong>{item.name}</strong><span className="ml-2 rounded bg-slate-100 px-2 py-0.5 text-xs">{item.status}</span><p className="mt-1">{item.explanation}</p><p className="mt-1 text-xs text-slate-600">{(item.objects || []).flatMap((obj) => (obj.alternatives || []).flatMap((alt) => alt.checks || [])).map((check) => `${check.field}: ${check.missing ? "缺少数据" : check.actual}（条件 ${check.expected}，${check.missing ? "未验证" : check.passed ? "满足" : "未满足"}）`).join("；")}</p></div>)}</div>}<strong className="mt-3 block">依据解释：</strong>{(proposal.evidence || proposal.basis || []).map((item) => <p key={item.conclusion} className="mt-1">{item.human_explanation || readableConclusion(item.conclusion)}</p>)}</div><details className="mt-3"><summary className="cursor-pointer text-sm text-violet-700">查看规则和证据（技术详情）</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-3 text-xs">{JSON.stringify({ basis: proposal.basis, evidence: proposal.evidence, ontology_revision_id: proposal.ontology_revision_id, agent_run_id: agentRunId }, null, 2)}</pre></details><button disabled={busy} className="mt-4 rounded bg-[#8B1A2B] px-4 py-2 text-sm text-white disabled:opacity-50" onClick={continueInPersistentSession}>转入持久会话复核</button></section>}
  </div>;
}

export default function AgentDecisionTab({ ontologyId, defaultSurface = "decision" }: { ontologyId: string; defaultSurface?: "decision" | "sessions" }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [surface, setSurface] = useState<"decision" | "sessions">(defaultSurface);
  const [handoff, setHandoff] = useState<SessionHandoff | null>(() => {
    const kind = searchParams.get("agent_context_kind");
    const ref = searchParams.get("agent_context_ref");
    if (kind !== "temporal_event" || !ref) return null;
    return { title: searchParams.get("agent_title") || "时序事件复核",
      message: searchParams.get("agent_task") || "请分析此时序事件并引用其固定证据。",
      candidateObjectIds: [], contextKind: "temporal_event", contextRef: ref };
  });
  const consumeHandoff = () => {
    setHandoff(null);
    const next = new URLSearchParams(searchParams);
    for (const key of ["agent_context_kind", "agent_context_ref", "agent_task", "agent_title"]) next.delete(key);
    setSearchParams(next, { replace: true });
  };
  return (
    <div className="space-y-4">
      <div role="tablist" aria-label="Agent 工作面" className="inline-flex flex-wrap gap-1 rounded-lg border border-[#E0CFCF] bg-white p-1">
        <button type="button" role="tab" aria-selected={surface === "sessions"}
          className={`rounded-md px-3 py-2 text-sm ${surface === "sessions" ? "bg-[#8B1A2B] text-white" : "text-[#1A1A1A] hover:bg-[#F5F0F0]"}`}
          onClick={() => setSurface("sessions")}>持久会话</button>
        <button type="button" role="tab" aria-selected={surface === "decision"}
          className={`rounded-md px-3 py-2 text-sm ${surface === "decision" ? "bg-[#8B1A2B] text-white" : "text-[#1A1A1A] hover:bg-[#F5F0F0]"}`}
          onClick={() => setSurface("decision")}>兼容决策助手</button>
      </div>
      {surface === "decision"
        ? <LegacyAgentDecisionTab ontologyId={ontologyId} onUsePersistentSession={(value) => { setHandoff(value); setSurface("sessions"); }} />
        : <AgentSessionsPanel ontologyId={ontologyId} handoff={handoff || undefined} onHandoffConsumed={consumeHandoff} />}
    </div>
  );
}
