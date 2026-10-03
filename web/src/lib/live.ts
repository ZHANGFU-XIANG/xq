import { useQuery } from '@tanstack/react-query'
import { get, type Json } from './api'

/** 即時報價：盤中每 10 秒、盤後每 60 秒更新（像看盤軟體一樣同步刷新）。 */
export function useQuotes(codes: string[]) {
  const key = [...new Set(codes)].sort().join(',')
  return useQuery<Json>({
    queryKey: ['quotes', key],
    queryFn: () => get(`/api/quotes?codes=${key}`),
    refetchInterval: (q) => ((q.state.data as Json)?.trading ? 10_000 : 60_000),
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  })
}

/** 國際市場影響註記（美股、日股等，依歷史連動估算）。 */
export function useIntl(codes: string[]) {
  const key = [...new Set(codes)].sort().join(',')
  return useQuery<Json>({ queryKey: ['intl', key], queryFn: () => get(`/api/intl?codes=${key}`), enabled: !!key, staleTime: 600_000 })
}
