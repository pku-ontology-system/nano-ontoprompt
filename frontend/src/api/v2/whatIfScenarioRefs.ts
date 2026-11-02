export type WhatIfScenarioSummary = {
  id: string
  ontology_id: string
  name: string
  status: string
  created_by: string | null
  baseline: { scenario_context?: Record<string, unknown> | null }
  target_ids: string[]
  assumptions: unknown[]
  rule_overrides: Record<string, unknown>
  scenario_input_digest: string
}

export type WhatIfScenarioRef = {
  kind: 'what_if'
  ref_id: string
  source_version: string
  permission_digest: string
  scenario_context: Record<string, unknown>
  scenario_input_digest: string
  assumptions: unknown[]
  rule_overrides: Record<string, unknown>
}

export function toWhatIfScenarioRef(scenario: WhatIfScenarioSummary): WhatIfScenarioRef | null {
  const scenarioContext = scenario.baseline?.scenario_context
  if (!scenarioContext
      || typeof scenarioContext.context_digest !== 'string'
      || typeof scenarioContext.permission_digest !== 'string'
      || typeof scenario.scenario_input_digest !== 'string'
      || !Array.isArray(scenario.assumptions)
      || !scenario.rule_overrides || typeof scenario.rule_overrides !== 'object'
      || Array.isArray(scenario.rule_overrides)) return null
  return {
    kind: 'what_if',
    ref_id: scenario.id,
    source_version: scenarioContext.context_digest,
    permission_digest: scenarioContext.permission_digest,
    scenario_context: scenarioContext,
    scenario_input_digest: scenario.scenario_input_digest,
    assumptions: scenario.assumptions,
    rule_overrides: scenario.rule_overrides,
  }
}
