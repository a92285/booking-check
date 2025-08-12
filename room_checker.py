import requests
from bs4 import BeautifulSoup
from urllib.parse import urlencode
import re

class RoomChecker:
    def __init__(self):
        self.base_url = "https://go-landabout.reservation.jp/ja/plans/10153436"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
    
    def build_url(self, checkin_date, checkout_date, adults=2, room_id="10011842"):
        """
        建立查詢網址
        checkin_date, checkout_date: 格式 "YYYY-MM-DD"
        adults: 成人數量
        room_id: 房間ID
        """
        # 將日期轉換為 YYYYMMDD 格式
        checkin_formatted = checkin_date.replace('-', '')
        checkout_formatted = checkout_date.replace('-', '')
        
        params = {
            'sort': 1,
            'room_id': room_id,
            'checkin_date': checkin_formatted,
            'checkout_date': checkout_formatted,
            'adults': adults,
            'child1': 0,
            'child2': 0,
            'child3': 0,
            'child4': 0,
            'child5': 0,
            'children': 0,
            'rooms': 1,
            'dayuseFlg': 0,
            'dateUndecidedFlg': 0
        }
        
        return f"{self.base_url}?{urlencode(params)}"
    
    def check_availability(self, url):
        """檢查房間是否有空"""
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # 檢查是否有預約按鈕
            reservation_button = soup.select_one('a.c-button-reservation')
            
            return reservation_button is not None
                
        except Exception as e:
            print(f"查詢失敗: {e}")
            return False
    
    def check_room_by_dates(self, checkin_date, checkout_date, adults=2):
        """根據入住和退房日期檢查房間"""
        url = self.build_url(checkin_date, checkout_date, adults)
        available = self.check_availability(url)
        
        return {
            'available': available,
            'url': url,
            'checkin': checkin_date,
            'checkout': checkout_date,
            'adults': adults
        }

if __name__ == "__main__":
    checker = RoomChecker()
    result = checker.check_room_by_dates("2025-10-10", "2025-10-15", 2)
    print(f"查詢結果: {result}")