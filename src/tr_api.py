import os
import httpx
import logging
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup
from .tr_stations import TR_STATIONS
from .ocr_engine import OCREngine

logger = logging.getLogger("TR_API")

class TaiwanRailwayAPI:
    """台鐵 API 通訊模組。"""

    BASE_URL = "https://tip.railway.gov.tw/tra-tip-web/tip"
    
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": "https://tip.railway.gov.tw",
            "Referer": "https://tip.railway.gov.tw/tra-tip-web/tip"
        }
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=self.headers, verify=False)
        self.ocr = OCREngine()
        self.csrf_token = ""
        self.complete_token = ""

    async def login(self, username: str, password: str) -> bool:
        """執行台鐵會員登入。"""
        try:
            # 1. 獲取登入頁面的 CSRF Token 與 action-token
            login_page_url = f"{self.BASE_URL}/tip008/tip811/memberLogin"
            response = await self.client.get(login_page_url)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            csrf = soup.find('input', {'name': '_csrf'})
            action_token = soup.find('input', {'name': 'action-token'})
            
            if not csrf:
                logger.error(f"無法獲取登入所需的 CSRF Token。頁面長度: {len(response.text)}，開頭: {response.text[:500]}")
                return False

            url = f"{self.BASE_URL}/login"
            payload = {
                "_csrf": csrf['value'],
                "pType": "",
                "username": username,
                "password": password,
                "action-token": action_token['value'] if action_token else "",
                "action-name": "submit_form"
            }

            # 2. 發送登入請求
            resp = await self.client.post(url, data=payload)
            
            # 檢查是否登入成功 (台鐵登入成功通常會跳轉回首頁或顯示登出按鈕)
            if "memberLogout" in resp.text or resp.status_code == 302:
                logger.info(f"台鐵會員 {username} 登入成功！")
                return True
            else:
                logger.error(f"台鐵登入失敗，回傳狀態碼: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"登入過程發生異常: {str(e)}")
            return False

    async def init_session(self, mode: str = "personal"):
        """
        初始化 Session 並獲取 CSRF/Token。
        mode: "personal" (tip123) 或 "quick" (tip121)
        """
        try:
            path = "tip123" if mode == "personal" else "tip121"
            url = f"{self.BASE_URL}/tip001/{path}/query"
            response = await self.client.get(url)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 獲取 CSRF
            csrf = soup.find('input', {'name': '_csrf'})
            if csrf:
                self.csrf_token = csrf['value']
                logger.info(f"獲取 {mode} CSRF 成功: {self.csrf_token[:8]}...")
            
            # 獲取 Complete Token (tip123 用) 或 Quick Token (tip121 用)
            token_name = "completeToken" if mode == "personal" else "quickTipToken"
            token_input = soup.find('input', {'name': token_name})
            self.complete_token = token_input['value'] if token_input else ""
            
            if not self.complete_token:
                logger.warning(f"未能獲取 {token_name}，這可能會導致 POST 失敗。")
            
            return response.text
        except Exception as e:
            logger.error(f"初始化 {mode} Session 失敗: {e}")
            return ""

    async def guest_book_ticket(self, pid: str, from_stn: str, to_stn: str, date: str, start_time: str, end_time: str) -> bool:
        """
        使用「快速訂票 (訪客模式)」直接訂票。
        """
        try:
            # 1. 初始化快速訂票 Session
            await self.init_session(mode="quick")
            
            url = f"{self.BASE_URL}/tip001/tip121/bookingTicket"
            
            from_full = f"{from_stn}-{TR_STATIONS.get(from_stn, '')}"
            to_full = f"{to_stn}-{TR_STATIONS.get(to_stn, '')}"
            
            payload = {
                "_csrf": self.csrf_token,
                "custIdTypeEnum": "PERSON_ID",
                "pid": pid,
                "startStation": from_full,
                "endStation": to_full,
                "tripType": "ONEWAY",
                "orderType": "BY_TIME",
                "normalQty": "1",
                "ticketOrderParamList[0].tripNo": "TRIP1",
                "ticketOrderParamList[0].rideDate": date.replace("-", "/"),
                "ticketOrderParamList[0].startOrEndTime": "true",
                "ticketOrderParamList[0].startTime": start_time,
                "ticketOrderParamList[0].endTime": end_time,
                "quickTipToken": self.complete_token, # 在 quick 模式下這存的是 quickTipToken
                "action-name": "submit_form"
            }
            
            # 台鐵快速訂票通常會直接回傳結果
            resp = await self.client.post(url, data=payload)
            
            if "訂票成功" in resp.text or "成功代碼" in resp.text:
                logger.info("訪客訂票可能成功，請檢查日誌或手機。")
                return True
            
            if "error=true" in str(resp.url) or "請輸入正確" in resp.text:
                logger.error("訪客訂票失敗：參數錯誤或驗證碼攔截。")
            
            return False
        except Exception as e:
            logger.error(f"訪客訂票發生異常: {e}")
            return False

    async def query_trains(self, from_stn: str, to_stn: str, date: str, start_time: str, end_time: str) -> List[Dict[str, Any]]:
        """查詢班次。"""
        try:
            if not self.csrf_token:
                await self.init_session()

            url = f"{self.BASE_URL}/tip001/tip123/queryTrain"
            
            # 格式化車站 (代碼-名稱)
            from_full = f"{from_stn}-{TR_STATIONS.get(from_stn, '')}"
            to_full = f"{to_stn}-{TR_STATIONS.get(to_stn, '')}"
            
            payload = {
                "_csrf": self.csrf_token,
                "custIdTypeEnum": "PERSON_ID",
                "pid": "",
                "tripType": "ONEWAY",
                "orderType": "BY_TIME",
                "ticketOrderParamList[0].tripNo": "TRIP1",
                "ticketOrderParamList[0].startStation": from_full,
                "ticketOrderParamList[0].endStation": to_full,
                "ticketOrderParamList[0].rideDate": date.replace("-", "/"),
                "ticketOrderParamList[0].startOrEndTime": "true",
                "ticketOrderParamList[0].startTime": start_time,
                "ticketOrderParamList[0].endTime": end_time,
                "ticketOrderParamList[0].normalQty": "1",
                "completeToken": self.complete_token
            }

            response = await self.client.post(url, data=payload)
            if response.status_code == 200:
                return self._parse_schedules(response.text)
            return []
        except Exception as e:
            logger.error(f"查詢班次失敗: {str(e)}")
            return []

    def _parse_schedules(self, html: str) -> List[Dict[str, Any]]:
        """解析查詢結果 HTML。"""
        soup = BeautifulSoup(html, 'html.parser')
        schedules = []
        
        # 尋找班次表格列 (根據台鐵實際 HTML 結構，車次通常在 tr.trip-column 中)
        rows = soup.select('tr.trip-column')
        for row in rows:
            try:
                train_info = row.select_one('ul.train-number')
                if not train_info: continue
                
                train_no = train_info.text.strip().split('(')[0].strip()
                departure = row.select_one('td.departure-time').text.strip()
                arrival = row.select_one('td.arrival-time').text.strip()
                
                # 檢查是否有剩餘座位 (台鐵會標註 "無剩餘座位" 或顯示訂票按鈕)
                has_seats = "無剩餘座位" not in row.text
                
                schedules.append({
                    "train_no": train_no,
                    "departure": departure,
                    "arrival": arrival,
                    "has_seats": has_seats
                })
            except Exception as e:
                logger.warning(f"解析班次列失敗: {str(e)}")
                continue
            
        return schedules

    async def book_ticket(self, pid: str, train_no: str, from_stn: str, to_stn: str, date: str) -> bool:
        """
        基礎訂票 POST 邏輯。
        注意：實際訂票流程可能需要處理圖形驗證碼或 reCAPTCHA。
        """
        try:
            url = f"{self.BASE_URL}/tip001/tip123/bookingTicket"
            
            from_full = f"{from_stn}-{TR_STATIONS.get(from_stn, '')}"
            to_full = f"{to_stn}-{TR_STATIONS.get(to_stn, '')}"
            
            payload = {
                "_csrf": self.csrf_token,
                "pid": pid,
                "tripType": "ONEWAY",
                "ticketOrderParamList[0].tripNo": "TRIP1",
                "ticketOrderParamList[0].trainNo": train_no,
                "ticketOrderParamList[0].startStation": from_full,
                "ticketOrderParamList[0].endStation": to_full,
                "ticketOrderParamList[0].rideDate": date.replace("-", "/"),
                "ticketOrderParamList[0].normalQty": "1",
                "completeToken": self.complete_token
            }
            
            resp = await self.client.post(url, data=payload)
            if "訂票成功" in resp.text or resp.status_code == 302:
                logger.info(f"車次 {train_no} 訂票嘗試完成")
                return True
            return False
        except Exception as e:
            logger.error(f"訂票發生錯誤: {str(e)}")
            return False


    async def close(self):
        await self.client.aclose()
