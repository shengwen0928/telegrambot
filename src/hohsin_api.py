import os
import httpx
import logging
from typing import List, Dict, Any, Optional
from .ocr_engine import OCREngine
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("HohsinAPI")

class HohsinAPI:
    """和欣客運 API 通訊模組。"""

    BASE_URL = "https://api.ebus.com.tw"
    VAPI_BASE = "https://vapi.ebus.com.tw/app/android"   # 手機 App API（QR 走這裡）
    # App 內建的匿名墊底 token（從 APK libapp.so 取得，sub:2，供未登入 vapi 呼叫/登入用）
    VAPI_DEFAULT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImN0eSI6IkpXVCJ9.eyJodHRwOi8vc2NoZW1hcy54bWxzb2FwLm9yZy93cy8yMDA1LzA1L2lkZW50aXR5L2NsYWltcy9uYW1laWRlbnRpZmllciI6IjIiLCJBc3BOZXQuSWRlbnRpdHkuU2VjdXJpdHlTdGFtcCI6ImJjZDI4NzdmLTgzN2MtNGRjZC05MTI3LWFhZWQ2YjMyYzZjNCIsInN1YiI6IjIiLCJqdGkiOiJlYTJlNDhmNi01NWQ2LTQxZTEtYWEzOS1hYmMwNGYxZmMzYTkiLCJpYXQiOjE3NDc3MTIwNjgsIm5iZiI6MTc0NzcxMjA2OCwiZXhwIjoyMDYzMDcyMDY4LCJpc3MiOiJCYWNrZW5kIiwiYXVkIjoiQmFja2VuZCJ9.kqU9J78vPb3nNCUjlagETDGh2krfrqph7sqIGzIAJD4"
    CAPTCHA_URL = "https://www.ebus.com.tw/Common/GetCaptchaImage"
    DEFAULT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImN0eSI6IkpXVCJ9.eyJodHRwOi8vc2NoZW1hcy54bWxzb2FwLm9yZy93cy8yMDA1LzA1L2lkZW50aXR5L2NsYWltcy9uYW1laWRlbnRpZmllciI6IjEiLCJBc3BOZXQuSWRlbnRpdHkuU2VjdXJpdHlTdGFtcCI6IjdhYThkYjA3LTJlMWQtNDdlYS1hMjQyLTg1NDJhNzZiMTg1YyIsInN1YiI6IjEiLCJqdGkiOiI5NTlhNWJlNy05YzI0LTQ5NTEtOGQxMS02MTY3ZDRjOWYyZmIiLCJpYXQiOjE3NDc3MTIwMzcsIm5iZiI6MTc0NzcxMjAzNywiZXhwIjoyMDYzMDcyMDM3LCJpc3MiOiJCYWNrZW5kIiwiYXVkIjoiQmFja2VuZCJ9.UwUVXBOVlmm64Os4masmSEME1TpZVzVWWxDOLkOabpg"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.ebus.com.tw",
            "Referer": "https://www.ebus.com.tw/",
            "Authorization": f"Bearer {self.DEFAULT_TOKEN}"
        }
        self.client = httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=self.headers)
        self.ocr = OCREngine()
        self.access_token: Optional[str] = None
        self._phone: Optional[str] = None       # 存起來供 App(vapi) 登入用
        self._password: Optional[str] = None
        self.user_info: Dict[str, Any] = {}
        self._stations_cache: List[Dict[str, Any]] = []

    async def get_station_name(self, station_id: str) -> str:
        """根據 ID 獲取站名。"""
        if not self._stations_cache:
            self._stations_cache = await self.get_stations()
        for s in self._stations_cache:
            if s.get("id") == station_id:
                return s.get("operatingName", station_id)
        return station_id

    async def get_member_info(self) -> Dict[str, Any]:
        """獲取會員詳細資料（需要使用者 Token）。"""
        if not self.access_token:
            raise ValueError("執行此操作前必須先登入。")
        
        url = f"{self.BASE_URL}/web/members"
        response = await self.client.get(url)
        response.raise_for_status()
        data = response.json()
        self.user_info = data.get("result", {})
        return self.user_info

    async def get_captcha(self) -> bytes:
        """獲取驗證碼圖片。"""
        response = await self.client.get(self.CAPTCHA_URL)
        response.raise_for_status()
        return response.content

    async def login(self, user_name: str, password: str) -> bool:
        """
        執行登入邏輯。強制要求傳入帳號密碼。
        """
        if not user_name or not password:
            raise ValueError("必須提供和欣客運的帳號與密碼。")

        self._phone, self._password = user_name, password   # 存起來供 App(vapi) 登入

        # 0. 先造訪登入頁面以建立基礎 Cookies
        await self.client.get("https://www.ebus.com.tw/Home/LogIn")

        # 1. 獲取驗證碼
        captcha_bytes = await self.get_captcha()
        captcha_code = self.ocr.classify(captcha_bytes)

        # 2. 登入請求
        login_url = f"{self.BASE_URL}/web/members/tokenauth"
        payload = {
            "userName": user_name,
            "securityCode": password,
            "CaptchaCode": captcha_code
        }

        response = await self.client.post(login_url, json=payload)
        
        if response.status_code == 200:
            data = response.json()
            result = data.get("result")
            if result and result.get("accessToken"):
                self.access_token = result["accessToken"]
                # 更新全局 Header，讓後續 client 請求自動帶上新 token
                self.headers["Authorization"] = f"Bearer {self.access_token}"
                self.client.headers.update(self.headers)
                
                # 登入成功後立即獲取完整會員資料 (含身分證號)
                await self.get_member_info()
                return True
            else:
                print(f"登入失敗，驗證碼: {captcha_code}, 回應: {data}")
        else:
            print(f"登入請求錯誤: {response.status_code}, 內容: {response.text}")
        
        return False

    async def get_seating_plans(self, schedule_id: int, into_station_id: str, outof_station_id: str, travel_date: str = "", start_time: str = "00:00", end_time: str = "23:59") -> List[Dict[str, Any]]:
        """獲取班次座位圖（根據真實封包重構）。"""
        
        url = f"{self.BASE_URL}/web/schedules/{schedule_id}/seatingplans"
        
        # 根據真實封包，只需要起訖站 ID
        params = {
            "intoStationId": into_station_id,
            "outofStationId": outof_station_id
        }

        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.DEFAULT_TOKEN}"
        headers["Referer"] = "https://www.ebus.com.tw/"

        response = await self.client.get(url, params=params, headers=headers)
        
        if response.status_code != 200:
            print(f"!!! [座位圖失敗] 狀態碼: {response.status_code} | 回應: {response.text[:100]}")
            response.raise_for_status()

        data = response.json()
        result = data.get("result")
        
        if isinstance(result, dict) and "seatings" in result:
            return result["seatings"]
            
        print(f"!!! [座位圖為空] 原始回應結構: {data}")
        return []


    async def book_ticket(self, schedule: Dict[str, Any], seat_nos: List[int], ticket_kind_id: Optional[str] = None) -> Dict[str, Any]:
        """
        執行訂位動作。支援多個座位。
        """
        if not self.access_token or not self.user_info:
            raise ValueError("執行訂位前必須先登入。")

        # 如果沒指定票種，自動從班次資料中抓取第一個可用的票種 ID
        if not ticket_kind_id:
            prices = schedule.get("ticketPrices", [])
            if prices:
                ticket_kind_id = prices[0]["ticketKindId"]
            else:
                ticket_kind_id = "S" # 萬一沒資料，回退到全票 S

        # 建立座位節點清單
        tickets_payload = []
        for sn in seat_nos:
            tickets_payload.append({
                "ticketKindId": ticket_kind_id,
                "seatNo": int(sn)
            })

        url = f"{self.BASE_URL}/web/orders/book"
        # 確保 Header 帶上最新的 access_token
        self.headers["Authorization"] = f"Bearer {self.access_token}"
        self.client.headers.update(self.headers)

        payload = {
            "dailyScheduleId": schedule["dailyScheduleId"],
            "intoStationId": schedule["intoStationId"],
            "outofStationId": schedule["outofStationId"],
            "returnIntoStationId": "",
            "returnOutofStationId": "",
            "tickets": tickets_payload,
            "memberId": self.user_info.get("id"),
            "passengerName": self.user_info.get("name", "使用者"),
            "passengerIdentityNo": self.user_info.get("identityNo", ""),
            "passengerPhoneNumber": self.user_info.get("phoneNumber", ""),
            "passengerEmailAddress": self.user_info.get("emailAddress", ""),
            "sex": self.user_info.get("sex", 1), # 預設男
            "isTaiwanTravelCard": False
        }
        
        response = await self.client.post(url, json=payload)
        
        # 嘗試解析 JSON，不論狀態碼為何
        try:
            result_data = response.json()
        except Exception:
            result_data = {"success": False, "error": {"message": response.text}}

        if response.status_code != 200:
            logger.error(f"訂位 API 失敗! 狀態碼: {response.status_code}, 回應: {response.text}")
            return result_data
            
        return result_data

    async def get_stations(self) -> List[Dict[str, Any]]:
        """獲取車站清單（使用預設 Token）。"""
        url = f"{self.BASE_URL}/web/stations"
        # 即使登入了，此 API 仍需使用預設 Token
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.DEFAULT_TOKEN}"
        response = await self.client.get(url, headers=headers)
        if response.status_code != 200:
            print(f"獲取車站失敗: {response.status_code}, 內容: {response.text}")
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("items", [])

    async def get_schedules(self, from_station_id: str, to_station_id: str, travel_date: str, start_time: str = "00:00", end_time: str = "23:59") -> List[Dict[str, Any]]:
        """
        查詢班次列表（使用預設 Token）。
        """
        url = f"{self.BASE_URL}/web/schedules"
        params = {
            "intoStationId": from_station_id,
            "outofStationId": to_station_id,
            "departureDate": travel_date.replace("-", "/"),
            "beginDepartureTime": start_time,
            "endDepartureTime": end_time,
            "isVacantOnly": "false",
            "isIncludeSoldOut": "true"
        }
        # 即使登入了，此 API 仍需使用預設 Token
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.DEFAULT_TOKEN}"
        
        response = await self.client.get(url, params=params, headers=headers)
        if response.status_code != 200:
            print(f"查詢班次失敗: {response.status_code}, 內容: {response.text}")
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("items", [])

    async def get_vacant_seats(self, from_station_id: str, to_station_id: str, travel_date: str) -> List[Dict[str, Any]]:
        """舊版餘票查詢（首頁小工具用）。"""
        url = f"{self.BASE_URL}/web/schedules/seats/vacant"
        params = {
            "fromStation": from_station_id,
            "toStation": to_station_id,
            "travelDate": travel_date
        }
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.DEFAULT_TOKEN}"
        response = await self.client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def _vapi_login(self, phone: str, password: str) -> Optional[str]:
        """登入和欣手機 App API（vapi），回 accessToken。診斷用：記錄狀態/錯誤訊息，不記 token 本身。"""
        import uuid
        url = f"{self.VAPI_BASE}/members/tokenauth"
        cid, did = str(uuid.uuid4()), str(uuid.uuid4())
        # 超集：網頁版密碼欄位是 securityCode，這裡兩個都放；platform 大小寫都給
        body = {
            "account": phone, "userName": phone, "phone": phone, "phoneNumber": phone,
            "password": password, "securityCode": password,
            "platform": "Android", "Platform": "Android",
            "androidClientId": cid, "deviceId": did,
            "version": "1.0.0", "fcmToken": "", "VerifyCode": "",
        }
        # 帶 App 內建匿名墊底 token（未登入呼叫 vapi 需要它，否則 ABP 回 401 did not login）
        hdr = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "Dart/3.4 (dart:io)",
               "Authorization": f"Bearer {self.VAPI_DEFAULT_TOKEN}"}
        for method in ("POST",):
            try:
                r = await self.client.request(method, url, json=body, headers=hdr, timeout=30.0)
                ctype = r.headers.get("Content-Type", "")
                if r.status_code == 200 and "json" in ctype:
                    j = r.json()
                    res = j.get("result", j) if isinstance(j, dict) else {}
                    res = res if isinstance(res, dict) else {}
                    tok = res.get("accessToken") or res.get("token")
                    logger.info(f"[VAPI登入] {method} 200 result_keys={list(res.keys())} has_token={bool(tok)}")
                    if tok:
                        return tok
                else:
                    emsg = edet = None
                    try:
                        err = (r.json().get("error") or {})
                        emsg, edet = err.get("message"), err.get("details")
                    except Exception:
                        emsg = r.text[:200]
                    logger.info(f"[VAPI登入] {method} status={r.status_code} allow={r.headers.get('Allow','')} error={emsg} details={edet}")
            except Exception as e:
                logger.warning(f"[VAPI登入] {method} 例外類型: {type(e).__name__} repr={e!r}")
        return None

    async def get_resilient_qrcode(self, ticket_id: int) -> Optional[bytes]:
        """
        取得車票 QR（和欣 2026 改版：走手機 App API vapi）：
          1. App 登入 vapi/members/tokenauth → accessToken
          2. PUT vapi/tickets/{id}/infos/back → result.qrcode（動態時效 payload）
          3. 用 segno 把 payload 產生成 QR PNG 回傳
        """
        if not (self._phone and self._password):
            logger.warning("[QR] 無帳密可做 App 登入")
            return None
        app_token = await self._vapi_login(self._phone, self._password)
        if not app_token:
            logger.warning("[QR] App 登入失敗，拿不到 vapi token")
            return None
        # 取 QR payload
        try:
            r = await self.client.put(
                f"{self.VAPI_BASE}/tickets/{ticket_id}/infos/back",
                headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json",
                         "User-Agent": "Dart/3.4 (dart:io)"}, json={}, timeout=30.0)
            if r.status_code != 200:
                logger.info(f"[QR] infos/back status={r.status_code} body={r.text[:200]}")
                return None
            j = r.json()
            res = j.get("result", j) if isinstance(j, dict) else {}
            res = res if isinstance(res, dict) else {}
            payload = res.get("qrcode")
            logger.info(f"[QR] infos/back 200 result_keys={list(res.keys())} "
                        f"has_qrcode={bool(payload)} expired={res.get('expired')}")
            if not payload:
                return None
        except Exception as e:
            logger.error(f"[QR] infos/back 例外類型: {type(e).__name__} repr={e!r}")
            return None
        # 用 payload 產生 QR 圖
        try:
            import io
            import segno
            buf = io.BytesIO()
            segno.make(str(payload), error="m").save(buf, kind="png", scale=8, border=2)
            logger.info("[QR] 已用 payload 產生 QR PNG ✅")
            return buf.getvalue()
        except Exception as e:
            logger.error(f"[QR] 產生 QR 圖失敗（伺服器需 pip install segno）: {e}")
            return None

    def _decode_qr_base64(self, data_str: str) -> Optional[bytes]:
        import base64
        try:
            if "," in data_str: data_str = data_str.split(",")[1]
            return base64.b64decode(data_str)
        except:
            return None

    async def get_ticket_detail(self, ticket_id: int) -> Dict[str, Any]:
        """獲取特定車票的詳細資訊，並加入擬真 Header 避免被擋。"""
        url = f"{self.BASE_URL}/web/tickets/{ticket_id}"
        headers = self.headers.copy()
        headers.update({
            "Referer": "https://www.ebus.com.tw/Home/TicketDetail",
            "X-Requested-With": "XMLHttpRequest"
        })
        
        try:
            response = await self.client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                logger.info(f"成功獲取車票詳情 JSON: {data}")
                return data.get("result", {})
            return {}
        except Exception as e:
            logger.error(f"獲取車票詳情失敗: {e}")
            return {}

    async def get_my_orders(self) -> List[Dict[str, Any]]:
        """獲取目前登入使用者的訂單列表。"""
        if not self.access_token:
            raise ValueError("執行此操作前必須先登入。")
            
        # 嘗試路徑清單
        paths = ["web/orders", "web/orders/upcoming", "web/members/orders"]
        
        for path in paths:
            url = f"{self.BASE_URL}/{path}"
            logger.info(f"嘗試抓取訂單路徑: {url}")
            response = await self.client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                items = data.get("result", {}).get("items", [])
                if items: return items
        
        return []

    async def close(self):
        """關閉 HTTP 客戶端。"""
        await self.client.aclose()
