import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClientV2, formatApiError } from '@/api/client'
import { playbackKey, playbackUrl, riskLabel, riskTone, usePlayback, type Playback, type PlaybackSeries } from './sensorPlayback'

const COLORS = ['#2563eb', '#7c3aed', '#0f766e', '#d97706', '#be185d']
const W = 960, H = 330, LEFT = 58, RIGHT = 24, TOP = 18, BOTTOM = 36

function Chart({ playback }: { playback: Playback }) {
  const threshold = playback.rule.threshold ?? 0
  const low = threshold - 30, high = threshold + 30
  const x = (cycle: number) => LEFT + (cycle / playback.max_cycle) * (W - LEFT - RIGHT)
  const y = (value: number) => TOP + (1 - (Math.min(high, Math.max(low, value)) - low) / (high - low)) * (H - TOP - BOTTOM)
  const path = (series: PlaybackSeries) => series.average.map((value, index) => value === null ? null : `${x(series.cycles[index])},${y(value)}`).filter(Boolean).join(' ')
  const marks = playback.alerts.flatMap(alert => {
    const series = playback.series.find(row => row.object_id === alert.subject_id)
    const at = alert.end_cycle ? series?.average[alert.end_cycle - 1] : null
    return series && alert.end_cycle && at != null ? [{ alert, cx: x(alert.end_cycle), cy: y(at), color: COLORS[playback.series.indexOf(series) % COLORS.length] }] : []
  })
  const ticks = [0, 1, 2, 3, 4].map(index => low + ((high - low) * index) / 4)
  return <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" data-testid="sensor-chart"
    aria-label={`${playback.dataset.channel_label}的${playback.rule.window_points}周期均值；阈值 ${threshold}；当前第 ${playback.cursor} 周期`}>
    {ticks.map(tick => <g key={tick}><line x1={LEFT} x2={W - RIGHT} y1={y(tick)} y2={y(tick)} stroke="#e2e8f0" /><text x={LEFT - 8} y={y(tick) + 4} textAnchor="end" fontSize="12" fill="#64748b">{Math.round(tick)}</text></g>)}
    {[0, 50, 100, 150, 200, 250].filter(cycle => cycle <= playback.max_cycle).map(cycle => <text key={cycle} x={x(cycle)} y={H - 12} textAnchor="middle" fontSize="12" fill="#64748b">{cycle}</text>)}
    <text x={W - RIGHT} y={H - 1} textAnchor="end" fontSize="11" fill="#94a3b8">周期（1 周期 = 1 小时）</text>
    <line x1={LEFT} x2={W - RIGHT} y1={y(threshold)} y2={y(threshold)} stroke="#dc2626" strokeDasharray="6 4" strokeWidth="1.5" />
    <text x={W - RIGHT - 4} y={y(threshold) - 6} textAnchor="end" fontSize="12" fill="#dc2626">阈值 {threshold} {playback.dataset.unit}</text>
    <line x1={x(playback.cursor)} x2={x(playback.cursor)} y1={TOP} y2={H - BOTTOM} stroke="#94a3b8" strokeDasharray="2 3" />
    {playback.series.map((series, index) => <polyline key={series.object_id} points={path(series)} fill="none" stroke={COLORS[index % COLORS.length]} strokeWidth="2"
      strokeDasharray={series.risk_level === 'High' ? '5 3' : undefined} opacity={series.risk_level === 'High' ? 0.55 : 1} data-asset={series.asset_id} />)}
    {marks.map(({ alert, cx, cy, color }) => <g key={alert.id} data-testid={`alert-mark-${alert.asset_id}`}><circle cx={cx} cy={cy} r="7" fill="#dc2626" stroke="#fff" strokeWidth="2" />
      <text x={cx - 10} y={cy - 12} textAnchor="end" fontSize="12" fontWeight="600" fill={color}>{alert.name}</text></g>)}
  </svg>
}

export default function SensorPlaybackWidget({ ontologyId, playbackId, onChanged }: { ontologyId: string; playbackId: string; onChanged?: () => void }) {
  const queryClient = useQueryClient()
  const { data: playback, error } = usePlayback(ontologyId, playbackId)
  const [playing, setPlaying] = useState(false)
  const [notice, setNotice] = useState('')

  const advance = useMutation({
    mutationFn: () => apiClientV2.post<Playback>(`${playbackUrl(ontologyId, playbackId)}:advance`, { steps: 1, until_alert: true }),
    onSuccess: result => {
      queryClient.setQueryData(playbackKey(ontologyId, playbackId), result)
      const opened = result.advanced?.flatMap(tick => tick.new_alert_ids) ?? []
      if (opened.length) {
        const names = result.alerts.filter(alert => opened.includes(alert.id)).map(alert => alert.name).join('、')
        setNotice(`第 ${result.cursor} 周期出现 ${opened.length} 条新告警：${names}。回放已暂停，处理后再继续。`)
        setPlaying(false)
        onChanged?.()
      }
      if (result.finished) setPlaying(false)
    },
    onError: () => setPlaying(false),
  })
  const { mutate, isPending } = advance
  useEffect(() => {
    if (!playing || isPending) return
    const timer = window.setTimeout(() => mutate(), 500)
    return () => window.clearTimeout(timer)
  }, [playing, isPending, mutate, playback?.cursor])

  if (error) return <p role="alert" className="p-3 text-sm text-red-700">{formatApiError(error)}</p>
  if (!playback) return <p role="status" className="p-3 text-sm text-slate-500">正在加载传感回放…</p>
  const progress = Math.round(((playback.cursor - playback.start_cycle) / Math.max(1, playback.max_cycle - playback.start_cycle)) * 100)
  const rule = playback.rule
  return <div className="flex h-full flex-col gap-2 overflow-auto p-1" data-testid="sensor-playback">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">传感回放</p>
        <h3 className="text-base font-semibold">{playback.dataset.channel_label}（{playback.dataset.unit}）· {rule.window_points} 周期均值</h3>
        <p className="text-xs text-slate-500">{playback.dataset.source} · {playback.dataset.clock}</p>
        <p className="text-xs text-slate-500">规则“{rule.display_name}”：均值 ≥ {rule.threshold} 且连续 {rule.min_consecutive_windows} 个窗口，设备尚未标为高风险</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm tabular-nums" data-testid="sensor-cursor">周期 {playback.cursor} / {playback.max_cycle}</span>
        <button type="button" className="wb-button-secondary" disabled={isPending || playback.finished} onClick={() => { setNotice(''); mutate() }}>推进 {playback.step_cycles} 个周期</button>
        <button type="button" className="wb-button-primary" disabled={playback.finished || (isPending && !playing)} aria-pressed={playing}
          onClick={() => { setNotice(''); setPlaying(value => !value) }}>{playing ? '暂停' : playback.finished ? '播放结束' : '自动播放'}</button>
      </div>
    </div>
    <div className="h-1.5 overflow-hidden rounded bg-slate-200" aria-hidden="true"><div className="h-full bg-slate-600 transition-all" style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} /></div>
    {notice && <p role="status" className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-900" data-testid="sensor-notice">{notice}</p>}
    {advance.error && <p role="alert" className="text-sm text-red-700">{formatApiError(advance.error)}</p>}
    <Chart playback={playback} />
    <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5" aria-label="设备">
      {playback.series.map((series, index) => {
        const alert = playback.alerts.find(row => row.subject_id === series.object_id)
        const latest = [...series.average].reverse().find(value => value !== null) ?? null
        return <li key={series.object_id} className="rounded-lg border bg-white px-3 py-2 text-sm" data-testid={`sensor-asset-${series.asset_id}`}>
          <div className="flex items-center justify-between gap-2"><span className="flex items-center gap-1.5 font-medium"><span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: COLORS[index % COLORS.length] }} />{series.name}</span>
            {series.risk_level && <span className={`rounded px-1.5 py-0.5 text-[11px] ${riskTone[series.risk_level] ?? 'bg-slate-100'}`}>{riskLabel[series.risk_level] ?? series.risk_level}</span>}</div>
          <p className="mt-1 text-xs text-slate-500">机组 {series.unit} · 均值 {latest === null ? '—' : latest.toFixed(1)} {playback.dataset.unit}</p>
          {alert && <p className="mt-1 text-xs font-medium text-red-700">{alert.status === 'resolved' ? '告警已解决' : `已告警（第 ${alert.end_cycle} 周期）`}</p>}
        </li>
      })}
    </ul>
  </div>
}
