import { countText } from './format'
import { ErrorNote } from './shared'
import type { OntologyChoice } from './useSource'

export function SourceTabs({ mode, onChange }: { mode: 'demo' | 'ontology'; onChange: (mode: 'demo' | 'ontology') => void }) {
  const tab = (value: 'demo' | 'ontology', label: string) => <button type="button" role="tab" aria-selected={mode === value} data-testid={`source-${value}`} onClick={() => onChange(value)}
    className={`rounded-lg border px-3 py-1.5 text-sm ${mode === value ? 'border-slate-900 bg-slate-900 text-white' : 'bg-white hover:bg-slate-50'}`}>{label}</button>
  return <div role="tablist" aria-label="数据来源" className="mb-4 flex gap-1.5">{tab('demo', '演示数据')}{tab('ontology', '本体里的对象')}</div>
}

/** Pick an ontology and one of its object types. The types that have no objects are marked, because there is nothing to analyse in them. */
export function OntologyPicker({ choice, onlyOntology = false }: { choice: OntologyChoice; onlyOntology?: boolean }) {
  return <div>
    <label className="flex flex-wrap items-center gap-3 text-sm text-slate-700">本体
      <select aria-label="本体" className="wb-input w-auto min-w-64" value={choice.ontologyId} onChange={e => choice.setOntology(e.target.value)}>
        {choice.ontologies.length === 0 && <option value="">（没有本体）</option>}
        {choice.ontologies.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}</select></label>
    {!onlyOntology && <div role="radiogroup" aria-label="对象类型" className="mt-3 grid gap-3 md:grid-cols-2 lg:grid-cols-3">
      {choice.types.map(t => {
        const selected = t.api_name === choice.objectType
        return <button key={t.api_name} type="button" role="radio" aria-checked={selected} data-testid={`type-${t.api_name}`} onClick={() => choice.setType(t.api_name)}
          className={`wb-choice-card text-left ${selected ? 'ring-2 ring-[var(--pku-red-action)]' : ''}`}>
          <span className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold text-slate-900">{t.api_name}</span>
            {t.object_count === 0 ? <span className="wb-status wb-status-warning">没有对象</span> : <span className="text-xs text-slate-500">{countText(t)}</span>}</span>
          <span className="mt-1 block text-xs text-slate-500">{t.properties.length} 个属性</span>
          <span className="mt-1 block truncate font-mono text-[11px] text-slate-500" title={t.properties.map(p => p.api_name).join(', ')}>{t.properties.map(p => p.api_name).join(', ')}</span>
        </button>
      })}
      {!choice.loading && choice.types.length === 0 && <p className="text-sm text-slate-500">这个本体里还没有对象类型。</p>}
    </div>}
    <div className="mt-2"><ErrorNote error={choice.error} /></div>
  </div>
}
