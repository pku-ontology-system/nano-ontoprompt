import { countText } from './format'
import { ErrorNote } from './shared'
import type { OntologyChoice } from './useSource'

export function SourceTabs({ mode, onChange }: { mode: 'demo' | 'ontology'; onChange: (mode: 'demo' | 'ontology') => void }) {
  const tab = (value: 'demo' | 'ontology', label: string) => <button type="button" role="tab" aria-selected={mode === value} data-testid={`source-${value}`} onClick={() => onChange(value)}
    className={`rounded-lg border px-3 py-1.5 text-sm ${mode === value ? 'border-slate-900 bg-slate-900 text-white' : 'bg-white hover:bg-slate-50'}`}>{label}</button>
  return <div role="tablist" aria-label="数据来源" className="mb-4 flex gap-1.5">{tab('ontology', '本体里的对象')}{tab('demo', '开源项目的示例文件')}</div>
}

/** Every ontology the caller can see, as cards (the ones with no objects are dimmed: there is nothing to analyse in them), then the types of the chosen one. */
export function OntologyPicker({ choice, onlyOntology = false }: { choice: OntologyChoice; onlyOntology?: boolean }) {
  return <div>
    <p className="text-sm text-slate-700">选择本体 <span className="text-xs text-slate-500">（共 {choice.ontologies.length} 个，和“本体库”里是同一批）</span></p>
    <div role="radiogroup" aria-label="本体" className="mt-2 grid max-h-64 gap-2 overflow-auto sm:grid-cols-2 lg:grid-cols-3">
      {choice.ontologies.map(o => {
        const selected = o.id === choice.ontologyId
        return <button key={o.id} type="button" role="radio" aria-checked={selected} data-testid={`ontology-${o.id}`} onClick={() => choice.setOntology(o.id)}
          className={`wb-choice-card text-left ${selected ? 'ring-2 ring-[var(--pku-red-action)]' : ''} ${o.objects === 0 ? 'opacity-60' : ''}`}>
          <span className="flex flex-wrap items-center gap-2"><span className="truncate text-sm font-semibold text-slate-900" title={o.name}>{o.name}</span>
            {o.imported && <span className="wb-status wb-status-success">数据导入</span>}</span>
          <span className="mt-1 block text-xs text-slate-500">{o.domain} · {o.types} 个类型 · {o.objects.toLocaleString('zh-CN')} 个对象 · {o.link_types} 种关系</span>
        </button>
      })}
      {!choice.loading && choice.ontologies.length === 0 && <p className="text-sm text-slate-500">还没有本体：到“数据源”导入一份数据。</p>}
    </div>
    {!onlyOntology && <div role="radiogroup" aria-label="对象类型" className="mt-4 grid gap-3 md:grid-cols-2 lg:grid-cols-3">
      {choice.types.map(t => {
        const selected = t.api_name === choice.objectType
        return <button key={t.api_name} type="button" role="radio" aria-checked={selected} data-testid={`type-${t.api_name}`} onClick={() => choice.setType(t.api_name)}
          className={`wb-choice-card text-left ${selected ? 'ring-2 ring-[var(--pku-red-action)]' : ''}`}>
          <span className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold text-slate-900">{t.api_name}</span>
            {t.object_count === 0 ? <span className="wb-status wb-status-warning">没有对象</span> : <span className="text-xs text-slate-500">{countText(t)}</span>}</span>
          <span className="mt-1 block text-xs text-slate-500">{t.properties.length} 个属性{(t.object_count ?? 0) > 200 && ` · 对象较多，读取约 ${Math.max(2, Math.ceil((t.object_count ?? 0) / 400))} 秒`}</span>
          <span className="mt-1 block truncate font-mono text-[11px] text-slate-500" title={t.properties.map(p => p.api_name).join(', ')}>{t.properties.map(p => p.api_name).join(', ')}</span>
        </button>
      })}
      {!choice.loading && choice.types.length === 0 && <p className="text-sm text-slate-500">这个本体里还没有对象类型。</p>}
    </div>}
    <div className="mt-2"><ErrorNote error={choice.error} /></div>
  </div>
}
