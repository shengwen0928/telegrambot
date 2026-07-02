"""
🛠️ 阿龜的工具箱（LINE 版）— 自足模組，供 ai_chat 的工具呼叫迴圈使用。

安全設計（因為 LINE bot 是公開的，任何加好友的人都能觸發）：
  • 人人可用（低風險）：web_search、web_fetch、get_weather、get_datetime、remember、recall
      - web_fetch/http_request 都經過 SSRF 防護（擋 localhost / 內網 / 雲端 metadata）
      - remember/recall 以 user_id 隔離，使用者彼此看不到對方的記憶
  • 僅限主人（LINE_OWNER_IDS 名單內）：run_python、http_request

只用 httpx（本專案既有相依）+ 標準庫。
"""
import os
import re
import ast
import sys
import json
import time
import uuid
import socket
import shutil
import base64
import random
import hashlib
import logging
import asyncio
import operator
import ipaddress
import subprocess
import tempfile
import urllib.parse
from html import unescape
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv, find_dotenv

# 檔案優先載入金鑰（蓋過系統殘留的舊/空環境變數），與 ai_chat 一致
load_dotenv(find_dotenv(usecwd=True), override=True)

try:
    import pytz
    _TZ = pytz.timezone("Asia/Taipei")
except Exception:
    _TZ = None

logger = logging.getLogger("AITools")

MEMORY_FILE = "ai_memory.json"       # {user_id: [fact, ...]}
# 用擬真的瀏覽器標頭，否則 DuckDuckGo 會回 202 擋爬蟲
_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# ══════════════════════════════════════════════════════════════════
# SSRF 防護：只允許 http/https，且解析後的 IP 必須是公開位址
# ══════════════════════════════════════════════════════════════════

def _url_is_safe(url: str) -> bool:
    try:
        m = re.match(r"^(https?)://([^/:\s]+)", url.strip(), re.IGNORECASE)
        if not m:
            return False
        host = m.group(2)
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
# 每人隔離的長期記憶
# ══════════════════════════════════════════════════════════════════

def _load_mem() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_mem(data: dict):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入記憶失敗: {e}")


# ══════════════════════════════════════════════════════════════════
# 工具實作　（每個 handler: (args: dict, ctx: dict) -> str）
# ctx = {"user_id": str, "is_owner": bool}
# ══════════════════════════════════════════════════════════════════

def _clean(html_frag: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html_frag)).strip()


def _ddg_real_url(href: str) -> str:
    """DuckDuckGo 的結果連結是轉址 //duckduckgo.com/l/?uddg=<編碼網址>，解出真實網址。"""
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    return ("https:" + href) if href.startswith("//") else href


async def _web_search(args, ctx):
    q = (args.get("query") or "").strip()
    if not q:
        return "沒有提供搜尋關鍵字。"
    maxr = min(int(args.get("max_results", 5)), 8)
    async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
    titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
    out = []
    for i, (href, title) in enumerate(titles[:maxr]):
        title = _clean(title)
        if not title:
            continue
        url = _ddg_real_url(href)
        snip = _clean(snippets[i]) if i < len(snippets) else ""
        block = f"• {title}\n  {url}"
        if snip:
            block += f"\n  {snip[:180]}"
        out.append(block)
    return f"「{q}」搜尋結果:\n" + "\n".join(out) if out else f"查無「{q}」的結果"


async def _web_fetch(args, ctx):
    url = (args.get("url") or "").strip()
    if not _url_is_safe(url):
        return "拒絕：網址無效或指向內網/本機位址（安全防護）。"
    maxc = min(int(args.get("max_chars", 4000)), 8000)
    async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as c:
        r = await c.get(url)
    text = r.text
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:maxc] + ("\n…（已截斷）" if len(text) > maxc else "")


async def _get_weather(args, ctx):
    loc = (args.get("location") or "").strip()
    fmt = "%l:+%c+%t+體感%f+濕度%h+風%w"
    async with httpx.AsyncClient(headers={"User-Agent": "curl/8"}, timeout=15) as c:
        r = await c.get(f"https://wttr.in/{loc}?format={fmt}&m")
    return r.text.strip() or "查詢天氣失敗"


def _get_datetime(args, ctx):
    now = datetime.now(_TZ) if _TZ else datetime.now()
    week = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return now.strftime(f"%Y-%m-%d %H:%M:%S 星期{week}（台北時間）")


def _remember(args, ctx):
    fact = (args.get("fact") or "").strip()
    if not fact:
        return "沒有要記的內容。"
    mem = _load_mem()
    uid = ctx["user_id"]
    mem.setdefault(uid, [])
    if fact not in mem[uid]:
        mem[uid].append(fact)
        _save_mem(mem)
    return f"好，我記住了：{fact}"


def _recall(args, ctx):
    mem = _load_mem().get(ctx["user_id"], [])
    if not mem:
        return "我還沒記住關於你的任何事。"
    q = (args.get("query") or "").strip()
    if q:
        hits = [f for f in mem if q.lower() in f.lower()]
        return "找到：\n" + "\n".join(f"• {h}" for h in hits) if hits else f"記憶裡沒有跟「{q}」有關的內容。"
    return "我記得這些：\n" + "\n".join(f"• {f}" for f in mem)


_DANGER = re.compile(
    r"(rm\s+-rf\s+/|shutil\.rmtree\s*\(\s*['\"]/|os\.system|subprocess|"
    r"socket\.|open\s*\(\s*['\"]/etc|:\(\)\{|mkfs|dd\s+if=)", re.IGNORECASE)


def _run_python(args, ctx):
    code = args.get("code") or ""
    if _DANGER.search(code):
        return "拒絕：程式碼包含高風險樣式（檔案/系統/網路操作），未執行。"
    timeout = min(int(args.get("timeout", 15)), 30)
    py = sys.executable or "python3"
    # 🔒 就算對所有人開放，仍把祕密從子行程環境中清掉（只留 PATH），
    # 讓被執行的程式碼讀不到 .env 裡的 token/密碼/金鑰（os.environ 會是空的）。
    safe_env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        r = subprocess.run([py, tmp], capture_output=True, text=True, env=safe_env,
                           timeout=timeout, encoding="utf-8", errors="ignore")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        res = out or "(無標準輸出)"
        if err:
            res += f"\n[stderr] {err[:500]}"
        return res[:3000]
    except subprocess.TimeoutExpired:
        return f"執行逾時（超過 {timeout} 秒）。"
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def _http_request(args, ctx):
    method = (args.get("method") or "GET").upper()
    url = (args.get("url") or "").strip()
    if not _url_is_safe(url):
        return "拒絕：網址無效或指向內網/本機位址（安全防護）。"
    headers = args.get("headers") or {}
    body = args.get("body")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.request(method, url, headers=headers,
                            content=body if isinstance(body, str) else None,
                            json=body if isinstance(body, (dict, list)) else None)
    text = r.text[:3000]
    return f"HTTP {r.status_code}\n{text}" + ("\n…（已截斷）" if len(r.text) > 3000 else "")


# ══════════════════════════════════════════════════════════════════
# 安全計算機（不需 run_python 也能算數）
# ══════════════════════════════════════════════════════════════════

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}
import math as _math
_FUNCS = {k: getattr(_math, k) for k in
          ("sqrt", "sin", "cos", "tan", "log", "log2", "log10", "exp", "floor", "ceil", "fabs")}
_CONSTS = {"pi": _math.pi, "e": _math.e}


def _eval_node(n):
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.BinOp):
        return _OPS[type(n.op)](_eval_node(n.left), _eval_node(n.right))
    if isinstance(n, ast.UnaryOp):
        return _OPS[type(n.op)](_eval_node(n.operand))
    if isinstance(n, ast.Name) and n.id in _CONSTS:
        return _CONSTS[n.id]
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS:
        return _FUNCS[n.func.id](*[_eval_node(a) for a in n.args])
    raise ValueError("不支援的運算式")


def _calculate(args, ctx):
    expr = (args.get("expression") or "").strip()
    if not expr:
        return "沒有提供算式。"
    try:
        val = _eval_node(ast.parse(expr, mode="eval").body)
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        return f"{expr} = {val}"
    except Exception:
        return f"我算不出「{expr}」，請確認算式（支援 + - * / ** % 與 sqrt/sin/log 等）。"


# ══════════════════════════════════════════════════════════════════
# 維基百科查詢
# ══════════════════════════════════════════════════════════════════

async def _wikipedia(args, ctx):
    q = (args.get("query") or "").strip()
    if not q:
        return "沒有提供查詢主題。"
    lang = (args.get("lang") or "zh").strip() or "zh"
    url = (f"https://{lang}.wikipedia.org/w/api.php?format=json&action=query"
           f"&prop=extracts&exintro=1&explaintext=1&redirects=1&generator=search"
           f"&gsrsearch={urllib.parse.quote(q)}&gsrlimit=1")
    # 維基要求「描述性」的 bot UA（用瀏覽器 UA 反而被 403），與 DuckDuckGo 相反
    wiki_ua = {"User-Agent": "Mozilla/5.0 (compatible; AguuLineBot/1.0; +https://example.com)",
               "Accept": "application/json"}
    async with httpx.AsyncClient(headers=wiki_ua, timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
    pages = ((r.json().get("query") or {}).get("pages") or {})
    for p in pages.values():
        extract = (p.get("extract") or "").strip()
        if extract:
            title = p.get("title", q)
            return f"📖 {title}\n{extract[:800]}"
    return f"維基百科查無「{q}」。"


# ══════════════════════════════════════════════════════════════════
# 匯率換算（open.er-api.com，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _currency_convert(args, ctx):
    try:
        amount = float(args.get("amount", 1))
    except Exception:
        amount = 1.0
    frm = (args.get("from") or "USD").strip().upper()
    to = (args.get("to") or "TWD").strip().upper()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://open.er-api.com/v6/latest/{frm}")
    data = r.json()
    rates = data.get("rates") or {}
    if to not in rates:
        return f"查不到 {frm}→{to} 的匯率。"
    result = amount * rates[to]
    return f"{amount:g} {frm} ≈ {result:,.2f} {to}（匯率 1 {frm}={rates[to]:g} {to}）"


# ══════════════════════════════════════════════════════════════════
# 提醒／鬧鐘（到時間主動 push 到 LINE）
# ══════════════════════════════════════════════════════════════════

REMINDER_FILE = "reminders.json"


def _load_reminders() -> list:
    if os.path.exists(REMINDER_FILE):
        try:
            with open(REMINDER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_reminders(items: list):
    try:
        with open(REMINDER_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入提醒失敗: {e}")


def _set_reminder(args, ctx):
    text = (args.get("text") or "提醒時間到囉！").strip()
    now = datetime.now(_TZ) if _TZ else datetime.now()
    fire = None
    if args.get("minutes_from_now") is not None:
        try:
            fire = now + timedelta(minutes=float(args["minutes_from_now"]))
        except Exception:
            fire = None
    if fire is None and args.get("at"):
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"):
            try:
                dt = datetime.strptime(args["at"].strip(), fmt)
                if fmt == "%H:%M":
                    dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
                    if dt <= now:
                        dt += timedelta(days=1)
                fire = dt if _TZ is None else _TZ.localize(dt.replace(tzinfo=None))
                break
            except Exception:
                continue
    if fire is None:
        return "我看不懂提醒時間，請說「幾分鐘後」或「幾點幾分」。"
    items = _load_reminders()
    items.append({"user_id": ctx["user_id"], "fire_ts": fire.timestamp(),
                  "text": text, "done": False})
    _save_reminders(items)
    return f"好，我會在 {fire.strftime('%m/%d %H:%M')} 提醒你：{text} ⏰"


async def push_line(user_id: str, text: str) -> bool:
    """主動 push 一則文字到 LINE（提醒用）。自足，直接打 LINE push API。"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not user_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"},
                             json={"to": user_id, "messages": [{"type": "text", "text": text[:4900]}]})
        return r.status_code == 200
    except Exception as e:
        logger.error(f"push 失敗: {e}")
        return False


async def reminder_loop(interval: int = 20):
    """背景迴圈：到期的提醒就 push 出去。由 line_bot 啟動時 create_task。"""
    logger.info("⏰ 提醒排程已啟動")
    while True:
        try:
            items = _load_reminders()
            now_ts = time.time()
            changed = False
            for it in items:
                if not it.get("done") and it.get("fire_ts", 0) <= now_ts:
                    ok = await push_line(it["user_id"], f"⏰ 提醒：{it['text']}")
                    it["done"] = ok or True   # 推過就標記，避免重複轟炸
                    changed = True
            if changed:
                # 清掉已完成且超過一天的舊提醒
                items = [it for it in items
                         if not (it.get("done") and it.get("fire_ts", 0) < now_ts - 86400)]
                _save_reminders(items)
        except Exception as e:
            logger.error(f"提醒迴圈錯誤: {e}")
        await asyncio.sleep(interval)


# ══════════════════════════════════════════════════════════════════
# 看圖（NVIDIA 視覺模型）— 由 line_bot 的圖片訊息處理呼叫
# ══════════════════════════════════════════════════════════════════

VISION_CHAIN = ["meta/llama-3.2-90b-vision-instruct", "meta/llama-3.2-11b-vision-instruct"]


async def vision_describe(image_bytes: bytes, prompt: str = "看懂這張圖，用繁體中文說明內容；若有文字請一併辨識出來。") -> str:
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        return "（看圖功能需要 NVIDIA 金鑰，目前沒設定）"
    b64 = base64.b64encode(image_bytes).decode()
    durl = f"data:image/jpeg;base64,{b64}"
    async with httpx.AsyncClient(timeout=90) as c:
        for model in VISION_CHAIN:
            payload = {"model": model, "max_tokens": 600, "temperature": 0.2,
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": prompt},
                           {"type": "image_url", "image_url": {"url": durl}}]}]}
            try:
                r = await c.post("https://integrate.api.nvidia.com/v1/chat/completions",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"}, json=payload)
                if r.status_code == 200:
                    choices = r.json().get("choices") or []
                    if choices:
                        txt = (choices[0].get("message") or {}).get("content", "").strip()
                        if txt:
                            return txt
            except Exception as e:
                logger.info(f"vision {model} 失敗: {e}")
    return "（我看不太懂這張圖，換一張試試？）"


# ══════════════════════════════════════════════════════════════════
# 加密貨幣報價（CoinGecko，免金鑰）
# ══════════════════════════════════════════════════════════════════

_COIN_MAP = {
    "比特幣": "bitcoin", "btc": "bitcoin", "bitcoin": "bitcoin",
    "以太幣": "ethereum", "以太坊": "ethereum", "eth": "ethereum", "ethereum": "ethereum",
    "狗狗幣": "dogecoin", "doge": "dogecoin", "dogecoin": "dogecoin",
    "萊特幣": "litecoin", "ltc": "litecoin", "bnb": "binancecoin", "幣安幣": "binancecoin",
    "sol": "solana", "solana": "solana", "xrp": "ripple", "ripple": "ripple",
    "泰達幣": "tether", "usdt": "tether", "ada": "cardano", "cardano": "cardano",
}


async def _crypto_price(args, ctx):
    raw = (args.get("coin") or "bitcoin").strip().lower()
    cid = _COIN_MAP.get(raw, raw)
    vs = (args.get("vs") or "twd").strip().lower()
    url = (f"https://api.coingecko.com/api/v3/simple/price?ids={cid}"
           f"&vs_currencies={vs},usd&include_24hr_change=true")
    async with httpx.AsyncClient(headers=_UA, timeout=15) as c:
        r = await c.get(url)
    data = r.json()
    if cid not in data:
        return f"查不到加密貨幣「{raw}」的報價（試試英文代號如 btc / eth）。"
    d = data[cid]
    chg = d.get(f"{vs}_24h_change") or d.get("usd_24h_change") or 0
    arrow = "🔺" if chg >= 0 else "🔻"
    lines = [f"💰 {cid.upper()}"]
    if vs in d:
        lines.append(f"  {d[vs]:,.2f} {vs.upper()}")
    if "usd" in d and vs != "usd":
        lines.append(f"  {d['usd']:,.2f} USD")
    lines.append(f"  24h {arrow} {chg:+.2f}%")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# 股價（Yahoo Finance chart API，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _stock_price(args, ctx):
    sym = (args.get("symbol") or "").strip()
    if not sym:
        return "請提供股票代號（美股如 AAPL、台股如 2330.TW）。"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
    # Yahoo 會依 Accept 內容協商；用瀏覽器 UA 但不要求 text/html，否則會回 HTML 導致解析失敗
    yh = {"User-Agent": _UA["User-Agent"], "Accept": "application/json"}
    async with httpx.AsyncClient(headers=yh, timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
    try:
        meta = r.json()["chart"]["result"][0]["meta"]
    except Exception:
        return f"查不到「{sym}」的股價（確認代號，台股要加 .TW，如 2330.TW）。"
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    cur = meta.get("currency", "")
    name = meta.get("longName") or meta.get("shortName") or meta.get("symbol", sym)
    if price is None:
        return f"查不到「{sym}」的即時價格。"
    out = f"📈 {name}（{meta.get('symbol', sym)}）\n  {price:,.2f} {cur}"
    if prev:
        chg = price - prev
        pct = chg / prev * 100 if prev else 0
        arrow = "🔺" if chg >= 0 else "🔻"
        out += f"\n  {arrow} {chg:+,.2f}（{pct:+.2f}%）"
    return out


# ══════════════════════════════════════════════════════════════════
# 翻譯強化（Google 翻譯 gtx 端點，免金鑰，自動偵測來源語言）
# ══════════════════════════════════════════════════════════════════

async def _translate(args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        return "沒有要翻譯的內容。"
    target = (args.get("target") or "zh-TW").strip()
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           f"&sl=auto&tl={urllib.parse.quote(target)}&dt=t&q={urllib.parse.quote(text)}")
    async with httpx.AsyncClient(headers=_UA, timeout=15) as c:
        r = await c.get(url)
    try:
        data = r.json()
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out if out else "翻譯失敗。"
    except Exception:
        return "翻譯失敗，請再試一次。"


# ══════════════════════════════════════════════════════════════════
# 記帳（每人隔離）
# ══════════════════════════════════════════════════════════════════

EXPENSE_FILE = "expenses.json"


def _load_exp() -> list:
    if os.path.exists(EXPENSE_FILE):
        try:
            with open(EXPENSE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_exp(items: list):
    try:
        with open(EXPENSE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入記帳失敗: {e}")


def _add_expense(args, ctx):
    try:
        amount = float(args.get("amount"))
    except Exception:
        return "請提供金額（數字）。"
    cat = (args.get("category") or "其他").strip()
    note = (args.get("note") or "").strip()
    now = datetime.now(_TZ) if _TZ else datetime.now()
    items = _load_exp()
    items.append({"user_id": ctx["user_id"], "ts": now.timestamp(),
                  "date": now.strftime("%Y-%m-%d"), "amount": amount,
                  "category": cat, "note": note})
    _save_exp(items)
    return f"已記帳：{cat} ${amount:,.0f}" + (f"（{note}）" if note else "") + " 📒"


def _expense_summary(args, ctx):
    period = (args.get("period") or "month").strip().lower()
    now = datetime.now(_TZ) if _TZ else datetime.now()
    mine = [e for e in _load_exp() if e.get("user_id") == ctx["user_id"]]
    if period in ("today", "day", "今天", "今日"):
        mine = [e for e in mine if e.get("date") == now.strftime("%Y-%m-%d")]
        label = "今天"
    else:
        pfx = now.strftime("%Y-%m")
        mine = [e for e in mine if (e.get("date") or "").startswith(pfx)]
        label = "本月"
    if not mine:
        return f"{label}還沒有任何記帳。"
    total = sum(e["amount"] for e in mine)
    by_cat = {}
    for e in mine:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    lines = [f"📒 {label}花費：${total:,.0f}"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  • {cat}：${amt:,.0f}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# YouTube 字幕逐字稿（供模型摘要）
# ══════════════════════════════════════════════════════════════════

def _yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else (url.strip() if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()) else "")


async def _youtube_summary(args, ctx):
    vid = _yt_id(args.get("url") or "")
    if not vid:
        return "請提供有效的 YouTube 影片網址。"
    hdr = dict(_UA)
    hdr["Cookie"] = "CONSENT=YES+1"          # 略過歐盟同意頁
    async with httpx.AsyncClient(headers=hdr, timeout=20, follow_redirects=True) as c:
        page = (await c.get(f"https://www.youtube.com/watch?v={vid}&hl=zh-TW")).text
        title_m = re.search(r'<title>(.*?)</title>', page)
        title = unescape(title_m.group(1)).replace(" - YouTube", "") if title_m else "YouTube 影片"
        # 1) 先試字幕逐字稿（YouTube 常從伺服器端封鎖，回空）
        transcript = ""
        m = re.search(r'"captionTracks":(\[.*?\])', page)
        if m:
            try:
                tracks = json.loads(m.group(1))
                track = next((t for t in tracks if t.get("languageCode", "").startswith("zh")), None) \
                    or next((t for t in tracks if t.get("languageCode", "").startswith("en")), None) \
                    or tracks[0]
                xml = (await c.get(track["baseUrl"])).text
                texts = re.findall(r"<text[^>]*>(.*?)</text>", xml, re.DOTALL)
                transcript = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", " ".join(texts)))).strip()
            except Exception:
                transcript = ""
    if transcript:
        return f"🎬 {title}\n以下是影片字幕逐字稿，請據此為使用者摘要重點：\n{transcript[:6000]}"
    # 2) 退而求其次：用影片說明欄（description）讓模型摘要
    dm = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', page)
    if dm:
        try:
            desc = json.loads('"' + dm.group(1) + '"')
        except Exception:
            desc = dm.group(1)
        if desc.strip():
            return (f"🎬 {title}\n（此影片無法取得字幕，以下是影片的說明欄內容，"
                    f"請據此摘要，並提醒使用者這是根據說明欄而非逐字稿）：\n{desc[:3000]}")
    return f"🎬 {title}\n（這支影片沒有可用的字幕或說明，無法摘要內容。）"


# ══════════════════════════════════════════════════════════════════
# 發語音（gTTS→ffmpeg m4a→push LINE 語音訊息）
# ══════════════════════════════════════════════════════════════════

_PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
VOICE_DIR = os.path.join("static", "voice")


async def _push_message_obj(user_id: str, message: dict) -> bool:
    """push 一個任意 LINE message 物件（語音/圖片用）。"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not user_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"},
                             json={"to": user_id, "messages": [message]})
        if r.status_code != 200:
            logger.info(f"push message HTTP {r.status_code}: {r.text[:150]}")
        return r.status_code == 200
    except Exception as e:
        logger.error(f"push message 失敗: {e}")
        return False


def _voice_missing() -> list:
    missing = []
    try:
        import gtts  # noqa: F401
    except Exception:
        missing.append("gTTS(pip install gTTS)")
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not _PUBLIC_BASE:
        missing.append("PUBLIC_BASE_URL(.env)")
    return missing


def _gen_voice(text: str, lang: str = "zh-TW"):
    """(阻塞) gTTS 產 mp3 → ffmpeg 轉 m4a，回傳 (檔名, 毫秒長度)。"""
    from gtts import gTTS
    os.makedirs(VOICE_DIR, exist_ok=True)
    name = uuid.uuid4().hex
    mp3 = os.path.join(VOICE_DIR, name + ".mp3")
    m4a = os.path.join(VOICE_DIR, name + ".m4a")
    gTTS(text=text, lang=lang).save(mp3)
    subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "aac", "-b:a", "64k", m4a],
                   capture_output=True, timeout=60)
    try:
        os.remove(mp3)
    except Exception:
        pass
    dur = 3000
    try:
        p = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", m4a], capture_output=True, text=True, timeout=15)
        dur = int(float(p.stdout.strip()) * 1000)
    except Exception:
        pass
    return name + ".m4a", dur


async def _voice_reply(args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        return "沒有要念的內容。"
    missing = _voice_missing()
    if missing:
        return "（語音功能還沒設定好，缺：" + "、".join(missing) + "；我先用文字回你）"
    lang = (args.get("lang") or "zh-TW").strip()
    try:
        fname, dur = await asyncio.to_thread(_gen_voice, text, lang)
    except Exception as e:
        return f"（語音生成失敗：{e}）"
    url = f"{_PUBLIC_BASE}/static/voice/{fname}"
    ok = await _push_message_obj(ctx["user_id"], {
        "type": "audio", "originalContentUrl": url, "duration": dur})
    return "🔊（已用語音回覆你囉）" if ok else "（語音送出失敗，改用文字回你）"


# ══════════════════════════════════════════════════════════════════
# QR Code 生成（qrserver API 直接給公開圖片網址，免自己 host）
# ══════════════════════════════════════════════════════════════════

async def _make_qrcode(args, ctx):
    data = (args.get("data") or "").strip()
    if not data:
        return "請提供要編碼的文字或網址。"
    url = ("https://api.qrserver.com/v1/create-qr-code/?size=400x400&data="
           + urllib.parse.quote(data))
    ok = await _push_message_obj(ctx["user_id"], {
        "type": "image", "originalContentUrl": url, "previewImageUrl": url})
    return f"🔳（QR Code 已產生並傳給你，內容：{data[:60]}）" if ok else \
        f"QR 產生了但傳送失敗，你可直接開這個網址：{url}"


# ══════════════════════════════════════════════════════════════════
# 新聞頭條（Google News RSS，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _news(args, ctx):
    topic = (args.get("topic") or "").strip()
    if topic:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
    titles = re.findall(r"<item>.*?<title>(.*?)</title>", r.text, re.DOTALL)
    out = [f"• {unescape(re.sub(r'<[^>]+>', '', t)).strip()}" for t in titles[1:8]]
    head = f"📰 「{topic}」相關新聞:" if topic else "📰 今日頭條:"
    return head + "\n" + "\n".join(out) if out else "查不到新聞。"


# ══════════════════════════════════════════════════════════════════
# 地點查詢（OpenStreetMap Nominatim，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _find_place(args, ctx):
    q = (args.get("query") or "").strip()
    if not q:
        return "請提供要查的地點。"
    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(q)}&format=json&limit=1&accept-language=zh-TW"
    hdr = {"User-Agent": "AguuLineBot/1.0 (place lookup)"}
    async with httpx.AsyncClient(headers=hdr, timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
    try:
        arr = r.json()
    except Exception:
        arr = []
    if not arr:
        return f"查不到「{q}」這個地點。"
    p = arr[0]
    lat, lon = p.get("lat"), p.get("lon")
    name = p.get("display_name", q)
    gmap = f"https://www.google.com/maps?q={lat},{lon}"
    return f"📍 {name}\n座標：{lat}, {lon}\n地圖：{gmap}"


# ══════════════════════════════════════════════════════════════════
# 待辦清單（每人隔離）
# ══════════════════════════════════════════════════════════════════

TODO_FILE = "todos.json"


def _load_todos() -> dict:
    if os.path.exists(TODO_FILE):
        try:
            with open(TODO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_todos(d: dict):
    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入待辦失敗: {e}")


def _add_todo(args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        return "要加什麼待辦事項呢？"
    d = _load_todos()
    d.setdefault(ctx["user_id"], []).append({"text": text, "done": False})
    _save_todos(d)
    n = len([t for t in d[ctx["user_id"]] if not t["done"]])
    return f"✅ 已加入待辦：{text}（目前有 {n} 項未完成）"


def _list_todos(args, ctx):
    items = _load_todos().get(ctx["user_id"], [])
    pending = [t for t in items if not t.get("done")]
    if not pending:
        return "🎉 你沒有未完成的待辦，讚！"
    lines = ["📝 你的待辦清單："]
    for i, t in enumerate(pending, 1):
        lines.append(f"  {i}. {t['text']}")
    return "\n".join(lines)


def _complete_todo(args, ctx):
    d = _load_todos()
    items = d.get(ctx["user_id"], [])
    pending = [t for t in items if not t.get("done")]
    if not pending:
        return "目前沒有待辦可以完成喔。"
    key = args.get("which")
    target = None
    if isinstance(key, (int, float)) or (isinstance(key, str) and key.isdigit()):
        idx = int(key) - 1
        if 0 <= idx < len(pending):
            target = pending[idx]
    if target is None and isinstance(key, str):
        target = next((t for t in pending if key.strip() in t["text"]), None)
    if target is None:
        return "找不到那一項待辦，先用「看待辦」確認編號吧。"
    target["done"] = True
    _save_todos(d)
    return f"🎯 完成：{target['text']}！"


# ══════════════════════════════════════════════════════════════════
# 單位換算（長度／重量／溫度）
# ══════════════════════════════════════════════════════════════════

_UNIT_TABLE = {
    "length": {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mi": 1609.34, "mile": 1609.34,
               "ft": 0.3048, "in": 0.0254, "inch": 0.0254, "yd": 0.9144,
               "公里": 1000, "公尺": 1, "公分": 0.01, "英里": 1609.34, "英尺": 0.3048, "英吋": 0.0254},
    "weight": {"kg": 1, "g": 0.001, "mg": 1e-6, "t": 1000, "ton": 1000, "lb": 0.453592,
               "pound": 0.453592, "oz": 0.0283495,
               "公斤": 1, "公克": 0.001, "噸": 1000, "磅": 0.453592, "盎司": 0.0283495,
               "台斤": 0.6, "斤": 0.6},
}


def _to_celsius(v, u):
    u = u.lower()
    if u in ("c", "°c", "攝氏"):
        return v
    if u in ("f", "°f", "華氏"):
        return (v - 32) * 5 / 9
    if u in ("k", "克耳文"):
        return v - 273.15
    return None


def _from_celsius(c, u):
    u = u.lower()
    if u in ("c", "°c", "攝氏"):
        return c
    if u in ("f", "°f", "華氏"):
        return c * 9 / 5 + 32
    if u in ("k", "克耳文"):
        return c + 273.15
    return None


def _unit_convert(args, ctx):
    try:
        amount = float(args.get("amount"))
    except Exception:
        return "請提供數值。"
    frm = (args.get("from_unit") or "").strip()
    to = (args.get("to_unit") or "").strip()
    # 溫度
    c = _to_celsius(amount, frm)
    if c is not None:
        res = _from_celsius(c, to)
        if res is not None:
            return f"{amount:g} {frm} = {res:.2f} {to}"
        return f"不支援溫度單位「{to}」。"
    # 長度/重量
    for cat, table in _UNIT_TABLE.items():
        if frm in table and to in table:
            res = amount * table[frm] / table[to]
            return f"{amount:g} {frm} = {res:,.4g} {to}"
    return f"我不會換算「{frm}」→「{to}」（支援長度/重量/溫度）。"


# ══════════════════════════════════════════════════════════════════
# 統一發票對獎（財政部官方號碼）
# ══════════════════════════════════════════════════════════════════

async def _invoice_lottery(args, ctx):
    # 註：財政部憑證缺 Subject Key Identifier，部分新版 OpenSSL 會拒絕（此機器測試即失敗），
    # 但在多數伺服器（如 Debian）可正常驗證。這裡維持正常 TLS 驗證，失敗時優雅回錯誤訊息。
    try:
        async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
            r = await c.get("https://invoice.etax.nat.gov.tw/invoice.xml")
        txt = r.text
    except Exception:
        return "目前連不到財政部的發票中獎號碼服務（可能是憑證問題），稍後再試。"
    items = re.findall(r"<item>(.*?)</item>", txt, re.DOTALL)
    if not items:
        return "抓不到統一發票中獎號碼，稍後再試。"
    # 取最新一期（第一個 item）
    it = items[0]
    title = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
    period = unescape(title.group(1)).strip() if title else "最新一期"
    desc = re.search(r"<description>(.*?)</description>", it, re.DOTALL)
    body = unescape(re.sub(r"<[^>]+>", " ", desc.group(1))) if desc else ""
    body = re.sub(r"\s+", " ", body).strip()
    return f"🧾 {period}\n{body[:600]}"


# ══════════════════════════════════════════════════════════════════
# 萌典（中文字／詞／成語 解釋，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _moedict(args, ctx):
    term = (args.get("term") or "").strip()
    if not term:
        return "要查哪個字或詞？"
    async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://www.moedict.tw/uni/{urllib.parse.quote(term)}.json")
    if r.status_code != 200:
        return f"萌典查無「{term}」。"
    try:
        data = r.json()
    except Exception:
        return f"萌典查無「{term}」。"
    out = [f"📗 {data.get('title', term)}"]
    for h in data.get("heteronyms", [])[:2]:
        if h.get("bopomofo"):
            out.append(f"注音：{h['bopomofo']}")
        for d in h.get("definitions", [])[:4]:
            defn = d.get("def", "")
            if defn:
                line = f"• {defn}"
                if d.get("example"):
                    line += f"（例：{'；'.join(d['example'])[:60]}）"
                out.append(line)
    return "\n".join(out) if len(out) > 1 else f"萌典查無「{term}」。"


# ══════════════════════════════════════════════════════════════════
# 英文字典（dictionaryapi.dev，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _dictionary(args, ctx):
    word = (args.get("word") or "").strip()
    if not word:
        return "要查哪個英文單字？"
    async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
        r = await c.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}")
    if r.status_code != 200:
        return f"字典查無「{word}」。"
    try:
        entry = r.json()[0]
    except Exception:
        return f"字典查無「{word}」。"
    out = [f"🔤 {entry.get('word', word)}"]
    ph = entry.get("phonetic") or next((p.get("text") for p in entry.get("phonetics", []) if p.get("text")), "")
    if ph:
        out.append(f"音標：{ph}")
    for m in entry.get("meanings", [])[:3]:
        pos = m.get("partOfSpeech", "")
        for d in m.get("definitions", [])[:2]:
            out.append(f"• ({pos}) {d.get('definition', '')}")
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════
# 世界時鐘（pytz，純本地計算、免網路）
# ══════════════════════════════════════════════════════════════════

_CITY_TZ = {
    "台北": "Asia/Taipei", "taipei": "Asia/Taipei", "台灣": "Asia/Taipei",
    "東京": "Asia/Tokyo", "tokyo": "Asia/Tokyo", "日本": "Asia/Tokyo",
    "首爾": "Asia/Seoul", "seoul": "Asia/Seoul", "韓國": "Asia/Seoul",
    "北京": "Asia/Shanghai", "上海": "Asia/Shanghai", "中國": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong", "hongkong": "Asia/Hong_Kong",
    "新加坡": "Asia/Singapore", "singapore": "Asia/Singapore",
    "曼谷": "Asia/Bangkok", "泰國": "Asia/Bangkok",
    "倫敦": "Europe/London", "london": "Europe/London", "英國": "Europe/London",
    "巴黎": "Europe/Paris", "paris": "Europe/Paris", "法國": "Europe/Paris",
    "柏林": "Europe/Berlin", "德國": "Europe/Berlin",
    "紐約": "America/New_York", "newyork": "America/New_York", "美東": "America/New_York",
    "洛杉磯": "America/Los_Angeles", "la": "America/Los_Angeles", "美西": "America/Los_Angeles",
    "舊金山": "America/Los_Angeles", "西雅圖": "America/Los_Angeles",
    "雪梨": "Australia/Sydney", "sydney": "Australia/Sydney", "澳洲": "Australia/Sydney",
}


def _world_time(args, ctx):
    place = (args.get("place") or "").strip()
    if not place:
        return "要查哪個城市的時間？"
    tzname = _CITY_TZ.get(place.lower().replace(" ", "")) or _CITY_TZ.get(place)
    if not tzname and "/" in place:
        tzname = place                       # 直接給時區名
    if not tzname:
        return f"我不確定「{place}」的時區，試試常見城市（東京、紐約、倫敦…）或給時區名如 Asia/Tokyo。"
    tz = None
    try:
        from zoneinfo import ZoneInfo      # 標準庫（Linux 免額外相依）
        tz = ZoneInfo(tzname)
    except Exception:
        try:
            import pytz
            tz = pytz.timezone(tzname)
        except Exception:
            tz = None
    if tz is None:
        return f"查不到「{place}」的時間（伺服器缺時區資料）。"
    now = datetime.now(tz)
    week = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return f"🕐 {place} 現在 {now.strftime('%Y-%m-%d %H:%M')} 星期{week}"


# ══════════════════════════════════════════════════════════════════
# 短網址（is.gd，免金鑰）
# ══════════════════════════════════════════════════════════════════

async def _short_url(args, ctx):
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "請提供有效的 http(s) 網址。"
    api = "https://is.gd/create.php?format=simple&url=" + urllib.parse.quote(url)
    async with httpx.AsyncClient(headers=_UA, timeout=15, follow_redirects=True) as c:
        r = await c.get(api)
    short = r.text.strip()
    return f"🔗 {short}" if short.startswith("http") else f"縮網址失敗：{short[:100]}"


# ══════════════════════════════════════════════════════════════════
# 抽籤／擲骰／隨機決定
# ══════════════════════════════════════════════════════════════════

def _random_pick(args, ctx):
    choices = args.get("choices")
    if isinstance(choices, list) and choices:
        return f"🎲 我幫你選：{random.choice([str(x) for x in choices])}"
    dice = (args.get("dice") or "").strip().lower()
    m = re.fullmatch(r"(\d*)d(\d+)", dice)
    if m:
        n = int(m.group(1) or 1)
        sides = int(m.group(2))
        if 1 <= n <= 20 and 2 <= sides <= 1000:
            rolls = [random.randint(1, sides) for _ in range(n)]
            return f"🎲 擲 {dice}：{rolls}（總和 {sum(rolls)}）"
    lo = args.get("min")
    hi = args.get("max")
    if lo is not None and hi is not None:
        try:
            return f"🎲 {random.randint(int(lo), int(hi))}"
        except Exception:
            pass
    return "給我選項清單（choices）、骰子（dice 如 2d6）或範圍（min/max）吧。"


# ══════════════════════════════════════════════════════════════════
# 編碼工具（base64 / url / hash）
# ══════════════════════════════════════════════════════════════════

def _encode_tool(args, ctx):
    op = (args.get("op") or "").strip().lower()
    text = args.get("text") or ""
    try:
        if op == "base64_encode":
            return base64.b64encode(text.encode()).decode()
        if op == "base64_decode":
            return base64.b64decode(text.encode()).decode("utf-8", errors="replace")
        if op == "url_encode":
            return urllib.parse.quote(text)
        if op == "url_decode":
            return urllib.parse.unquote(text)
        if op in ("md5", "sha1", "sha256"):
            return getattr(hashlib, op)(text.encode()).hexdigest()
    except Exception as e:
        return f"處理失敗：{e}"
    return "op 請用 base64_encode/base64_decode/url_encode/url_decode/md5/sha1/sha256 其中之一。"


# ══════════════════════════════════════════════════════════════════
# 工具註冊表：OpenAI 相容 function schema + handler + 是否主人限定
# ══════════════════════════════════════════════════════════════════

_DEF = [
    (_web_search, False, "web_search", "用 DuckDuckGo 搜尋網路最新資訊，回傳標題與連結。需要即時、最新、你不確定的資訊時使用。",
     {"query": {"type": "string", "description": "搜尋關鍵字"},
      "max_results": {"type": "integer", "description": "回傳幾筆（預設5）"}}, ["query"]),
    (_web_fetch, False, "web_fetch", "抓取一個網址的網頁內容並回傳純文字（用來閱讀/摘要網頁）。",
     {"url": {"type": "string", "description": "要抓取的網址"},
      "max_chars": {"type": "integer"}}, ["url"]),
    (_get_weather, False, "get_weather", "查詢某地天氣（省略地點則依伺服器 IP 判斷）。",
     {"location": {"type": "string", "description": "地點，如 Taipei、Kaohsiung"}}, []),
    (_get_datetime, False, "get_datetime", "取得目前的日期、時間與星期（台北時間）。",
     {}, []),
    (_remember, False, "remember", "把使用者要你記住的事實存進長期記憶（下次對話仍記得）。",
     {"fact": {"type": "string", "description": "要記住的事情"}}, ["fact"]),
    (_recall, False, "recall", "從長期記憶取回跟這位使用者有關的內容。",
     {"query": {"type": "string", "description": "查詢關鍵字（可省略＝全部）"}}, []),
    (_run_python, False, "run_python", "執行一段 Python 程式碼並回傳 stdout（適合較複雜的資料處理／演算）。",
     {"code": {"type": "string"}, "timeout": {"type": "integer"}}, ["code"]),
    (_http_request, False, "http_request", "發送 HTTP 請求並回傳結果（呼叫外部 API）。",
     {"method": {"type": "string"}, "url": {"type": "string"},
      "headers": {"type": "object"}, "body": {}}, ["url"]),
    (_calculate, False, "calculate", "安全地計算一個數學算式（支援 + - * / ** % 與 sqrt/sin/cos/log/exp 等）。單純算數優先用這個。",
     {"expression": {"type": "string", "description": "算式，如 (3+4)*5 或 sqrt(2)"}}, ["expression"]),
    (_wikipedia, False, "wikipedia", "查詢維基百科的條目摘要（知識、人物、地點、名詞解釋）。",
     {"query": {"type": "string"}, "lang": {"type": "string", "description": "語言碼，預設 zh"}}, ["query"]),
    (_currency_convert, False, "currency_convert", "即時匯率換算。",
     {"amount": {"type": "number"}, "from": {"type": "string", "description": "來源幣別碼如 USD"},
      "to": {"type": "string", "description": "目標幣別碼如 TWD"}}, ["amount", "from", "to"]),
    (_set_reminder, False, "set_reminder", "設定提醒／鬧鐘，到時間會主動傳 LINE 訊息提醒使用者。用 minutes_from_now（幾分鐘後）或 at（幾點，格式 HH:MM 或 YYYY-MM-DD HH:MM）。",
     {"text": {"type": "string", "description": "要提醒的內容"},
      "minutes_from_now": {"type": "number", "description": "幾分鐘後提醒"},
      "at": {"type": "string", "description": "指定時間，如 08:30 或 2026-07-03 09:00"}}, ["text"]),
    (_crypto_price, False, "crypto_price", "查詢加密貨幣即時價格與 24 小時漲跌。",
     {"coin": {"type": "string", "description": "幣別，如 btc/eth/比特幣"},
      "vs": {"type": "string", "description": "計價幣別，預設 twd"}}, ["coin"]),
    (_stock_price, False, "stock_price", "查詢股票即時報價（美股如 AAPL、台股加 .TW 如 2330.TW）。",
     {"symbol": {"type": "string", "description": "股票代號"}}, ["symbol"]),
    (_translate, False, "translate", "把文字翻譯成指定語言（自動偵測來源語言，支援任意語言）。",
     {"text": {"type": "string"},
      "target": {"type": "string", "description": "目標語言碼，如 zh-TW/en/ja/ko，預設 zh-TW"}}, ["text"]),
    (_add_expense, False, "add_expense", "幫使用者記一筆帳（記帳）。",
     {"amount": {"type": "number", "description": "金額"},
      "category": {"type": "string", "description": "分類，如 餐飲/交通/娛樂"},
      "note": {"type": "string", "description": "備註"}}, ["amount"]),
    (_expense_summary, False, "expense_summary", "查詢記帳統計（今天或本月的花費，依分類）。",
     {"period": {"type": "string", "description": "today 或 month，預設 month"}}, []),
    (_youtube_summary, False, "youtube_summary", "取得 YouTube 影片的字幕逐字稿，供你為使用者摘要影片重點。",
     {"url": {"type": "string", "description": "YouTube 影片網址"}}, ["url"]),
    (_voice_reply, False, "voice_reply", "把一段文字轉成語音，用 LINE 語音訊息傳給使用者（當對方要求用語音／念出來時使用）。",
     {"text": {"type": "string", "description": "要念出來的文字"},
      "lang": {"type": "string", "description": "語言碼，預設 zh-TW；英文用 en、日文 ja"}}, ["text"]),
    (_make_qrcode, False, "make_qrcode", "把文字或網址產生成 QR Code 圖片並傳給使用者。",
     {"data": {"type": "string", "description": "要編碼的文字或網址"}}, ["data"]),
    (_news, False, "news", "查詢新聞頭條（可指定主題關鍵字，否則回今日頭條）。",
     {"topic": {"type": "string", "description": "主題關鍵字，可省略"}}, []),
    (_find_place, False, "find_place", "查詢地點的地址、座標與地圖連結。",
     {"query": {"type": "string", "description": "地點名稱或地址"}}, ["query"]),
    (_add_todo, False, "add_todo", "新增一項待辦事項到使用者的待辦清單。",
     {"text": {"type": "string"}}, ["text"]),
    (_list_todos, False, "list_todos", "列出使用者尚未完成的待辦事項。", {}, []),
    (_complete_todo, False, "complete_todo", "把某項待辦標記為完成（用編號或關鍵字）。",
     {"which": {"type": "string", "description": "待辦編號或關鍵字"}}, ["which"]),
    (_unit_convert, False, "unit_convert", "單位換算（長度／重量／溫度），如公里↔英里、公斤↔磅、攝氏↔華氏。",
     {"amount": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}},
     ["amount", "from_unit", "to_unit"]),
    (_invoice_lottery, False, "invoice_lottery", "查詢台灣統一發票最新一期中獎號碼。", {}, []),
    (_moedict, False, "moedict", "查萌典：中文字／詞／成語的注音與解釋（台灣國語辭典）。",
     {"term": {"type": "string", "description": "要查的中文字詞或成語"}}, ["term"]),
    (_dictionary, False, "dictionary", "查英文單字的音標與釋義。",
     {"word": {"type": "string", "description": "英文單字"}}, ["word"]),
    (_world_time, False, "world_time", "查世界各城市的現在時間。",
     {"place": {"type": "string", "description": "城市名，如 東京/紐約/倫敦，或時區名 Asia/Tokyo"}}, ["place"]),
    (_short_url, False, "short_url", "把長網址縮短。",
     {"url": {"type": "string"}}, ["url"]),
    (_random_pick, False, "random_pick", "抽籤／擲骰／隨機決定：給選項清單、骰子(如 2d6)或範圍(min/max)。",
     {"choices": {"type": "array", "items": {"type": "string"}},
      "dice": {"type": "string"}, "min": {"type": "integer"}, "max": {"type": "integer"}}, []),
    (_encode_tool, False, "encode_tool", "編碼/雜湊工具：base64、URL 編解碼、md5/sha1/sha256。",
     {"op": {"type": "string", "description": "base64_encode/base64_decode/url_encode/url_decode/md5/sha1/sha256"},
      "text": {"type": "string"}}, ["op", "text"]),
]

HANDLERS = {name: fn for (fn, _own, name, _d, _p, _r) in _DEF}
OWNER_ONLY = {name for (_fn, own, name, _d, _p, _r) in _DEF if own}

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": req}}}
    for (_fn, _own, name, desc, props, req) in _DEF
]


def owner_ids() -> set:
    raw = os.getenv("LINE_OWNER_IDS", "")
    return {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


async def dispatch(name: str, args: dict, ctx: dict) -> str:
    """執行一個工具呼叫，回傳字串結果（永不拋例外）。"""
    if name not in HANDLERS:
        return f"（未知的工具：{name}）"
    if name in OWNER_ONLY and not ctx.get("is_owner"):
        return "（這個功能只開放給管理者使用喔）"
    try:
        fn = HANDLERS[name]
        if asyncio.iscoroutinefunction(fn):
            return await fn(args, ctx)
        return fn(args, ctx)
    except Exception as e:
        logger.error(f"工具 {name} 失敗: {e}")
        return f"（工具 {name} 執行失敗：{e}）"
