import { useQuery } from '@tanstack/react-query'
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { get } from '../lib/api'
import { Loading } from './ui'
import { liveEvery } from '../lib/live'

type Candle = { time: string | number; open: number; high: number; low: number; close: number; volume: number; ma5: number | null; ma20: number | null; ma60: number | null; inst: number | null }
type KData = { intraday?: boolean; note?: string; candles: Candle[]; markers: { time: string; side: string; shares: number; price: number }[]; lines: { label: string; price: number; kind: string }[] }

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

const PERIODS = [
  { key: 'M', label: '月' }, { key: 'W', label: '週' }, { key: 'D', label: '日' },
  { key: '1', label: '1分' }, { key: '3', label: '3分' }, { key: '5', label: '5分' }, { key: '10', label: '10分' },
  { key: '15', label: '15分' }, { key: '30', label: '30分' }, { key: '60', label: '60分' },
]

const RANGES = [
  { label: '3 個月', days: 65 },
  { label: '6 個月', days: 130 },
  { label: '1 年', days: 260 },
  { label: '2 年', days: 520 },
]

type ChartState = { set: (d: KData) => void }

export default function KLine({ code, height = 380, url, unit = '張', futures = false, defaultPeriod = 'D' }: {
  code: string; height?: number; url?: string; unit?: string; futures?: boolean; defaultPeriod?: string
}) {
  const [days, setDays] = useState(130)
  const [period, setPeriod] = useState(url ? 'D' : defaultPeriod)
  const [showMA, setShowMA] = useState(true)
  const [lower, setLower] = useState<'volume' | 'inst'>('volume')
  const minute = /^\d+$/.test(period)
  const box = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ChartState | null>(null)
  const { data, isLoading, error } = useQuery<KData>({
    queryKey: ['kline', code, url, period],
    queryFn: () => get(url ?? `/api/stock/${code}/kline?period=${period}`),
    // 盤中：分 K 每 5 秒、日週月 K 每 10 秒（最後一根用證交所即時價）；盤後放慢
    refetchInterval: liveEvery(minute ? 5_000 : 10_000, 600_000),
  })

  // 圖表只在週期／顯示設定改變時重建；資料刷新只換資料（盤中每幾秒更新不閃爍、不重置縮放）
  useEffect(() => {
    if (!box.current) return
    const up = css('--color-up')
    const down = css('--color-down')
    const ink = css('--color-ink-2')
    const line = css('--color-line')
    const chart: IChartApi = createChart(box.current, {
      height,
      layout: { background: { color: 'transparent' }, textColor: ink, fontFamily: css('--font-sans') },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line, timeVisible: minute, secondsVisible: false },
      // 價格區下方留給成交量，低價股的刻度會延伸成負數；股價不會是負的，負刻度不顯示
      localization: { locale: 'zh-TW', priceFormatter: (p: number) => (p < 0 ? '' : p.toFixed(2)) },
      autoSize: true,
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
    })
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.28 } })
    const maColors: Record<string, string> = { ma5: '#C08A2B', ma20: css('--color-link'), ma60: css('--color-offense') }
    const maSeries = showMA
      ? (['ma5', 'ma20', 'ma60'] as const).map((k) => [k, chart.addSeries(LineSeries, {
          color: maColors[k], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        })] as const)
      : []
    const hist = chart.addSeries(HistogramSeries, { priceScaleId: 'lower', priceLineVisible: false, lastValueVisible: false })
    chart.priceScale('lower').applyOptions({ scaleMargins: { top: 0.76, bottom: 0 } })
    const kindColor: Record<string, string> = { entry: css('--color-link'), stop: down, tp: up, high: ink }
    const markers = createSeriesMarkers(candle, [])
    let priceLines: IPriceLine[] = []
    let fitted = false

    chartRef.current = {
      set: (d: KData) => {
        const rows = period === 'D' ? d.candles.slice(-days) : d.candles
        const t = (r: Candle) => r.time as Time
        candle.setData(rows.map((r) => ({ time: t(r), open: r.open, high: r.high, low: r.low, close: r.close })))
        for (const [k, s] of maSeries) s.setData(rows.filter((r) => r[k] != null).map((r) => ({ time: t(r), value: r[k] as number })))
        hist.setData(rows.map((r) => {
          const v = lower === 'volume' ? r.volume : r.inst ?? 0
          const pos = lower === 'volume' ? r.close >= r.open : v >= 0
          return { time: t(r), value: v, color: pos ? `${up}99` : `${down}99` }
        }))
        for (const pl of priceLines) candle.removePriceLine(pl)
        priceLines = d.lines.filter((l) => !minute || l.kind !== 'high').map((l) => candle.createPriceLine({
          price: l.price, title: l.label, color: kindColor[l.kind] ?? ink, lineWidth: 1,
          lineStyle: l.kind === 'high' ? LineStyle.Dotted : LineStyle.Dashed, axisLabelVisible: true,
        }))
        const first = rows[0]?.time
        markers.setMarkers(period !== 'D' ? [] : d.markers
          .filter((m) => first != null && m.time >= String(first))
          .map((m) => ({
            time: m.time as Time,
            position: m.side === 'BUY' ? ('belowBar' as const) : ('aboveBar' as const),
            shape: m.side === 'BUY' ? ('arrowUp' as const) : ('arrowDown' as const),
            color: m.side === 'BUY' ? up : down,
            text: `${m.side === 'BUY' ? '買' : '賣'} ${m.shares.toLocaleString()}股@${m.price}`,
          })))
        if (!fitted) {
          if (minute && rows.length > 150) chart.timeScale().setVisibleLogicalRange({ from: rows.length - 150, to: rows.length + 3 })
          else chart.timeScale().fitContent()
          fitted = true
        }
      },
    }
    return () => {
      chart.remove()
      chartRef.current = null
    }
  }, [period, days, showMA, lower, height, minute])

  useEffect(() => {
    if (data?.candles.length && chartRef.current) chartRef.current.set(data)
  }, [data, period, days, showMA, lower, height, minute])

  const last = data?.candles[data.candles.length - 1]
  const btn = (on: boolean) => `px-2.5 py-1 ${on ? 'bg-link text-white' : 'bg-paper text-ink-2 hover:bg-mist'}`
  return (
    <div>
      {!url && (
        <div className="mb-2 inline-flex flex-wrap overflow-hidden rounded-md border border-line text-sm" role="group" aria-label="K 線週期">
          {PERIODS.map((p) => (
            <button key={p.key} onClick={() => setPeriod(p.key)} className={btn(period === p.key)}>{p.label}</button>
          ))}
        </div>
      )}
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        {period === 'D' && (
          <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="期間">
            {RANGES.map((r) => <button key={r.days} onClick={() => setDays(r.days)} className={btn(days === r.days)}>{r.label}</button>)}
          </div>
        )}
        <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="下方圖表">
          {(futures || period !== 'D' ? (['volume'] as const) : (['volume', 'inst'] as const)).map((k) => (
            <button key={k} onClick={() => setLower(k)} className={btn(lower === k)}>{k === 'volume' ? '成交量' : '法人買賣超'}</button>
          ))}
        </div>
        <label className="inline-flex items-center gap-1.5 text-ink-2">
          <input type="checkbox" checked={showMA} onChange={(e) => setShowMA(e.target.checked)} /> 均線
        </label>
        {showMA && last && (
          <span className="num flex gap-3 text-xs">
            <span style={{ color: '#C08A2B' }}>MA5 {last.ma5 ?? '—'}</span>
            <span className="text-link">MA20 {last.ma20 ?? '—'}</span>
            <span className="text-offense">MA60 {last.ma60 ?? '—'}</span>
          </span>
        )}
        {last && <span className="num ml-auto text-sm font-medium">{last.close}</span>}
      </div>
      <div className="relative">
        <div ref={box} style={{ height }} aria-label={`${code} K 線圖`} />
        {(isLoading || error || !data?.candles.length) && (
          <div className="absolute inset-0 flex items-center justify-center bg-paper/70">
            {isLoading ? <Loading label="載入 K 線" /> : <span className="text-muted">查無 K 線資料</span>}
          </div>
        )}
      </div>
      <div className="mt-1 text-xs text-muted">
        虛線：建議限價（藍）、停損（綠）、停利（紅）{futures || minute ? '' : '；點線：52 週高點'}。
        {period === 'D' ? '箭頭為實際成交紀錄。' : ''}{data?.note ? `${data.note}。` : ''}
        單位：{unit}{futures ? '（期貨價位：點）' : ''}
      </div>
    </div>
  )
}
