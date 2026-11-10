import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { analysisApi, type OntologyType } from '@/api/analysis'
import { readStoredOntology } from '@/components/navigation'

/**
 * Where an analysis page gets its data: the objects of a platform ontology (the default), or one of the open-source projects' sample files.
 * The choice lives in the URL (?source=demo, or ?ontology=<id>&type=<ObjectType>) so a link can open a page on a given type. Nothing is copied
 * into state: what is shown is derived from the URL and the lists, so a type that disappears simply falls back to the next one.
 */
export function useSourceMode() {
  const [params, setParams] = useSearchParams()
  const mode: 'demo' | 'ontology' = params.get('source') === 'demo' ? 'demo' : 'ontology'
  const setMode = (next: 'demo' | 'ontology') => {
    const p = new URLSearchParams(params)
    if (next === 'demo') { p.set('source', 'demo'); p.delete('ontology'); p.delete('type') }
    else p.delete('source')
    setParams(p, { replace: true })
  }
  return { mode, setMode }
}

const IDENTIFYING = /(^|_)(name|title|label|email|phone|address|code|sku|model|serial)(_|$)/i

/** The type to open first: one with objects, of a size that reads quickly, that has something to tell individuals apart (so all three analyses have something to say). */
function bestType(types: OntologyType[]): OntologyType | undefined {
  const rank = (t: OntologyType) => {
    const n = t.object_count ?? 0
    if (n === 0) return 9
    return (t.properties.some(p => p.data_type === 'string' && IDENTIFYING.test(p.api_name)) ? 0 : 1) + (n > 3000 ? 2 : 0) + (n < 3 ? 1 : 0)
  }
  return types.reduce<OntologyType | undefined>((best, t) => (best === undefined || rank(t) < rank(best) ? t : best), undefined)
}

export function useOntologyChoice(enabled: boolean, { withTypes = true } = {}) {
  const [params, setParams] = useSearchParams()
  const overview = useQuery({ queryKey: ['platform-overview'], queryFn: analysisApi.overview, enabled })
  // the ontologies that have objects come first, the biggest first: they are what the analyses can work on
  const items = [...(overview.data?.ontologies ?? [])].sort((a, b) => Number(b.objects > 0) - Number(a.objects > 0) || b.objects - a.objects)
  const wanted = params.get('ontology') ?? ''
  const stored = readStoredOntology()
  const ontologyId = items.some(i => i.id === wanted) ? wanted : items.some(i => i.id === stored && i.objects > 0) ? stored! : items[0]?.id ?? ''
  const types = useQuery({ queryKey: ['analysis', 'types', ontologyId], queryFn: () => analysisApi.ontologyTypes(ontologyId), enabled: enabled && withTypes && !!ontologyId })
  const all = types.data ?? []
  const wantedType = params.get('type') ?? ''
  const objectType = all.some(t => t.api_name === wantedType) ? wantedType : bestType(all)?.api_name ?? ''
  const change = (next: Record<string, string>) => {
    const p = new URLSearchParams(params)
    p.delete('source')
    for (const [key, value] of Object.entries(next)) { if (value) p.set(key, value); else p.delete(key) }
    setParams(p, { replace: true })
  }
  return {
    ontologies: items, ontologyId, types: all, objectType, selected: all.find(t => t.api_name === objectType) ?? null,
    loading: overview.isLoading || types.isLoading, error: overview.error ?? types.error,
    setOntology: (id: string) => change({ ontology: id, type: '' }), setType: (type: string) => change({ ontology: ontologyId, type }),
  }
}

export type OntologyChoice = ReturnType<typeof useOntologyChoice>
