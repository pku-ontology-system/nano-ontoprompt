import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnImpact, Impact, LineageEdge, LineageNode } from '@/api/analysis'
import { ColumnImpactView } from './ColumnImpactView'
import { LAYER_STYLE, cell, refLink } from './format'
import { NODE_H, NODE_W, STATUS_COLOR, closure, layoutLineage, statusLabel } from './lineageLayout'
import { useElementWidth } from './useElementWidth'

const clip = (text: string, max: number) => (text.length > max ? `${text.slice(0, max - 9)}…${text.slice(-8)}` : text) // the end of a name is what tells jobs apart

export function LineageGraph({ nodes, edges, selected, focus, onSelect }: { nodes: LineageNode[]; edges: LineageEdge[]; selected: string | null; focus?: string | null; onSelect: (id: string | null) => void }) {
  const { positions, width, height } = useMemo(() => layoutLineage(nodes, edges), [nodes, edges])
  const lit = useMemo(() => (selected ? closure(selected, edges) : null), [selected, edges])
  const [box, boxWidth] = useElementWidth()
  const [zoom, setZoom] = useState<number | null>(null) // null: fit the width of the box (not below 35%: a lineage of eleven layers is wide)
  const fit = boxWidth ? Math.min(1, Math.max(0.35, (boxWidth - 4) / width)) : 1
  const scale = zoom ?? fit
  const step = (factor: number) => setZoom(Math.min(1.6, Math.max(0.3, scale * factor)))
  // bring a clicked node to the middle of the box; only the box scrolls, never the page
  const centre = (el: Element, x: number) => {
    const holder = el.closest('[data-graph-scroll]')
    if (holder instanceof HTMLElement) holder.scrollTo({ left: Math.max(0, (x + NODE_W / 2) * scale - holder.clientWidth / 2), behavior: 'smooth' })
  }
  return <div data-testid="lineage-graph">
    <div className="mb-1.5 ml-auto flex w-fit items-center gap-1 rounded-lg border border-slate-200 bg-white p-0.5 text-xs shadow-sm">
      <button type="button" aria-label="缩小" className="h-6 w-6 rounded hover:bg-slate-100" onClick={() => step(1 / 1.25)}>−</button>
      <span className="w-10 text-center tabular-nums text-slate-600">{Math.round(scale * 100)}%</span>
      <button type="button" aria-label="放大" className="h-6 w-6 rounded hover:bg-slate-100" onClick={() => step(1.25)}>+</button>
      <button type="button" className="rounded px-1.5 py-1 hover:bg-slate-100" onClick={() => setZoom(null)}>适应宽度</button>
    </div>
    <div ref={box} data-graph-scroll className="max-h-[680px] overflow-auto rounded-lg border border-slate-200 bg-slate-50/60">
    <svg width={width * scale} height={height * scale} viewBox={`0 0 ${width} ${height}`} role="group" aria-label="血缘图" onClick={() => onSelect(null)}>
      <defs><marker id="lineage-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#94a3b8" /></marker>
        <marker id="lineage-arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#b91c3c" /></marker></defs>
      {edges.map(e => {
        const a = positions.get(e.source), b = positions.get(e.target)
        if (!a || !b) return null
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2
        const bend = Math.max(36, (x2 - x1) / 2)
        const hot = !!lit && lit.has(e.source) && lit.has(e.target)
        return <path key={`${e.source}>${e.target}`} d={`M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`} fill="none" stroke={hot ? '#b91c3c' : '#cbd5e1'} strokeWidth={hot ? 2 : 1.4}
          opacity={lit && !hot ? 0.3 : 1} markerEnd={`url(#${hot ? 'lineage-arrow-hot' : 'lineage-arrow'})`} />
      })}
      {nodes.map(n => {
        const p = positions.get(n.id)
        if (!p) return null
        const isJob = n.kind === 'job'
        const dim = !!lit && !lit.has(n.id)
        const chosen = n.id === selected
        const failed = n.quality?.failed ?? 0
        const layer = n.layer ? LAYER_STYLE[n.layer] : undefined
        return <g key={n.id} transform={`translate(${p.x},${p.y})`} role="button" tabIndex={0} aria-pressed={chosen} aria-label={`${isJob ? '作业' : '数据集'} ${n.name}`} data-testid={`lineage-node-${n.name}`} data-kind={n.kind} opacity={dim ? 0.35 : 1}
          className="cursor-pointer outline-none" onClick={e => { e.stopPropagation(); onSelect(n.id); centre(e.currentTarget, p.x) }} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(n.id) } }}>
          <rect width={NODE_W} height={NODE_H} rx={isJob ? 10 : 23} fill={layer?.fill ?? (isJob ? '#fff7ed' : '#ffffff')} stroke={chosen ? '#b91c3c' : n.id === focus ? '#0f172a' : layer?.stroke ?? (isJob ? '#fdba74' : '#94a3b8')} strokeWidth={chosen || n.id === focus ? 2.5 : 1.4} />
          {isJob && <circle cx={16} cy={NODE_H / 2} r={5} fill={STATUS_COLOR[n.status ?? 'unknown']}><title>{statusLabel(n.status)}</title></circle>}
          {!isJob && <rect x={13} y={NODE_H / 2 - 6} width={12} height={12} rx={2} fill="none" stroke="#64748b" strokeWidth={1.4} />}
          <text x={34} y={NODE_H / 2 - 3} fontSize="12" fontWeight="600" fill="#0f172a">{clip(n.label, 20)}<title>{n.label}</title></text>
          <text x={34} y={NODE_H / 2 + 11} fontSize="10" fill="#64748b">{isJob ? `${n.run_count ?? 0} 次运行 · ${statusLabel(n.status)}` : n.quality ? `${n.quality.passed ?? 0} 项检查通过${failed ? ` · ${failed} 项未通过` : ''}` : `${n.fields?.length ?? 0} 个字段`}</text>
          {!isJob && n.quality && (n.quality.assertions?.length ?? 0) > 0 && <g transform={`translate(${NODE_W - 22},${NODE_H / 2})`}><circle r={8} fill={failed ? '#dc2626' : '#059669'} /><text y={4} fontSize="10" fontWeight="700" fill="#fff" textAnchor="middle">{failed ? '!' : '✓'}</text></g>}
        </g>
      })}
    </svg>
    </div>
    {scale < 0.8 && <p className="mt-1 text-xs text-slate-500">图比较宽，已缩小到 {Math.round(scale * 100)}%：点右上角的 + 放大，点节点会自动滚到它，勾选“只看所选节点附近”可以只看它的上下游。</p>}
  </div>
}

/** The colours of an ontology's lineage: which part of the chain a node belongs to. */
export function LayerLegend({ nodes }: { nodes: LineageNode[] }) {
  const present = Object.entries(LAYER_STYLE).filter(([key]) => nodes.some(n => n.layer === key))
  if (present.length === 0) return null
  return <ul aria-label="图例" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">{present.map(([key, style]) =>
    <li key={key} className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm" style={{ background: style.fill, border: `1.5px solid ${style.stroke}` }} />{style.label}</li>)}</ul>
}

const fmtTime = (iso?: string | null) => (iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—')

export interface FieldImpact { field: string | null; result?: ColumnImpact; onAsk: (field: string) => void }

export function NodeDetail({ node, impact, onImpact, impactBusy, fieldImpact }: { node: LineageNode; impact?: Impact; onImpact: () => void; impactBusy?: boolean; fieldImpact?: FieldImpact }) {
  const job = node.kind === 'job'
  const layer = node.layer ? LAYER_STYLE[node.layer] : undefined
  const link = refLink(node.ref)
  return <aside aria-label="节点详情" data-testid="lineage-detail" className="wb-surface p-4 text-sm">
    <div className="grid gap-5 md:grid-cols-3">
      <div className="space-y-2">
        <p className="wb-eyebrow">{layer ? `${layer.label} · ` : ''}{job ? '作业' : '数据集'} · {node.namespace}</p>
        <h3 className="break-all text-base font-semibold text-slate-900">{node.name}</h3>
        {node.description && <p className="text-slate-600">{node.description}</p>}
        {job && <p className="text-slate-600">最近一次：<b>{statusLabel(node.status)}</b>{node.last_duration_seconds != null ? `，用时 ${node.last_duration_seconds} 秒` : ''}，{fmtTime(node.last_run_at)}</p>}
        {!job && node.source && <p className="text-slate-600">来源：{node.source}</p>}
        {!job && node.updated_at && <p className="text-slate-600">最近更新：{fmtTime(node.updated_at)}</p>}
        {link && <p><Link to={link.to} className="text-sm underline">{link.label}</Link></p>}
        {job && node.run_facets && Object.entries(node.run_facets).map(([name, facet]) => <p key={name} className="break-words text-xs text-slate-500">{Object.entries(facet).map(([k, v]) => `${k}: ${cell(v)}`).join(' · ')}</p>)}
      </div>
      <div className="space-y-3">
        {job && node.sql && <div><p className="text-xs font-medium text-slate-500">SQL</p><pre className="mt-1 max-h-44 overflow-auto rounded-lg bg-slate-900 p-3 text-[11px] leading-5 text-slate-100">{node.sql}</pre></div>}
        {job && (node.runs?.length ?? 0) > 0 && <div><p className="text-xs font-medium text-slate-500">运行记录</p><ul className="mt-1 space-y-1">{node.runs!.slice(0, 5).map(r => <li key={r.run_id} className="flex justify-between gap-2 text-xs"><span>{statusLabel(r.state === 'COMPLETE' ? 'success' : r.state === 'FAIL' ? 'failed' : r.state === 'ABORT' ? 'aborted' : 'running')}</span><span className="text-slate-500">{fmtTime(r.ended_at ?? r.started_at)}</span></li>)}</ul></div>}
        {!job && (node.fields?.length ?? 0) > 0 && <div><p className="text-xs font-medium text-slate-500">字段（{node.fields!.length}）</p>
          <ul className="mt-1 max-h-48 space-y-0.5 overflow-auto text-xs">{node.fields!.map(f => <li key={f.name} className="flex items-center justify-between gap-2"><span className="font-mono">{f.name}</span>
            <span className="flex items-center gap-2 text-slate-500">{cell(f.type)}{fieldImpact && <button type="button" data-testid={`field-impact-${f.name}`} aria-pressed={fieldImpact.field === f.name} className={`rounded border px-1.5 py-0.5 text-[11px] ${fieldImpact.field === f.name ? 'border-slate-900 bg-slate-900 text-white' : 'bg-white text-slate-700 hover:bg-slate-50'}`}
              title="这一列改了会影响哪些下游" onClick={() => fieldImpact.onAsk(f.name)}>影响</button>}</span></li>)}</ul></div>}
      </div>
      <div className="space-y-3">
        {!job && node.quality?.assertions && node.quality.assertions.length > 0 && <div><p className="text-xs font-medium text-slate-500">数据质量（{node.quality.passed} 通过，{node.quality.failed} 未通过）</p>
          <ul className="mt-1 max-h-48 space-y-1 overflow-auto text-xs">{node.quality.assertions.map((a, i) => <li key={i} className={`flex gap-2 ${a.success ? 'text-slate-700' : 'text-red-700'}`}><span aria-hidden="true">{a.success ? '✓' : '✕'}</span><span className="break-all">{a.assertion.replace(/^expect_/, '')}{a.column ? ` · ${a.column}` : ''}</span></li>)}</ul></div>}
        {!job && node.column_lineage && <div><p className="text-xs font-medium text-slate-500">列血缘</p><div className="max-h-40 overflow-auto">{Object.entries(node.column_lineage).map(([col, inputs]) => <p key={col} className="mt-1 text-xs"><span className="font-mono">{col}</span> ← <span className="text-slate-600">{inputs.slice(0, 8).join('，')}{inputs.length > 8 ? '…' : ''}</span></p>)}</div></div>}
      </div>
    </div>
    <div className="mt-4 border-t border-slate-100 pt-3">
      <button type="button" className="wb-button-secondary" disabled={impactBusy} onClick={onImpact}>{impactBusy ? '分析中…' : '影响分析：它改了会影响谁'}</button>
      {impact && <div className="mt-3" data-testid="impact-result">
        <p className="text-xs text-slate-600">下游共 {impact.downstream_jobs} 个作业、{impact.downstream_datasets} 个数据集；上游 {impact.upstream_count} 个节点。</p>
        {impact.downstream.length === 0 ? <p className="mt-1 text-xs text-slate-500">下游没有东西依赖它。</p> : <ol className="mt-2 grid gap-x-6 gap-y-1 text-xs md:grid-cols-2">{impact.downstream.map(d => <li key={d.id} className="flex items-center justify-between gap-2"><span className="break-all">{d.kind === 'job' ? '作业' : '数据集'} · {d.label}</span><span className="shrink-0 text-slate-500">第 {d.distance} 跳{d.quality_failed ? ` · ${d.quality_failed} 项检查未通过` : ''}</span></li>)}</ol>}
      </div>}
      {fieldImpact?.field && fieldImpact.result && <ColumnImpactView impact={fieldImpact.result} dataset={node.name} />}
    </div>
  </aside>
}
