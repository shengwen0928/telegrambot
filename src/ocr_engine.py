import ddddocr
import cv2
import numpy as np
from PIL import Image
import io

class OCREngine:
    """進階驗證碼辨識引擎。"""
    
    def __init__(self):
        """初始化 ddddocr 實例。"""
        self.ocr = ddddocr.DdddOcr(show_ad=False)
    
    def _preprocess(self, image_bytes: bytes) -> bytes:
        """使用 OpenCV 進行影像預處理，去除干擾線。"""
        try:
            # 轉換為 OpenCV 格式
            nparray = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparray, cv2.IMREAD_COLOR)
            
            # 1. 轉灰階
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # 2. 增加對比度
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            contrast = clahe.apply(gray)
            
            # 3. 自適應二值化 (去除背景干擾線的核心)
            thresh = cv2.adaptiveThreshold(contrast, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2)
            
            # 4. 去除小噪點 (降噪)
            kernel = np.ones((2, 2), np.uint8)
            opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
            
            # 反轉回白底黑字 (ddddocr 較擅長)
            final_img = cv2.bitwise_not(opening)
            
            # 轉回 bytes
            _, buffer = cv2.imencode('.png', final_img)
            return buffer.tobytes()
        except Exception:
            return image_bytes

    def classify(self, image_bytes: bytes) -> str:
        """
        將圖片二進位數據轉換為文字（含自動預處理）。
        
        Args:
            image_bytes: 驗證碼圖片的 bytes 數據。
            
        Returns:
            辨識出的字串。
        """
        # 1. 嘗試預處理後的圖片
        processed = self._preprocess(image_bytes)
        result = self.ocr.classification(processed)
        
        # 2. 如果結果明顯長度不對，嘗試原始圖片
        if len(result) < 5:
            result_raw = self.ocr.classification(image_bytes)
            if len(result_raw) >= len(result):
                result = result_raw
                
        return result
