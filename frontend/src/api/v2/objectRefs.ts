export type ObjectRef = {
  ontology_id: string;
  object_type: string;
  object_id: string;
  data_view_id?: string;
  what_if_scenario_id?: string;
};

export function objectRefHref(ref: ObjectRef): string {
  const params = new URLSearchParams({
    objectType: ref.object_type,
    objectId: ref.object_id,
  });
  if (ref.data_view_id) params.set("dataView", ref.data_view_id);
  if (ref.what_if_scenario_id) params.set("whatIfScenario", ref.what_if_scenario_id);
  return `/ontologies/${encodeURIComponent(ref.ontology_id)}/object?${params.toString()}`;
}
