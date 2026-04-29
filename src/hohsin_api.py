import os
import httpx
from typing import List, Dict, Any, Optional
from .ocr_engine import OCREngine
from dotenv import load_dotenv

load_dotenv()

class HohsinAPI:
    """和欣客運 API 通訊模組。"""

    BASE_URL = "https://api.ebus.com.tw"
    CAPTCHA_URL = "https://www.ebus.com.tw/Common/GetCaptchaImage"
    DEFAULT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCIsImN0eSI6IkpXVCJ9.eyJodHRwOi8vc2NoZW1hcy54bWxzb2FwLm9yZy93cy8yMDA1LzA1L2lkZW50aXR5L2NsYWltcy9uYW1laWRlbnRpZmllciI6IjEiLCJBc3BOZXQuSWRlbnRpdHkuU2VjdXJpdHlTdGFtcCI6IjdhYThkYjA3LTJlMWQtNDdlYS1hMjQyLTg1NDJhNzZiMTg1YyIsInN1YiI6IjEiLCJqdGkiOiI5NTlhNWJlNy05YzI0LTQ5NTEtOGQxMS02MTY3ZDRjOWYyZmIiLCJpYXQiOjE3NDc3MTIwMzcsIm5iZiI6MTc0NzcxMjAzNywiZXhwIjoyMDYzMDcyMDM3LCJpc3MiOiJCYWNrZW5kIiwiYXVkIjoiQmFja2VuZCJ9.UwUVXBOVlmm64Os4masmSEME1TpZVzVWWxDOLkOabpg"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        self.ocr = OCREngine()
        self.access_token: Optional[str] = None
        self.user_info: Dict[str, Any] = {}
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.ebus.com.tw",
            "Referer": "https://www.ebus.com.tw/",
            "Authorization": f"Bearer {self.DEFAULT_TOKEN}"
        }

    async def get_member_info(self) -> Dict[str, Any]:
        """獲取會員詳細資料（需要使用者 Token）。"""
        if not self.access_token:
            raise ValueError("執行此操作前必須先登入。")
        
        url = f"{self.BASE_URL}/web/members"
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.access_token}"
        
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        self.user_info = data.get("result", {})
        return self.user_info

    async def get_captcha(self) -> bytes:
        """獲取驗證碼圖片。"""
        response = await self.client.get(self.CAPTCHA_URL)
        response.raise_for_status()
        return response.content

    async def login(self, user_name: Optional[str] = None, password: Optional[str] = None) -> bool:
        """
        執行登入邏輯。
        """
        user_name = user_name or os.getenv("USER_PHONE")
        password = password or os.getenv("USER_PASSWORD")

        if not user_name or not password:
            raise ValueError("必須提供帳號密碼或設置環境變數。")

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

        response = await self.client.post(login_url, json=payload, headers=self.headers)
        
        if response.status_code == 200:
            data = response.json()
            result = data.get("result")
            if result and result.get("accessToken"):
                self.access_token = result["accessToken"]
                self.headers["Authorization"] = f"Bearer {self.access_token}"
                # 登入成功後立即獲取完整會員資料 (含身分證號)
                await self.get_member_info()
                return True
            else:
                print(f"登入失敗，驗證碼: {captcha_code}, 回應: {data}")
        else:
            print(f"登入請求錯誤: {response.status_code}, 內容: {response.text}")
        
        return False

    async def get_seating_plans(self, schedule_id: int) -> List[Dict[str, Any]]:
        """獲取班次座位圖。"""
        url = f"{self.BASE_URL}/web/schedules/{schedule_id}/seatingplans"
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.DEFAULT_TOKEN}"
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("result", [])

    async def book_ticket(self, schedule: Dict[str, Any], seat_no: int, ticket_kind_id: str = "G") -> Dict[str, Any]:
        """
        執行訂位動作。
        
        Args:
            schedule: 班次資料物件。
            seat_no: 座位號碼。
            ticket_kind_id: 票種 ID (預設為促銷票 G)。
        """
        if not self.access_token or not self.user_info:
            raise ValueError("執行訂位前必須先登入。")

        url = f"{self.BASE_URL}/web/orders/book"
        payload = {
            "dailyScheduleId": schedule["dailyScheduleId"],
            "intoStationId": schedule["intoStationId"],
            "outofStationId": schedule["outofStationId"],
            "returnIntoStationId": "",
            "returnOutofStationId": "",
            "tickets": [
                {
                    "ticketKindId": ticket_kind_id,
                    "seatNo": seat_no
                }
            ],
            "memberId": self.user_info["id"],
            "passengerName": self.user_info["name"],
            "passengerIdentityNo": self.user_info["identityNo"],
            "passengerPhoneNumber": self.user_info["phoneNumber"],
            "passengerEmailAddress": self.user_info["emailAddress"],
            "sex": self.user_info["sex"],
            "isTaiwanTravelCard": False
        }
        
        headers = self.headers.copy()
        headers["Authorization"] = f"Bearer {self.access_token}"
        
        response = await self.client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    async def get_stations(self) -> List[Dict[str, Any]]:
        """獲取車站清單（使用預設 Token）。"""
        url = f"{self.BASE_URL}/web/stations"
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
        
        Args:
            from_station_id: 起點車站 ID (如 G03)。
            to_station_id: 終點車站 ID (如 B01)。
            travel_date: 乘車日期 (YYYY-MM-DD)。
            start_time: 開始時間 (HH:mm)。
            end_time: 結束時間 (HH:mm)。
        """
        url = f"{self.BASE_URL}/web/schedules"
        params = {
            "intoStationId": from_station_id,
            "outofStationId": to_station_id,
            "departureDate": travel_date.replace("-", "/"),
            "beginDepartureTime": start_time,
            "endDepartureTime": end_time
        }
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
