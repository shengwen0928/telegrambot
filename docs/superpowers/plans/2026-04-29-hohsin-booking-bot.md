# 和欣客運全自動監控搶票系統 實作計劃

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計劃。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 建立一個能全自動登入、監控餘票並透過 Telegram 通知的和欣客運搶票工具。

**架構：** 採用模組化設計，分離 OCR 辨識、API 通訊、監控引擎與通知功能。使用 `httpx` 進行高效 API 請求，`ddddocr` 處理驗證碼。

**技術棧：** Python 3.10+, httpx, ddddocr, python-dotenv

---

## 檔案結構
- `.env`: 儲存機密資訊（手機、密碼、Token）。
- `requirements.txt`: 專案依賴。
- `src/ocr_engine.py`: 負責驗證碼辨識。
- `src/hohsin_api.py`: 負責所有與和欣 API 的通訊。
- `src/notifier.py`: 負責 Telegram 通知。
- `src/monitor.py`: 核心監控與搶票邏輯。
- `main.py`: 程式入口。
- `tests/`: 存放各模組測試程式。

---

### 任務 1：環境初始化與配置

- [ ] **步驟 1：建立專案目錄與 `.env` 檔案**
```bash
# 建立目錄
mkdir -p src tests docs/superpowers
```
建立 `.env` 檔案並寫入您的資訊。

- [ ] **步驟 2：建立 `requirements.txt`**
內容需包含 `httpx`, `ddddocr`, `python-dotenv`。

- [ ] **步驟 3：安裝依賴**
執行：`pip install -r requirements.txt`

---

### 任務 2：OCR 驗證碼辨識模組

**檔案：**
- 建立：`src/ocr_engine.py`
- 測試：`tests/test_ocr.py`

- [ ] **步驟 1：編寫 OCR 測試**
測試程式碼應能讀取一張範例圖片並輸出 4 位字串。

- [ ] **步驟 2：實作 `src/ocr_engine.py`**
```python
import ddddocr

class OCREngine:
    def __init__(self):
        self.ocr = ddddocr.DdddOcr(show_ad=False)
    
    def classify(self, image_bytes):
        return self.ocr.classification(image_bytes)
```

- [ ] **步驟 3：驗證辨識功能**

---

### 任務 3：Telegram 通知模組

**檔案：**
- 建立：`src/notifier.py`

- [ ] **步驟 1：實作發送訊息功能**
```python
import httpx

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    async def send_message(self, text):
        payload = {"chat_id": self.chat_id, "text": text}
        async with httpx.AsyncClient() as client:
            await client.post(self.url, json=payload)
```

- [ ] **步驟 2：手動執行測試腳本，確認手機收到訊息**

---

### 任務 4：和欣 API 通訊模組（核心）

**檔案：**
- 建立：`src/hohsin_api.py`

- [ ] **步驟 1：實作 Session 獲取與登入邏輯**
包含獲取驗證碼、辨識、POST 登入並取得 `Access Token`。

- [ ] **步驟 2：實作車站清單獲取 (`/web/stations`)**

- [ ] **步驟 3：實作餘票查詢 API (`/web/schedules/seats/vacant`)**

---

### 任務 5：監控與自動搶票邏輯

**檔案：**
- 建立：`src/monitor.py`

- [ ] **步驟 1：編寫監控循環**
設定 1-3 秒隨機延遲。
- [ ] **步驟 2：實作「偵測到票源」後的自動訂位 POST 請求**
- [ ] **步驟 3：整合 Telegram 通知**

---

### 任務 6：總裝與測試運行

- [ ] **步驟 1：編寫 `main.py` 串接所有流程**
- [ ] **步驟 2：執行初步運行測試，確認能成功登入並開始監控**
