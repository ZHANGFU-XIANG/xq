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
| GET | `/api/intraday?refresh=` | 盤中建議（盤中每 10 分鐘重算；沒有持股時為建倉建議），含股票／ETF／期貨 |
| GET | `/api/holding-advice?refresh=` | 每檔庫存的加碼／續抱／減碼／出清建議 |
| GET | `/api/alerts?refresh=` | 警示（股票／ETF／期貨／市場與規則），5 分鐘快取 |
| GET | `/api/decisions` | 買賣決策總表：可以買／等回檔再買／先不要買／持股續抱・減碼・出清，每檔一句理由與改變結論的條件（盤中每 10 分鐘以即時價重算） |
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
| GET | `/api/portfolio` | 由成交紀錄重算的持倉（含每批成交 `lots`：成本、損益）、現金、總資產、持股率（即時價）、期貨部位、即時規則檢查、總資產歷史 |
| GET | `/api/trades` | 全部交易紀錄（`id`＝在 trades.csv 的順序、`version`＝內容指紋）、目前持有、統計（`stats`：已實現交易先進先出、勝率、每檔、每日、競賽門檻） |
| POST | `/api/trades` | `{version, trade:{kind, date, code, side, shares, price, margin, contract, note}}` 手動新增一筆（kind：STOCK／ETF／FUT（contract=TMF202610，shares=口數）／OPT（contract=TXO202610P22000，只做買方）／TRANSFER（side=TO_FUTURES/TO_SECURITIES，price=金額）／CASH（手續費校正，可為負））；重算整個帳戶驗證（賣超過持有回 400），版本不符回 409；雲端 commit 到 GitHub |
| PUT | `/api/trades/{id}` | 同上，修改一筆 |
| DELETE | `/api/trades/{id}?version=` | 刪除一筆 |
| POST | `/api/trades/bulk` | `{version, trades:[...]}` 一次新增多筆（CSV 匯入用；任一筆不合法全部不寫入，一次 commit） |
| POST | `/api/trades/bulk-delete` | `{version, ids:[...]}` 多選刪除（一次 commit） |
| POST | `/api/xq/csv` | `{data}`（base64）XQ 委託／成交回報 CSV（Big5，證券與期貨）→ 成交草稿，標示已記錄過的（同日期、代號／合約、買賣、數量）；不寫入 |
| GET | `/api/holding-watch` | 持股動向（50 秒快取）：每檔「盡快賣出／注意／低檔可望反彈／正常」與理由 |
| GET | `/api/value-picks?max_price=&etf=` | 平價潛力股：股價上限內、通過安全條件的模型排名 |
| POST | `/api/xq/import` | `{text, complete}` 貼上 XQ 庫存文字 → 解析並與系統持倉對帳，差異建立待確認動作 |
| POST | `/api/xq/screenshot` | `{image, complete}`（data URL）XQ 截圖 → 視覺模型判斷是庫存或成交回報：庫存與系統對帳；成交回報轉成待確認的成交紀錄（今天已記錄的相同成交不重複） |

## AI 助理與待確認動作

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/api/chat` | `{messages}` → 回答、使用的模型、出處、下載檔、待確認動作 |
| POST | `/api/actions/{id}/confirm` | 執行待確認動作（記錄成交、資金調撥、重跑分析） |
| POST | `/api/actions/{id}/cancel` | 取消待確認動作 |

寫入類動作在雲端模式會透過 GitHub API 寫入 repo 或觸發 workflow，完成後網站自動同步。
