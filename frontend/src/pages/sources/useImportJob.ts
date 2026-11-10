import { useQuery } from '@tanstack/react-query'
import { importApi } from '@/api/imports'

/** An import job, polled until it is done or has failed. */
export function useImportJob(jobId: string | null) {
  return useQuery({
    queryKey: ['import-job', jobId],
    queryFn: () => importApi.job(jobId!),
    enabled: !!jobId,
    refetchInterval: query => (query.state.data && ['done', 'failed'].includes(query.state.data.status) ? false : 600),
  })
}
