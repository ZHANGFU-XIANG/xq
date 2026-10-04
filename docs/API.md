# 後端 API（`src/api/server.py`）

FastAPI；React 前端與 API 在同一個網址。

**認證**：
- 雲端模式需登入：`POST /api/login` 取得權杖（HttpOnly cookie，並以 `Authorization: Bearer` 標頭傳送，相容 Space iframe）。
- 除了 `login`、`logout`、`status`，所有 `/api/*` 都需要權杖。
- 本機模式未設密碼時不需登入。

**慢的端點採「先回上一份、背景重算」**：
- `intraday` 與 `alerts` 在沒有當前時段結果時，會先回傳上一份結果，並帶 `stale: true`（盤中建議另附 `cached_from`）。
- 前端看到 `stale` 會縮短輪詢間隔，直到拿到新結果。
- 加 `refresh=true` 則強制同步重算。

## 系統

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/login` | `{password}` → 權杖；失敗 5 次鎖定 10 分鐘 |
| POST | `/api/logout` | 登出 |
| GET | `/api/status` | 是否需登入、預載完成（`warm`）、同步狀態、目標交易日 |
| GET | `/api/health` | 排程最近執行、報告與盤中資料新鮮度、同步與 FinMind 狀態；`overall` 為 ok／warn／error |
| GET | `/api/data-sources` | 各資料來源最近成功／失敗時間、FinMind token 數與冷卻狀態 |
| GET | `/api/model` | 回測、實際預測表現（依月份與籃子）、調整 Agent 紀錄、目前覆寫參數 |

## 報告與建議

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/reports` | 報告清單 |
| GET | `/api/report?key=` | 指定（預設最新）報告的事實資料：建議、三籃比例、規則檢查、期貨計畫 |
| GET | `/api/files/{path}` | 下載報告檔（docx／pptx／csv） |
| GET | `/api/intraday?refresh=` | 目前 40 分鐘時段的盤中建議（沒有持股時為建倉建議），含股票／ETF／期貨 |
| GET | `/api/holding-advice?refresh=` | 每檔庫存的加碼／續抱／減碼／出清建議 |
| GET | `/api/alerts?refresh=` | 警示（股票／ETF／期貨／市場與規則），5 分鐘快取 |
| GET | `/api/potential` | 潛在標的：模型看好但今天沒列入下單的股票與 ETF，附「為什麼還沒買」與觸發條件 |
| GET | `/api/weekly-reports` | 競賽各週與已產生的週報 |
| POST | `/api/weekly-report` | `{week}` → 產生週報 PPT，回傳下載路徑 |

## 行情與個股

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/quotes?codes=` | 即時報價（逗號分隔代號；含加權、櫃買指數） |
| GET | `/api/intl?codes=` | 持股受國際市場影響的標註 |
| GET | `/api/stock/search?q=` | 搜尋股票、ETF、期貨 |
| GET | `/api/stock/{code}/profile` | 基本資料 |
| GET | `/api/stock/{code}/kline?days=` | 日 K |
| GET | `/api/stock/{code}/analysis` | 三籃評估、張數試算、漲跌歸因、國際因子 |
| GET | `/api/stock/{code}/company` | 公司概況、月營收、財報摘要、重大訊息、股利（公開資訊觀測站） |
| GET | `/api/stock/{code}/news` | 個股相關新聞 |
| POST | `/api/mops/news` | `{marketKind, companyId, enterDate, serialNumber}` → 重大訊息全文 |
| GET | `/api/futures/{root}` | TX／MTX／TMF／TXO：各月報價、基差、保證金、結算日、賣權報價 |
| GET | `/api/futures/{root}/kline?days=` | 近月連續日 K |

## 新聞與持股

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/news?key=` | 報告內的新聞、事件、國際大事、市場觀點 |
| GET | `/api/news/live?refresh=` | 即時新聞（5 分鐘快取，只留財經，含分類） |
| GET | `/api/portfolio` | 由成交紀錄重算的持倉、現金、總資產、持股率、期貨部位、總資產歷史 |
| POST | `/api/xq/import` | `{text, complete}` 貼上 XQ 庫存文字 → 解析並與系統持倉對帳，差異建立待確認動作 |

## AI 助理與待確認動作

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/chat` | `{messages}` → 回答、使用的模型、出處、下載檔、待確認動作 |
| POST | `/api/actions/{id}/confirm` | 執行待確認動作（記錄成交、資金調撥、重跑分析） |
| POST | `/api/actions/{id}/cancel` | 取消待確認動作 |

寫入類動作在雲端模式會透過 GitHub API 寫入 repo 或觸發 workflow，完成後網站自動同步。
