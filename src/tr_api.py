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

            # 獲取 action-token (重要：v3 驗證)
            at = soup.find('input', {'name': 'action-token'})
            self.action_token = at['value'] if at else ""

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

    async def get_captcha(self) -> str:
        """下載並辨識台鐵驗證碼。"""
        try:
            # 加入 Referer 以免被阻擋
            url = f"{self.BASE_URL}/player/picture"
            headers = {"Referer": f"{self.BASE_URL}/tip001/tip121/query"}
            resp = await self.client.get(url, headers=headers)
            if resp.status_code == 200:
                captcha_text = self.ocr.classify(resp.content)
                logger.info(f"台鐵驗證碼辨識結果: {captcha_text}")
                return captcha_text
            return ""
        except Exception as e:
            logger.error(f"獲取驗證碼失敗: {e}")
            return ""

    async def guest_book_ticket(self, pid: str, from_stn: str, to_stn: str, date: str, start_time: str, end_time: str, num_tickets: int = 1) -> bool:
        """
        使用「快速訂票 (訪客模式)」直接訂票。
        """
        try:
            # 1. 初始化快速訂票 Session (獲取最新 CSRF 與 action-token)
            await self.init_session(mode="quick")
            
            # 設定擬真 Headers
            query_url = f"{self.BASE_URL}/tip001/tip121/query"
            self.client.headers.update({
                "Referer": query_url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "https://tip.railway.gov.tw"
            })
            
            # 2. 獲取驗證碼 (最多嘗試 3 次辨識出符合格式的 6 碼)
            captcha_text = ""
            for _ in range(3):
                c = await self.get_captcha()
                import re
                c = re.sub(r'[^a-zA-Z0-9]', '', c)
                if len(c) == 6:
                    captcha_text = c
                    break
            
            if not captcha_text:
                logger.error("無法獲取有效的 6 碼驗證碼，停止本次嘗試。")
                return False

            url = f"{self.BASE_URL}/tip001/tip121/bookingTicket"
            
            from_full = f"{from_stn}-{TR_STATIONS.get(from_stn, '')}"
            to_full = f"{to_stn}-{TR_STATIONS.get(to_stn, '')}"
            
            # 完整模擬官方 Payload (包含所有隱藏標記)
            payload = {
                "_csrf": self.csrf_token,
                "custIdTypeEnum": "PERSON_ID",
                "_custIdTypeEnum": "on",
                "pid": pid.upper(),
                "startStation": from_full,
                "endStation": to_full,
                "tripType": "ONEWAY",
                "orderType": "BY_TIME",
                "normalQty": str(num_tickets),
                "wheelChairQty": "0",
                "parentChildQty": "0",
                "ticketOrderParamList[0].tripNo": "TRIP1",
                "ticketOrderParamList[0].rideDate": date.replace("-", "/"),
                "ticketOrderParamList[0].startOrEndTime": "true",
                "ticketOrderParamList[0].startTime": start_time,
                "ticketOrderParamList[0].endTime": end_time,
                "ticketOrderParamList[0].chgSeat": "true",
                "_ticketOrderParamList[0].chgSeat": "on",
                "ticketOrderParamList[0].seatPref": "NONE",
                "ticketOrderParamList[0].trainTypeList": ["11", "1", "2", "3", "4", "5"],
                "_ticketOrderParamList[0].trainTypeList": ["on"] * 6,
                "g-recaptcha-response": captcha_text,
                "verifyType": "text",
                "isSecondVerify": "true",
                "quickTipToken": self.complete_token,
                "action-token": self.action_token if hasattr(self, 'action_token') else "",
                "action-name": "submit_form"
            }
            
            import asyncio, random
            await asyncio.sleep(random.uniform(0.8, 1.8))
            
            # 禁用自動重定向以精確診斷
            resp = await self.client.post(url, data=payload, follow_redirects=False)
            
            if "訂票成功" in resp.text or "成功代碼" in resp.text:
                logger.info("恭喜！台鐵訪客訂票成功！")
                return True
            
            if resp.status_code == 302:
                location = resp.headers.get("Location", "")
                if "query" not in location and ("tip121" in location or "tip123" in location):
                    logger.info(f"檢測到成功頁面跳轉: {location}")
                    return True
                
                # 解析失敗原因
                error_url = location if location.startswith("http") else f"https://tip.railway.gov.tw{location}"
                err_page = await self.client.get(error_url)
                err_soup = BeautifulSoup(err_page.text, 'html.parser')
                
                # 擷取錯誤文字
                errors = [s.text.strip() for s in err_soup.find_all('span', class_='error')]
                alerts = [d.text.strip() for div in err_soup.find_all('div', class_='alert') for d in [div] if "認明本公司" not in div.text]
                
                if errors:
                    msg = ", ".join(errors)
                    logger.error(f"訂票失敗，官方提示: {msg}")
                elif alerts:
                    msg = ", ".join(alerts)
                    logger.error(f"訂票失敗，系統警告: {msg}")
                else:
                    logger.error("訂票失敗，原因不明 (疑似驗證碼不符)，將自動進行下一次重試。")
            
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
