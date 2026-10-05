import { useQuery } from '@tanstack/react-query'
import { get, type Json } from './api'

/** 台股盤中（週一～五 09:00～13:35，以使用者本機時間＝台灣時間判斷）。 */
export function isTradingNow(d = new Date()) {
  const hm = d.getHours() * 60 + d.getMinutes()
  return d.getDay() >= 1 && d.getDay() <= 5 && hm >= 540 && hm <= 815
}

/** 盤中用較短的更新間隔，盤後放慢（毫秒）。 */
export const liveEvery = (fast: number, slow: number) => () => (isTradingNow() ? fast : slow)

/** 即時報價：盤中每 5 秒、盤後每 60 秒更新（像看盤軟體一樣同步刷新）。 */
export function useQuotes(codes: string[]) {
  const key = [...new Set(codes)].sort().join(',')
  return useQuery<Json>({
    queryKey: ['quotes', key],
    queryFn: () => get(`/api/quotes?codes=${key}`),
    refetchInterval: (q) => ((q.state.data as Json)?.trading ? 5_000 : 60_000),
    refetchOnWindowFocus: true,
    staleTime: 5_000,
  })
}

/** 國際市場影響註記（美股、日股等，依歷史連動估算）。 */
export function useIntl(codes: string[]) {
  const key = [...new Set(codes)].sort().join(',')
  return useQuery<Json>({ queryKey: ['intl', key], queryFn: () => get(`/api/intl?codes=${key}`), enabled: !!key, staleTime: 600_000 })
}
