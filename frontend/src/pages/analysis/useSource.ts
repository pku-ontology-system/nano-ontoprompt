import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { analysisApi } from '@/api/analysis'
import { ontologyApi } from '@/api/ontologies'
import { readStoredOntology } from '@/components/navigation'

/**
 * Where an analysis page gets its data: a demo dataset, or the objects of a platform ontology. The choice lives in the URL
 * (?source=ontology&ontology=<id>&type=<ObjectType>) so a link can open a page on a given type. Nothing is copied into state: what is
 * shown is derived from the URL and the lists, so a type that disappears simply falls back to the next one.
 */
export function useSourceMode() {
  const [params, setParams] = useSearchParams()
  const mode: 'demo' | 'ontology' = params.get('source') === 'ontology' || params.get('ontology') ? 'ontology' : 'demo'
  const setMode = (next: 'demo' | 'ontology') => {
    const p = new URLSearchParams(params)
    if (next === 'ontology') p.set('source', 'ontology')
    else { p.delete('source'); p.delete('ontology'); p.delete('type') }
    setParams(p, { replace: true })
  }
  return { mode, setMode }
}

export function useOntologyChoice(enabled: boolean, { withTypes = true } = {}) {
  const [params, setParams] = useSearchParams()
  const list = useQuery({ queryKey: ['ontologies', 'analysis-picker'], queryFn: () => ontologyApi.list({ page_size: 200 }).then(r => r.items), enabled })
  const items = list.data ?? []
  const wanted = params.get('ontology') ?? ''
  const stored = readStoredOntology()
  const ontologyId = items.some(i => i.id === wanted) ? wanted : items.some(i => i.id === stored) ? stored! : items[0]?.id ?? ''
  const types = useQuery({ queryKey: ['analysis', 'types', ontologyId], queryFn: () => analysisApi.ontologyTypes(ontologyId), enabled: enabled && withTypes && !!ontologyId })
  const all = types.data ?? []
  const wantedType = params.get('type') ?? ''
  const objectType = all.some(t => t.api_name === wantedType) ? wantedType : (all.find(t => (t.object_count ?? 0) > 0) ?? all[0])?.api_name ?? ''
  const change = (next: Record<string, string>) => {
    const p = new URLSearchParams(params)
    p.set('source', 'ontology')
    for (const [key, value] of Object.entries(next)) { if (value) p.set(key, value); else p.delete(key) }
    setParams(p, { replace: true })
  }
  return {
    ontologies: items, ontologyId, types: all, objectType, selected: all.find(t => t.api_name === objectType) ?? null,
    loading: list.isLoading || types.isLoading, error: list.error ?? types.error,
    setOntology: (id: string) => change({ ontology: id, type: '' }), setType: (type: string) => change({ ontology: ontologyId, type }),
  }
}

export type OntologyChoice = ReturnType<typeof useOntologyChoice>
