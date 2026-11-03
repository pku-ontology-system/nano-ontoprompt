import type { Counts, Stats } from './types'

const basisOrder: Array<[string, string]> = [['declared_fk', '声明外键'], ['semantic_fk', '模型推断外键'], ['bridge_table', '桥表合成'], ['term', '术语（对象类型）']]

function Tile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return <div className="rounded-lg border bg-white px-3 py-2"><p className="text-[11px] text-slate-500">{label}</p><p className={`text-xl font-semibold ${tone ?? ''}`}>{value}</p></div>
}

export default function StatsPanel({ stats, categories }: { stats: Stats; categories: Array<{ id: string; label: string }> }) {
  const rows = basisOrder.flatMap(([key, label]) => stats.by_basis[key] ? [[key, label, stats.by_basis[key]] as [string, string, Counts]] : [])
  return <section className="wb-surface p-4" data-testid="ai-stats" aria-label="审阅统计">
    <div className="flex flex-wrap items-baseline justify-between gap-2"><p className="wb-eyebrow">质量统计</p>
      <p className="text-xs text-slate-500">{stats.decided} / {stats.total} 条已处理{stats.accept_rate !== null && `，采纳率 ${Math.round(stats.accept_rate * 100)}%`}</p></div>
    <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
      <Tile label="候选总数" value={stats.total} /><Tile label="待审" value={stats.pending} tone="text-slate-600" />
      <Tile label="已采纳" value={stats.accepted} tone="text-emerald-700" /><Tile label="修改后采纳" value={stats.modified} tone="text-blue-700" />
      <Tile label="已驳回" value={stats.rejected} tone="text-red-700" />
    </div>
    <table className="mt-3 w-full text-left text-xs"><thead><tr className="text-slate-500"><th className="py-1">判断方式</th><th>总数</th><th>采纳</th><th>修改后采纳</th><th>驳回</th><th>待审</th></tr></thead>
      <tbody>{rows.map(([key, label, counts]) => <tr key={key} className="border-t"><td className="py-1">{label}</td><td>{counts.total}</td><td>{counts.accepted}</td><td>{counts.modified}</td><td>{counts.rejected}</td><td>{counts.pending}</td></tr>)}</tbody></table>
    {Object.keys(stats.reject_reasons).length > 0 && <p className="mt-2 text-xs text-slate-600">驳回原因：{Object.entries(stats.reject_reasons).map(([key, count]) => `${categories.find(row => row.id === key)?.label ?? key} ${count}`).join('、')}</p>}
  </section>
}
