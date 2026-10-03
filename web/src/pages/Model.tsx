import { useQuery } from '@tanstack/react-query'
import { Bullets, Empty, ErrorNote, Loading, PageTitle, Section } from '../components/ui'
import { get, type Json } from '../lib/api'
import { fmtRatio } from '../lib/format'

export default function Model() {
  const { data: m, isLoading, error } = useQuery<Json>({ queryKey: ['model'], queryFn: () => get('/api/model') })
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  const b = m.backtest
  const live = m.live || {}
  return (
    <>
      <PageTitle aside={<span className="text-sm text-muted">{m.date}・掃描 {m.n_stocks} 檔</span>}>模型表現</PageTitle>
      <p className="mb-6 max-w-3xl leading-relaxed text-ink-2">
        回測用「時間外樣本」：每段只用該段之前的資料訓練，再看接下來一個月的實際結果。股票池以目前流動性挑選，存在存活者偏誤，實際表現可能比回測差。
      </p>
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
      <Section title="開賽後實際表現">
        <ul className="space-y-1">
          <li>大盤隔夜跳空命中率：{live.taiex_gap_hit != null ? `${fmtRatio(live.taiex_gap_hit)}（${live.taiex_gap_n} 次）` : '累積中'}</li>
          <li>個股隔夜跳空命中率：{live.stock_gap_hit != null ? `${fmtRatio(live.stock_gap_hit)}（${live.stock_gap_n} 次）` : '累積中'}</li>
          <li>一個月推薦實際超額報酬：{live.alpha_excess != null ? fmtRatio(live.alpha_excess, 2, true) : '推薦滿 20 個交易日後開始計算'}</li>
        </ul>
      </Section>
      <Section title="調整 Agent 紀錄">
        {!m.tuning_log?.length ? <Empty>還沒有調整紀錄。</Empty> : (
          <div className="space-y-4">
            {[...m.tuning_log].reverse().map((e: Json, i: number) => (
              <div key={i}>
                <div className="font-medium">{e.date}　{e.changes?.length ? `修改 ${e.changes.length} 項參數` : e.note || '無需調整'}</div>
                <Bullets items={[...(e.issues ?? []).map((x: Json) => `偵測：${x.detail}`), ...(e.changes ?? []).map((c: Json) => `修改 ${c.param}：${JSON.stringify(c.before)} → ${JSON.stringify(c.after)}${c.reason ? `（${c.reason}）` : ''}`)]} />
                {e.changes?.length > 0 && e.note && <p className="mt-1 text-sm text-ink-2">{e.note}</p>}
              </div>
            ))}
          </div>
        )}
      </Section>
    </>
  )
}
