import { useQuery } from '@tanstack/react-query'
import { get, type Json } from '../lib/api'
import { fmtInt, fmtRatio, tone } from '../lib/format'
import { liveEvery } from '../lib/live'
import { Section } from './ui'

const LEVEL: Record<string, string> = { 嚴重: 'border-up bg-up/5', 注意: 'border-warn bg-warn/5', 重要: 'border-up bg-up/5' }

/** 風險儀表板：壓力測試、期貨追繳點位、VaR、到 11/27 的模擬、黑天鵝指標、未來注意事項、資料檢查。只提醒，不改建議。 */
export default function RiskDashboard() {
  const { data: d } = useQuery<Json>({ queryKey: ['risk'], queryFn: () => get('/api/risk'), refetchInterval: liveEvery(30_000, 300_000) })
  if (!d?.stress) return null
  const m = d.margin
  const v = d.var ?? {}
  const g = d.data_guard
  return (
    <Section title="風險儀表板" aside={<span className="text-sm text-muted">{d.policy}・{d.time}</span>}>
      {g && g.level !== 'ok' && (
        <div className={`mb-3 rounded-lg border-l-4 px-4 py-2 text-sm ${g.level === 'severe' ? 'border-up bg-up/5' : 'border-warn bg-warn/5'}`}>
          <span className="font-bold">{g.level === 'severe' ? '資料不完整，勿依此下單' : '資料已自動校正'}</span>：{g.issues.map((i: Json) => i.msg).join('；')}
        </div>
      )}
      {d.alerts?.length > 0 && (
        <ul className="mb-3 space-y-2">
          {d.alerts.map((a: Json) => (
            <li key={a.key} className={`rounded-lg border-l-4 px-4 py-2 ${LEVEL[a.level] ?? LEVEL['注意']}`}>
              <div className="font-bold">［{a.level}］{a.title}</div><div className="text-sm text-ink-2">{a.body}</div>
            </li>
          ))}
        </ul>
      )}
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        {m && (
          <div className="rounded-lg border border-line p-3">
            <div className="text-sm text-muted">期貨距追繳</div>
            <div className={`num text-xl font-bold ${m.pct_to_call < 3 ? 'text-up' : m.pct_to_call < 6 ? 'text-warn' : ''}`}>{m.pct_to_call}%</div>
            <div className="text-xs text-muted">台指 {fmtInt(m.call_level)} 點會被追繳（再跌 {fmtInt(m.points_to_call)} 點）</div>
          </div>
        )}
        <div className="rounded-lg border border-line p-3">
          <div className="text-sm text-muted">單日風險值（95%）</div>
          <div className="num text-xl font-bold text-up">−{fmtInt(v.var95)}</div>
          <div className="text-xs text-muted">總資產 {v.var95_pct}%・最壞 5% 日子平均 −{fmtInt(v.cvar95)}</div>
        </div>
        {v.end && (
          <div className="rounded-lg border border-line p-3">
            <div className="text-sm text-muted">到 11/27 收正機率（部位不變）</div>
            <div className="num text-xl font-bold">{fmtRatio(v.end.p_positive, 0)}</div>
            <div className="text-xs text-muted">中位 {v.end.median > 0 ? '+' : ''}{v.end.median}%・最差 10% {v.end.p10}%・剩 {v.days_left} 交易日</div>
          </div>
        )}
        <div className="rounded-lg border border-line p-3">
          <div className="text-sm text-muted">目前累積報酬</div>
          <div className={`num text-xl font-bold ${tone(d.return_pct)}`}>{d.return_pct > 0 ? '+' : ''}{d.return_pct}%</div>
          <div className="text-xs text-muted">總資產 {fmtInt(d.nav)}</div>
        </div>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="overflow-x-auto">
          <div className="mb-1 text-sm font-medium">壓力測試（估計損益）</div>
          <table className="w-full min-w-[420px] text-sm">
            <thead className="text-left text-muted"><tr className="border-b border-line"><th className="py-1 font-normal">情境</th>
              <th className="text-right font-normal">股票</th><th className="text-right font-normal">期貨</th><th className="text-right font-normal">合計</th><th className="text-right font-normal">跌後報酬率</th></tr></thead>
            <tbody>
              {d.stress.map((s: Json) => (
                <tr key={s.scenario} className="border-b border-line/60">
                  <td className="py-1">{s.scenario}</td>
                  <td className={`num text-right ${tone(s.stock)}`}>{fmtInt(s.stock)}</td>
                  <td className={`num text-right ${tone(s.futures)}`}>{fmtInt(s.futures)}</td>
                  <td className={`num text-right font-medium ${tone(s.total)}`}>{fmtInt(s.total)}（{s.pct_nav}%）</td>
                  <td className={`num text-right ${tone(s.return_after)}`}>{s.return_after}%</td>
                </tr>
              ))}
            </tbody>
          </table>
          {m?.text && <p className="mt-1 text-xs text-muted">{m.text}</p>}
          {v.note && <p className="mt-1 text-xs text-muted">{v.note}</p>}
        </div>
        <div>
          <div className="mb-1 text-sm font-medium">未來 5 個交易日注意事項</div>
          {d.calendar?.length ? (
            <ul className="mb-3 space-y-1 text-sm">
              {d.calendar.map((e: Json, i: number) => (
                <li key={i}><span className="num mr-2 text-muted">{e.date.slice(5).replace('-', '/')}</span>
                  <span className={e.level === '重要' ? 'font-bold text-up' : 'font-medium'}>{e.title}</span>
                  <div className="ml-12 text-xs text-ink-2">{e.note}</div></li>
              ))}
            </ul>
          ) : <p className="mb-3 text-sm text-muted">沒有特別事件。</p>}
          <div className="mb-1 text-sm font-medium">黑天鵝指標（26 年回測，隔日開盤才動作）</div>
          <ul className="space-y-1 text-sm">
            {(d.black_swan?.items ?? []).map((b: Json) => (
              <li key={b.name}>
                <span className={`mr-1 inline-block h-2 w-2 rounded-full ${b.active ? 'bg-up' : 'bg-line'}`} />
                <span className={b.active ? 'font-bold' : ''}>{b.name}</span>
                <span className={`ml-1 text-xs ${b.edge ? 'text-up' : 'text-muted'}`}>{b.label}</span>
                <div className="ml-3 text-xs text-muted">{b.text}</div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Section>
  )
}
