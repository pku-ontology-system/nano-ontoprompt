import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pause, Play, StepForward } from 'lucide-react'
import { formatApiError } from '@/api/client'
import { importApi } from '@/api/imports'

/** An event stream that is loaded in steps: how far it has got, one step forward, or play on its own until it is pause or done. */
export function ReplayPanel({ ontologyId }: { ontologyId: string }) {
  const queryClient = useQueryClient()
  const state = useQuery({ queryKey: ['import-replay', ontologyId], queryFn: () => importApi.replay(ontologyId) })
  const [playing, setPlaying] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['import-replay', ontologyId] })
  const advance = useMutation({ mutationFn: () => importApi.advance(ontologyId), onSuccess: async () => { await refresh() } })
  const replay = state.data?.replay
  if (!replay) return null
  const done = replay.cursor >= replay.total
  const stop = () => { if (timer.current) clearTimeout(timer.current); timer.current = null; setPlaying(false) }
  const loop = () => {
    timer.current = setTimeout(async () => {
      try {
        const out = await advance.mutateAsync()
        if (out.done) { stop(); return }
        loop()
      } catch { stop() }
    }, 700)
  }
  const play = () => { setPlaying(true); loop() }
  const percent = Math.min(100, Math.round((replay.cursor / Math.max(replay.total, 1)) * 100))
  return <div data-testid="replay-panel" className="rounded-lg border border-slate-200 bg-white p-3">
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <span className="font-medium text-slate-900">事件流</span>
      <span className="tabular-nums text-slate-600" data-testid="replay-cursor">已载入 {replay.cursor.toLocaleString('zh-CN')} / {replay.total.toLocaleString('zh-CN')} 条（{replay.table}，按 {replay.order_by} 排序）</span>
      <button type="button" className="wb-button-secondary" disabled={done || advance.isPending || playing} onClick={() => advance.mutate()}><StepForward size={14} />推进 {replay.batch} 条</button>
      {playing ? <button type="button" className="wb-button-secondary" onClick={stop}><Pause size={14} />暂停</button>
        : <button type="button" className="wb-button-secondary" disabled={done} onClick={play}><Play size={14} />自动播放</button>}
    </div>
    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full bg-teal-500 transition-all" style={{ width: `${percent}%` }} /></div>
    {advance.error && <p role="alert" className="mt-2 text-xs text-red-700">{formatApiError(advance.error)}</p>}
    {done && <p className="mt-2 text-xs text-slate-500">事件已经全部载入。</p>}
  </div>
}
