# 維運手冊

## 每日使用流程（隊員）

| 時間（台灣） | 系統自動做 | 隊員要做 |
|---|---|---|
| 07:00 | 盤前報告：Email 寄出 Word／PPT／下單清單，網站更新 | 看「總覽」與「每日建議」，決定要掛哪些單 |
| 08:20 | 開盤前更新（加入那斯達克期貨即時報價） | 有變動時調整預掛單 |
| 09:00～13:30 | 網站每 10 分鐘依即時報價重算建議、持股動向每分鐘；雲端每 40 分鐘一輪並寄 Email（09:00／09:40／10:20／11:00／11:40／12:20／13:00） | 看「總覽」的持股動向與盤中建議，「可以下單」的到競賽平台下單 |
| 成交後 | — | 在網站「持股 → 從 XQ 匯入」貼上 XQ 庫存或成交回報的**截圖**（Win+Shift+S 後 Ctrl+V），或貼上庫存文字；也可告訴 AI 助理「我買了 2330 一張 1000 元」（都要按確認才寫入） |
| 15:45 | 盤後檢討＋隔日預掛單、調整 Agent 健康檢查 | 15:30 後可在平台預掛隔日單 |
| 週五 16:30 | 週報（含完整模型調校）與週報 PPT | 檢查週報，在 AI 助理補記成交理由後可重新產生 |

注意事項：
- **持股率**：建倉期（10/05–10/16）結束後，每日收盤持股率要 ≥70%（只算實際成交，掛單不算）。期貨戶多餘資金要轉入證券戶，並在 AI 助理記錄「資金調撥」。
- **交易過的標的數**：第三週（10/19）起每日追蹤，要 ≥5 檔。
- **最後一日 11/27**：收盤後仍要持有 ≥5 檔，且現金 <30% 總資產，不能清倉。
- 成交紀錄是持倉的唯一來源（`data/trades.csv`），沒記錄的成交系統看不到。週報的「理由」也來自這裡。
- 同一檔分整股、零股或不同時間成交時，每筆各記一列（用成交回報截圖匯入會自動這樣記），成本分批計算、賣出先進先出。手續費也是逐筆計算（含最低手續費）。
- 「總覽 → 持股動向」盤中每分鐘檢查持股；按「開啟通知」後，有持股新進入「盡快賣出」或「低檔可望反彈」時，瀏覽器會跳通知（網頁要開著）。

## 排程（`.github/workflows/`）

| Workflow | 觸發 | 內容 |
|---|---|---|
| `invest-agent.yml` | 上表各時段（網站準點觸發＋cron 備援）、手動 | 執行 `python -m src.main <mode>`，結果提交回 repo，網站每 10 分鐘同步 |
| `keepalive.yml` | 每 6 小時 | 造訪網站，避免 Hugging Face 免費 Space 48 小時無人造訪休眠 |
| `deploy-space.yml` | 推送 `main`（文件、資料變動除外）、手動 | 建置前端並部署到 Hugging Face Space |

**準點觸發**：GitHub 的 cron 在整點附近常延遲數小時甚至漏跑（10/05 實際：07:00 盤前 09:29 才跑、08:20 開盤前 13:52 才跑、盤中 7 輪只跑 3 輪且都在收盤後）。因此改由網站（`src/api/scheduler.py`）每 30 秒對照時刻表（`src/schedule.py`），到點以 `workflow_dispatch` 觸發（幾秒內開始）；cron 保留當備援。

兩種觸發都會先經過 gate：
- 超過有效時間就略過：盤前最晚 12:00、開盤前最晚 09:15、盤中每輪 35 分鐘內、盤後當天 23:45 前；
- 同一時段已完成就略過（紀錄在 `data/schedule_log.json`），所以重複觸發不會重算兩次。GitHub 取消排隊中的重複執行（cancelled）屬正常。

網站休眠時準點觸發也會停，此時只剩 cron 備援；`keepalive` 會定期喚醒網站。三個 workflow 失敗時都會寄 Email 通知（`scripts/notify_failure.py`），信中附錯誤紀錄連結。

### 手動執行（GitHub → Actions → invest-agent → Run workflow）

| 選項 | 用途 |
|---|---|
| `mode` | premarket／preopen／intraday／postclose／weekly／tune／record |
| `force` | 非交易日也執行 |
| `scheduled` | 排程觸發（網站自動帶入）：過了有效時間或已完成就略過；手動執行不用勾 |
| `dry_run` | **測試模式**：照常計算與寄信（主旨加「[測試]」），但不提交任何檔案、不影響正式紀錄 |
| `rollback` | 搭配 tune：回復上一次參數調整 |
| `code`/`side`/`shares`/`price`/`margin` | 搭配 record：記錄一筆成交 |

也可以在網站 AI 助理說「重新執行盤前報告」（需按確認）。

## 系統健康

網站「模型」頁的「系統健康」區塊與選單底部的狀態點會顯示：
- 每個排程最近一次執行時間與結果、網站準點觸發是否成功
- 交易日 07:40 後是否已有當天的盤前報告
- 盤中時段雲端盤中建議是否超過 70 分鐘沒更新
- 與 GitHub 同步狀態、FinMind token 狀態

各資料來源的最近成功／失敗時間列在同頁「資料來源狀態」。

## 雲端部署

### 金鑰與變數

本機 `.env`（範本見 `.env.example`，**不可提交**）：

| 變數 | 用途 |
|---|---|
| `NVIDIA_API_KEY` | NVIDIA build.nvidia.com（主要 LLM） |
| `GROQ_API_KEY`、`GEMINI_API_KEY` | LLM 備援 |
| `FINMIND_TOKEN`、`FINMIND_TOKENS` | FinMind（`FINMIND_TOKENS` 為多組逗號分隔，輪流使用） |
| `SMTP_USER`、`SMTP_APP_PASSWORD`、`REPORT_TO` | Gmail 寄送報告（應用程式密碼；收件人可逗號分隔） |
| `APP_PASSWORD` | 網站登入密碼 |
| `HF_TOKEN` | Hugging Face write token（部署用） |
| `GH_PAT` | GitHub fine-grained token（repo xq：Contents、Actions、Secrets、Variables 讀寫） |

`scripts/setup_cloud.py` 會一次把這些設定到雲端：

```powershell
.venv\Scripts\python scripts\setup_cloud.py
```

| 位置 | 設定 |
|---|---|
| GitHub Secrets | `NVIDIA_API_KEY`、`GEMINI_API_KEY`、`GROQ_API_KEY`、`FINMIND_TOKEN`、`FINMIND_TOKENS`、`SMTP_USER`、`SMTP_APP_PASSWORD`、`REPORT_TO`、`HF_TOKEN` |
| GitHub Variables | `HF_SPACE`（`avad8/xq`） |
| Space Secrets | LLM 與 FinMind 金鑰、`APP_PASSWORD`、`JWT_SECRET`、`GITHUB_REPO`、`GITHUB_TOKEN`、`APP_MODE=cloud` |

### Space 的運作方式

- Space 使用 **Gradio SDK**，執行 `app.py` 在 7860 埠啟動 FastAPI＋前端（相容免費 ZeroGPU 的啟動檢查）。系統套件見 `packages.txt`。
- Space **不包含任何資料**（`data/`、`reports/`、`learned_params.yaml` 都不部署），啟動後從私人 GitHub repo 同步，之後每 10 分鐘同步一次。
- 啟動時預先載入全市場資料並計算盤中建議與警示，約 1～2 分鐘；這段時間網站先顯示雲端上一次的結果。
- 登入失敗 5 次鎖定 10 分鐘。

## 本機開發

```powershell
.venv\Scripts\python -m uvicorn src.api.server:app --port 8000   # 後端＋已建置的前端
cd web; npm run dev                                              # 前端熱更新 http://localhost:5173
.venv\Scripts\python -m pytest -q                                # 單元測試
cd web; npm run build; npm run lint                              # 型別檢查、建置、lint
```

本機模式（未設 `APP_MODE=cloud`）直接讀寫專案資料夾，不需登入。

## 故障排除

| 狀況 | 處理 |
|---|---|
| 收到「排程失敗」Email | 點信中連結看錯誤；暫時性問題（網路、API 過載）直接按 Re-run jobs |
| 健康面板顯示「今天的盤前報告」異常 | 到 Actions 看 07:00 那次是否延遲或失敗；可手動執行 premarket |
| 報告文字變成固定模板 | 所有 LLM 都失敗（額度或過載）；數字與建議不受影響。Actions 日誌會列出每個模型的錯誤 |
| NVIDIA 模型回傳空白 | 推理模型思考用完輸出額度；JSON 呼叫必須帶 `thinking=False`（`src/llm/openai_compat.py`） |
| FinMind 額度用完 | 自動冷卻 1 小時並改用官方來源；可在 `.env` 的 `FINMIND_TOKENS` 加 token 後執行 `setup_cloud.py` |
| 網站很慢或打不開 | Space 可能正在重啟或休眠後喚醒，等 1～2 分鐘；keepalive 每 6 小時會喚醒一次 |
| 持倉與平台不一致 | 在「持股」頁從 XQ 貼上庫存，系統會列出差異並建立補記（需確認） |
| 調整 Agent 改壞參數 | `python -m src.main tune --rollback`，或 Actions 手動 tune＋rollback；刪除 `config/learned_params.yaml` 則全部回到預設 |
