# 台股投資競賽 多 Agent 助手

為「2026 德明X富達投資擂台 全國大專院校組模擬投資競賽」打造：定時抓新聞與美股科技巨頭隔夜走勢，
預測台股科技股開盤與開盤後走向，產出含**選股理由、張數計算、多買／少買原因、賣出條件與張數**的
Word 報告、PowerPoint 簡報與下單清單。

## 架構

```
資料層  UniverseAgent(全市場~640檔)  USMarketAgent  TWMarketAgent(法人/月營收)  NewsAgent  FilingAgent(PDF)
分析層  MonthlyAlphaAgent(一個月創新高模型)  OpenPredictorAgent(隔夜美股→開盤)  SentimentAgent  MacroAgent(國際大事)
        AttributionAgent(今日漲跌歸因)  StrategyAgent(綜合評分)
決策層  RecoveryAgent(虧損補回/換股)  PositionSizerAgent(張數)  ComplianceAgent(競賽規則)
產出層  ReportWriterAgent(LLM) → VerifierAgent(數字核對) → docx / pptx / CSV / Email
回饋層  TunerAgent(調整 Agent)：記錄預測 → 對答案 → 偵測重大失誤 → 回測驗證後修正參數（可回復）
```

| 檔案 | 角色 |
|---|---|
| `src/agents/universe.py` | 全市場上市櫃股票＋ETF，流動性篩選、排除權證／槓桿反向／處置股，批次下載日 K |
| `src/agents/alpha_model.py` | **主軸**：預測未來 20 交易日「創 52 週新高機率」與「超額報酬」（梯度提升樹、滾動時間外驗證） |
| `src/agents/strategy.py` | 綜合分數 = 一個月模型 55% + 法人 15% + 月營收 10% + 隔夜美股 10% + 新聞 10%（調整 Agent 可修改） |
| `src/agents/attribution.py` | 為何今日漲跌：大盤 β、產業、個股特有因素各貢獻幾 % 與幾元；法人、量能、跳空結構、新聞 |
| `src/agents/recovery.py` | 虧損如何補回：續抱／加碼／減碼／換股／停損，含換股成本與預估回本時間 |
| `src/agents/macro.py` | 國際大事 → 國際因子（利率、匯率、油價、美股科技、中國、VIX…）→ 各持股歷史敏感度 → 影響金額與決策 |
| `src/agents/predictor.py` | 隔夜美股 → 台股開盤跳空與開盤後走勢（用於進場時機與限價） |
| `src/agents/sizer.py` | 張數計算、停損停利、換股賣出，每一步寫成理由 |
| `src/agents/tuner.py` | 調整 Agent |
| `src/state/params.py` | 可調參數（`config/learned_params.yaml` 覆寫 `settings.yaml`） |

### 攻守配置（進攻籃 / 穩健籃）
| | 進攻籃 | 穩健籃 |
|---|---|---|
| 目的 | 追強勢、一個月內創新高，拉高報酬率 | 提高「正報酬才能排名」的晉級機率，降低回撤 |
| 選股 | 一個月創新高機率＋超額報酬模型；RSI > 82 或 20 日漲幅 > 45% 列為「過熱不追」 | 一個月收正報酬機率＋預期最大回檔最小；限 RSI < 68、股價在月線 ±6%、β < 1.1 |
| 上限 | 6 檔、單檔 25%、可融資；RSI > 75 限價等回檔 | 4 檔、單檔 20%、不融資；限價不高於前一日收盤 |
| 停利 | +10% 賣 1/3、+20% 再賣一半 | +6% 賣 1/3、+12% 再賣一半 |

比例由 `AllocationAgent` 每天決定（基準進攻 60%，範圍 40%～70%）：大盤過熱每項 −5%、大盤健康上漲 +5%、
總資產回撤 ≥4% −10%／≥8% −20%、競賽剩 ≤15 個交易日時「已正報酬 −10% 保住晉級／仍虧損 +10% 爭取轉正」、
調整 Agent 偵測到失誤 −5%。兩籃到期表現累積足夠後，調整 Agent 會微調基準比例。

### 防止 AI 幻覺
1. **價格、張數、金額、比例、敏感度全部由程式計算**，LLM 不產生任何數字。
2. LLM 一律 temperature 0、JSON schema 輸出、pydantic 驗證；Gemini 過載自動換模型，再失敗改 Groq，全失敗用模板。
3. 新聞／國際事件必須引用新聞編號，事件中的數字必須出現在引用的新聞原文，否則丟棄。
4. 報告文字經 `VerifierAgent` 比對事實資料：除 0–3 外，每個數字都必須有出處，不符退回重寫，兩次失敗改用模板。
5. 國際因子敏感度需達統計顯著（|相關係數| ≥ 0.125）才列入影響金額。
6. 模型附樣本外回測（並註明存活者偏誤）；調整 Agent 的新參數必須在同一份資料上回測勝過現行參數 5% 以上才採用。

### 調整 Agent 何時會修正
| 偵測 | 觸發條件（`settings.yaml` tuner） | 修正 |
|---|---|---|
| 隔夜模型失準 | 近 10 次大盤跳空命中率 < 45%，或單次誤差 > 3 倍誤差帶 | 比較 Ridge 正則化與歷史長度，近 60 日命中率較佳才換 |
| 一個月模型失準 | 已到期推薦的實際超額報酬 < −3%，或創新高率遠低於預估 | 測試 5 組超參數／近期加權，回測需進步 5% |
| 因子失效 | 已到期推薦 ≥ 20 筆 | 依各因子與實際報酬的 IC，由最差因子移 0.05 權重給最佳因子 |
| 回撤過大／重大虧損 | 總資產自高點回落 > 8%，或單檔單日虧損 > 總資產 1.5% | 每筆風險 ×0.8、單一標的上限 −5%；恢復後逐步回到預設 |

每次修正都記錄在 `data/tuning_log.json`（含修改前參數），報告中有「模型健康度」章節；
`python -m src.main tune --rollback` 可回復。競賽規則（`rules.yaml`）永遠不在可調範圍。

## 安裝（本機）

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # 填入金鑰
```

## 使用

```powershell
# 盤前報告（以最新美股收盤預測今日開盤）
.venv\Scripts\python -m src.main premarket
# 指定日期 / 非交易日強制執行
.venv\Scripts\python -m src.main premarket --date 2026-10-05
# 開盤前更新（加入那斯達克期貨即時報價）
.venv\Scripts\python -m src.main preopen
# 盤後檢討＋隔日預掛單（平台 15:30 後可預掛）
.venv\Scripts\python -m src.main postclose
# 週報（含完整模型調校）
.venv\Scripts\python -m src.main weekly
# 手動執行調整 Agent／回復上一次調整
.venv\Scripts\python -m src.main tune
.venv\Scripts\python -m src.main tune --rollback

# 在競賽平台成交後，記錄實際成交（系統以此重算持倉與現金）
.venv\Scripts\python -m src.main record --code 3037 --side BUY --shares 1463 --price 1340
.venv\Scripts\python -m src.main record --code 3231 --side BUY --shares 13000 --price 188 --margin

# 測試
.venv\Scripts\python -m pytest -q
```

輸出：`reports/YYYY-MM/` 內的 `.docx`、`.pptx`、`_下單清單.csv`；事實資料存於 `data/facts/`。

## 網頁前端與 AI 助理

FastAPI（`src/api/`）＋ React（`web/`）。頁面：總覽（攻守天平、今日建議、規則檢查）、每日建議（K 線＋限價/停損/停利線＋理由分頁＋下載）、
個股（任一上市櫃股票：基本盤、K 線、雙籃評估與張數試算、漲跌歸因、國際因子、新聞）、新聞與國際大事、持股、模型表現、AI 助理。

AI 助理可以：分析任何股票（包含每日建議以外的）、查詢基本盤與新聞、模擬試算（換股後的成本與持股率）、
**記錄成交**與**重跑分析**（這兩項會先出現確認卡片，按「確認執行」才會寫入／觸發）。
回答中的數字全部來自工具計算並逐一核對；找不到出處的數字會以虛線標示「無法核對」。

本機啟動：
```powershell
cd web; npm install; npm run build; cd ..
.venv\Scripts\python -m uvicorn src.api.server:app --port 8000     # 開 http://localhost:8000
# 開發前端（熱更新）：另開視窗 cd web; npm run dev → http://localhost:5173
```

### 部署到 Hugging Face Spaces（免費 16GB 記憶體）
1. 在 GitHub 建立 **private** repo 並推上專案（`main` 分支），設定好每日排程的 Secrets。
2. 到 huggingface.co 建立 Space：SDK 選 **Docker**、可見度選 **Private** 或 Public（有密碼保護）。
3. Space → Settings → Variables and secrets 新增：
   `APP_PASSWORD`（隊伍共用密碼）、`JWT_SECRET`（任意 32 字以上亂碼）、`GEMINI_API_KEY`、`GROQ_API_KEY`、`FINMIND_TOKEN`、
   `GITHUB_REPO`（例如 `your-name/invest`）、`GITHUB_TOKEN`（GitHub fine-grained token：該 repo 的 Contents 與 Actions 讀寫權限）。
4. GitHub repo → Settings → Secrets 新增 `HF_TOKEN`（Hugging Face 的 write token）；Variables 新增 `HF_SPACE`（例如 `your-name/invest-desk`）。
5. 推送到 `main` 後 `deploy-space` workflow 會自動部署；Space 啟動後每 10 分鐘從 GitHub 同步報告與成交紀錄。
   第一次開啟約需 1～2 分鐘載入全市場資料；閒置 48 小時會休眠，再開啟時自動重新載入。

## GitHub Actions 定時執行

1. 建立 GitHub **private** repo 並推上此專案。
2. Settings → Secrets and variables → Actions 新增：`GEMINI_API_KEY`、`GROQ_API_KEY`、`FINMIND_TOKEN`、
   `SMTP_USER`、`SMTP_APP_PASSWORD`、`REPORT_TO`。
3. 排程（台灣時間）：07:00 盤前、08:20 開盤前更新、15:45 盤後（含調整 Agent 健康檢查）、週五 16:30 週報（含完整調校）。
   GitHub 排程可能延遲 10–30 分鐘。
4. 成交後可在 Actions → invest-agent → Run workflow 選 `record` 填入成交資料（手機也能操作），
   或在本機 `record` 後 `git push`。

## 競賽規則（摘要，完整見 `config/rules.yaml`）
- 期間 2026/10/05–11/27（38 交易日）；10/05–10/16 建倉期不罰持股率
- 初始 1,000 萬（證券 800 萬／期貨 200 萬，可自由出入金）→ 系統會建議將期貨戶多餘資金轉入證券戶
- 禁止權證；股票稅 0.3%、ETF 0.1%、手續費 0.1425%；融資成數 60%
- 門檻：交易 ≥10 筆、≥5 標的、持股率 ≥70%（每日收盤檢查，未達扣報酬率 0.2%，掛單不算）
- 只有正報酬才排名；前 20 名現場簡報 6 分鐘，總分 = 排名 50% + 簡報 50%

## 策略參數
都在 `config/settings.yaml`：全市場篩選門檻、一個月模型設定、評分權重、目標持股率 90%、單一標的上限 25%、單一產業上限 50%、
每筆風險 1.5%、2×ATR 停損（上限 10%）、+10% / +20% 分段停利、換股門檻 1.5%、融資借款上限 30%、產業對應美股驅動因子、國際因子。
調整 Agent 的修改寫在 `config/learned_params.yaml`，刪除即回到預設。

## 注意事項
- 規則規定「參賽者應自行交易，不可委託他人」。自動下單模組（規劃中）須由隊員自己的程式操作自己的帳號，
  建議先向主辦單位確認是否允許程式下單。
- 若競賽平台阻擋海外 IP，自動下單需改在本機執行。
- 本系統為模擬競賽研究用途，不構成投資建議。
