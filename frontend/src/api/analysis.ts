import { apiClientV2 } from './client'

export type Tool = 'splink' | 'great_expectations' | 'marquez'

export interface DemoDataset {
  id: string
  tool: Tool
  title: string
  description: string
  default: boolean
  file: string
  format: string
  available: boolean
  bytes: number | null
  rows?: number
  columns?: string[]
  ground_truth: string | null
  has_health_suite: boolean
  has_resolution_preset: boolean
  problem?: string
  source: { project: string; url: string; license: string }
}

export interface Preview { columns: string[]; rows: unknown[][]; total: number }

// ---- data health ----------------------------------------------------------------------------------------
export interface Expectation { expectation_type: string; kwargs: Record<string, unknown>; meta?: { notes?: string } }
export interface Suite { name: string; expectations: Expectation[] }
export interface ExpectationResult extends Expectation {
  label: string
  success: boolean
  result: {
    element_count?: number
    unexpected_count?: number
    unexpected_percent?: number
    missing_count?: number
    partial_unexpected_list?: unknown[]
    partial_unexpected_counts?: { value: unknown; count: number }[]
    observed_value?: unknown
  }
  exception_info: { raised_exception: boolean; exception_message?: string }
}
export interface HealthReport {
  success: boolean
  statistics: { evaluated_expectations: number; successful_expectations: number; unsuccessful_expectations: number; errored_expectations: number; success_percent: number }
  results: ExpectationResult[]
  meta: { suite_name: string; dataset_id: string; row_count: number; column_count: number; run_time_ms: number }
}

// ---- entity resolution -----------------------------------------------------------------------------------
export type Row = Record<string, string | number | boolean | null>
export interface Level { level: number; label: string; m: number | null; u: number | null; weight: number | null }
export interface ModelComparison { column: string; type: string; term_frequency_adjusted: boolean; levels: Level[] }
export interface Contribution { column: string; label: string; weight: number }
export interface PairExample {
  id_l: string | number
  id_r: string | number
  left: Row
  right: Row
  weight: number
  probability: number
  same_entity: boolean | null
  contributions: Contribution[]
}
export interface ResolutionResult {
  settings: { unique_id: string; threshold_match_probability: number; ground_truth: string | null; blocking_rules: unknown[]; comparisons: { column: string; type: string; tf?: boolean }[] }
  summary: {
    records: number
    total_possible_pairs: number
    candidate_pairs: number
    pairs_above_threshold: number
    entities_found: number
    records_in_clusters: number
    singletons: number
    largest_cluster: number
    run_time_ms: number
  }
  evaluation: {
    ground_truth_column: string
    true_entities: number
    true_pairs: number
    predicted_pairs: number
    true_positive_pairs: number
    precision: number
    recall: number
    f1: number
    blocking_recall: number
    threshold: number
  } | null
  model: { lambda: number; prior_weight: number; random_pairs_for_u: number; sessions: { rule: unknown; pairs: number; iterations: number; lambda: number | null }[]; comparisons: ModelComparison[] }
  histogram: { bin_start: number[]; bin_width: number; match?: number[]; non_match?: number[]; all?: number[] }
  clusters: { cluster_id: number; size: number; records: Row[]; true_entities_inside?: number }[]
  examples: { borderline: PairExample[]; false_positives: PairExample[]; false_negatives: PairExample[]; confident: PairExample[] }
}

// ---- lineage -------------------------------------------------------------------------------------------
export interface QualityAssertion { assertion: string; success: boolean; column?: string | null }
export interface LineageNode {
  id: string
  kind: 'job' | 'dataset'
  namespace: string
  name: string
  label: string
  status: 'success' | 'failed' | 'aborted' | 'running' | 'unknown' | null
  description?: string | null
  run_count?: number
  last_run_at?: string | null
  last_duration_seconds?: number | null
  sql?: string | null
  runs?: { run_id: string; state: string; started_at: string | null; ended_at: string | null }[]
  fields?: { name: string; type: string | null; description: string | null }[]
  source?: string | null
  updated_at?: string | null
  quality?: { assertions?: QualityAssertion[]; passed?: number; failed?: number; metrics?: Record<string, unknown> } | null
  column_lineage?: Record<string, string[]> | null
}
export interface LineageEdge { source: string; target: string }
export interface LineageGraph { nodes: LineageNode[]; edges: LineageEdge[]; namespaces: string[]; event_count?: number; focus?: string }
export interface Impact {
  node: string
  downstream: { id: string; kind: 'job' | 'dataset'; label: string; namespace: string; distance: number; status: string | null; quality_failed: number }[]
  downstream_jobs: number
  downstream_datasets: number
  upstream_count: number
}

// ---- runs and the pipeline ---------------------------------------------------------------------------------
export interface AnalysisRun<T = unknown> {
  id: string
  kind: 'health' | 'resolution' | 'pipeline'
  dataset_id: string
  title: string
  summary: Record<string, unknown>
  params: Record<string, unknown>
  created_at: string | null
  result: T
}
export interface PipelineStep { key: 'ingest' | 'validate' | 'resolve'; title: string; node: string; summary: Record<string, number | string | boolean | undefined> }
export interface PipelineResult {
  dataset_id: string
  title: string
  sha256: string
  steps: PipelineStep[]
  health: HealthReport
  resolution: ResolutionResult
  lineage: { namespace: string; focus: string; events: number; datasets: Record<string, string> }
}

export const analysisApi = {
  demos: () => apiClientV2.get<{ datasets: DemoDataset[] }>('/analysis/demos').then(r => r.datasets),
  preview: (id: string, limit = 12) => apiClientV2.get<Preview>(`/analysis/demos/${id}/preview`, { params: { limit } }),
  healthSuite: (id: string, baseline = false) => apiClientV2.get<{ suite: Suite; expectation_types: string[] }>(`/analysis/health/suites/${id}`, { params: baseline ? { baseline: true } : {} }),
  runHealth: (datasetId: string, suite?: Suite) => apiClientV2.post<AnalysisRun<HealthReport>>('/analysis/health/run', { dataset_id: datasetId, suite }),
  resolutionPreset: (id: string) => apiClientV2.get<{ settings: Record<string, unknown> }>(`/analysis/resolution/presets/${id}`).then(r => r.settings),
  runResolution: (datasetId: string, settings?: Record<string, unknown>) => apiClientV2.post<AnalysisRun<ResolutionResult>>('/analysis/resolution/run', { dataset_id: datasetId, settings }),
  seedLineage: () => apiClientV2.post<{ events: number; namespace: string }>('/analysis/lineage/seed', {}),
  lineageGraph: (params: { namespace?: string; node?: string; depth?: number; direction?: string } = {}) => apiClientV2.get<LineageGraph>('/analysis/lineage/graph', { params }),
  lineageImpact: (node: string, namespace?: string) => apiClientV2.get<Impact>('/analysis/lineage/impact', { params: { node, namespace } }),
  resetLineage: (params: { namespace?: string; source?: string }) => apiClientV2.delete<{ deleted: number }>('/analysis/lineage', { params }),
  runPipeline: (datasetId: string) => apiClientV2.post<AnalysisRun<PipelineResult>>('/analysis/pipeline/run', { dataset_id: datasetId }),
  runs: (params: { kind?: string; dataset_id?: string; limit?: number } = {}) => apiClientV2.get<{ runs: Omit<AnalysisRun, 'result'>[] }>('/analysis/runs', { params }).then(r => r.runs),
  run: <T,>(id: string) => apiClientV2.get<AnalysisRun<T>>(`/analysis/runs/${id}`),
}
