"""
🐢 阿龜 AI 聊天（LINE 版）— 具備工具呼叫能力的輕量代理。

哲學（弱模型＋強工具）：不逼免費模型自己硬想，而是給它一組穩健的工具
（見 ai_tools.py），用 OpenAI 相容的 tool-calling 協定讓它「呼叫」工具，
拿到結果再回答。模型只負責決定「要不要用工具、用哪個」。

後端：有 NVIDIA 金鑰 → 先用 NVIDIA（快穩、支援 tool calling）；
否則／失敗 → OpenRouter 免費模型換手鏈。金鑰讀環境變數（.env）。
"""
import os
import re
import json
import logging

import httpx
from dotenv import load_dotenv, find_dotenv

# 🔑 讓 .env 的金鑰「檔案優先」，蓋過系統殘留的舊/空環境變數（否則 load_dotenv 預設不覆寫，
# 會被系統的舊 NVIDIA_API_KEY/OPENROUTER_API_KEY 悄悄蓋掉 → 401）。與桌面版 cloud_ai 同一原則。
load_dotenv(find_dotenv(usecwd=True), override=True)

from src.ai_tools import TOOL_SCHEMAS, dispatch, owner_ids

logger = logging.getLogger("AIChat")

NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

NVIDIA_CHAIN = [
    "qwen/qwen3-next-80b-a3b-instruct",  # 實測 2.8s + 會呼叫工具（33 工具完整請求下最佳）— 主力
    "meta/llama-3.3-70b-instruct",       # 快備援
    # 註：DeepSeek v4-flash/pro 與 qwen3.5-122b/397b 雖聰明，但配 33 個工具的完整請求
    #     ~40s+ 逾時（就是害「大腦連不上」的原因），不適合即時 LINE，故不用。
]
FREE_CHAIN = [
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
]

PERSONA = (
    "你叫阿龜，是這個 LINE 帳號背後的 AI 助理。個性親切、像個聰明的朋友，"
    "講話自然、口語、好懂，一般 1～3 句就好，可帶一點點幽默。\n"
    "1. 對方問什麼就答什麼，不要答非所問，也不要空泛的詩意鬼扯。\n"
    "2. 你有一組工具可用，能用工具得到正確答案時就用，別亂猜：\n"
    "   • 即時／最新資訊 → web_search 搜尋、web_fetch 讀網頁\n"
    "   • 知識/名詞/人物 → wikipedia；天氣 → get_weather；時間日期 → get_datetime\n"
    "   • 算數 → calculate（單純算式優先用它）；較複雜的資料處理 → run_python\n"
    "   • 匯率 → currency_convert；加密貨幣 → crypto_price；股票 → stock_price\n"
    "   • 翻譯 → translate；記事情 → remember、回想 → recall\n"
    "   • 記帳 → add_expense、看花費統計 → expense_summary\n"
    "   • YouTube 影片摘要 → youtube_summary（會拿到字幕/說明欄，你負責摘要重點）\n"
    "   • 新聞 → news；地點/地址/地圖 → find_place；產生 QR Code → make_qrcode\n"
    "   • 待辦清單 → add_todo/list_todos/complete_todo；單位換算 → unit_convert；統一發票中獎號碼 → invoice_lottery\n"
    "   • 中文字詞/成語 → moedict；英文單字 → dictionary；世界時間 → world_time；縮網址 → short_url\n"
    "   • 抽籤/擲骰/隨機 → random_pick；編碼雜湊(base64/url/md5…) → encode_tool\n"
    "   • 使用者要你「用語音/念出來」→ voice_reply（會傳 LINE 語音訊息）\n"
    "   • 使用者要你提醒他（幾分鐘後／幾點）→ set_reminder，到時間會自動傳 LINE 給他\n"
    "   • 呼叫外部 API → http_request\n"
    "3. 這個 LINE 帳號同時能幫忙搶【和欣客運／台灣鐵路】車票——若對方想訂票、搶票、查票，"
    "提醒他直接輸入「搶票」啟動流程；輸入「我的車票」可查詢已購票。\n"
    "4. 只用台灣繁體中文（專有名詞例外），不可出現簡體字。\n"
    "5. 【純文字】回覆用純文字，不要用 Markdown：不要 ** 粗體、# 標題、`程式碼`、- 或 * 條列符號、"
    "[文字](網址) 連結語法。LINE 不會渲染這些，直接打出符號會很醜。要條列就用「・」或直接分行。"
)

MAX_TOKENS = 800
MAX_STEPS = 4                             # 工具呼叫最多來回幾次
_HISTORY: dict[str, list[dict]] = {}      # user_id -> [{role, content}, ...]
_MAX_TURNS = 8


def _strip_reasoning(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _clean_for_line(text: str) -> str:
    """把 Markdown/雜訊清成 LINE 友善純文字（LINE 不會渲染 Markdown，直接印出符號會很醜）。"""
    if not text:
        return text
    t = text
    # 程式碼圍欄 ```lang ... ``` → 去掉圍欄，留內容
    t = re.sub(r"```[^\n`]*\n?", "", t)
    # 連結 [文字](網址) → 文字 網址；[文字](其他) → 文字
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 \2", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    # 行首：標題 #、引用 >
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)
    # 水平線 ---、***、___
    t = re.sub(r"(?m)^\s{0,3}([-*_])\1{2,}\s*$", "", t)
    # 粗體/斜體：**x**、__x__、*x*、_x_
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"__(.+?)__", r"\1", t)
    t = re.sub(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])", r"\1", t)
    t = re.sub(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])", r"\1", t)
    # 行內程式碼 `x`
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = t.replace("`", "").replace("**", "")
    # 項目符號 -、*、+ → ・
    t = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1・", t)
    # 壓縮行尾空白與多餘空行
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _providers():
    """回傳 [(url, model, key), ...]，NVIDIA 優先（有金鑰才排），再免費鏈。"""
    out = []
    nv = os.getenv("NVIDIA_API_KEY", "").strip()
    if nv:
        out += [(NVIDIA_API_URL, m, nv) for m in NVIDIA_CHAIN]
    orr = os.getenv("OPENROUTER_API_KEY", "").strip()
    out += [(OPENROUTER_API_URL, m, orr) for m in FREE_CHAIN]
    return out


async def _chat_once(client, url, model, messages, key, use_tools):
    """呼叫一次模型，回傳 assistant 訊息 dict；失敗回 None。"""
    payload = {"model": model, "messages": messages, "temperature": 0.7,
               "max_tokens": MAX_TOKENS, "top_p": 0.9}
    if use_tools:
        payload["tools"] = TOOL_SCHEMAS
        payload["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=35)
        if r.status_code != 200:
            logger.info(f"{model} HTTP {r.status_code} (tools={use_tools})")
            return None
        choices = r.json().get("choices") or []
        return choices[0].get("message") if choices else None
    except Exception as e:
        logger.info(f"{model} 呼叫失敗: {e}")
        return None


async def _complete(client, messages, use_tools):
    """依序試各 provider，回傳第一個成功的 assistant 訊息。"""
    for url, model, key in _providers():
        msg = await _chat_once(client, url, model, messages, key, use_tools)
        if msg is not None:
            return msg
    return None


async def ai_reply(user_id: str, user_text: str) -> str:
    """產生阿龜的回覆（含工具呼叫迴圈與該使用者近期對話上下文）。永遠回字串。"""
    ctx = {"user_id": user_id, "is_owner": user_id in owner_ids()}
    history = _HISTORY.get(user_id, [])
    messages = [{"role": "system", "content": PERSONA}] + history + \
               [{"role": "user", "content": user_text}]

    final = None
    used_tools = False
    async with httpx.AsyncClient() as client:
        for _step in range(MAX_STEPS):
            msg = await _complete(client, messages, use_tools=True)
            if msg is None:                       # 可能是模型不支援 tools → 退成純聊天
                msg = await _complete(client, messages, use_tools=False)
            if msg is None:
                break

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                used_tools = True
                # 保留 assistant 的工具呼叫訊息，再逐一執行、把結果餵回
                messages.append({"role": "assistant",
                                 "content": msg.get("content") or "",
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    result = await dispatch(name, args, ctx)
                    logger.info(f"🛠️ {user_id[:8]} 用了工具 {name}")
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": str(result)[:4000]})
                continue                          # 帶著工具結果再問一次

            content = _strip_reasoning(msg.get("content") or "")
            if content:
                final = content
            break

        # 收尾：工具已經跑完拿到結果，但還沒產出最終回答
        # （用完步數時最後一步仍在呼叫工具，或後續請求失敗）→ 再要一次純文字總結，
        # 避免「明明查到了卻回大腦連不上」。
        if final is None and used_tools:
            try:
                msg = await _complete(client, messages, use_tools=False)
                if msg:
                    final = _strip_reasoning(msg.get("content") or "") or None
            except Exception:
                pass

    if not final:
        return "抱歉，我的大腦剛好連不上（雲端模型忙線中），等一下再傳一次給我吧～"

    final = _clean_for_line(final) or final   # 清成 LINE 友善純文字

    hist = _HISTORY.setdefault(user_id, [])
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": final})
    del hist[: max(0, len(hist) - _MAX_TURNS * 2)]
    return final
