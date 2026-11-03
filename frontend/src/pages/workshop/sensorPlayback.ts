import { useQuery } from '@tanstack/react-query'
import { apiClientV2 } from '@/api/client'

export type PlaybackSeries = {
  asset_id: string; object_id: string; unit: number; name: string; risk_level: string | null; health_score: number | null
  cycles: number[]; values: number[]; average: Array<number | null>; total_cycles: number
}
export type PlaybackCase = { id: string; status: string; created_by: string; reviewed_by: string | null; action_run_id: string | null }
export type PlaybackAlert = {
  id: string; subject_id: string; asset_id: string | null; name: string; status: 'open' | 'acknowledged' | 'resolved'; severity: string
  window_start: string | null; window_end: string | null; start_cycle: number | null; end_cycle: number | null
  peak: number | null; breaching_windows: number | null; ongoing: boolean | null; occurrence_count: number; recovered: boolean
  suggested_action: { action_id: string } | null; case: PlaybackCase | null
}
export type PlaybackRule = { id: string; display_name: string; status: string | null; window_points: number; aggregate: string | null; op: string | null; threshold: number | null; min_consecutive_windows: number | null }
export type Playback = {
  id: string; ontology_id: string; name: string; etag: number
  dataset: { id: string; label: string; channel_label: string; unit: string; source: string; clock: string }
  cursor: number; start_cycle: number; step_cycles: number; max_cycle: number; finished: boolean; base_time: string
  rule: PlaybackRule; series: PlaybackSeries[]; alerts: PlaybackAlert[]
  ticks: Array<{ sequence: number; cursor: number; view_id: string; new_alert_ids: string[] }>
  advanced?: Array<{ sequence: number; cursor: number; new_alert_ids: string[] }>
}

export const playbackKey = (ontologyId: string, playbackId: string) => ['sensor-playback', ontologyId, playbackId]
export const playbackUrl = (ontologyId: string, playbackId: string) => `/ontologies/${ontologyId}/sensor-playbacks/${playbackId}`

/** Both widgets read this one query, so a step or a review shows up in the chart and the alert list together. */
export function usePlayback(ontologyId: string, playbackId: string) {
  return useQuery({ queryKey: playbackKey(ontologyId, playbackId), queryFn: () => apiClientV2.get<Playback>(playbackUrl(ontologyId, playbackId)), enabled: !!playbackId, refetchInterval: 4000 })
}

export const riskTone: Record<string, string> = { High: 'bg-red-100 text-red-800', Medium: 'bg-amber-100 text-amber-800', Low: 'bg-emerald-100 text-emerald-800' }
export const riskLabel: Record<string, string> = { High: '高风险', Medium: '中风险', Low: '低风险' }
