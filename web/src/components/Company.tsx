import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { get, post, type Json } from '../lib/api'
import { fmtNum, fmtPct, tone } from '../lib/format'
import { Loading, Section } from './ui'

function NewsItem({ n }: { n: Json }) {
  const [open, setOpen] = useState(false)
  const { data, isLoading } = useQuery<Json>({
    queryKey: ['mops-news', n.params],
    queryFn: () => post('/api/mops/news', n.params),
    enabled: open && !!n.params,
    staleTime: Infinity,
  })
  return (
    <li className="border-b border-line/60 py-2">
      <button onClick={() => setOpen(!open)} aria-expanded={open} className="flex w-full gap-3 text-left">
        <span className="num w-24 shrink-0 text-sm text-muted">{n.date}</span>
        <span className="leading-snug hover:text-link">{n.title}</span>
      </button>
      {open && (
        <div className="ml-0 mt-2 rounded-md bg-mist px-3 py-2 text-sm leading-relaxed md:ml-[6.75rem]">
          {isLoading ? '載入全文中' : data?.error ? data.error : (
            <>
              <div className="mb-1 text-xs text-muted">{data?.clause}・事實發生日 {data?.fact_date}</div>
              <div className="whitespace-pre-wrap">{data?.description}</div>
            </>
          )}
        </div>
      )}
    </li>
  )
}

export default function Company({ code }: { code: string }) {
  const { data: c, isLoading } = useQuery<Json>({ queryKey: ['company', code], queryFn: () => get(`/api/stock/${code}/company`), staleTime: 3600_000 })
  if (isLoading) return <Loading label="讀取公開資訊觀測站" />
  if (!c || c.error) return <Section title="公司概況"><p className="text-muted">{c?.error ?? '查無公司資料'}</p></Section>
  const b = c.basic
  const facts: [string, string | undefined][] = [
    ['董事長', b.chairman], ['總經理', b.president], ['成立', b.established], ['上市櫃', b.listed],
    ['實收資本額', b.capital], ['簽證會計師', b.auditor], ['股利發放', b.dividend_frequency],
  ]
  const q: Json[] = c.quarterly ?? []
  return (
    <Section title="公司概況" aside={<a href={c.mops_url} target="_blank" rel="noreferrer">公開資訊觀測站</a>}>
      {b.main_business && <p className="title mb-4 max-w-3xl text-[1.05rem] font-semibold leading-relaxed">{b.main_business}</p>}
      <dl className="mb-6 grid grid-cols-2 gap-x-6 gap-y-2 text-[0.95rem] sm:grid-cols-4">
        {facts.filter(([, v]) => v).map(([k, v]) => (
          <div key={k}><dt className="text-xs text-muted">{k}</dt><dd>{v}</dd></div>
        ))}
        {b.website && <div><dt className="text-xs text-muted">網站</dt><dd className="truncate"><a href={b.website} target="_blank" rel="noreferrer">{b.website.replace(/^https?:\/\//, '')}</a></dd></div>}
      </dl>

      {q.length > 0 && (
        <>
          <h3 className="mb-2 font-bold">近 {q.length} 季損益</h3>
          <div className="mb-6 overflow-x-auto">
            <table className="w-full min-w-[640px] text-[0.95rem]">
              <thead className="text-right text-sm text-muted">
                <tr className="border-b border-line">
                  <th className="py-2 text-left font-normal">季別</th><th className="font-normal">營收（億）</th><th className="font-normal">營收年增</th>
                  <th className="font-normal">毛利率</th><th className="font-normal">營益率</th><th className="font-normal">稅後淨利（億）</th>
                  <th className="font-normal">EPS</th><th className="font-normal">EPS 年增</th>
                </tr>
              </thead>
              <tbody className="num text-right">
                {[...q].reverse().map((r) => (
                  <tr key={r.quarter} className="border-b border-line/60">
                    <td className="py-1.5 text-left">{r.quarter}</td>
                    <td>{fmtNum(r.revenue_100m)}</td>
                    <td className={tone(r.revenue_yoy)}>{r.revenue_yoy != null ? fmtPct(r.revenue_yoy, 1) : '—'}</td>
                    <td>{r.gross_margin != null ? `${r.gross_margin.toFixed(1)}%` : '—'}</td>
                    <td>{r.op_margin != null ? `${r.op_margin.toFixed(1)}%` : '—'}</td>
                    <td className={tone(r.net_income_100m)}>{fmtNum(r.net_income_100m)}</td>
                    <td className="font-bold">{fmtNum(r.eps)}</td>
                    <td className={tone(r.eps_yoy)}>{r.eps_yoy != null ? `${r.eps_yoy > 0 ? '+' : ''}${r.eps_yoy.toFixed(2)}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="mb-6 grid gap-8 md:grid-cols-2">
        {c.revenue?.length > 0 && (
          <div>
            <h3 className="mb-2 font-bold">月營收（仟元）</h3>
            <ul className="space-y-1 text-[0.95rem]">
              {c.revenue.slice(0, 5).map((r: Json) => (
                <li key={r.label} className="flex justify-between gap-3 border-b border-line/60 py-1">
                  <span>{r.label}</span>
                  <span className="num">{r.value?.toLocaleString('zh-TW')} <span className={tone(r.growth_pct)}>（{fmtPct(r.growth_pct, 2)}）</span></span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {c.financial?.periods?.length > 0 && (
          <div>
            <h3 className="mb-2 font-bold">最新財報摘要（{c.financial.unit}）</h3>
            <table className="w-full text-sm">
              <thead className="text-right text-muted"><tr><th className="text-left font-normal" />{c.financial.periods.map((p: string) => <th key={p} className="font-normal">{p}</th>)}</tr></thead>
              <tbody className="num text-right">
                {[...c.financial.income, ...c.financial.balance.filter((r: Json) => r.item.includes('每股淨值'))].map((r: Json) => (
                  <tr key={r.item} className="border-b border-line/60"><td className="py-1 text-left">{r.item}</td>{r.raw.map((v: string, i: number) => <td key={i}>{v}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {c.news?.length > 0 && (
        <>
          <h3 className="mb-1 font-bold">重大訊息</h3>
          <p className="mb-2 text-xs text-muted">點標題展開全文</p>
          <ul className="mb-6">{c.news.slice(0, 10).map((n: Json, i: number) => <NewsItem key={i} n={n} />)}</ul>
        </>
      )}

      <div className="grid gap-6 text-[0.95rem] md:grid-cols-2">
        {Object.keys(c.dividend ?? {}).length > 0 && (
          <div>
            <h3 className="mb-1 font-bold">最近股利</h3>
            <p className="text-ink-2">
              {c.dividend.dividendBelongsYear}　{c.dividend.resolutionProgress}
              {c.dividend.cashDividendsDistributed && <>　現金股利 <b className="num">{c.dividend.cashDividendsDistributed}</b> 元</>}
              {c.dividend.cashDividendPaymentDate && <>　發放日 {c.dividend.cashDividendPaymentDate}</>}
            </p>
          </div>
        )}
        {c.insider?.length > 0 && (
          <div>
            <h3 className="mb-1 font-bold">董監持股</h3>
            <p className="num text-ink-2">{c.insider[0].period}：{c.insider[0].lots} 張（持股比 {c.insider[0].ratio}，變動 {c.insider[0].change_pct}）</p>
          </div>
        )}
      </div>
    </Section>
  )
}
