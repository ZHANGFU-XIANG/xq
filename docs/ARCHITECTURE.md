# 系統架構

## 設計原則

1. **數字一律由程式計算**：價格、張數、金額、比例、機率、敏感度都來自程式；LLM 只負責把程式算好的事實寫成文字，並經 Verifier 逐一核對。
2. **每個建議都附理由**：買多少、為什麼多買或少買、何時賣、賣多少，每一步計算都寫成可讀的理由。
3. **資料來源都有備援**：任一來源失效時自動換下一個，缺資料會明確標示「無法確認」，不當成中性。
4. **規則優先**：競賽規則（`config/rules.yaml`）由 ComplianceAgent 檢查，調整 Agent 永遠不能修改。
5. **使用者親手下單**：系統不連接券商、不自動下單；寫入動作（記錄成交、重跑分析）都要使用者在網站上按確認。

## Agent 與資料流

```
資料層   UniverseAgent（全市場上市櫃＋ETF、日 K）   USMarketAgent / TWMarketAgent（美股隔夜、法人、月營收、估值、融資券）
         NewsAgent（RSS＋Google News，只留財經）    MopsAgent（公開資訊觀測站）   FilingAgent（法說會 PDF）
         futures_data（期交所即時與歷史行情、保證金）  providers（FinMind 多 token → 官方來源備援）
分析層   MonthlyAlphaAgent（一個月創新高／超額報酬）  OpenPredictorAgent（隔夜美股 → 開盤跳空）
         SentimentAgent（新聞事件）  MacroAgent（國際大事 → 因子 → 持股敏感度）  AttributionAgent（今日漲跌歸因）
         StrategyAgent（綜合評分）  ETFAgent（類別、折溢價）  FuturesAgent（方向模型回測、補回與避險）
決策層   AllocationAgent（三籃比例）  PositionSizerAgent（張數）  RecoveryAgent（虧損補回／換股）
         HoldingAdviceAgent（每檔持股加碼／續抱／減碼／出清）  ComplianceAgent（競賽規則）  AlertAgent（警示）
產出層   ReportWriterAgent（LLM）→ VerifierAgent（數字核對）→ Word / PPT / 下單清單 CSV / Email
         IntradayAgent（盤中每 40 分鐘重算）  WeeklyReportAgent → 週報 PPT
互動層   ChatAgent（AI 助理，工具呼叫）  OnDemandAgent（任一股票即時分析）
回饋層   TunerAgent（調整 Agent）：記錄預測 → 對答案 → 偵測重大失誤 → 回測驗證後修正參數（可回復）
         BackfillAgent：回補歷史預測，讓調整 Agent 第一天就有樣本
```

`src/pipeline.py` 是共用的分析流程：每日報告（`src/main.py`）與網站即時分析（`src/agents/on_demand.py`）都經過這裡，確保兩邊結果一致。

### 模組對照

| 檔案 | 角色 |
|---|---|
| `agents/universe.py` | 全市場清單（FinMind，失敗改用證交所 ISIN 清單）、流動性篩選（20 日均成交值 ≥5,000 萬、股價 ≥10 元）、排除權證／槓桿反向／債券 ETF，批次下載 5 年日 K |
| `agents/alpha_model.py` | **主模型**：預測未來 20 交易日「創 52 週新高機率」與「超額報酬」；滾動時間外驗證 4 期、近期加權（半衰期 365 天） |
| `agents/predictor.py` | 隔夜美股（費半、那斯達克、個股驅動因子）→ 台股開盤跳空與開盤後走向（Ridge 迴歸） |
| `agents/strategy.py` | 綜合分數＝一個月模型 35%＋創新高 20%＋法人 15%＋月營收 10%＋隔夜美股 10%＋新聞 10%（調整 Agent 可修改） |
| `agents/etf.py` | ETF 類別（市值型、高股息、產業、債券…）、即時折溢價（溢價 >1% 不追） |
| `agents/futures.py` / `futures_data.py` | 台指期方向模型（walk-forward 回測、需勝過「一直做多」才啟用）、補回虧損加碼、賣權避險；期交所 MIS 即時報價、OpenAPI 保證金、歷史行情 |
| `agents/allocation.py` | 三籃比例（見下節） |
| `agents/sizer.py` | 風險預算法決定張數與零股、停損（2×ATR、上限 10%）、分批停利、換股與減碼 |
| `agents/recovery.py` | 虧損如何補回：續抱／加碼／減碼／換股／停損，含換股成本與預估回本時間 |
| `agents/holding_advice.py` | 每檔庫存：加碼／續抱／減碼／全部出清的股數、限價與後續條件 |
| `agents/compliance.py` | 持股率、交易筆數、交易過標的數、最後一日持有檔數與現金比例、禁止權證 |
| `agents/alerts.py` | 市場（漲跌停、注意處置股）、持股（負面新聞、重大訊息）、ETF 折溢價、期貨追繳與結算、選擇權到期、競賽門檻 |
| `agents/intraday.py` | 盤中每 40 分鐘以即時價重算計畫；與盤前比較新增／取消／調整；標示「現在能不能下單」 |
| `agents/macro.py` | 國際大事 → 國際因子（利率、匯率、油價、美股科技、中國、VIX）→ 各持股歷史敏感度（|相關| ≥0.125 才計入） |
| `agents/attribution.py` | 今日漲跌拆成大盤 β、產業、個股特有因素，換算成金額 |
| `agents/news.py` | 新聞抓取、去重、財經過濾、股票／ETF／期貨分類、LLM 事件萃取（必須引用新聞編號） |
| `agents/writer.py` / `verifier.py` | 報告文字（LLM）與數字核對（純程式） |
| `agents/weekly.py` + `reports/weekly_pptx.py` | 週報：當週每筆成交、理由來源、每日市場與事件、總資產與加權比較、期末持股、損益來源、檢討與下週計畫 |
| `agents/tuner.py` | 調整 Agent（見下節） |
| `agents/xq_import.py` | 解析從 XQ「帳務→庫存」複製貼上的文字，與系統持倉對帳 |
| `agents/xq_vision.py` | XQ 截圖（庫存或成交回報）→ 視覺模型（Gemini 2.5 Flash → NVIDIA Nemotron Nano Omni）轉成表格；每列一筆；股數依盤別判斷（整股＝張、零股＝股），用成交價金核對；代號不認得或名稱對不上會標示 |
| `schedule.py` | 排程時刻表、有效時間與防重複（gate／mark）；`api/scheduler.py` 在網站端準點觸發 |
| `state/portfolio.py` | 持倉一律由 `data/trades.csv` 成交紀錄重算（股票、ETF、期貨、選擇權、資金調撥）；每筆買進是一批（`Lot`），成本分開記錄，賣出先進先出 |
| `state/params.py` | 可調參數：`settings.yaml` 預設值＋`learned_params.yaml` 覆寫 |

## 三籃配置

比例是「股票＋ETF 目標持股市值」的分數，三籃合計 1；期貨另計。目標持股率 90%。

| | 進攻籃 | 穩健籃 | ETF 籃 |
|---|---|---|---|
| 目的 | 追強勢、一個月內創新高 | 提高「正報酬才能排名」的機率、降低回撤 | 分散個股風險、證交稅只有 0.1% |
| 選股 | 創新高機率＋超額報酬；RSI >82 或 20 日漲幅 >45% 列為過熱不追 | 收正報酬機率＋預期回檔最小；RSI <68、股價在月線 ±6%、β <1.1 | ETF 分數；同類別最多 1 檔；溢價 >1% 不追 |
| 上限 | 6 檔、單檔 25%、可融資 | 4 檔、單檔 20%、不融資、限價不高於前一日收盤 | 3 檔、單檔 15%、不融資 |
| 停利 | +10% 賣 1/3、+20% 再賣一半 | +6% 賣 1/3、+12% 再賣一半 | 同穩健籃 |

AllocationAgent 每天調整比例：
- **ETF 籃**：基準 15%（5%～35%）；大盤過熱每項 +5%，大盤健康上漲 −5%。
- **股票部分**：基準進攻 60%／穩健 40%（進攻 40%～70%）：
  - 大盤過熱（RSI、20 日漲幅、距季線）每項 −5%；
  - 大盤站上季線且 20 日漲幅 >2% 且無失誤 +5%；
  - 總資產回撤 ≥4% −10%、≥8% −20%；
  - 剩 ≤15 個交易日時「已正報酬 −10% 保住晉級／仍虧損 +10% 爭取轉正」；
  - 調整 Agent 偵測到失誤 −5%。

## 期貨與選擇權

walk-forward 回測顯示台指期方向模型**沒有擇時優勢**（模型做多平均每 5 日 +0.54%，一直做多 +0.82%；做空虧損），所以方向交易由回測門檻自動關閉，只提供：

- **補回虧損加碼**：總資產回撤 ≥4% 且大盤在季線之上時，以微台／小台小部位加碼（明確標示「不是優勢，是補回工具」）。
- **賣權避險**：大盤過熱或 VIX 急升時買進價外賣權，權利金上限總資產 0.5%；只做買方，只選流動性足夠（買賣價差 ≤15%）的履約價。
- 沒有建議時，期貨分頁列出四種操作的觸發條件與目前數值，以及 1 口微台／小台／賣權的參考試算。

保證金與追繳由 AlertAgent 監控；期貨戶保留 30 萬，其餘建議轉入證券戶以提高持股率。

## 資料來源與備援（`src/data/providers.py`）

| 資料 | 主要 | 備援 |
|---|---|---|
| 股票清單 | FinMind | 證交所 ISIN 清單 |
| 全市場日 K | yfinance 批次下載（每日增量，快取） | — |
| 單檔日 K | 全市場快取 → FinMind | yfinance |
| 當日成交值初篩 | 證交所 STOCK_DAY_ALL、櫃買收盤行情 | — |
| 三大法人 | FinMind | 證交所 T86、櫃買 dailyTrade |
| 月營收 | FinMind | 證交所 t187ap05 |
| 本益比／殖利率 | FinMind | 證交所 BWIBBU、櫃買 peratio |
| 融資融券 | FinMind | 證交所 MI_MARGN |
| 即時報價 | 證交所 MIS | — |
| ETF 淨值與折溢價 | 證交所 MIS all_etf | — |
| 期貨與選擇權 | 期交所 MIS（即時）、OpenAPI（保證金）、futDataDown（歷史） | — |
| 美股、那斯達克期貨、加權指數 | yfinance | — |

FinMind 支援多組 token（`FINMIND_TOKENS` 逗號分隔）輪流使用；額度用完的 token 冷卻 1 小時，全部用完就改走官方來源。抓取失敗的空資料不會被快取。各來源最近成功／失敗時間顯示在網站模型頁。

## LLM 與防幻覺

供應商順序（`settings.yaml` llm.providers）：

1. **NVIDIA build.nvidia.com**：Nemotron-3 Super → DeepSeek V4.1 Flash → GLM-5.3 Flash
2. **Groq**：gpt-oss-120b → Qwen
3. **Gemini**：3.5 Flash → 2.5 Flash

全部失敗時改用固定模板，報告照常產出。

- 結構化輸出（新聞事件、國際大事、報告文字、週報）一律 JSON schema＋pydantic 驗證，temperature 0。
- **NVIDIA 推理模型做 JSON 輸出時必須關閉思考**（`chat_template_kwargs`）。否則思考會用完輸出額度，回傳空白。
- 新聞事件必須引用新聞編號，事件裡的數字必須出現在引用的原文，否則丟棄。
- 報告文字經 VerifierAgent 核對：除 0–3 之外，每個數字與股票代號都必須在事實資料（facts）裡，不符就退回重寫，兩次失敗改用模板。
- AI 助理：
  - 只回答投資經濟相關問題，其他婉拒；
  - 每個論點標註出處〔S#〕，由程式核對；
  - 找不到出處的數字標示「未核實」；
  - 缺資料時回答「無法確認」。

## 調整 Agent（`agents/tuner.py`）

| 偵測 | 觸發條件 | 修正 |
|---|---|---|
| 隔夜模型失準 | 近 10 次大盤跳空命中率 <45%，或單次誤差 >3 倍誤差帶 | 比較 Ridge 正則化與歷史長度，近 60 日命中率較佳才換 |
| 一個月模型失準 | 已到期推薦（≥20 筆）實際超額報酬 < −3% | 測試多組超參數與近期加權，回測需進步 5% 以上 |
| 因子失效 | 已到期推薦 ≥20 筆 | 依各因子 IC，由最差因子移 0.05 權重給最佳因子 |
| 回撤過大／重大虧損 | 總資產自高點回落 >8%，或單檔單日虧損 >總資產 1.5% | 每筆風險 ×0.8、單一標的上限 −5%；恢復後逐步回到預設 |

所有修正記錄在 `data/tuning_log.json`（含修改前的值），同一天的健康檢查合併為一筆。`python -m src.main tune --rollback` 可回復上一次調整。調整範圍受 `settings.yaml` tuner.bounds 限制。

## 前端（`web/`）

React 19＋TypeScript＋Vite＋Tailwind 4，資料用 TanStack Query 取得，K 線用 lightweight-charts。

- 盤中指數每 10 秒刷新；盤中建議每 60 秒、新聞每 5 分鐘刷新。
- 網站重啟後先顯示雲端上一次的計算結果（標示計算時間），背景重算完自動替換。
- 支援深色／淺色模式與手機版；AI 助理對話紀錄存在使用者裝置（localStorage）。
