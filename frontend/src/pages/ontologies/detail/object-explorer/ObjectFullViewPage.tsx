import { useMemo } from "react";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { apiClientV2 } from "@/api/client";
import { toWhatIfScenarioRef, type WhatIfScenarioSummary } from "@/api/v2/whatIfScenarioRefs";
import ObjectPanel, { type ObjectPanelRecord } from "./ObjectPanel";
import { explorerError } from "./runtime";

type QueryContext = {
  ontology_id: string;
  consistency: "live" | "snapshot";
  revision_policy?: "latest" | "pinned";
  revision_id?: string;
  data_view_id?: string;
  what_if_scenario_id?: string;
  what_if_context_digest?: string;
  what_if_input_digest?: string;
};

type ResultManifest = {
  status?: string;
  adapter?: string;
  source_manifest_id?: string | null;
  source_version?: string | null;
  metadata_revision_id?: string | null;
  metadata_digest?: string | null;
  generated_at?: string | null;
};

type ObjectLoadResult = {
  objects: ObjectPanelRecord[];
  completeness: "complete" | "partial" | "incomplete";
  result_manifest?: ResultManifest | null;
};

function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <span className="text-slate-400">—</span>;
  const display = typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  return <span className="break-words">{display}</span>;
}

export default function ObjectFullViewPage() {
  const { id: ontologyId = "" } = useParams<{ id: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const objectType = searchParams.get("objectType") || "";
  const objectId = searchParams.get("objectId") || "";
  const dataViewId = searchParams.get("dataView") || "";
  const whatIfScenarioId = searchParams.get("whatIfScenario") || "";
  const conflictingContexts = !!dataViewId && !!whatIfScenarioId;

  const whatIfScenarios = useQuery({
    queryKey: ["object-full-view-what-if", ontologyId, whatIfScenarioId],
    enabled: !!whatIfScenarioId && !conflictingContexts,
    queryFn: ({ signal }) => apiClientV2.get<{ scenarios: WhatIfScenarioSummary[] }>(
      `/ontologies/${ontologyId}/what-if/scenarios?summary=true`, { signal },
    ),
  });
  const scenario = useMemo(
    () => whatIfScenarios.data?.scenarios.find(item => item.id === whatIfScenarioId) || null,
    [whatIfScenarios.data, whatIfScenarioId],
  );
  const scenarioRef = useMemo(() => scenario ? toWhatIfScenarioRef(scenario) : null, [scenario]);
  const scenarioRevisionId = String(scenarioRef?.scenario_context.schema_revision_id || "");

  const context: QueryContext = scenarioRef ? {
    ontology_id: ontologyId,
    consistency: "snapshot",
    revision_policy: "pinned",
    revision_id: scenarioRevisionId,
    what_if_scenario_id: scenarioRef.ref_id,
    what_if_context_digest: scenarioRef.source_version,
    what_if_input_digest: scenarioRef.scenario_input_digest,
  } : dataViewId ? {
    ontology_id: ontologyId,
    consistency: "snapshot",
    data_view_id: dataViewId,
  } : {
    ontology_id: ontologyId,
    consistency: "live",
    revision_policy: "latest",
  };
  const contextReady = !conflictingContexts && (!whatIfScenarioId || (!!scenarioRef && !!scenarioRevisionId));

  const objectQuery = useQuery({
    queryKey: ["object-full-view", ontologyId, objectType, objectId, context],
    enabled: !!ontologyId && !!objectType && !!objectId && contextReady,
    queryFn: ({ signal }) => apiClientV2.post<ObjectLoadResult>(
      `/ontologies/${ontologyId}/object-query/load`,
      {
        expression: {
          kind: "static",
          type_ref: { kind: "object", api_name: objectType },
          object_ids: [objectId],
        },
        context,
        read: { page_size: 1 },
      },
      { signal },
    ),
  });

  const object = objectQuery.data?.objects.find(item => item.object_id === objectId && item.object_type === objectType) || null;
  const resultManifest = objectQuery.data?.result_manifest || null;
  const state = (location.state || {}) as { from?: unknown };
  const fallbackHref = useMemo(() => {
    const params = new URLSearchParams({ tab: "objects", objectType });
    if (objectId) params.set("activeObject", objectId);
    if (dataViewId) params.set("dataView", dataViewId);
    if (whatIfScenarioId) params.set("whatIfScenario", whatIfScenarioId);
    return `/ontologies/${encodeURIComponent(ontologyId)}?${params.toString()}`;
  }, [dataViewId, objectId, objectType, ontologyId, whatIfScenarioId]);
  const safeReturnFrom = typeof state.from === "string"
    && state.from.startsWith(`/ontologies/${encodeURIComponent(ontologyId)}`)
    && !state.from.startsWith("//")
    ? state.from
    : null;

  const sourceLabel = whatIfScenarioId
    ? `固定 What-if 基线${scenario ? ` · ${scenario.name}` : ""}`
    : dataViewId ? "固定 Data View" : "当前对象图";
  const back = () => safeReturnFrom ? navigate(-1) : navigate(fallbackHref, { replace: true });

  return <div className="min-h-full bg-[#F9F5F5] p-4 md:p-7">
    <div className="mx-auto max-w-6xl space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <button type="button" onClick={back} className="mb-3 inline-flex items-center gap-2 text-sm text-[#64131F] hover:underline">
            <ArrowLeft size={16} />返回对象工作台
          </button>
          <p className="text-[11px] font-semibold tracking-[0.12em] text-[#816C72]">OBJECT REFERENCE · {sourceLabel}</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-[#1A1A1A]">对象详情</h1>
          <p className="mt-1 break-all font-mono text-xs text-[#6B6B6B]">{objectType || "缺少对象类型"} · {objectId || "缺少对象 ID"}</p>
        </div>
        <button type="button" onClick={() => void objectQuery.refetch()} disabled={!contextReady || !objectType || !objectId || objectQuery.isFetching}
          className="inline-flex items-center gap-2 rounded-lg border border-[#D5D5D5] bg-white px-3 py-2 text-sm text-[#1A1A1A] disabled:opacity-50">
          <RefreshCw size={15} className={objectQuery.isFetching ? "animate-spin" : ""} />刷新并重新授权
        </button>
      </header>

      {conflictingContexts && <div role="alert" className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">对象引用同时包含 Data View 与 What-if 场景。请从来源工作台重新打开对象。</div>}
      {!objectType || !objectId ? <div role="alert" className="rounded-xl border border-[#E0CFCF] bg-white p-5 text-sm text-[#6B6B6B]">对象链接缺少类型或对象 ID，无法读取。</div> : null}
      {whatIfScenarioId && whatIfScenarios.isPending && <div role="status" className="rounded-xl border border-[#E0CFCF] bg-white p-5 text-sm text-[#6B6B6B]">正在验证 What-if 场景与固定基线…</div>}
      {whatIfScenarioId && whatIfScenarios.error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">What-if 场景不可访问或已变化。{explorerError(whatIfScenarios.error)}<button type="button" onClick={() => void whatIfScenarios.refetch()} className="ml-2 underline">重试</button></div>}
      {whatIfScenarioId && !whatIfScenarios.isPending && !whatIfScenarios.error && !scenarioRef && <div role="alert" className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">该 What-if 场景没有可用的固定上下文，已停止读取。</div>}
      {contextReady && objectType && objectId && objectQuery.isPending && <div role="status" className="rounded-xl border border-[#E0CFCF] bg-white p-5 text-sm text-[#6B6B6B]">正在按当前权限读取对象…</div>}
      {contextReady && objectType && objectId && objectQuery.error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">对象不存在、不可访问或读取失败。{explorerError(objectQuery.error)}<button type="button" onClick={() => void objectQuery.refetch()} className="ml-2 underline">重试</button></div>}
      {contextReady && objectType && objectId && objectQuery.isSuccess && !object && <div role="status" className="rounded-xl border border-[#E0CFCF] bg-white p-5 text-sm text-[#6B6B6B]">对象不存在或当前用户无权查看。</div>}

      {object && <>
        <ObjectPanel
          object={object}
          onClose={back}
          ontologyId={ontologyId}
          dataViewId={dataViewId || undefined}
          whatIfScenarioId={whatIfScenarioId || undefined}
          readOnly={!!dataViewId}
          fullPage
          className="oe-object-full-page"
        />
        <section className="rounded-xl border border-[#E0CFCF] bg-white p-4 md:p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div><h2 className="text-sm font-semibold text-[#1A1A1A]">来源与授权</h2><p className="mt-1 text-xs text-[#6B6B6B]">属性由 Object Query 在本次请求中按当前用户权限筛选。</p></div>
            <span className="rounded-md bg-[#F5F0F0] px-2 py-1 text-[11px] text-[#64131F]">{resultManifest?.status || "ready"}</span>
          </div>
          {objectQuery.data?.completeness !== "complete" && <p role="status" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">当前查询标记为{objectQuery.data?.completeness || "部分"}结果；请结合来源版本与查询状态查看，未返回的字段不会在此页补猜。</p>}
          <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
            <div><dt className="text-[#6B6B6B]">读取来源</dt><dd className="mt-1 break-all text-[#1A1A1A]">{resultManifest?.adapter || sourceLabel}</dd></div>
            <div><dt className="text-[#6B6B6B]">本体修订</dt><dd className="mt-1 break-all font-mono text-[#1A1A1A]">{resultManifest?.metadata_revision_id || "按当前可用修订"}</dd></div>
            <div><dt className="text-[#6B6B6B]">结果生成时间</dt><dd className="mt-1 text-[#1A1A1A]">{resultManifest?.generated_at ? new Date(resultManifest.generated_at).toLocaleString() : "—"}</dd></div>
            {resultManifest?.source_manifest_id && <div><dt className="text-[#6B6B6B]">来源快照</dt><dd className="mt-1 break-all font-mono text-[#1A1A1A]">{resultManifest.source_manifest_id}</dd></div>}
            {resultManifest?.source_version && <div><dt className="text-[#6B6B6B]">来源摘要</dt><dd className="mt-1 break-all font-mono text-[#1A1A1A]">{resultManifest.source_version}</dd></div>}
          </dl>
        </section>
        <section className="rounded-xl border border-[#E0CFCF] bg-white p-4 md:p-5">
          <h2 className="text-sm font-semibold text-[#1A1A1A]">授权属性</h2>
          {Object.entries(object.properties).length
            ? <dl className="mt-3 divide-y divide-[#F0E7E7]">{Object.entries(object.properties).map(([name, value]) => <div key={name} className="grid gap-1 py-3 sm:grid-cols-[minmax(150px,.7fr)_minmax(0,2fr)] sm:gap-5"><dt className="text-xs text-[#6B6B6B]">{name}</dt><dd className="text-sm text-[#1A1A1A]"><Value value={value} /></dd></div>)}</dl>
            : <p className="mt-3 rounded-lg border border-dashed border-[#E0CFCF] p-4 text-sm text-[#6B6B6B]">当前用户没有可显示的属性。</p>}
        </section>
      </>}
    </div>
  </div>;
}
