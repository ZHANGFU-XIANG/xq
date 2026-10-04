# 台股投資競賽 多 Agent 研究台

為「2026 德明X富達投資擂台 全國大專院校組模擬投資競賽」打造的投資研究與決策輔助系統。
每天自動抓取台股、美股、期貨、新聞與國際大事，產出**股票／ETF／期貨三類建議**（選股理由、張數、限價、
停損停利、賣出條件），盤中每 40 分鐘依即時報價重算，並提供網站、AI 助理、Word／PPT 報告與週報簡報。

> 系統只提供建議，**所有下單由隊員在競賽平台親手操作**（競賽規定須自行交易）。本系統為競賽研究用途，不構成投資建議。

- 網站：<https://avad8-xq.hf.space>（隊伍密碼登入）
- 程式與資料：私人 GitHub repo `ZHANGFU-XIANG/xq`
- 全部在雲端執行（GitHub Actions＋Hugging Face Space），電腦關機也照常運作

## 文件

| 文件 | 內容 |
|---|---|
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | **每日使用流程**、排程時間表、雲端部署與金鑰、系統健康、故障排除 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Agent 架構與資料流、三籃配置、期貨策略、資料來源備援、LLM 與防幻覺設計、調整 Agent |
| [docs/API.md](docs/API.md) | 後端 API 清單 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 開發紀錄 |
| `config/rules.yaml` | 競賽規則（程式檢查依據，調整 Agent 永遠不會修改） |
| `config/settings.yaml` | 策略與模型參數 |

## 功能一覽

| 網站頁面 | 內容 |
|---|---|
| 總覽 | 加權／櫃買即時指數、三籃比例（進攻／穩健／ETF）、總資產與門檻進度、股票／ETF／期貨分頁的建倉與盤中建議 |
| 每日建議 | 盤前報告的每筆建議：K 線＋限價／停損／停利線、理由、Word／PPT／下單清單下載 |
| 警示 | 依股票／ETF／期貨／市場與規則分頁：漲跌停、注意處置股、持股負面新聞、ETF 折溢價、期貨追繳與結算、選擇權到期、競賽門檻 |
| 個股／ETF | 任一上市櫃股票或 ETF：K 線、三籃評估與張數試算、漲跌歸因、國際因子、公司概況與財報、重大訊息 |
| 期貨與選擇權 | 大台／小台／微台 K 線、各月報價、基差、保證金、結算日；台指選擇權賣權報價；觸發條件與參考試算 |
| 新聞 | 只保留金融經濟新聞，依股票／ETF／期貨分類，每 5 分鐘更新 |
| 持股 | 由成交紀錄重算的持倉、損益、總資產走勢；從 XQ 貼上庫存匯入對帳 |
| 模型 | 回測、實際預測表現、**系統健康**（排程與資料新鮮度）、資料來源狀態、調整 Agent 紀錄 |
| AI 助理 | 依工具計算結果回答投資問題（每個論點附出處）、記錄成交（需確認）、產生**週報 PPT** |

## 快速開始（本機）

需要 Python 3.12＋、Node.js 22＋。

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # 填入金鑰（說明見 docs/OPERATIONS.md）

cd web; npm install; npm run build; cd ..
.venv\Scripts\python -m uvicorn src.api.server:app --port 8000    # 開 http://localhost:8000
```

常用指令：

```powershell
.venv\Scripts\python -m src.main premarket            # 盤前報告（加 --force 可在非交易日執行）
.venv\Scripts\python -m src.main intraday             # 盤中建議
.venv\Scripts\python -m src.main postclose            # 盤後檢討＋隔日預掛單
.venv\Scripts\python -m src.main weekly               # 週報與完整調校
.venv\Scripts\python -m src.main tune [--rollback]    # 調整 Agent／回復上一次調整
.venv\Scripts\python -m pytest -q                     # 單元測試
```

## 專案結構

```
app.py                  Hugging Face Space 進入點（啟動 FastAPI＋前端）
config/
  rules.yaml            競賽規則
  settings.yaml         策略、模型、LLM、新聞來源等參數
  learned_params.yaml   調整 Agent 寫入的覆寫參數（由雲端產生；刪除即回到預設）
src/
  main.py               排程進入點（premarket / preopen / intraday / postclose / weekly / tune / record / backfill）
  pipeline.py           共用分析流程（每日報告與即時分析都經過這裡）
  config.py             設定載入、路徑、交易日曆
  agents/               各分析與決策 Agent（見 docs/ARCHITECTURE.md）
  api/                  FastAPI 伺服器、AI 助理、GitHub 同步、登入
  data/providers.py     資料來源備援（FinMind 多 token 輪替 → 官方來源）
  llm/                  LLM 用戶端（NVIDIA → Groq → Gemini）
  reports/              Word／PPT／週報 PPT／圖表／Email
  state/                持倉（由成交紀錄重算）與可調參數
web/                    React＋TypeScript 前端（Vite、Tailwind）
tests/                  單元測試（pytest）
scripts/
  setup_cloud.py        一鍵設定 GitHub Secrets 與 HF Space 變數
  notify_failure.py     排程失敗時寄 Email（只用標準函式庫）
.github/workflows/
  invest-agent.yml      每日排程（盤前、盤中、盤後、週報）
  keepalive.yml         每 6 小時喚醒網站
  deploy-space.yml      推送 main 時建置前端並部署到 Space
data/                   成交紀錄、持倉中繼資料、預測紀錄、每日報告事實（facts）、盤中紀錄；cache/ 為行情快取（不進版控）
reports/                每日 Word／PPT／下單清單、週報（雲端排程產生）
```

## 競賽規則摘要（完整見 `config/rules.yaml`）

- 期間 2026/10/05–11/27（38 個交易日）；10/05–10/16 建倉期不罰持股率
- 初始 1,000 萬（證券 800 萬／期貨 200 萬，可自由出入金）
- 可投資上市櫃股票、ETF、台指期（大／小／微台）、台指選擇權、股票期貨；**禁止權證**
- 成本：股票證交稅 0.3%、ETF 0.1%、手續費 0.1425%；融資成數 60%
- 排名門檻：交易 ≥10 筆（買賣各算一筆）、交易過 ≥5 個標的（第三週 10/19 起每日追蹤）、持股率 ≥70%
  （每日收盤檢查，未達每次扣報酬率 0.2%，掛單不算）
- 最後一日（11/27）收盤後須仍持有 ≥5 檔且現金 <30% 總資產，否則不列入排名
- 只有正報酬才排名；前 20 名現場簡報 6 分鐘，總分＝排名 50%＋簡報 50%；簡報上傳期限 12/16，決賽 12/19
- 期貨是否計入 70% 持股率簡章未載明，系統**以不計入**處理
