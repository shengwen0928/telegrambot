# LINE 互動式搶票機器人設計 (16天版)

## 核心設計理念
此擴充功能將建立一個全新的啟動程式 (`line_bot.py`)，**完全不更動現有的**核心搶票邏輯。
不同於 Telegram 的 Long Polling 模式，LINE Bot 必須使用 **Webhook (HTTP Server)** 來接收訊息。我們將使用 `FastAPI` 或 `Flask` 搭配 `line-bot-sdk` 來實作。

## 狀態機與記憶體管理 (State Management)
由於 HTTP 是無狀態的 (Stateless)，且 LINE 沒有內建的 FSM (不像 aiogram)，我們需要實作一個簡單的記憶體快取 (Dictionary 或 Redis) 來記錄每個使用者的操作步驟。
*   `user_states = { "U12345": {"step": "waiting_for_from", "bus": "hohsin"} }`

**狀態流轉**：
1. **WaitingForBus**: 選擇客運公司（和欣客運）。
2. **WaitingForFrom**: 選擇上車站。
3. **WaitingForTo**: 選擇下車站。
4. **WaitingForDate**: 選擇日期（16 天選擇器）。
5. **WaitingForTime**: 選擇時段（00:00~03:00 等）。

## 互動介面設計 (Flex Message & Quick Reply)

### 1. 啟動選單 (圖文選單 / 輸入特定字)
*   使用者輸入「搶票」。
*   **回覆 (Quick Reply)**: 「請選擇您要搶票的客運：」
    *   `[ 🚌 和欣客運 ]`

### 2. 選擇上車站 / 下車站
*   LINE 的 Quick Reply 最多只能放 13 個按鈕，而和欣的站點多達數十個。
*   **解決方案 (Carousel Template)**: 實作一個左右滑動的「輪播選單」。每張卡片放 3 個站點按鈕。例如第一張卡片是北部車站，第二張是中部車站。

### 3. 日期選擇器 (未來 16 天)
*   **LINE 內建功能**: 我們將直接使用 LINE 強大的 **「日期選擇器 (Datetime Picker) Action」**！
*   當需要選日期時，彈出一個按鈕 `[ 📅 選擇乘車日期 ]`。
*   使用者點擊後，LINE 會在手機下方彈出原生的月曆滾輪，並設定 `max` (最大值為 16 天後) 與 `min` (最小值為今天)。

### 4. 時段選擇器
*   使用 Quick Reply 提供和欣客運標準的 8 個時段按鈕：
    *   `[00:00~03:00]`, `[03:00~06:00]` 等。

## 部署與環境要求 (關鍵挑戰)
1.  **HTTPS Webhook**: LINE 要求伺服器必須有合法的 SSL 憑證 (HTTPS)。
2.  **本地開發**: 需使用 `ngrok` 將本地端 Port 暴露至網際網路。
3.  **GCE 雲端部署**: 若要部署在 Google Cloud Compute Engine，需要：
    *   固定外部 IP。
    *   申請免費網域並設定 SSL (例如使用 Let's Encrypt 或 Cloudflare)。
    *   開啟 GCP 的 HTTP/HTTPS 防火牆規則。

## 依賴套件
*   `line-bot-sdk` (官方 SDK)
*   `fastapi` (高效能 API 伺服器)
*   `uvicorn` (ASGI 伺服器)

---
> **等待使用者確認：**
> LINE Bot 的實作比 Telegram 複雜許多（特別是 Webhook 與 SSL 憑證的需求）。如果您準備好了 Line Developer 帳號，並能接受在 GCE 上設定網域/HTTPS，請核准此計畫，我將為您打造 `line_bot.py`！