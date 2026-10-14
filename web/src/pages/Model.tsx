import { useQuery } from '@tanstack/react-query'
import SystemHealth from '../components/Health'
import { BasketTag, Bullets, Empty, ErrorNote, Loading, PageTitle, Section, Stat } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtRatio, tone } from '../lib/format'

function DataSources() {
  const { data: d } = useQuery<Json>({ queryKey: ['data-sources'], queryFn: () => get('/api/data-sources'), refetchInterval: 120_000 })
  if (!d) return null
  return (
    <Section title="資料來源狀態" aside={d.finmind_available ? 'FinMind 可用' : 'FinMind 額度用完，改用官方來源'}>
      <p className="mb-2 text-sm text-ink-2">
        FinMind token {d.finmind_tokens} 組（另有匿名額度），輪流使用；額度用完的會冷卻 1 小時，期間法人、月營收、本益比、融資券改由證交所與櫃買中心官方資料提供。
        {d.finmind_cooling?.length > 0 && `冷卻中：${d.finmind_cooling.map((c: Json) => `${c.token}（到 ${c.until}）`).join('、')}`}
      </p>
      {d.sources?.length > 0 ? (
        <ul className="space-y-1 text-[0.95rem]">
          {d.sources.map((s: Json) => (
            <li key={s.source} className="flex flex-wrap gap-x-3">
              <span className="font-medium">{s.source}</span>
              <span className="text-down">{s.ok_at ? `最近成功 ${s.ok_at}` : ''}</span>
              {s.fail_at && <span className="text-warn">最近失敗 {s.fail_at}：{s.msg}</span>}
            </li>
          ))}
        </ul>
      ) : <Empty>伺服器剛啟動，尚未呼叫任何資料來源。</Empty>}
    </Section>
  )
}

/** 調整紀錄依日期合併：同一天只顯示一列；有修改就列出修改，健康檢查只顯示次數。 */
function groupByDate(log: Json[]) {
  const by = new Map<string, { date: string; changes: Json[]; issues: Json[]; notes: string[]; checks: number }>()
  for (const e of log) {
    const g = by.get(e.date) ?? { date: e.date, changes: [] as Json[], issues: [] as Json[], notes: [] as string[], checks: 0 }
    g.changes.push(...(e.changes ?? []))
    g.issues.push(...(e.issues ?? []))
    g.checks += e.checks ?? 1
    if (e.changes?.length && e.note) g.notes.push(e.note)
    by.set(e.date, g)
  }
  return [...by.values()].sort((x, y) => (x.date < y.date ? 1 : -1))
}

/** 策略調參 Agent：各演算法十年樣本外回測（主策略以年化 90% 為達標線）。 */
function Scorecard({ card }: { card: Json }) {
  if (!card?.algorithms?.length) return null
  const pf = (v: number | null | undefined) => (v == null ? '—' : fmtRatio(v, 1, true))
  return (
    <Section title="演算法回測總表（策略調參 Agent）" aside={`更新 ${String(card.generated ?? '').replace('T', ' ')}`}>
      <p className="mb-3 text-sm text-ink-2">
        每週（與重大失誤時）用近 5 年樣本外回測檢查參數，模擬「盤後算預測、隔日開盤成交」（實戰做得到的方式）。當沖、期貨只在前半段挑參數、後半段不能變差才採用；投資組合參數因近 5 年模擬對參數太敏感，只列候選，須經下方全歷史回測確認才改。主策略達標線：年化 {fmtRatio(card.target_cagr, 0)}。
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line"><th className="py-2 font-normal">演算法</th><th className="font-normal">指標</th><th className="text-right font-normal">回測結果</th><th className="font-normal pl-3">判定</th></tr></thead>
          <tbody>
            {card.algorithms.map((a: Json) => (
              <tr key={a.name} className="border-b border-line/60 align-top">
                <td className="py-1.5">{a.name}{a.changed && <span className="ml-1 text-xs text-warn">本次已調參</span>}</td>
                <td className="text-sm text-ink-2">{a.error ? '—' : a.metric}</td>
                <td className={`num text-right ${tone(a.value)}`}>
                  {a.error ? '—' : a.metric?.includes('%') ? `${a.value > 0 ? '+' : ''}${a.value?.toFixed(2)}%` : pf(a.value)}
                  {a.worst != null && <div className="text-xs text-muted">最差起始日 {pf(a.worst)}・回檔 {fmtRatio(a.max_dd, 0)}</div>}
                  {a.candidate && <div className="text-xs text-muted">候選（未採用）：{Object.entries(a.candidate).map(([k, v]) => `${k}=${v}`).join('、')}</div>}
                </td>
                <td className="pl-3 text-sm">
                  {a.error ? <span className="text-warn">失敗：{a.error}</span>
                    : a.meets_target != null ? (a.meets_target ? <span className="text-up">達標</span> : <span className="text-warn">未達 90%</span>)
                    : a.has_edge ? <span className="text-up">有正報酬</span> : <span className="text-muted">{a.note}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

/** 全歷史回測：所有查得到的日 K（約 2002 年起樣本外），比較實戰可行的隔日開盤成交與舊的同日收盤假設。 */
function FullHistory({ r }: { r: Json }) {
  if (!r?.next_open) return null
  const pf = (v: number | null | undefined, sign = true) => (v == null ? '—' : fmtRatio(v, 1, sign))
  const rows: [string, Json][] = [['主策略・隔日開盤成交（實戰）', r.next_open], ['主策略・同日收盤成交（舊假設，做不到）', r.close_same_day],
    ['加權指數（不含息）', r.next_open.taiex]]
  if (r.next_open_tuned) rows.splice(1, 0, ['主策略・全歷史調參候選', r.next_open_tuned])
  const years = Object.keys(r.next_open.yearly ?? {})
  return (
    <Section title="全歷史回測（所有查得到的時間）" aside={`${r.next_open.from}～${r.next_open.to}・${r.codes} 檔`}>
      <p className="mb-3 text-sm text-ink-2">
        樣本外預測（模型每年只用當時以前的資料重訓）＋當時的流動性篩選＋競賽來回成本 0.585%。「38 日」＝把歷史上每一段 38 個交易日（一次競賽長度）都算一次。{r.note}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-[0.95rem]">
          <thead className="text-left text-sm text-muted"><tr className="border-b border-line">
            <th className="py-2 font-normal">情境</th><th className="text-right font-normal">年化</th><th className="text-right font-normal">最大回檔</th>
            <th className="text-right font-normal">38 日收正機率</th><th className="text-right font-normal">38 日中位</th><th className="text-right font-normal">38 日最差 10%</th></tr></thead>
          <tbody>
            {rows.map(([name, x]) => (
              <tr key={name} className="border-b border-line/60">
                <td className="py-1.5">{name}</td>
                <td className={`num text-right ${tone(x.cagr)}`}>{pf(x.cagr)}</td>
                <td className="num text-right">{pf(x.max_dd, false)}</td>
                <td className="num text-right">{x.window?.p_positive != null ? fmtRatio(x.window.p_positive, 0) : '—'}</td>
                <td className={`num text-right ${tone(x.window?.median)}`}>{pf(x.window?.median)}</td>
                <td className={`num text-right ${tone(x.window?.p10)}`}>{pf(x.window?.p10)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {r.calibration?.buckets && (
        <div className="mt-4 overflow-x-auto">
          <div className="mb-1 text-sm font-medium">推薦的真實勝率（每天依模型排名分組，隔日開盤買、{r.calibration.horizon} 天後賣、扣成本）</div>
          <table className="w-full min-w-[520px] text-sm">
            <thead className="text-left text-muted"><tr className="border-b border-line"><th className="py-1 font-normal">當天排名</th><th className="text-right font-normal">筆數</th>
              <th className="text-right font-normal">賺錢機率</th><th className="text-right font-normal">平均報酬</th><th className="text-right font-normal">中位數</th><th className="text-right font-normal">贏大盤機率</th></tr></thead>
            <tbody>
              {r.calibration.buckets.map((b: Json) => (
                <tr key={b.rank} className="border-b border-line/60">
                  <td className="py-1">第 {b.rank} 名</td><td className="num text-right">{b.n.toLocaleString()}</td>
                  <td className="num text-right">{fmtRatio(b.win, 1)}</td><td className={`num text-right ${tone(b.avg)}`}>{fmtRatio(b.avg, 2, true)}</td>
                  <td className={`num text-right ${tone(b.median)}`}>{fmtRatio(b.median, 2, true)}</td><td className="num text-right">{fmtRatio(b.beat_market, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-xs text-muted">對照：大盤 {r.calibration.horizon} 天上漲機率 {fmtRatio(r.calibration.market_win, 1)}；前 5 名分散買 {fmtRatio(r.calibration.top5_portfolio_win, 1)}；「創新高機率 ≥ 90%」的推薦實際賺錢 {fmtRatio(r.calibration.high_p_newhigh_win, 1)}。{r.calibration.note}</p>
        </div>
      )}
      {years.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-muted"><tr><th className="py-1 text-left font-normal">年度</th>
              {years.map((y) => <th key={y} className="px-1 text-right font-normal">{String(y).slice(2)}</th>)}</tr></thead>
            <tbody>
              {([['策略', r.next_open.yearly], ['大盤', r.next_open.taiex?.yearly]] as [string, Json][]).map(([n, ys]) => (
                <tr key={n}><td className="py-1">{n}</td>
                  {years.map((y) => <td key={y} className={`num px-1 text-right ${tone(ys?.[y])}`}>{ys?.[y] == null ? '—' : Math.round(ys[y] * 100)}</td>)}</tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-xs text-muted">單位：%（年度報酬）</p>
        </div>
      )}
    </Section>
  )
}

export default function Model() {
  const { data: m, isLoading, error } = useQuery<Json>({ queryKey: ['model'], queryFn: () => get('/api/model') })
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const b = m.backtest
  const live = m.live || {}
  return (
    <>
      <PageTitle aside={<span className="text-xs text-muted">{m.date}・掃描 {m.n_stocks} 檔</span>}>模型表現</PageTitle>
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="大盤隔夜跳空命中率" value={live.taiex_gap_hit != null ? fmtRatio(live.taiex_gap_hit) : '累積中'} sub={live.taiex_gap_n ? `${live.taiex_gap_n} 次實際預測` : undefined} />
        <Stat label="個股隔夜跳空命中率" value={live.stock_gap_hit != null ? fmtRatio(live.stock_gap_hit) : '累積中'} sub={live.stock_gap_n ? `${live.stock_gap_n} 次實際預測` : undefined} />
        <Stat label="一個月推薦實際超額報酬" value={live.alpha_excess != null ? fmtRatio(live.alpha_excess, 2, true) : '累積中'}
          valueClass={live.alpha_excess > 0 ? 'text-up' : live.alpha_excess < 0 ? 'text-down' : ''} sub={live.alpha_n ? `${live.alpha_n} 筆` : '推薦滿 20 個交易日後開始計算'} />
        <Stat label="創新高模型 AUC（回測）" value={b?.auc != null ? b.auc.toFixed(3) : '—'} sub="0.5＝亂猜，越接近 1 越準" />
      </div>
      <p className="mb-6 max-w-4xl text-sm leading-relaxed text-ink-2">
        回測用「時間外樣本」：每段只用該段之前的資料訓練，再看接下來一個月的實際結果。股票池以目前流動性挑選，存在存活者偏誤，實際表現可能比回測差。
      </p>
      <div className="grid items-start gap-x-6 lg:grid-cols-2">
      {b && (
        <Section title="兩籃回測比較（每期前 20 名，一個月後）">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted"><tr className="border-b border-line"><th className="py-2 font-normal" /><th className="text-right font-normal text-offense">進攻籃</th><th className="text-right font-normal text-defense">穩健籃</th><th className="text-right font-normal">全市場</th></tr></thead>
              <tbody className="num">
                <tr className="border-b border-line/70"><td className="py-2">收正報酬比例</td><td className="text-right">{fmtRatio(b.off_up_rate)}</td><td className="text-right">{fmtRatio(b.def_up_rate)}</td><td className="text-right">{fmtRatio(b.all_up_rate)}</td></tr>
                <tr className="border-b border-line/70"><td className="py-2">平均報酬</td><td className="text-right">{fmtRatio(b.topk_ret, 1, true)}</td><td className="text-right">{fmtRatio(b.def_ret, 1, true)}</td><td className="text-right">{fmtRatio(b.mkt_ret, 1, true)}</td></tr>
                <tr className="border-b border-line/70"><td className="py-2">期間平均最大回檔</td><td className="text-right">{fmtRatio(b.off_dd, 1)}</td><td className="text-right">{fmtRatio(b.def_dd, 1)}</td><td className="text-right">—</td></tr>
                <tr><td className="py-2">一個月內創新高比例</td><td className="text-right">{fmtRatio(b.topk_newhigh)}</td><td className="text-right">—</td><td className="text-right">{fmtRatio(b.base_rate)}</td></tr>
              </tbody>
            </table>
          </div>
        </Section>
      )}
      {b?.folds && (
        <Section title="各測試期間（進攻籃）" aside={`AUC ${b.auc.toFixed(3)}`}>
          <table className="w-full text-[0.95rem]">
            <thead className="text-left text-sm text-muted"><tr className="border-b border-line"><th className="py-2 font-normal">期間</th><th className="text-right font-normal">前 20 名創高率</th><th className="text-right font-normal">基準</th><th className="text-right font-normal">超額報酬</th></tr></thead>
            <tbody className="num">
              {b.folds.map((f: Json) => (
                <tr key={f.start} className="border-b border-line/60"><td className="py-1.5">{f.start} ～ {f.end}</td><td className="text-right">{fmtRatio(f.topk_newhigh)}</td><td className="text-right text-muted">{fmtRatio(f.base_rate)}</td><td className={`text-right ${f.topk_excess >= 0 ? 'text-up' : 'text-down'}`}>{fmtRatio(f.topk_excess, 2, true)}</td></tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
      </div>
      <Section title="實際預測表現" aside={m.backfill_note}>

        {m.monthly?.length > 0 && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[520px] text-[0.95rem]">
              <thead className="text-left text-sm text-muted">
                <tr className="border-b border-line">
                  <th className="py-2 font-normal">到期月份</th><th className="font-normal">籃子</th><th className="text-right font-normal">筆數</th>
                  <th className="text-right font-normal">平均報酬</th><th className="text-right font-normal">超額報酬</th>
                  <th className="text-right font-normal">上漲比例</th><th className="text-right font-normal">創新高比例</th>
                </tr>
              </thead>
              <tbody>
                {m.monthly.map((r: Json) => (
                  <tr key={r.basket + r.month} className="border-b border-line/60">
                    <td className="num py-1.5">{r.month}</td>
                    <td><BasketTag basket={r.basket} /></td>
                    <td className="num text-right">{r.n}</td>
                    <td className={`num text-right ${tone(r.avg_ret)}`}>{fmtRatio(r.avg_ret, 2, true)}</td>
                    <td className={`num text-right ${tone(r.avg_excess)}`}>{fmtRatio(r.avg_excess, 2, true)}</td>
                    <td className="num text-right">{fmtRatio(r.up_rate)}</td>
                    <td className="num text-right">{fmtRatio(r.newhigh_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <Scorecard card={m.strategy_scorecard} />
      <FullHistory r={m.full_history} />
      <SystemHealth />
      <DataSources />
      <Section title="調整 Agent 紀錄">
        {!m.tuning_log?.length ? <Empty>還沒有調整紀錄。</Empty> : (
          <div className="space-y-4">
            {groupByDate(m.tuning_log).map((g) => (
              <div key={g.date}>
                <div className="font-medium">
                  {g.date}　{g.changes.length ? `修改 ${g.changes.length} 項參數` : g.issues.length ? `偵測到 ${g.issues.length} 項問題，未修改` : '無需調整'}
                  <span className="ml-2 text-sm font-normal text-muted">當天健康檢查 {g.checks} 次</span>
                </div>
                <Bullets items={[...g.issues.map((x: Json) => `偵測：${x.detail}`), ...g.changes.map((c: Json) => `修改 ${c.param}：${JSON.stringify(c.before)} → ${JSON.stringify(c.after)}${c.reason ? `（${c.reason}）` : ''}`)]} />
                {g.notes.map((n, i) => <p key={i} className="mt-1 text-sm text-ink-2">{n}</p>)}
              </div>
            ))}
          </div>
        )}
      </Section>
    </>
  )
}
