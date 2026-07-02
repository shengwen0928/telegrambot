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

    async def get_resilient_qrcode(self, ticket_id: int) -> Optional[bytes]:
        """
        多層 Fallback 獲取 QR Code：
        0. 新版 APP API：vapi.ebus.com.tw/app/android/tickets/{id}/infos/back（動態 qrcode token）
        1. 嘗試 /web/tickets/{id}/qrcode
        2. 嘗試 /web/tickets/{id} 找 qrCodeData (base64)
        """
        # 第零層：新版 APP 端點（診斷：試 POST/GET，印狀態、Allow 標頭與欄位，不印個資）
        vapi_url = f"https://vapi.ebus.com.tw/app/android/tickets/{ticket_id}/infos/back"
        _vhdr = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        for _m in ("POST", "GET"):
            try:
                if _m == "POST":
                    r = await self.client.post(vapi_url, headers=_vhdr, json={})
                else:
                    r = await self.client.get(vapi_url, headers=_vhdr)
                ct = r.headers.get("Content-Type", "")
                allow = r.headers.get("Allow", "")
                if r.status_code == 200 and "json" in ct:
                    j = r.json()
                    res = j.get("result", j) if isinstance(j, dict) else {}
                    res = res if isinstance(res, dict) else {}
                    payload = res.get("qrcode")
                    logger.info(f"[QR新端點] {_m} 200 top_keys={list(j.keys()) if isinstance(j, dict) else '?'} "
                                f"result_keys={list(res.keys())} has_qrcode={bool(payload)} qrlen={len(str(payload)) if payload else 0}")
                    break
                else:
                    logger.info(f"[QR新端點] {_m} status={r.status_code} allow={allow} ct={ct} body={r.text[:300]}")
            except Exception as e:
                logger.warning(f"[QR新端點] {_m} 失敗: {e}")

        # 第一層：直接 API 下載
        url_api = f"{self.BASE_URL}/web/tickets/{ticket_id}/qrcode"
        try:
            resp = await self.client.get(url_api)
            ct = resp.headers.get("Content-Type", "")
            if "image" in ct:
                logger.info(f"[QR診斷] L1 {resp.status_code} 收到圖片 ({ct})，直接用")
                if resp.status_code == 200:
                    return resp.content
            else:
                logger.info(f"[QR診斷] L1 status={resp.status_code} ct={ct} body={resp.text[:800]}")
                if resp.status_code == 200:
                    data = resp.json()
                    res = data.get("result")
                    logger.info(f"[QR診斷] L1 top_keys={list(data.keys())} "
                                f"result_keys={list(res.keys()) if isinstance(res, dict) else type(res).__name__}")
                    qr_base64 = (res or {}).get("qrCodeData") if isinstance(res, dict) else None
                    if qr_base64:
                        return self._decode_qr_base64(qr_base64)
        except Exception as e:
            logger.warning(f"第一層獲取失敗: {e}")

        # 第二層：從詳情 JSON 提取
        try:
            detail = await self.get_ticket_detail(ticket_id)
            logger.info(f"[QR診斷] L2 detail_keys={list(detail.keys()) if isinstance(detail, dict) else type(detail).__name__}")
            qr_base64 = detail.get("qrCodeData") if isinstance(detail, dict) else None
            if qr_base64:
                return self._decode_qr_base64(qr_base64)
        except Exception as e:
            logger.warning(f"第二層獲取失敗: {e}")

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
