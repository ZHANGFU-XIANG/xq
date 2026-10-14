"""ChatAgent：AI 助理。工具呼叫（function calling）依序使用 NVIDIA 開源模型 → Groq → Gemini，只根據工具回傳的程式計算結果回答。

防幻覺：
- 工具全部回傳程式計算的數字；系統提示要求任何數字都必須來自工具結果
- 最終回答用 verifier 核對（事實＝本輪所有工具輸出＋使用者訊息），不通過重寫一次，仍有未核實數字則標記「〔未核實〕」
- 寫入類動作（記錄成交、重跑分析）只建立「待確認動作」，使用者在前端按確認才執行
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from src.agents import on_demand
from src.agents.verifier import NUM_RE, fact_numbers, verify
from src.api import data as apidata
from src.config import now_tw, rules, settings, stock_meta
from src.llm.client import get_client, mark_exhausted, model_ok
from src.state import portfolio as pfm

log = logging.getLogger(__name__)
# 工具呼叫不設次數上限（使用者要求）；只防「一直重複查同樣東西」：連續 REPEAT_LIMIT 步全是已查過的查詢 → 不再給工具，要求直接回答
REPEAT_LIMIT = 2
REPEAT_NOTE = "（這項查詢本輪已經執行過，結果與先前相同，請直接根據已取得的資料回答）"
WRITE_TOOLS = {"record_trade", "record_transfer", "run_analysis", "import_xq_holdings", "make_weekly_report"}
_TL = threading.local()  # 本輪對話的工具快取：換模型重來或核對重寫時，同樣的查詢直接沿用，不重算
_USED: dict = {"model": None}  # 本輪實際回答的模型（顯示在回答下方）
PENDING: dict[str, dict] = {}

SYSTEM = """你是「2026 德明X富達投資擂台」參賽隊伍的投資顧問助理，用專業、精簡、自然的繁體中文（台灣用語）回答，像資深營業員或研究員對客戶說明。

服務範圍：
只回答投資、股市、經濟、金融、產業與公司營運、競賽規則、本隊持股與交易相關的問題，以及和這些主題高度相關的問題（例如國際局勢、政策對市場的影響）。
其他問題（閒聊、寫作、翻譯、程式、感情、娛樂、健康、法律等）一律婉拒：用一到兩句話說明這裡只提供投資與經濟相關的協助，並舉一個可以改問的例子；婉拒時不要呼叫工具。

誠實與出處（最高優先）：
A. 每個工具結果都有 source_id（例如 S1）。每個論點、數字、事件，都要在句末標註出處，格式「〔S1〕」；同一句用到多個來源寫「〔S1〕〔S3〕」。沒有出處可標的內容不要寫。
B. 只能使用本輪工具結果或前文已核實內容裡的數字與事件；禁止估算、憑記憶、編造數據或描述工具沒有回傳的新聞事件。提到新聞要寫出新聞標題重點與日期。
C. 因果關係只有在資料直接支持時才能下結論。資料只顯示數字時，只陳述數字本身，不要加上「顯示市場情緒」「代表資金湧入」這類解讀；真的需要解讀時，句首寫「推論：」。例如「法人賣超、股價同期下跌」只能寫成兩者同時發生，不能寫成因為賣超所以下跌。
D. 模型的機率、預期報酬、預測開盤都要說明是「模型估計，不是保證」。
E. 資料不足、互相矛盾或無法證實的部分，獨立一行以「無法確認：」開頭說明缺什麼；完全沒有資料時直接回答「目前沒有這項資料，無法回答」並說明原因（例如尚未公布），再提供工具查得到的最接近資料（例如最近已公布的數字），不要猜。

寫作格式：
- 不使用粗體、井字號標題、表情符號、分隔線、HTML 標籤，也不要用「當然」「好的」「以下是」「希望對你有幫助」「總結來說」「值得注意的是」這類客套或制式用語。
- 第一句直接給結論；接著用「1. 2. 3.」列出依據（每點附出處）；最後一行以「建議：」開頭寫具體動作。比較多檔時可以用簡單表格。
- 買賣結論一律用這幾個詞開頭，不要模稜兩可：「可以買」「等回檔再買（到多少價）」「先不要買」「續抱」「減碼」「全部出清」；當沖問題改用「可以當沖做多」「可以當沖做空」「等突破再進（到多少價）」「等回檔再進（到多少價）」「開盤後觀察」「準備平倉」，不可寫「先不要當沖」「不建議當沖」，一定要推薦標的與價位，而且一定照工具 verdict 原文，不可自行升級（例如工具寫開盤後觀察，就不能寫成可以當沖做多）。
  不要寫「可以考慮」「視情況」「建議觀察」這種沒有結論的話；工具判斷是觀望就寫「先不要買」並說原因。
  續抱一定要講續抱的理由（例如沒跌破停損、模型預期報酬仍為正）與「什麼情況要改賣」（停損價、停利價）。
- 註明資料日期。不要輸出 JSON、英文欄位名稱或工具名稱（例如 get_futures_view、record_transfer）；需要使用者做的事用一般說法，例如「跟我說要從證券戶轉多少到期貨戶」。

工具使用規則：
1. 回答任何股價、張數、金額、比例、機率等數字前，必須先呼叫工具取得；只能使用工具結果中的數字，禁止自行估算或憑記憶。
2. 使用者問到任何股票（包含每日建議沒有的），先用 find_stock 找代號，再用 analyze_stock 取得分析與操作建議。
2b. 問公司在做什麼、財報、EPS、營收、股利、重大訊息時，用 company_profile（公開資訊觀測站官方資料）。
3. 給買進建議時，要說明：屬於進攻籃或穩健籃與原因、建議股數（張＋零股）、限價、停損、出場計畫、主要風險。
4. 使用者說已成交（例如「我買了 2330 一張 1000 元」）時呼叫 record_trade；要重新產生報告時呼叫 run_analysis。這兩個工具只會建立待確認動作，請提醒使用者在畫面上按「確認」。
5. 「如果買/賣…會怎樣」用 simulate_trades 試算，說明成本、持股率與競賽規則影響。
5c. 使用者貼上看起來像 XQ 庫存表格的內容時，呼叫 import_xq_holdings（text 原樣傳入），說明差異並提醒按確認同步。
5d. 問到手上股票要不要加碼、續抱、減碼或賣掉時，呼叫 get_holding_advice，照工具給的結論、股數與條件回答。
5k. 問「推薦什麼／買什麼報酬高／有什麼好股票／現在能買什麼」時呼叫 get_top_picks（全市場高報酬排行，不看本隊持股），
    照排名列出前幾檔的綜合預期報酬（score）與組成、勝率、可信度、停損與目標、買點與退場日；這是持有約 40 個交易日的波段推薦，
    不是當沖，不要寫「收盤前出場」「不可留倉」；使用者說「股價 200 以下」時帶 max_price，給預算時帶 budget；
    可信度低（只有單一來源、查無消息或公司澄清）要明講可能是傳聞；
    不要因為本隊持股、資金或籃子額度而改結論。
5n. 問「今天有什麼消息面的股票／法說會利多／哪些因為消息大漲／能不能追」時呼叫 get_events，
    照分級回答買法（收盤前買或漲停排隊、隔天開盤買）、持有天數與歷史同類事件報酬，並用新聞標題說明可能原因（標註出處、不要自行推測）。
5m. 問美股、費半、那斯達克跌了對台股或持股的影響、「電子股跌爛要抱多久」「要不要先跑」時呼叫 get_us_shock，
    照 level 回答：一般重挫→續抱、約幾天回本；嚴重→開盤先賣、哪天買回；附歷史次數與平均數字。
5o. 問「要不要結單換股／賣哪檔換哪檔／獲利了要不要換」時呼叫 get_holding_advice，看每檔的 swap（換到哪檔、多幾 %、觸發規則、
    兩筆委託、競賽規則檢查）；沒有 swap 就說明原因（持有未滿 10 個交易日且獲利未達 10%，或綜合排名最佳股票扣成本後優勢不足）。
    10 個交易日、1%、10%、2% 這些門檻來自十年回測（避免來回換股付成本），不是競賽規則；競賽規則只有持股率 70%、持有與交易檔數、最後一日規定。
    換股一律全部賣出、同日買進。
5p. 所有金額（手續費、稅、損益、能買幾股、打平價）一律用 calculate 計算，不可自己乘除。
5q. 問風險、「大盤跌會虧多少」「會不會被追繳」「黑天鵝」「最近要注意什麼（結算、除權息、處置）」時呼叫 get_risk；
    問國際股市、日股韓股、ADR、匯率時呼叫 get_global_markets；問「今天整體狀況／總報告」時呼叫 get_briefing；
    問「這檔多空理由／為什麼還抱」時呼叫 get_debate（結論仍以 get_holding_advice 為準）。
5r. 準確率要誠實：2002～2026 樣本外，排名第 1 的推薦 20 天後賺錢機率約 50%、前 5 名分散約 56%，優勢來自賺多賠少；
    「創新高機率」是 20 天內碰到 52 週新高的機率，不是賺錢機率。使用者問準確率時照這個說，不可宣稱 90% 以上。
5l. 問某檔「什麼時候賣／退場／出場／獲利了要不要跑／抱到什麼時候」時呼叫 get_exit_plan，第一句照 verdict（續抱／出場），
    講退場日、移動停利線與理由。
5j. 問「我的持股要不要抱／要不要換股／手上的股票今天怎麼做」這類持股問題時，先呼叫 get_decision_board，
    只要寫 2～4 句總結：第一句講現在最該做的一件事（例如先賣哪檔、可以買哪檔），再點出要注意的風險，句末加出處。
    逐檔的結論、張數、限價、理由與停損條件，系統會自動附在你的回答後面，不要再逐檔列出。
5b. 問到警示、漲跌停、負面新聞、要不要賣、該賣多少時，先呼叫 get_alerts，照工具給的建議股數與理由回答。
6. 競賽規則（持股率 ≥70%、至少 5 檔、10 筆交易、禁止權證、籃子比例、最短持有天數）只用在「本隊持股、持股建議、交易紀錄、每日／週報告」。
   查個股、問能不能買、融資、當沖、放空、權證或其他一般投資問題時，不要套用競賽規則、不要加上持股率或籃子額度的限制或提醒，
   照工具給的個股結論與張數直接回答；使用者要融資就照融資算（成數 60%），要當沖就給當沖計畫。
5i. 要做週報、週報 PPT、本週交易報告時呼叫 make_weekly_report，回答時說明週次、期間、成交筆數，並提醒用下方的下載按鈕下載。
5g. 問 ETF 時用 get_etf_view，比例一律用 today_etf_share_pct（今日實際比例），不要把「基準比例」當成今日比例；個別 ETF 用 analyze_stock（含折溢價、類別、ETF 籃張數）；問期貨、台指期、選擇權、避險時用 get_futures_view，並照實說明回測有沒有優勢；沒有優勢就直說不建議。
5h. 使用者說期貨／選擇權成交時，用 record_trade 並填 contract（例如 TMF202610、TXO202610P46000）；說轉帳到期貨戶或證券戶時用 record_transfer。
5e. 問「還有什麼可以注意／潛在標的／候補」時呼叫 get_potential_picks；問某檔為什麼沒被推薦時，用 analyze_stock，依其中 today_status 回答：排第幾名、和最後一檔入選者的分數差距、拖累分數的項目（weakest_components 與 risks），不要自行推測其他原因。
5f. 這是連續對話：使用者說「它」「這檔」「剛剛那檔」「第二個」時，指前文提到的股票或項目，直接沿用，不要再問一次；需要數字時仍要重新呼叫工具。
7. 問到新聞、事件、國際局勢對股票的影響時，用 search_news 或 get_macro 取得真實新聞，並用 analyze_stock 的國際敏感度或歸因數據佐證影響幅度；沒有數據支持的影響不要寫。"""

# 出處說明：讓使用者知道每個 source_id 背後是哪個資料來源
SOURCE_DESC = {
    "find_stock": "證交所／櫃買中心上市櫃清單",
    "stock_profile": "證交所／櫃買中心日成交資料、FinMind（本益比、月營收、法人買賣超、融資券）",
    "company_profile": "公開資訊觀測站",
    "analyze_stock": "本系統模型計算（資料：證交所／櫃買日 K、FinMind 法人與月營收、Yahoo Finance 美股收盤）",
    "get_daily_report": "本系統每日盤前報告",
    "get_portfolio": "本隊成交紀錄（系統登錄）",
    "search_news": "新聞（Google 新聞、鉅亨網等，附原文連結）",
    "get_macro": "本系統國際大事分析（新聞＋Yahoo Finance 國際市場收盤）",
    "get_rules": "競賽官方規則（德明財經科技大學競賽網站）",
    "get_holding_advice": "本系統持股建議（證交所即時報價計算）",
    "get_alerts": "證交所／櫃買中心即時報價、注意與處置股公告、新聞",
    "get_model_health": "本系統模型回測與實際命中紀錄",
    "simulate_trades": "本系統試算（競賽手續費 0.1425%、證交稅）",
    "get_potential_picks": "本系統每日排名",
    "get_decision_board": "本系統買賣決策總表（盤中即時價、一個月模型、每日排名）",
    "get_holding_watch": "本系統持股動向（證交所即時報價、一個月模型）",
    "get_value_picks": "本系統平價潛力股排名（一個月模型、注意處置股公告）",
    "get_top_picks": "本系統高報酬推薦（全市場一個月模型排序、證交所即時報價，不參考本隊持股）",
    "get_events": "本系統資訊事件偵測（證交所即時價量、新聞與公開資訊觀測站標題；2022～2026 年 15,960 次事件研究）",
    "get_us_shock": "本系統美股重挫預警（Yahoo Finance 費城半導體收盤、2021～2026 年 117 次重挫事件研究）",
    "get_exit_plan": "本系統退場規則（回測：時間出場、移動停利、大盤燈號；證交所即時報價）",
    "get_daytrade": "本系統當沖排行（證交所／櫃買可當沖名單、證交所即時報價、日 K 回測）",
    "get_recovery_plan": "本系統虧損補回計畫（一個月模型、期貨回測、ETF 預估淨值）",
    "get_trade_stats": "本系統交易紀錄統計（data/trades.csv 重播計算，先進先出）",
    "get_regime": "本系統大盤攻守燈號（加權指數日 K 與即時報價、美股 VIX／費半，回測驗證）",
    "get_data_sources": "本系統資料來源監控",
    "get_etf_view": "本系統 ETF 籃計算（證交所 ETF 預估淨值、一個月模型）",
    "get_futures_view": "本系統期貨建議（期交所 MIS 即時報價與 OpenAPI 保證金、加權指數 5 年回測）",
    "record_transfer": "待確認動作",
    "get_futures_quote": "期交所 MIS 即時報價與 OpenAPI 保證金",
    "make_weekly_report": "本系統週報（成交紀錄、每日報告、證交所／期交所收盤價）",
    "record_trade": "待確認動作", "run_analysis": "待確認動作", "import_xq_holdings": "使用者貼上的 XQ 庫存",
}
def _system() -> str:
    """系統提示＋今天日期＋目前持股名單（只放代號與名稱；數字一律要用工具取得，才能核對出處）。"""
    try:
        held = "、".join(f"{c} {stock_meta(c)['name']}" for c in pfm.build_portfolio().positions) or "目前沒有持股"
    except Exception:  # noqa: BLE001
        held = "（讀取失敗，請呼叫 get_portfolio）"
    return (SYSTEM + f"\n今天是 {now_tw().date().isoformat()}。"
            f"\n本隊目前持股：{held}。使用者說「我的股票」「持股」「庫存」「這些股票」時就是指這些；"
            "問到持股表現、要不要賣、要不要加碼時，先呼叫 get_portfolio 與 get_holding_watch（盤中動向），需要具體股數再呼叫 get_holding_advice；"
            "問到虧損怎麼補回、ETF 或期貨怎麼操作時呼叫 get_recovery_plan；問便宜又安全的股票時呼叫 get_value_picks；"
            "問當沖、今天能不能當沖、哪檔適合當沖、某檔可不可以當沖時，每次都要重新呼叫 get_daytrade（問特定股票要帶 code），"
            "不可沿用前面對話的當沖結果（盤中價格每分鐘在變）；回答要寫出資料時間（fetched_at、quote_time），"
            "並用 market_now 說明當下盤勢：加權即時漲跌、大盤燈號、美股預警、今天因消息大漲的股票（附新聞標題）；"
            "（以下只限當沖問題）問當沖買賣時機或掛單價時，用 levels 的買點、賣點、停損回答，並照 levels.note 說明這些價位未經分鐘資料驗證、"
            "當沖買到就要在賣點或收盤前出場；"
            "第一句照工具的 verdict 給結論（可以當沖做多／可以當沖做空／等突破再進／等回檔再進），講進場價、停損、目標、張數與平倉時間；"
            "問今天當沖什麼時一定要推薦：用 picks 的前幾名（plan、trigger、levels）回答，不要說不建議當沖；"
            "問交易紀錄、勝率、賺賠、手續費、操作檢討時呼叫 get_trade_stats；問大盤、該攻該守、要不要撤資時呼叫 get_regime。"
            "建議只能來自工具結果，不要自己加上工具沒有的買賣建議（例如工具沒說減碼就不要建議減碼）；"
            "也不要自己加上工具沒有的風險警語或保守提醒（例如「當沖風險高請謹慎」），使用者已了解風險。")


RULES_URL = "https://sites.google.com/gs.takming.edu.tw/2026pmcxc/競賽專區/競賽規則"


def _tag(out, n: int, name: str):
    """在工具結果最前面加上出處編號，模型引用時寫〔S{n}〕。"""
    if not isinstance(out, dict):
        out = {"result": out}
    return {"source_id": f"S{n}", "source": SOURCE_DESC.get(name, name), **out}


def _links(name: str, r: dict) -> list[dict]:
    """出處可點的原文連結（新聞、觀測站、競賽規則）。"""
    if name == "search_news":
        return [{"title": n.get("title", ""), "url": n["url"]} for n in r.get("news", [])[:6] if n.get("url")]
    if name == "analyze_stock":
        return [{"title": n.get("title", ""), "url": n["url"]} for n in (r.get("news") or [])[:3] if n.get("url")]
    if name == "get_macro":
        return [{"title": n.get("title", ""), "url": n["url"]} for e in (r.get("macro") or [])[:3]
                for n in (e.get("news") or [])[:1] if n.get("url")]
    if name == "company_profile":
        return [{"title": "公開資訊觀測站", "url": "https://mops.twse.com.tw/mops/#/web/t146sb05"}]
    if name == "get_rules":
        return [{"title": "競賽規則", "url": RULES_URL}]
    return []


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️]")
_CITE_VARIANT = re.compile(r"[【\[［（(]\s*S\s*(\d+)\s*[】\]］）)]")
# 模型偶爾自己加的制式警語（使用者已了解風險，要求不要；只刪這幾種固定說法，不動有具體數字的風險說明）
_BOILER = re.compile(r"[，、；]?\s*(請|並)?(務必|自行)?(注意|留意|評估)(當沖|投資|操作)?風險(並謹慎(操作|評估))?[。！]?"
                     r"|[，、；]?\s*並?遵守競賽規則（如適用）[。]?|[，、；]?\s*投資一定有風險[^。\n]*[。]?")


DT_VERDICTS = ("可以當沖做多", "可以當沖做空", "等突破再進", "等回檔再進", "等打開再進", "開盤後觀察", "準備平倉",
               "不在當沖名單", "資料不足", "先不要當沖")


def _fix_daytrade_verdict(text: str, outputs: list) -> str:
    """單檔當沖：第一行的結論一定要和工具 verdict 一致（模型有時把「開盤後觀察」寫成「可以當沖做多」）。"""
    dts = [o["result"] for o in outputs if o["tool"] == "get_daytrade" and isinstance(o["result"], dict)
           and o["result"].get("code") and o["result"].get("verdict") in DT_VERDICTS]
    if len(dts) != 1 or not text:
        return text
    v = dts[0]["verdict"]
    first, _, rest = text.partition("\n")
    for w in DT_VERDICTS:
        if w != v and w in first:
            first = first.replace(w, v)
    if v not in first:
        first = f"{v}：{first}"
    return first + ("\n" + rest if rest else "")


def _norm_cites(text: str) -> str:
    """出處格式統一成〔S1〕：模型有時寫成【S1】、[S1]、（S1），原本會被判成「未附出處」。"""
    return _CITE_VARIANT.sub(lambda m: f"〔S{m.group(1)}〕", text or "")


_FILLERS = re.compile(r"^(當然|好的|沒問題|以下是[^。：]*[。：]?|希望(以上|這些)?對你有(所)?幫助[。！]?)\s*")


def _style(text: str) -> str:
    """去除 AI 慣用符號：粗體、井字號標題、分隔線、HTML 換行、表情符號、制式開場白。"""
    lines = []
    for ln in text.replace("<br>", "；").replace("<br/>", "；").splitlines():
        if re.fullmatch(r"\s*([-*_]\s*){3,}\s*", ln):
            continue
        ln = re.sub(r"^\s{0,3}#{1,6}\s*", "", ln)
        ln = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), ln)
        ln = _EMOJI.sub("", ln)
        lines.append(ln.rstrip())
    out = "\n".join(lines).strip()
    out = _FILLERS.sub("", out)
    out = _BOILER.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out)


_DIG = str.maketrans("0123456789", "abcdefghij")
_UNDIG = str.maketrans("abcdefghij", "0123456789")


def _hide_cites(text: str) -> str:
    """出處編號〔S12〕與條列序號（行首的「6.」「7、」）暫時改成不含數字的字母，避免被數字核對當成數據。"""
    text = re.sub(r"〔S(\d+)〕", lambda m: f"〔S{m.group(1).translate(_DIG)}〕", text)
    return re.sub(r"(?m)^(\s*)(\d{1,2})([.、])", lambda m: f"{m.group(1)}§{m.group(2).translate(_DIG)}§{m.group(3)}", text)


def _show_cites(text: str) -> str:
    text = re.sub(r"§([a-j]+)§", lambda m: m.group(1).translate(_UNDIG), text)
    return re.sub(r"〔S([a-j]+)〕", lambda m: f"〔S{m.group(1).translate(_UNDIG)}〕", text)


MODEL_TOOLS = {"analyze_stock", "get_daily_report", "get_potential_picks", "get_model_health", "stock_profile",
               "get_holding_advice", "get_macro", "get_decision_board"}


def _flag_uncited(text: str) -> tuple[str, int]:
    """條列的每一點都要有出處；沒有出處、也沒標明「推論／無法確認」的，標示〔未附出處〕。"""
    n = 0
    out = []
    for ln in text.splitlines():
        body = re.sub(r"^\s*(\d+[.、]|[-*])\s*", "", ln)
        is_point = body != ln.lstrip() or re.match(r"^\s*(\d+[.、]|[-*])\s", ln)
        if is_point and body and "〔S" not in ln and not body.startswith(("推論", "無法確認", "建議")):
            if not re.search(r"\d", body) and re.search(r"建議|可能|若|如果|否則|則|應該|宜|風險|最好", body):
                # 沒有數字、語氣是判斷或建議的句子（例如「未減碼可能面臨成交困難，建議執行」）是模型的推論，
                # 標明「推論：」即可，不算無法證實；沒有數字但在陳述事實的句子仍要出處
                ln = ln[:len(ln) - len(body)] + "推論：" + body
            else:
                ln = ln.rstrip() + "〔未附出處〕"
                n += 1
        out.append(ln)
    return "\n".join(out), n


def _check_cites(text: str, n_sources: int) -> tuple[str, list[str]]:
    """引用不存在的出處編號 → 標示「出處不明」。"""
    bad = []

    def repl(m):
        i = int(m.group(1))
        if 1 <= i <= n_sources:
            return m.group(0)
        bad.append(f"S{i}")
        return "〔出處不明〕"

    return re.sub(r"〔S(\d+)〕", repl, text), bad

TOOLS = [
    {"name": "find_stock", "description": "用股票名稱或代號搜尋台股（上市櫃、ETF）",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "stock_profile", "description": "個股基本盤：價量、52 週高低、本益比/淨值比/殖利率、月營收、法人、融資券、模型分數",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "company_profile", "description": "公開資訊觀測站公司資料：主要業務、董事長、資本額、成立／上市日、近 8 季營收毛利率營益率 EPS、最新財報摘要、月營收、重大訊息、內部人持股、股利",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "analyze_stock", "description": "個股完整分析與操作建議：進攻/穩健雙籃分數與理由、一個月模型、隔夜預測、今日漲跌歸因、國際因子敏感度、新聞，以及買進張數或持股賣出建議",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "get_daily_report", "description": "取得某日（預設最新）每日報告重點：攻守配置、交易建議、規則檢查、大盤預測、排行",
     "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，可省略"}}}},
    {"name": "get_portfolio", "description": "目前持股、現金、總資產、報酬率、持股率、交易紀錄。nav／return_pct 是競賽 App 口徑（投資組合總值＝1,000 萬＋未實現＋已實現，手續費不扣，與 App 一致），nav_net／return_net_pct 為扣手續費後淨值，app.cash 為 App 的持有現金",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_news", "description": "搜尋新聞（個股名稱或關鍵字），回傳標題與連結",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_macro", "description": "最新國際大事與對持股的影響金額、後續決策",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_rules", "description": "競賽規則", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_holding_advice", "description": "庫存中每檔股票的建議：加碼／續抱／減碼／全部出清，含股數、限價、理由與停損停利條件（依目前庫存與即時價計算）；以及結單轉股（swap：全部賣出換到綜合排名哪一檔、扣成本後多幾 %、觸發規則、兩筆委託、競賽規則檢查）",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_alerts", "description": "即時警示：持股跌停／跌破停損／負面新聞與建議賣出股數、今日建議買進前的風險、全市場漲跌停與原因、注意／處置股",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_model_health", "description": "模型回測表現、實際命中率與調整 Agent 紀錄",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "simulate_trades", "description": "試算一組假設買賣後的現金、交易成本、持股率、雙籃比例與規則檢查（不會真的下單或記錄）",
     "parameters": {"type": "object", "properties": {"trades": {"type": "array", "items": {"type": "object", "properties": {
         "code": {"type": "string"}, "side": {"type": "string", "enum": ["BUY", "SELL"]}, "shares": {"type": "integer"},
         "price": {"type": "number"}, "margin": {"type": "boolean"}}, "required": ["code", "side", "shares"]}}},
         "required": ["trades"]}},
    {"name": "record_trade", "description": "記錄在競賽平台的實際成交（建立待確認動作，使用者確認後才寫入）",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string", "description": "股票／ETF 代號；期貨填 TX、MTX、TMF；選擇權填 TXO"},
         "side": {"type": "string", "enum": ["BUY", "SELL"]},
         "shares": {"type": "integer", "description": "股數；期貨與選擇權為口數"},
         "price": {"type": "number", "description": "成交價；期貨為點數、選擇權為權利金點數"}, "margin": {"type": "boolean"},
         "contract": {"type": "string", "description": "期貨／選擇權合約，例如 TMF202610、TXO202610P46000"},
         "date": {"type": "string", "description": "YYYY-MM-DD，預設今天"}},
         "required": ["code", "side", "shares", "price"]}},
    {"name": "record_transfer", "description": "記錄證券戶與期貨戶之間的資金調撥（建立待確認動作）",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["TO_FUTURES", "TO_SECURITIES"], "description": "TO_FUTURES＝證券戶轉入期貨戶"},
         "amount": {"type": "number"}, "date": {"type": "string"}}, "required": ["direction", "amount"]}},
    {"name": "get_etf_view", "description": "ETF 籃建議：今日配置比例與理由、候選 ETF 的類別、分數、折溢價、建議張數",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "make_weekly_report", "description": "製作週報 PPT：彙整指定一週的實際成交、每筆理由、每日市場與事件、總資產變化、期末持股與下週計畫，產生可下載的 .pptx",
     "parameters": {"type": "object", "properties": {"week": {"type": "string", "description": "那一週的任一天 YYYY-MM-DD，省略為本週"}}}},
    {"name": "get_futures_quote", "description": "期貨／選擇權行情與規格：TX 大台、MTX 小台、TMF 微台的各月報價、基差、保證金、每點價值、槓桿、結算日；TXO 則回傳賣權報價表",
     "parameters": {"type": "object", "properties": {"root": {"type": "string", "enum": ["TX", "MTX", "TMF", "TXO"]}}, "required": ["root"]}},
    {"name": "get_futures_view", "description": "期貨與選擇權建議：方向模型機率、回測是否有優勢、模式（衝名次／補回虧損／保名次）、建議口數、保證金、賣權避險",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "import_xq_holdings", "description": "解析使用者從 XQ 全球贏家庫存畫面複製貼上的表格，與系統持股比對，產生需確認的同步調整",
     "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "使用者貼上的原始表格文字"}},
                    "required": ["text"]}},
    {"name": "get_holding_watch", "description": "持股動向（盤中每分鐘）：每檔持股目前是「盡快賣出／注意／低檔可望反彈／正常」與理由、即時漲跌、RSI、模型一個月收正機率",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_value_picks", "description": "平價潛力股：股價上限內、模型看漲且波動與回檔小的股票排名（一張金額、收正機率、預期報酬、預期回檔）",
     "parameters": {"type": "object", "properties": {"max_price": {"type": "number", "description": "股價上限（元），預設 50"}}}},
    {"name": "get_recovery_plan", "description": "虧損怎麼補回：每檔持股續抱／加碼／換股／停損的比較與理由（含換股成本與回本所需天數）、期貨補回虧損方案、ETF 籃建議",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_regime", "description": "大盤攻守燈號與撤資預警：綠燈積極進攻／灰燈中性／黃燈預警暫停追高／紅燈轉弱提早撤資，含轉守與轉攻的具體點位、預警訊號與回測依據",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_trade_stats", "description": "交易紀錄統計與檢討：每次賣出的已實現損益與持有天數、勝率、平均獲利／虧損、獲利因子、每檔買賣次數與已實現＋未實現損益、手續費與證交稅、每日交易、競賽筆數與標的門檻",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_decision_board", "description": "買賣決策總表：哪些可以買（張數、限價、資金從哪來）、哪些等回檔再買、哪些先不要買與原因、每檔持股續抱／減碼／出清的理由與改變結論的條件（盤中即時價每 1 分鐘重算）",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_top_picks", "description": "綜合排名（高報酬推薦）：一個月模型＋今天的資訊事件（乘上消息可信度、防假消息）＋美股重挫影響，"
                                       "換算成綜合預期 20 日報酬排序（不參考本隊持股與資金）；含組成明細、勝率、可信度、買點、停損、目標、退場日",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "幾檔，預設 10"},
         "max_price": {"type": "number", "description": "股價上限（元／股），例如 200 代表只看一股 200 元以內"},
         "budget": {"type": "number", "description": "資金預算（元），只列買得起的股票並算張數"},
         "split": {"type": "integer", "description": "預算分成幾檔，預設 1"},
         "odd": {"type": "boolean", "description": "允許零股（預算買不起一張時）"}}}},
    {"name": "get_events", "description": "今天因消息（法說會、營收、重大訊息、新聞）爆量跳空大漲的股票：分級（買得到的資訊衝擊／漲停／強勢放量）、"
                                    "漲幅、跳空、量比、買法與持有天數、歷史同類事件報酬、相關新聞與重大訊息標題",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_us_shock", "description": "美股（費城半導體）重挫對台股電子股的影響：最近一晚費半漲跌、是一般重挫（續抱、約幾天回本）還是嚴重（開盤先賣、哪天買回），"
                                      "每檔持股（或指定代號）與費半的連動 β、估計開盤影響與動作，附歷史事件研究數字",
     "parameters": {"type": "object", "properties": {"codes": {"type": "string", "description": "逗號分隔代號（選填，預設本隊持股）"}}}},
    {"name": "get_exit_plan", "description": "個股什麼時候退場、獲利後怎麼出場：退場日、中途檢查日、移動停利線、目前該續抱或出場與理由（持有中用實際買進日與成本，"
                                       "沒持有則以今天買進試算，也可指定買進日與價格）",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"},
                                                     "entry_date": {"type": "string", "description": "買進日 YYYY-MM-DD（選填）"},
                                                     "entry_price": {"type": "number", "description": "買進價（選填）"}},
                    "required": ["code"]}},
    {"name": "get_daytrade", "description": "當沖建議：今日可現股當沖名單中最適合當沖的排行（盤中即時強弱、估計量比、進場價、停損、目標、張數、扣成本後損益）、"
                                     "整體結論（verdict）與同規則日線回測是否有優勢；給 code 則回傳該檔能不能當沖與理由",
     "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "股票代號（選填；問特定股票能不能當沖時填）"},
                                                     "max_price": {"type": "number", "description": "股價上限（元／股，選填），例如 100 代表只排一股 100 元以內的當沖標的"}}}},
    {"name": "get_potential_picks", "description": "潛在標的：模型看好但今天沒列入下單的股票（候補遞補、過熱等回檔），含排名、分數、為什麼還沒買、觸發條件",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_data_sources", "description": "各資料來源目前狀態：FinMind 各組 token 是否冷卻、證交所／櫃買中心官方來源最近成功或失敗時間",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run_analysis", "description": "重新執行分析並產生報告（建立待確認動作）",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["premarket", "postclose", "weekly", "tune"]}},
                    "required": ["mode"]}},
    {"name": "calculate", "description": "交易計算（與競賽 App 相同的進位規則）：op=cost 一筆成交的價金／手續費／證交稅／淨收付；op=pnl 買到賣的淨損益；op=shares_for_budget 預算可買幾股；op=breakeven 打平價。所有金額都要用這個算，不可心算",
     "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["cost", "pnl", "shares_for_budget", "breakeven"]},
                    "code": {"type": "string"}, "side": {"type": "string", "enum": ["BUY", "SELL"]}, "shares": {"type": "integer"},
                    "price": {"type": "number"}, "buy_price": {"type": "number"}, "sell_price": {"type": "number"},
                    "budget": {"type": "number"}}, "required": ["op", "code"]}},
    {"name": "get_risk", "description": "風險儀表板：壓力測試（大盤 −3～−20%、費半 −8%、台幣急貶）損益、期貨追繳點位、單日 VaR、到 11/27 收正機率模擬、黑天鵝指標（附 26 年回測是否有預測力）、未來 5 個交易日注意事項（結算、除權息、處置股、競賽關鍵日）、資料檢查",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_global_markets", "description": "美日韓陸港歐股、台股 ADR、AI 半導體、商品、匯率、利率最新收盤與漲跌、異常波動重點、對持股的估計影響金額",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_briefing", "description": "總報告：統整資料檢查、風險、日曆、夜盤與國際、產業新聞、假消息過濾、PTT 論壇風向、持股多空辯論",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_debate", "description": "持股與前 5 名推薦的多空辯論：只引用程式事實的多方／空方論點、主要分歧、什麼情況會改變結論（結論以持股建議為準）",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_fin_reports", "description": "財報：今天新公布財報的公司、持股與指定股票最新一季（年初至今累計）EPS、去年同期、營收年增、毛利率、轉虧／轉盈／財報利多／利空標記",
     "parameters": {"type": "object", "properties": {"codes": {"type": "string", "description": "逗號分隔的代號，可省略"}}}},
    {"name": "get_market_anomaly", "description": "台股市場波動是否異常：加權今天相當於幾倍平常波動、波動率在近 10 年的百分位、上漲下跌與漲跌停家數、偏離大盤最多的產業、量價異常個股、歷史上類似情況之後 20 日的表現",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_presentation", "description": "決賽簡報（佔總分 50%）：最新版投影片與講稿檔案、每張講稿內容與秒數、Q&A 預備題與回答重點",
     "parameters": {"type": "object", "properties": {}}},
]


# ---------------------------------------------------------------- 工具實作
def _record(args: dict) -> dict:
    from src.agents.compliance import is_warrant
    from src.state.portfolio import DERIV_ROOTS, asset_of

    code = str(args["code"]).strip().upper()
    if is_warrant(code):
        return {"error": "權證不可交易（競賽規則）"}
    row = {"date": args.get("date") or now_tw().date().isoformat(), "code": code, "side": args["side"].upper(),
           "shares": int(args["shares"]), "price": float(args["price"]), "margin": int(bool(args.get("margin"))),
           "note": "AI 助理記錄", "contract": str(args.get("contract") or "").upper()}
    row["asset"] = asset_of(row)
    if code in DERIV_ROOTS:
        if not row["contract"].startswith(code) or len(row["contract"]) < len(code) + 6:
            return {"error": f"期貨／選擇權請提供完整合約，例如 {code}202610" + ("P46000" if code == "TXO" else "")}
        name = row["contract"]
        aid = uuid.uuid4().hex[:10]
        PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
        return {"pending_action": aid, "summary": f"記錄成交：{row['date']} {'買進' if row['side'] == 'BUY' else '賣出'} "
                f"{name} {row['shares']} 口 @ {row['price']}", "status": "等待使用者在畫面上按確認"}
    name = on_demand.stock_meta(code)["name"]
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
    return {"pending_action": aid, "summary": f"記錄成交：{row['date']} {'買進' if row['side'] == 'BUY' else '賣出'} "
            f"{code} {name} {row['shares']:,} 股 @ {row['price']}" + ("（融資）" if row["margin"] else ""),
            "status": "等待使用者在畫面上按確認"}


def xq_sync_preview(text: str, complete: bool = True) -> dict:
    """解析 XQ 貼上的庫存並與系統比對；有差異時建立待確認動作。"""
    from src.agents import xq_import

    parsed = xq_import.parse(text)
    if not parsed["rows"]:
        return {"error": "沒有解析到任何持股。請在 XQ 庫存畫面全選（Ctrl+A）後複製（Ctrl+C），連同表頭一起貼上。",
                "skipped": parsed["skipped"]}
    rec = xq_import.reconcile(parsed, complete)
    out = {"parsed": parsed["rows"], "columns": parsed["columns"], **rec}
    if rec["adjustments"]:
        aid = uuid.uuid4().hex[:10]
        PENDING[aid] = {"kind": "xq_sync", "payload": rec["adjustments"], "created": time.time()}
        out["pending_action"] = aid
        out["summary"] = "依 XQ 庫存同步：" + "、".join(
            f"{'補記買進' if a['side'] == 'BUY' else '補記賣出'} {a['code']} {a['shares']:,} 股@{a['price']}" for a in rec["adjustments"])
    return out


def xq_screenshot_preview(image: bytes, complete: bool = True) -> dict:
    """從截圖讀取 XQ 庫存或成交回報；庫存 → 與系統對帳，成交 → 建立待確認的成交紀錄。都需使用者確認才寫入。"""
    from src.agents import xq_import, xq_vision

    try:
        table, model = xq_vision.read_table(image)
    except Exception as e:  # noqa: BLE001
        return {"error": f"截圖辨識失敗：{str(e)[:200]}"}
    rows, warns = xq_vision.check_rows(table)
    base = {"source": "screenshot", "model": model, "kind": table.kind, "warnings": warns,
            "read": [{k: r[k] for k in ("code", "name", "read_name", "shares", "board", "price", "amount", "side", "status")} for r in rows]}
    if table.kind == "holdings":
        rows = [r for r in rows if r["shares"] > 0]
        if not rows:
            return {**base, "error": "截圖中沒有讀到持股，請確認截的是「庫存」畫面且表格完整。"}
        rec = xq_import.reconcile({"rows": rows}, complete)
        out = {**base, "parsed": rows, **rec}
        if rec["adjustments"]:
            aid = uuid.uuid4().hex[:10]
            PENDING[aid] = {"kind": "xq_sync", "payload": rec["adjustments"], "created": time.time()}
            out["pending_action"] = aid
            out["summary"] = "依截圖庫存同步：" + "、".join(
                f"{'補記買進' if a['side'] == 'BUY' else '補記賣出'} {a['code']} {a['shares']:,} 股@{a['price']}" for a in rec["adjustments"])
        return out
    if table.kind == "fills":
        return {**base, **_fills_to_trades(rows)}
    return {**base, "error": "看不出是庫存或成交回報的畫面，請截 XQ「帳務→庫存」或「成交回報」的表格。"}


def _fills_to_trades(rows: list[dict]) -> dict:
    """成交回報 → 成交紀錄。每一列各記一筆（整股、零股、不同價格分開，成本分批計算）；
    已刪單、未成交略過；今天已記錄過的相同成交（代號、方向、股數、價格都一樣）標示為重複。"""
    from collections import Counter

    today = now_tw().date().isoformat()
    have = Counter((t["code"], t["side"], int(float(t["shares"])), round(float(t["price"]), 3))
                   for t in pfm.load_trades() if t.get("date") == today)
    trades, dup, skipped = [], [], []
    for r in rows:
        st = r.get("status") or ""
        if r["side"] not in ("BUY", "SELL") or r["filled"] <= 0 or any(k in st for k in ("刪", "失敗", "未成交", "取消")):
            skipped.append(f"{r['code']} {r['name']}（{st or '未成交'}）")
            continue
        if not r["price"]:
            skipped.append(f"{r['code']}（缺成交價，請手動記錄）")
            continue
        k = (r["code"], r["side"], r["filled"], round(float(r["price"]), 3))
        if have[k] > 0:
            have[k] -= 1
            dup.append(f"{r['code']} {'買' if r['side'] == 'BUY' else '賣'} {r['filled']:,} 股@{r['price']}")
            continue
        trades.append({"date": today, "code": r["code"], "side": r["side"], "shares": r["filled"], "price": r["price"],
                       "margin": int(r["margin"]), "note": f"截圖匯入成交{('・' + r['board']) if r.get('board') else ''}"})
    out = {"trades": trades, "skipped": skipped, "duplicates": dup}
    if not trades:
        out["in_sync"] = True
        return out
    try:  # 先驗證（例如賣出超過持有）
        pfm.build_portfolio(pfm.load_trades() + [{k: str(v) for k, v in t.items()} for t in trades])
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"這些成交與系統持倉對不起來：{str(e)[:160]}。若之前有漏記，請改用庫存截圖對帳。"}
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trades", "payload": trades, "created": time.time()}
    out["pending_action"] = aid
    out["summary"] = f"記錄 {len(trades)} 筆成交：" + "、".join(
        f"{'買進' if t['side'] == 'BUY' else '賣出'} {t['code']} {stock_meta(t['code'])['name']} {t['shares']:,} 股@{t['price']}" for t in trades)
    return out


def _transfer(args: dict) -> dict:
    amt = float(args["amount"])
    if amt <= 0:
        return {"error": "金額必須大於 0"}
    row = {"date": args.get("date") or now_tw().date().isoformat(), "code": "TRANSFER", "side": args["direction"],
           "shares": 1, "price": amt, "margin": 0, "note": "資金調撥", "asset": "CASH", "contract": ""}
    aid = uuid.uuid4().hex[:10]
    PENDING[aid] = {"kind": "record_trade", "payload": row, "created": time.time()}
    text = "證券戶 → 期貨戶" if args["direction"] == "TO_FUTURES" else "期貨戶 → 證券戶"
    return {"pending_action": aid, "summary": f"記錄資金調撥：{row['date']} {text} {amt:,.0f} 元", "status": "等待使用者在畫面上按確認"}


def _run(args: dict) -> dict:
    aid = uuid.uuid4().hex[:10]
    names = {"premarket": "盤前報告", "postclose": "盤後檢討", "weekly": "週報", "tune": "模型調校"}
    PENDING[aid] = {"kind": "run_analysis", "payload": {"mode": args["mode"]}, "created": time.time()}
    return {"pending_action": aid, "summary": f"重新執行：{names.get(args['mode'], args['mode'])}", "status": "等待使用者在畫面上按確認"}


def _data_gaps(a: dict) -> dict | None:
    """哪些資料這次沒取得（例如 FinMind 額度用完）；這些項目在評分中以 0 計，不代表中性。"""
    from src.agents import market_data

    gaps = []
    if not a.get("revenue"):
        gaps.append("月營收")
    if market_data.tw_institutional(a.get("code", "")).empty:
        gaps.append("三大法人買賣超")
    from src.data import providers

    used = {k: providers.source_of(k, a.get("code", "")) for k in ("institutional", "revenue", "valuation", "margin")}
    used = {k: v for k, v in used.items() if v}
    if not gaps:
        return {"missing": [], "sources_used": used} if used else None
    return {"missing": gaps, "sources_used": used,
            "note": "這些資料這次沒有取得（資料來源暫時無法使用或尚無資料），評分時以 0 計；"
                    "這不代表中性，相關分數與排名可能失真，回答時必須以「無法確認：」說明"}



def _market_now() -> dict:
    """當沖要看的即時盤勢：加權／櫃買即時指數、大盤燈號、美股（費半）預警、今天因消息爆量大漲的股票。每次都即時抓。"""
    out: dict = {}
    try:
        idx = apidata.live_quotes([]).get("indices") or {}
        out["indices"] = {k: {f: v.get(f) for f in ("name", "price", "chg_pct", "time")} for k, v in idx.items()}
    except Exception as e:  # noqa: BLE001
        out["indices_error"] = str(e)[:80]
    try:
        rg = apidata.live_regime()
        out["regime"] = rg.get("headline")
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.agents import us_shock

        sh = us_shock.assess()
        out["us_shock"] = {k: sh.get(k) for k in ("level", "headline", "advice")}
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.agents import events

        ev = events.scan()
        out["events_today"] = [{"code": e["code"], "name": e["name"], "label": e["label"], "chg_pct": e["chg_pct"],
                                "volume_ratio": e["volume_ratio"], "news": (e.get("news") or [{}])[0].get("title")}
                               for e in ev["items"][:6]]
        out["events_time"] = ev["time"]
    except Exception:  # noqa: BLE001
        pass
    return out


def _daytrade_live(code: str = "", max_price: float | None = None) -> dict:
    """當沖建議：強制以證交所最新即時報價重算（不用快取），並附即時盤勢與個股最新新聞／重大訊息。"""
    from src.agents import daytrade, events

    market = _market_now()
    if code:
        r = daytrade.stock(code)
        try:
            r["news"] = [{k: n.get(k) for k in ("title", "date", "source")} for n in events._headlines(code, r.get("name") or code)[:4]]
        except Exception:  # noqa: BLE001
            pass
        try:
            ev = next((e for e in events.scan()["items"] if e["code"] == code), None)
        except Exception:  # noqa: BLE001
            ev = None
        if ev:
            r["event_today"] = {k: ev.get(k) for k in ("label", "chg_pct", "gap_pct", "volume_ratio", "plan", "stats")}
        return {**r, "market_now": market, "fetched_at": now_tw().strftime("%Y-%m-%d %H:%M:%S")}
    b = daytrade.board(refresh=True, max_price=max_price)
    keep = ("rank", "code", "name", "verdict", "signal", "score", "price", "chg_pct", "from_open_pct", "volume_ratio",
            "atr_pct", "val20_e8", "short_ok", "entry", "stop", "target", "qty_text", "net_reward_pct", "net_risk_pct",
            "profit_at_target", "loss_at_stop", "trigger", "plan", "reasons", "size_note", "quote_time", "levels")
    slim = lambda r: {k: r[k] for k in keep if r.get(k) is not None}  # noqa: E731
    return {"verdict": b["verdict"], "time": b["time"], "fetched_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
            "phase": b["phase"], "phase_note": b["phase_note"], "market_now": market, "max_price": b["max_price"],
            "counts": b["counts"], "edge": b["edge"], "backtest": b["backtest"], "cost_note": b["cost_note"],
            "top": [slim(r) for r in b["top"]], "picks": [slim(r) for r in b["picks"]], "ranking": [slim(r) for r in b["rows"][:12]], "rules": b["rules"],
            "note": b["note"]}

def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "find_stock":
            return {"results": on_demand.resolve(args["query"])}
        if name == "stock_profile":
            return on_demand.profile(args["code"])
        if name == "company_profile":
            from src.agents import mops

            c = mops.company(args["code"])
            if not c.get("error"):
                c["news"] = [{k: n[k] for k in ("date", "title")} for n in c["news"][:8]]
            return c
        if name == "analyze_stock":
            a = on_demand.analyze(args["code"])
            if isinstance(a, dict) and not a.get("error"):
                a = {"data_gaps": _data_gaps(a), "today_status": apidata.today_status(a.get("code", args["code"]), a),
                     **a}  # 放最前面，壓縮時不會被截掉
            return a
        if name == "get_daily_report":
            return apidata.report_summary(args.get("date"))
        if name == "get_portfolio":
            return apidata.portfolio()
        if name == "search_news":
            return {"news": on_demand.search_news(args["query"], 10)}
        if name == "get_macro":
            return apidata.macro_latest()
        if name == "get_presentation":
            from src.agents import presentation

            return presentation.latest() or {"error": "還沒有產生簡報（每週五週報後自動產生）"}
        if name == "get_market_anomaly":
            from src.agents import market_anomaly

            return market_anomaly.snapshot()
        if name == "get_fin_reports":
            from src.agents import fin_report

            codes = [c.strip() for c in str(args.get("codes") or "").split(",") if c.strip()]
            return fin_report.run(codes)
        if name == "calculate":
            from src.agents import calc

            kw = {k: v for k, v in args.items() if k != "op" and v is not None}
            return calc.calculate(args["op"], **kw)
        if name == "get_risk":
            from src.agents import risk_lab, trade_calendar
            from src.state import portfolio as pfm2

            r = dict(risk_lab.snapshot())
            pf2 = pfm2.build_portfolio()
            r["calendar"] = trade_calendar.events(list(pf2.positions), list(pf2.derivs))
            r["black_swan"] = {"items": [{k: b[k] for k in ("name", "active", "label", "text")} for b in r["black_swan"]["items"]]}
            return r
        if name == "get_global_markets":
            from src.agents import global_markets

            return global_markets.run()
        if name == "get_briefing":
            from src.agents import agents_catalog, briefing
            from src.api import agents_bg

            b = briefing.LAST["v"] or agents_bg.refresh(agents_catalog.FAST)
            return {"time": b.get("time"), "sections": b.get("sections"), "failed": b.get("failed")}
        if name == "get_debate":
            from src.agents import debate

            d = debate.run()
            return {"time": d["time"], "note": d["note"], "items": [{k: x[k] for k in ("code", "name", "verdict", "lean", "bull", "bear",
                                                                                       "key_dispute", "what_changes")} for x in d["items"]]}
        if name == "get_rules":
            return rules()
        if name == "get_holding_advice":
            return on_demand.portfolio_plan()
        if name == "get_alerts":
            a = apidata.live_alerts()
            return {k: a[k] for k in ("generated_at", "trading_now", "quotes_date", "counts", "alerts", "limit_up_count",
                                      "limit_down_count", "up_industries", "down_industries", "attention_watch")} | {
                "limit_up_top": a["limit_up"][:10], "limit_down_top": a["limit_down"][:10]}
        if name == "get_data_sources":
            from src.data import providers

            return providers.status()
        if name == "get_decision_board":
            return apidata.decision_board()
        if name == "get_potential_picks":
            return apidata.potential()
        if name == "get_holding_watch":
            return apidata.holding_watch()
        if name == "get_value_picks":
            return apidata.value_picks(float(args.get("max_price") or 50), limit=10)
        if name == "get_top_picks":
            r = apidata.high_return_picks(int(args.get("limit") or 10), budget=args.get("budget") or None,
                                          split=int(args.get("split") or 1), odd=bool(args.get("odd")),
                                          max_price=args.get("max_price") or None)
            return {**r, "items": [{k: v for k, v in i.items() if k != "reasons"} | {"reasons": i["reasons"][:3]} for i in r["items"]]}
        if name == "get_events":
            from src.agents import events

            r = events.scan()
            return {**{k: v for k, v in r.items() if k != "tiers"},
                    "items": [{**i, "news": [n["title"] for n in i["news"][:3]]} for i in r["items"]]}
        if name == "get_us_shock":
            from src.agents import us_shock

            return us_shock.view([c.strip() for c in str(args.get("codes") or "").split(",") if c.strip()] or None)
        if name == "get_exit_plan":
            return apidata.exit_plan(str(args["code"]), args.get("entry_date"), args.get("entry_price"))
        if name == "get_daytrade":
            return _daytrade_live(str(args.get("code") or "").strip(), float(args.get("max_price") or 0) or None)
        if name == "get_recovery_plan":
            return apidata.recovery_plan()
        if name == "get_regime":
            return apidata.live_regime()
        if name == "get_trade_stats":
            st = apidata.trade_ledger()["stats"]
            return {**st, "closed": st["closed"][:30], "daily": st["daily"][:20]}
        if name == "get_model_health":
            return apidata.model_health()
        if name == "simulate_trades":
            return on_demand.simulate(args["trades"])
        if name == "record_trade":
            return _record(args)
        if name == "record_transfer":
            return _transfer(args)
        if name == "get_etf_view":
            return apidata.etf_view()
        if name == "make_weekly_report":
            r = apidata.weekly_report(args.get("week"))
            return {**r, "download": {"label": r["name"], "path": r["file"]}}
        if name == "get_futures_quote":
            d = apidata.futures_detail(args["root"])
            d.pop("system_view", None)
            return d
        if name == "get_futures_view":
            return apidata.futures_view()
        if name == "run_analysis":
            return _run(args)
        if name == "import_xq_holdings":
            return xq_sync_preview(args["text"])
        return {"error": f"未知工具 {name}"}
    except Exception as e:  # noqa: BLE001
        log.exception("工具 %s 失敗", name)
        return {"error": f"{name} 執行失敗：{str(e)[:200]}"}


def confirm(aid: str) -> dict:
    from src.api import sync

    act = PENDING.pop(aid, None)
    if not act or time.time() - act["created"] > 1800:
        return {"ok": False, "message": "動作不存在或已過期（30 分鐘），請重新詢問"}
    if act["kind"] == "record_trade":
        return {"ok": True, "message": sync.record_trade(act["payload"])}
    if act["kind"] == "xq_sync":
        rows = sorted(act["payload"], key=lambda r: r["side"] != "SELL")  # 先賣後買
        sync.record_trades(rows)
        return {"ok": True, "message": f"已依 XQ 庫存寫入 {len(rows)} 筆調整"}
    if act["kind"] == "record_trades":
        rows = sorted(act["payload"], key=lambda r: r["side"] != "SELL")
        sync.record_trades(rows)
        return {"ok": True, "message": f"已記錄 {len(rows)} 筆成交"}
    return {"ok": True, "message": sync.run_workflow(act["payload"]["mode"])}


def cancel(aid: str) -> dict:
    PENDING.pop(aid, None)
    return {"ok": True, "message": "已取消"}


# ---------------------------------------------------------------- LLM 迴圈
def _key(name: str, args: dict) -> str:
    return name + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)


def _cached_call(name: str, args: dict, cache: dict | None) -> dict:
    if cache is None or name in WRITE_TOOLS:  # 寫入類每次都要建立自己的待確認動作
        return call_tool(name, args)
    k = _key(name, args)
    if k not in cache:
        cache[k] = call_tool(name, args)
    return cache[k]


def _exec_step(calls: list[tuple[str, dict]], emit, outputs: list, start: int, seen: dict) -> tuple[list[dict], bool]:
    """執行模型這一步要求的所有工具：彼此獨立的查詢平行執行（加快回答）；
    本次嘗試已查過的相同查詢直接沿用原本的結果與出處編號。回傳（每個呼叫的結果, 是否全部都是重複查詢）。"""
    keys = [_key(n, a) for n, a in calls]
    all_repeat = all(k in seen for k in keys)
    todo: dict[str, tuple[str, dict]] = {}
    for k, (n, a) in zip(keys, calls):
        if k not in seen and k not in todo:
            todo[k] = (n, a)
            emit({"type": "tool", "name": n, "args": a})
    if todo:
        cache = getattr(_TL, "cache", None)
        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
            futs = {k: ex.submit(_cached_call, n, a, cache) for k, (n, a) in todo.items()}
        for k, (n, a) in todo.items():
            out = _tag(futs[k].result(), start + len(outputs) + 1, n)
            outputs.append({"tool": n, "args": a, "result": out})
            seen[k] = out
            emit({"type": "tool_result", "name": n, "ok": "error" not in out,
                  "action": out.get("pending_action") and {"id": out["pending_action"], "summary": out["summary"]}})
    return [seen[k] for k in keys], all_repeat


def _gemini_thinking(model: str):
    """關閉／降低思考：工具呼叫只需要挑工具與整理結果，長思考只會拖慢回答。"""
    from google.genai import types

    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=0)
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)  # 使用者要求不要深度思考
    return None


def _gemini_loop(messages: list[dict], emit, start: int = 0) -> tuple[str, list]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=t["parameters"])
             for t in TOOLS]
    sys_text = _system()
    outputs = []
    last_err = None
    for model in [m for m in settings()["llm"]["gemini_models"] if model_ok(m)]:
        contents = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])])
                    for m in messages]
        seen: dict = {}
        repeats = 0
        try:
            while True:
                force = repeats >= REPEAT_LIMIT
                cfg = types.GenerateContentConfig(
                    temperature=0, system_instruction=sys_text, thinking_config=_gemini_thinking(model),
                    tools=[types.Tool(function_declarations=decls)],
                    tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE")) if force else None,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
                resp = client.models.generate_content(model=model, contents=contents, config=cfg)
                cand = resp.candidates[0].content
                calls = [p.function_call for p in (cand.parts or []) if p.function_call]
                if not calls:
                    _USED["model"] = f"Google {model}（閉源備援）"
                    return resp.text or "", outputs
                if force:
                    raise RuntimeError("模型一直重複呼叫相同工具")
                contents.append(cand)
                outs, rep = _exec_step([(fc.name, dict(fc.args or {})) for fc in calls], emit, outputs, start, seen)
                repeats = repeats + 1 if rep else 0
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(name=fc.name, response={"result": out, **({"note": REPEAT_NOTE} if rep else {})})
                    for fc, out in zip(calls, outs)]))
        except Exception as e:  # noqa: BLE001
            last_err = e
            mark_exhausted(model, str(e))
            log.warning("Gemini %s 失敗：%s", model, str(e)[:200])
            if outputs:
                break
    raise RuntimeError(str(last_err))


GROQ_TOOL_CHARS = 4500  # Groq 免費方案每分鐘 token 上限小，備援時壓縮工具結果與前文
_HEAVY = ("news", "macro_sensitivity", "attribution", "kline", "top_drivers", "feature_ic", "limit_up_top", "limit_down_top")


def _compact(out, budget: int = GROQ_TOOL_CHARS) -> str:
    """去掉大型明細欄位（新聞列表、歸因、敏感度等），仍超過預算就截斷。完整結果仍用於數字核對。"""
    def strip(x, depth=0):
        if isinstance(x, dict):
            return {k: strip(v, depth + 1) for k, v in x.items() if k not in _HEAVY}
        if isinstance(x, list):
            return [strip(v, depth + 1) for v in x[:12]]
        return x

    s = json.dumps(strip(out), ensure_ascii=False, default=str)
    return s if len(s) <= budget else s[:budget] + "…（已截斷）"


NV_TOOL_CHARS = 12000  # NVIDIA 開源模型的上下文較大，工具結果可以給多一點
NV_TIMEOUT = 75  # 單次請求逾時；逾時就換下一個模型，不要卡 2 分鐘


def _nvidia_loop(messages: list[dict], emit, start: int = 0) -> tuple[str, list]:
    """NVIDIA build.nvidia.com 開源模型（OpenAI 相容 function calling）；額度用完或不支援工具就換下一個模型。"""
    from src.llm import openai_compat as oc

    key = os.environ["NVIDIA_API_KEY"]
    base = oc.PROVIDERS["nvidia"]["base_url"]
    tools = [{"type": "function", "function": t} for t in TOOLS]
    sys_msg = {"role": "system", "content": _system()}
    models = [m for m in settings()["llm"].get("nvidia_models", []) if model_ok(m)]
    last_err = None
    for model in models:
        msgs = [sys_msg] + messages[-12:]
        outputs: list = []
        try:
            seen: dict = {}
            repeats = 0
            while True:
                force = repeats >= REPEAT_LIMIT
                # thinking=False：推理模型不先長篇思考，直接挑工具／回答（回應時間大幅縮短）
                msg = oc.chat(base, key, model, msgs, tools=None if force else tools, temperature=0, thinking=False,
                              timeout=NV_TIMEOUT)
                calls = msg["tool_calls"]
                if not calls:
                    text = re.sub(r"<think>.*?</think>", "", msg["content"], flags=re.S).strip()
                    if not text:
                        raise RuntimeError("模型沒有回答內容")
                    _USED["model"] = f"NVIDIA {model}（開源）"
                    return text, outputs
                if force:
                    raise RuntimeError("模型一直重複呼叫相同工具")
                msgs.append({"role": "assistant", "content": msg["content"] or "", "tool_calls": calls})
                parsed = [(tc["function"]["name"], json.loads(tc["function"].get("arguments") or "{}")) for tc in calls]
                outs, rep = _exec_step(parsed, emit, outputs, start, seen)
                repeats = repeats + 1 if rep else 0
                for tc, (name, _), out in zip(calls, parsed, outs):
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id", name),
                                 "content": _compact(out, NV_TOOL_CHARS) + (REPEAT_NOTE if rep else "")})
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("NVIDIA %s 失敗：%s", model, str(e)[:200])
            if "429" in str(e):
                mark_exhausted(model, "429")
            if outputs and any(o["result"].get("pending_action") for o in outputs if isinstance(o["result"], dict)):
                raise  # 已建立待確認動作，不要換模型重做
    raise RuntimeError(str(last_err) if last_err else "沒有可用的 NVIDIA 模型")


def _groq_loop(messages: list[dict], emit, start: int = 0) -> tuple[str, list]:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    tools = [{"type": "function", "function": t} for t in TOOLS]
    recent = messages[-8:]  # 前文只留最近幾輪，較早的回答截短
    recent = [m if i >= len(recent) - 3 else {**m, "content": m["content"][:600]} for i, m in enumerate(recent)]
    msgs = [{"role": "system", "content": _system()}] + recent
    outputs = []
    models = [m for m in settings()["llm"]["groq_models"] if model_ok(m)] or settings()["llm"]["groq_models"][:1]
    seen: dict = {}
    repeats = 0
    while True:
        force = repeats >= REPEAT_LIMIT
        resp, err = None, None
        for model in list(models):  # 額度用完（429）就換下一個模型
            try:
                extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}  # 降低推理量，回答較快
                resp = client.chat.completions.create(model=model, messages=msgs, temperature=0,
                                                      **({} if force else {"tools": tools}), **extra)
                break
            except Exception as e:  # noqa: BLE001
                err = e
                if "429" in str(e) or "rate" in str(e).lower():
                    mark_exhausted(model, str(e))
                    models.remove(model)
                    continue
                raise
        if resp is None:
            raise RuntimeError(str(err))
        msg = resp.choices[0].message
        if not msg.tool_calls:
            _USED["model"] = f"Groq {model}（開源）"
            return msg.content or "", outputs
        if force:
            raise RuntimeError("模型一直重複呼叫相同工具")
        msgs.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls]})
        parsed = [(tc.function.name, json.loads(tc.function.arguments or "{}")) for tc in msg.tool_calls]
        outs, rep = _exec_step(parsed, emit, outputs, start, seen)
        repeats = repeats + 1 if rep else 0
        for tc, out in zip(msg.tool_calls, outs):
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": _compact(out) + (REPEAT_NOTE if rep else "")})


BOARD_GROUPS = (("hold", "手上持股"), ("buy", "可以買"), ("wait", "等回檔再買"), ("avoid", "先不要買"))


def _board_detail(outputs: list) -> str:
    """決策總表的逐檔明細由程式直接附上（每檔：結論、張數、限價、理由、什麼情況要賣），不靠模型改寫，數字一定與工具一致。"""
    o = next((o for o in reversed(outputs) if o["tool"] == "get_decision_board" and isinstance(o["result"], dict)
              and "buy" in o["result"]), None)
    if not o:
        return ""
    r, sid = o["result"], o["result"]["source_id"]
    parts = []
    for k, title in BOARD_GROUPS:
        items = r.get(k) or []
        if items:
            parts.append(f"{title}（{len(items)} 檔）\n" + "\n".join(f"{i}. {x['line']}〔{sid}〕" for i, x in enumerate(items, 1)))
    return "\n\n" + "\n\n".join(parts) if parts else ""


def _mark_unverified(text: str, facts) -> tuple[str, list[str]]:
    ok, bad = verify(text, facts)
    if ok:
        return text, []
    nums = fact_numbers(facts)
    from src.agents.verifier import _matches

    def repl(m):
        raw = m.group(0)
        try:
            x = float(raw.replace(",", ""))
        except ValueError:
            return raw
        return raw if (_matches(x, nums) or _matches(abs(x), nums)) else f"{raw}〔未核實〕"

    return NUM_RE.sub(repl, text), bad


def run(messages: list[dict], emit, loop=None) -> dict:
    """執行一輪對話。emit(event) 用來串流進度。回傳最終答案事件。"""
    client = get_client()
    if not client.available():
        return {"type": "answer", "text": "尚未設定 GEMINI_API_KEY / GROQ_API_KEY，AI 助理無法使用。", "verified": False}
    user_text = messages[-1]["content"]
    by_name = {"nvidia": _nvidia_loop, "groq": _groq_loop, "gemini": _gemini_loop}
    loops = [loop] if loop else [by_name[p] for p in client.providers() if p in by_name]
    _TL.cache = {}
    try:
        return _run(messages, emit, loop, loops, user_text)
    finally:
        _TL.cache = None


def _run(messages: list[dict], emit, loop, loops: list, user_text: str) -> dict:
    text, outputs, err = "", [], None
    for fn in loops:
        try:
            text, outputs = fn(messages, emit)
            break
        except Exception as e:  # noqa: BLE001
            err = e
    if not text and err:
        return {"type": "answer", "text": f"AI 服務暫時無法使用：{str(err)[:120]}", "verified": False}
    text = _norm_cites(text)
    # 前文（含先前已核實的回答）也算事實來源，延續對話時引用前面的數字不會被誤判
    facts = {"user": user_text, "tools": [o["result"] for o in outputs], "date": now_tw().date().isoformat(),
             "history": [m["content"] for m in messages[:-1]]}
    ok, bad = verify(_hide_cites(text), facts)
    if not ok and outputs and not loop:
        # 重寫一次：附上未出處數字，要求改用工具結果
        # 附上本輪已查到的工具結果（含出處編號），模型直接改寫即可，不必整輪重查（重寫快很多）
        prior = "\n".join(_compact(o["result"], 3000) for o in outputs)[:12000]
        retry = messages + [{"role": "assistant", "content": text},
                            {"role": "user", "content": f"你的回答中 {bad} 這些數字在工具結果中找不到。以下是本輪已查到的工具結果：\n{prior}\n"
                                                        "請只用這些結果裡的數字重寫，沿用原本的出處編號；真的缺資料才再查詢，"
                                                        "查不到的就寫「無法確認」（不要再呼叫寫入類工具）。"}]
        try:
            text2, more = (loops[0])(retry, lambda e: None, start=len(outputs))
            text2 = _norm_cites(text2)
            facts["tools"] += [o["result"] for o in more]
            outputs = outputs + more
            if verify(_hide_cites(text2), facts)[0]:
                text, ok, bad = text2, True, []
            else:
                text = text2
        except Exception:  # noqa: BLE001
            pass
    text = _style(text)
    text = _fix_daytrade_verdict(text, outputs)
    text, uncited = _flag_uncited(text) if outputs else (text, 0)
    text += _board_detail(outputs)
    used_model = any(o["tool"] in MODEL_TOOLS for o in outputs)
    if used_model and re.search(r"預測|機率|預期|預估|命中率", text) and "不是保證" not in text:
        text += "\n\n說明：文中的預測、機率與預期報酬是模型依歷史資料估計的結果，不是保證。"
    text, bad = _mark_unverified(_hide_cites(text), facts)
    text = _show_cites(text)
    text, bad_cites = _check_cites(text, len(outputs))
    sources = []
    for i, o in enumerate(outputs, 1):
        r = o["result"] if isinstance(o["result"], dict) else {}
        sources.append({"id": f"S{i}", "tool": o["tool"], "args": o["args"], "label": SOURCE_DESC.get(o["tool"], o["tool"]),
                        "date": r.get("data_date") or r.get("date") or r.get("asof") or r.get("generated_at"),
                        "links": _links(o["tool"], r), "cited": f"〔S{i}〕" in text})
    actions = [{"id": o["result"]["pending_action"], "summary": o["result"]["summary"]}
               for o in outputs if isinstance(o["result"], dict) and o["result"].get("pending_action")]
    downloads = [o["result"]["download"] for o in outputs if isinstance(o["result"], dict) and o["result"].get("download")]
    return {"type": "answer", "text": text, "model": _USED["model"], "downloads": downloads, "verified": not bad and not bad_cites and not uncited, "unverified": bad + bad_cites + ["未附出處"] * uncited,
            "sources": sources, "actions": actions}


def stream(messages: list[dict]) -> Iterator[str]:
    """SSE：先逐步送出工具進度，最後送出答案。"""
    import queue
    import threading

    q: queue.Queue = queue.Queue()

    def worker():
        try:
            q.put(run(messages, q.put))
        except Exception as e:  # noqa: BLE001
            log.exception("chat 失敗")
            q.put({"type": "answer", "text": f"發生錯誤：{str(e)[:160]}", "verified": False})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        ev = q.get()
        if ev is None:
            break
        yield json.dumps(ev, ensure_ascii=False, default=str)


_RATE: dict[str, list[float]] = {}


def rate_ok(sid: str, per_min: int = 8) -> bool:
    now = time.time()
    hist = [t for t in _RATE.get(sid, []) if now - t < 60]
    if len(hist) >= per_min:
        _RATE[sid] = hist
        return False
    _RATE[sid] = hist + [now]
    return True
