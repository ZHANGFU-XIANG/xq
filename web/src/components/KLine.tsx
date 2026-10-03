import { useQuery } from '@tanstack/react-query'
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { get } from '../lib/api'
import { Loading } from './ui'

type Candle = { time: string; open: number; high: number; low: number; close: number; volume: number; ma5: number | null; ma20: number | null; ma60: number | null; inst: number | null }
type KData = { candles: Candle[]; markers: { time: string; side: string; shares: number; price: number }[]; lines: { label: string; price: number; kind: string }[] }

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

const RANGES = [
  { label: '3 個月', days: 65 },
  { label: '6 個月', days: 130 },
  { label: '1 年', days: 260 },
]

export default function KLine({ code, height = 380 }: { code: string; height?: number }) {
  const [days, setDays] = useState(130)
  const [showMA, setShowMA] = useState(true)
  const [lower, setLower] = useState<'volume' | 'inst'>('volume')
  const box = useRef<HTMLDivElement>(null)
  const { data, isLoading, error } = useQuery<KData>({ queryKey: ['kline', code], queryFn: () => get(`/api/stock/${code}/kline?days=260`) })

  useEffect(() => {
    if (!box.current || !data?.candles.length) return
    const up = css('--color-up')
    const down = css('--color-down')
    const ink = css('--color-ink-2')
    const line = css('--color-line')
    const chart: IChartApi = createChart(box.current, {
      height,
      layout: { background: { color: 'transparent' }, textColor: ink, fontFamily: css('--font-sans') },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line },
      localization: { locale: 'zh-TW' },
      autoSize: true,
    })
    const rows = data.candles.slice(-days)
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
    })
    candle.setData(rows.map((r) => ({ time: r.time as Time, open: r.open, high: r.high, low: r.low, close: r.close })))
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.28 } })

    if (showMA) {
      const maColors: Record<string, string> = { ma5: '#C08A2B', ma20: css('--color-link'), ma60: css('--color-offense') }
      for (const k of ['ma5', 'ma20', 'ma60'] as const) {
        const s = chart.addSeries(LineSeries, { color: maColors[k], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
        s.setData(rows.filter((r) => r[k] != null).map((r) => ({ time: r.time as Time, value: r[k] as number })))
      }
    }

    const hist = chart.addSeries(HistogramSeries, { priceScaleId: 'lower', priceLineVisible: false, lastValueVisible: false })
    chart.priceScale('lower').applyOptions({ scaleMargins: { top: 0.76, bottom: 0 } })
    hist.setData(
      rows.map((r) => {
        const v = lower === 'volume' ? r.volume : r.inst ?? 0
        const pos = lower === 'volume' ? r.close >= r.open : v >= 0
        return { time: r.time as Time, value: v, color: pos ? `${up}99` : `${down}99` }
      }),
    )

    const kindColor: Record<string, string> = { entry: css('--color-link'), stop: down, tp: up, high: ink }
    for (const l of data.lines) {
      candle.createPriceLine({
        price: l.price, title: l.label, color: kindColor[l.kind] ?? ink, lineWidth: 1,
        lineStyle: l.kind === 'high' ? LineStyle.Dotted : LineStyle.Dashed, axisLabelVisible: true,
      })
    }
    const first = rows[0]?.time
    createSeriesMarkers(
      candle,
      data.markers
        .filter((m) => m.time >= first)
        .map((m) => ({
          time: m.time as Time,
          position: m.side === 'BUY' ? ('belowBar' as const) : ('aboveBar' as const),
          shape: m.side === 'BUY' ? ('arrowUp' as const) : ('arrowDown' as const),
          color: m.side === 'BUY' ? up : down,
          text: `${m.side === 'BUY' ? '買' : '賣'} ${m.shares.toLocaleString()}股@${m.price}`,
        })),
    )
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [data, days, showMA, lower, height])

  if (isLoading) return <Loading label="載入 K 線" />
  if (error || !data?.candles.length) return <div className="py-6 text-muted">查無 K 線資料</div>
  const last = data.candles[data.candles.length - 1]
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="期間">
          {RANGES.map((r) => (
            <button key={r.days} onClick={() => setDays(r.days)}
              className={`px-2.5 py-1 ${days === r.days ? 'bg-ink text-paper' : 'bg-paper text-ink-2 hover:bg-mist'}`}>
              {r.label}
            </button>
          ))}
        </div>
        <div className="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="下方圖表">
          {(['volume', 'inst'] as const).map((k) => (
            <button key={k} onClick={() => setLower(k)}
              className={`px-2.5 py-1 ${lower === k ? 'bg-ink text-paper' : 'bg-paper text-ink-2 hover:bg-mist'}`}>
              {k === 'volume' ? '成交量' : '法人買賣超'}
            </button>
          ))}
        </div>
        <label className="inline-flex items-center gap-1.5 text-ink-2">
          <input type="checkbox" checked={showMA} onChange={(e) => setShowMA(e.target.checked)} /> 均線
        </label>
        {showMA && (
          <span className="num flex gap-3 text-xs">
            <span style={{ color: '#C08A2B' }}>MA5 {last.ma5 ?? '—'}</span>
            <span className="text-link">MA20 {last.ma20 ?? '—'}</span>
            <span className="text-offense">MA60 {last.ma60 ?? '—'}</span>
          </span>
        )}
      </div>
      <div ref={box} style={{ height }} aria-label={`${code} K 線圖`} />
      <div className="mt-1 text-xs text-muted">虛線：建議限價（藍）、停損（綠）、停利（紅）；點線：52 週高點。箭頭為實際成交紀錄。單位：張</div>
    </div>
  )
}
