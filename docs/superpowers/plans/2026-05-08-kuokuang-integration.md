# 國光客運整合實作計畫

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計畫。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 在現有系統中新增國光客運 (Kuo-Kuang) 的支援，包含 API 通訊、監控引擎、任務持久化以及 Telegram Bot 整合。

**架構：** 採用獨立模組化設計。新增 `KuoKuangAPI` 與 `KuoKuangMonitor` 模組，並擴充 `persistence.py` 以支援新的客運類型。整合至 `tg_bot.py` 以供使用者透過對話發起任務。

**技術棧：** Python, httpx, BeautifulSoup (bs4), logging

---

### 檔案結構

- `src/kuokuang_api.py`: (新增) 處理國光客運 API 通訊。
- `src/kuokuang_monitor.py`: (新增) 國光客運監控輪詢與自動訂票邏輯。
- `src/persistence.py`: (修改) 支援 `kuokuang` 類型的任務存檔與讀取。
- `tg_bot.py`: (修改) 新增國光客運指令與對話流程。
- `tests/test_kuokuang_api.py`: (新增) API 模組測試。

---

### 任務 1：實作 KuoKuangAPI 模組

**文件：**
- 創建：`src/kuokuang_api.py`
- 測試：`tests/test_kuokuang_api.py`

- [x] **步驟 1：編寫失敗的測試 (測試查詢功能)**

```python
# tests/test_kuokuang_api.py
import pytest
from src.kuokuang_api import KuoKuangAPI

@pytest.mark.asyncio
async def test_search_tickets():
    api = KuoKuangAPI()
    # 這裡使用 Mock 或預期實作後的行為
    result = await api.search_tickets("Taipei", "Kaohsiung", "2026-06-01")
    assert isinstance(result, list)
```

- [x] **步驟 2：運行測試驗證失敗**

運行：`pytest tests/test_kuokuang_api.py`
預期：FAIL (ModuleNotFoundError: No module named 'src.kuokuang_api')

- [x] **步驟 3：實作 KuoKuangAPI 基本結構**

```python
# src/kuokuang_api.py
import httpx
import logging
from typing import List, Dict, Any

logger = logging.getLogger("KuoKuangAPI")

class KuoKuangAPI:
    def __init__(self):
        self.base_url = "https://order.kingbus.com.tw" # 示意 URL，實作時依實際分析調整
        self.client = httpx.AsyncClient(timeout=10.0)

    async def search_tickets(self, from_st: str, to_st: str, date: str) -> List[Dict[str, Any]]:
        # 實作查詢邏輯
        return []

    async def book_ticket(self, schedule_id: str, id_no: str, phone: str, name: str) -> bool:
        # 實作訂票邏輯
        return False
```

- [x] **步驟 4：運行測試驗證通過**

- [x] **步驟 5：Commit**

```bash
git add src/kuokuang_api.py tests/test_kuokuang_api.py
git commit -m "feat(api): add KuoKuangAPI base module"
```

---

### 任務 2：實作 KuoKuangMonitor 監控引擎

**文件：**
- 創建：`src/kuokuang_monitor.py`

- [x] **步驟 1：實作 KuoKuangMonitor 類別**

參考 `HohsinMonitor` 的結構，實作 `run` 迴圈。

```python
# src/kuokuang_monitor.py
import asyncio
import logging
from .kuokuang_api import KuoKuangAPI

class KuoKuangMonitor:
    def __init__(self, from_station, to_station, travel_date, start_time, end_time, user_id_no, user_phone, user_name, notifier=None):
        self.api = KuoKuangAPI()
        self.from_station = from_station
        self.to_station = to_station
        self.travel_date = travel_date
        self.start_time = start_time
        self.end_time = end_time
        self.user_id_no = user_id_no
        self.user_phone = user_phone
        self.user_name = user_name
        self.notifier = notifier
        self.is_running = True

    async def run(self):
        while self.is_running:
            # 1. 查詢餘票
            # 2. 如果有票且在時間區聯內，嘗試訂票
            # 3. 成功後發送通知並停止監控
            await asyncio.sleep(60)
```

- [x] **步驟 2：Commit**

```bash
git add src/kuokuang_monitor.py
git commit -m "feat(monitor): add KuoKuangMonitor engine"
```

---

### 任務 3：擴充任務持久化 (Persistence)

**文件：**
- 修改：`src/persistence.py`

- [ ] **步驟 1：修改 `save_tasks_to_file` 以支援 `kuokuang`**

```python
# src/persistence.py 修改內容
# 在判斷 bus_type 的邏輯中加入:
if "KuoKuang" in monitor_type:
    bus_type = "kuokuang"
```

- [ ] **步驟 2：修改 `load_tasks_from_file` (若存在) 以重建 `KuoKuangMonitor` 實體**

- [ ] **步驟 3：Commit**

```bash
git add src/persistence.py
git commit -m "feat(persistence): support kuokuang task type"
```

---

### 任務 4：Telegram Bot 整合

**文件：**
- 修改：`tg_bot.py`

- [ ] **步驟 1：新增國光客運選單選項**

- [ ] **步驟 2：實作收集身分證、手機、姓名的對話流程**

- [ ] **步驟 3：啟動監控任務並存入任務清單**

- [ ] **步驟 4：Commit**

```bash
git add tg_bot.py
git commit -m "feat(bot): integrate kuokuang booking flow into tg_bot"
```
