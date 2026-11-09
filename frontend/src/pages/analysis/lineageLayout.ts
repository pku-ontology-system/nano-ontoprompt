import type { LineageEdge, LineageNode } from '@/api/analysis'

export const NODE_W = 172
export const NODE_H = 46
const COL_GAP = 44
const ROW_GAP = 20
const PAD = 24

export interface Placed { id: string; x: number; y: number }

/** Left-to-right layers by the longest path from a source; inside a layer, nodes sit near the average height of their inputs. */
export function layoutLineage(nodes: LineageNode[], edges: LineageEdge[]): { positions: Map<string, Placed>; width: number; height: number } {
  const known = new Set(nodes.map(n => n.id))
  const parents = new Map<string, string[]>(nodes.map(n => [n.id, []]))
  for (const e of edges) if (known.has(e.source) && known.has(e.target)) parents.get(e.target)!.push(e.source)
  const layer = new Map<string, number>()
  const visiting = new Set<string>()
  const depth = (id: string): number => {
    const cached = layer.get(id)
    if (cached !== undefined) return cached
    if (visiting.has(id)) return 0 // a cycle: stop here
    visiting.add(id)
    const inputs = parents.get(id) ?? []
    const value = inputs.length ? 1 + Math.max(...inputs.map(depth)) : 0
    visiting.delete(id)
    layer.set(id, value)
    return value
  }
  nodes.forEach(n => depth(n.id))
  const columns: string[][] = []
  for (const n of nodes) { const l = layer.get(n.id) ?? 0; (columns[l] ??= []).push(n.id) }
  const tallest = Math.max(1, ...columns.map(c => (c ?? []).length))
  const height = tallest * (NODE_H + ROW_GAP) - ROW_GAP
  const positions = new Map<string, Placed>()
  columns.forEach((column, c) => {
    const col = column ?? []
    const bary = (id: string) => { const ys = (parents.get(id) ?? []).map(p => positions.get(p)?.y ?? 0); return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 0 }
    col.sort((a, b) => (c === 0 ? 0 : bary(a) - bary(b)) || a.localeCompare(b))
    const top = PAD + (height - (col.length * (NODE_H + ROW_GAP) - ROW_GAP)) / 2
    col.forEach((id, i) => positions.set(id, { id, x: PAD + c * (NODE_W + COL_GAP), y: top + i * (NODE_H + ROW_GAP) }))
  })
  return { positions, width: PAD * 2 + columns.length * NODE_W + Math.max(0, columns.length - 1) * COL_GAP, height: PAD * 2 + height }
}

export function closure(start: string, edges: LineageEdge[]): Set<string> {
  const up = new Map<string, string[]>(), down = new Map<string, string[]>()
  for (const e of edges) { (down.get(e.source) ?? down.set(e.source, []).get(e.source)!).push(e.target); (up.get(e.target) ?? up.set(e.target, []).get(e.target)!).push(e.source) }
  const seen = new Set([start])
  for (const step of [down, up]) {
    const queue = [start]
    const visited = new Set([start])
    while (queue.length) { const id = queue.shift()!; for (const next of step.get(id) ?? []) if (!visited.has(next)) { visited.add(next); seen.add(next); queue.push(next) } }
  }
  return seen
}

export const STATUS_LABEL: Record<string, string> = { success: '成功', failed: '失败', aborted: '中止', running: '运行中', unknown: '未知' }
export const statusLabel = (s: string | null | undefined) => STATUS_LABEL[s ?? 'unknown'] ?? s ?? '—'
export const STATUS_COLOR: Record<string, string> = { success: '#059669', failed: '#dc2626', aborted: '#d97706', running: '#2563eb', unknown: '#94a3b8' }
