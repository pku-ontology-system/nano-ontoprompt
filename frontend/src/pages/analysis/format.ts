import type { Tool } from '@/api/analysis'

export const num = (n: number | null | undefined) => (n == null ? '—' : n.toLocaleString('zh-CN'))
export const pct = (n: number | null | undefined, digits = 1) => (n == null ? '—' : `${(n * 100).toFixed(digits)}%`)
export const size = (bytes: number | null | undefined) => (bytes == null ? '' : bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`)
export const cell = (value: unknown) => (value == null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value))

export const TOOL_LABEL: Record<Tool, string> = { splink: 'Splink', great_expectations: 'Great Expectations', marquez: 'Marquez / OpenLineage' }
