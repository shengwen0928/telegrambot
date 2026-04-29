import ddddocr

class OCREngine:
    """和欣客運驗證碼辨識引擎。"""
    
    def __init__(self):
        """初始化 ddddocr 實例。"""
        # show_ad=False 避免在終端印出廣告訊息
        self.ocr = ddddocr.DdddOcr(show_ad=False)
    
    def classify(self, image_bytes: bytes) -> str:
        """
        將圖片二進位數據轉換為文字。
        
        Args:
            image_bytes: 驗證碼圖片的 bytes 數據。
            
        Returns:
            辨識出的字串（通常為 4 位）。
        """
        return self.ocr.classification(image_bytes)
