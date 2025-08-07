import os
import time
import schedule
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)
import sqlite3
import threading
import logging
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, parse_qs, unquote
import urllib.parse

# 設置日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 載入環境變數
load_dotenv()

# LINE Bot 設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("❌ 請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")
    exit(1)

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = Flask(__name__)

# 資料庫初始化
def init_db():
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            hotel_url TEXT NOT NULL,
            hotel_name TEXT,
            checkin_date TEXT NOT NULL,
            checkout_date TEXT NOT NULL,
            guests INTEGER NOT NULL,
            room_type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 為現有表格添加 hotel_name 欄位（如果不存在）
    cursor.execute("PRAGMA table_info(bookings)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'hotel_name' not in columns:
        cursor.execute('ALTER TABLE bookings ADD COLUMN hotel_name TEXT')
    
    conn.commit()
    conn.close()

# 用戶狀態管理
user_states = {}

class BookingSession:
    def __init__(self, user_id):
        self.user_id = user_id
        self.step = 0  # 0: 等待URL, 1: 等待入住時間, 2: 等待退房時間, 3: 等待人數, 4: 等待房型
        self.hotel_url = None
        self.hotel_name = None
        self.checkin_date = None
        self.checkout_date = None
        self.guests = None
        self.room_type = None

def create_session():
    """創建HTTP會話，設置適當的headers"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'DNT': '1'
    })
    return session

def resolve_short_url(short_url):
    """解析短網址，獲取真實網址"""
    try:
        session = create_session()
        # 跟隨重定向但不下載內容
        response = session.head(short_url, allow_redirects=True, timeout=15)
        logger.info(f"短網址 {short_url} 解析為: {response.url}")
        return response.url
    except Exception as e:
        logger.error(f"無法解析短網址 {short_url}: {e}")
        return short_url

def get_hotel_info_from_url(url):
    """使用 requests + BeautifulSoup 獲取飯店資訊"""
    try:
        # 如果是短網址，先解析
        if 'booking.com/Share-' in url or len(url) < 50:
            logger.info(f"解析短網址: {url}")
            full_url = resolve_short_url(url)
            logger.info(f"解析後網址: {full_url}")
        else:
            full_url = url
        
        # 從URL嘗試提取基本信息作為備用
        hotel_name_from_url = "Booking.com 飯店"
        if 'booking.com' in full_url:
            parsed = urlparse(full_url)
            if '/hotel/' in parsed.path:
                path_parts = parsed.path.split('/hotel/')
                if len(path_parts) > 1:
                    hotel_part = path_parts[1].split('.')[0].split('/')[0]
                    hotel_name_from_url = hotel_part.replace('-', ' ').replace('_', ' ').title()[:50]
        
        logger.info(f"正在獲取飯店資訊: {full_url}")
        
        # 使用 requests 獲取頁面
        session = create_session()
        
        # 添加延遲避免被檢測為機器人
        time.sleep(2)
        
        # 添加 cookies 和 referer
        session.cookies.update({
            'bkng': '1',
            'bkng_stt': '1'
        })
        
        response = session.get(full_url, timeout=15)
        response.raise_for_status()
        
        # 檢查是否被重定向到錯誤頁面
        if 'javascript' in response.text.lower() and 'disabled' in response.text.lower():
            logger.warning("檢測到 JavaScript 錯誤頁面，重試中...")
            # 重試一次，使用不同的 headers
            time.sleep(3)
            session.headers.update({
                'Referer': 'https://www.booking.com/',
                'Origin': 'https://www.booking.com'
            })
            response = session.get(full_url, timeout=15)
        
        # 使用 BeautifulSoup 解析 HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 嘗試獲取飯店名稱
        hotel_name = hotel_name_from_url
        
        # 方法1: 從 title 標籤獲取
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text().strip()
            if title_text and len(title_text) > 5:
                # 清理標題文本，移除 Booking.com 相關後綴
                cleaned_title = re.sub(r'\s*[-–|]\s*(Booking\.com|預訂|Book|Reserve).*$', '', title_text, flags=re.IGNORECASE)
                cleaned_title = re.sub(r'預訂.*', '', cleaned_title)
                cleaned_title = cleaned_title.strip()
                if len(cleaned_title) > 3 and len(cleaned_title) < 80:
                    hotel_name = cleaned_title[:60]
        
        # 方法2: 從各種可能的選擇器獲取飯店名稱
        if hotel_name == hotel_name_from_url:
            selectors = [
                'h1[data-testid="title"]',
                'h1.pp-header__title',
                'h1#hp_hotel_name',
                '[data-testid="title"]',
                '.hp__hotel-name',
                '.property-name',
                '.hotel-name',
                'h1[class*="title"]',
                'h1[class*="name"]',
                'h1[class*="hotel"]',
                'h1'
            ]
            
            for selector in selectors:
                try:
                    element = soup.select_one(selector)
                    if element:
                        text = element.get_text().strip()
                        # 過濾掉太短或太長的文本
                        if text and 3 < len(text) < 100:
                            # 清理文本
                            cleaned_text = re.sub(r'^\s*\d+[\.\s]*', '', text)  # 移除開頭數字
                            cleaned_text = re.sub(r'\s+', ' ', cleaned_text)    # 標準化空白
                            if len(cleaned_text) > 3:
                                hotel_name = cleaned_text[:60]
                                break
                except Exception:
                    continue
        
        # 方法3: 從 Open Graph 或 meta 標籤獲取
        if hotel_name == hotel_name_from_url:
            meta_selectors = [
                'meta[property="og:title"]',
                'meta[name="title"]',
                'meta[property="og:site_name"]'
            ]
            
            for selector in meta_selectors:
                try:
                    element = soup.select_one(selector)
                    if element and element.get('content'):
                        text = element.get('content').strip()
                        if 3 < len(text) < 100:
                            cleaned_text = re.sub(r'\s*[-–|]\s*(Booking\.com).*$', '', text, flags=re.IGNORECASE)
                            if len(cleaned_text) > 3:
                                hotel_name = cleaned_text[:60]
                                break
                except Exception:
                    continue
        
        # 最終清理飯店名稱
        if hotel_name and hotel_name != hotel_name_from_url:
            # 移除常見的後綴
            hotel_name = re.sub(r'\s*[-–|]\s*(Book|Reserve|預訂|立即預訂).*$', '', hotel_name, flags=re.IGNORECASE)
            hotel_name = hotel_name.strip()
        
        logger.info(f"找到飯店名稱: {hotel_name}")
        return hotel_name, full_url
        
    except Exception as e:
        logger.error(f"獲取飯店資訊時發生錯誤: {e}")
        # 返回從URL解析的基本信息
        if 'booking.com' in url:
            return "Booking.com 飯店", url
        else:
            domain = urlparse(url).netloc.replace('www.', '')
            return f"{domain} 飯店", url

def save_booking(user_id, hotel_url, hotel_name, checkin_date, checkout_date, guests, room_type):
    """儲存預訂查詢到資料庫"""
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO bookings (user_id, hotel_url, hotel_name, checkin_date, checkout_date, guests, room_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, hotel_url, hotel_name, checkin_date, checkout_date, guests, room_type))
    conn.commit()
    conn.close()

def get_active_bookings():
    """取得所有活躍的預訂查詢"""
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM bookings WHERE is_active = 1')
    bookings = cursor.fetchall()
    conn.close()
    return bookings

def get_user_bookings(user_id):
    """取得特定用戶的預訂查詢"""
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM bookings WHERE user_id = ? AND is_active = 1', (user_id,))
    bookings = cursor.fetchall()
    conn.close()
    return bookings

def cancel_user_booking(user_id, booking_id):
    """取消用戶的預訂查詢"""
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE bookings SET is_active = 0 WHERE id = ? AND user_id = ?', (booking_id, user_id))
    rows_affected = cursor.rowcount
    conn.commit()
    conn.close()
    return rows_affected > 0

def calculate_nights(checkin_date, checkout_date):
    """計算住宿天數"""
    try:
        checkin = datetime.strptime(checkin_date, '%Y-%m-%d')
        checkout = datetime.strptime(checkout_date, '%Y-%m-%d')
        nights = (checkout - checkin).days
        return nights
    except:
        return 0

def check_hotel_availability(hotel_url, checkin_date, checkout_date, guests, room_type):
    """使用 requests + BeautifulSoup 檢查飯店空房狀況"""
    try:
        logger.info(f"開始檢查空房: {hotel_url}")
        
        # 如果是短網址，先解析
        if 'booking.com/Share-' in hotel_url or len(hotel_url) < 50:
            full_url = resolve_short_url(hotel_url)
        else:
            full_url = hotel_url
        
        # 構建帶有日期和人數的搜尋 URL
        checkin_dt = datetime.strptime(checkin_date, '%Y-%m-%d')
        checkout_dt = datetime.strptime(checkout_date, '%Y-%m-%d')
        
        checkin_str = checkin_dt.strftime('%Y-%m-%d')
        checkout_str = checkout_dt.strftime('%Y-%m-%d')
        
        # 構建查詢URL
        if '?' in full_url:
            search_url = f"{full_url}&checkin={checkin_str}&checkout={checkout_str}&group_adults={guests}"
        else:
            search_url = f"{full_url}?checkin={checkin_str}&checkout={checkout_str}&group_adults={guests}"
        
        logger.info(f"搜尋網址: {search_url}")
        
        # 使用 requests 獲取頁面
        session = create_session()
        
        # 添加 Referer 以提高成功率
        session.headers.update({
            'Referer': 'https://www.booking.com/'
        })
        
        # 添加延遲
        time.sleep(3)
        
        response = session.get(search_url, timeout=20)
        response.raise_for_status()
        
        # 使用 BeautifulSoup 解析 HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 獲取頁面文字內容
        page_text = soup.get_text().lower()
        
        # 添加詳細日志用於調試
        page_title = soup.title.get_text() if soup.title else '無標題'
        logger.info(f"頁面標題: {page_title}")
        logger.info(f"頁面長度: {len(page_text)} 字符")
        logger.info(f"頁面前150字符: {page_text[:150]}")
        
        # 檢查頁面是否正常載入
        if len(page_text) < 100:
            logger.warning("頁面內容過短，可能載入失敗")
            nights = calculate_nights(checkin_date, checkout_date)
            return False, f"頁面載入異常 ({nights}晚住宿)"
        
        # 檢查可用性
        availability_found = False
        availability_message = "目前無空房"
        
        # 檢查明確的可用性指標 - 擴充版本
        positive_indicators = [
            'book now', 'reserve now', 'available', 'select room', 'choose room',
            'book this room', 'reserve this room', 'check availability',
            '立即預訂', '現在預訂', '預訂', '可預訂', '選擇房間', '查看房間',
            '預訂此房間', '立即預約', '馬上預訂', '可供預訂',
            'availability', 'rooms left', 'rooms available', 'in stock',
            'select', 'choose', 'reserve', 'confirm', 'proceed',
            '有房', '剩餘', '可選', '確認', '繼續', '房間可訂',
            'see availability', 'view rooms', 'show prices',
            '查看價格', '顯示價格', '房價'
        ]
        
        negative_indicators = [
            'no availability', 'sold out', 'no rooms available', 'fully booked',
            'not available', 'no longer available',
            '無空房', '已滿房', '暫無空房', '無可用房間', '已售完',
            '無法預訂', '暫時無法預訂', '客滿'
        ]
        
        # 檢查找到的關鍵詞
        positive_words_found = []
        negative_words_found = []
        
        for indicator in positive_indicators:
            if indicator in page_text:
                positive_words_found.append(indicator)
        
        for indicator in negative_indicators:
            if indicator in page_text:
                negative_words_found.append(indicator)
        
        logger.info(f"找到正面關鍵詞: {positive_words_found}")
        logger.info(f"找到負面關鍵詞: {negative_words_found}")
        
        # 首先檢查負面指標
        if negative_words_found:
            availability_found = False
            availability_message = f"確認無空房 (找到: {', '.join(negative_words_found[:2])})"
        # 檢查正面指標
        elif positive_words_found:
            availability_found = True
            availability_message = f"找到可預訂選項 (找到: {', '.join(positive_words_found[:2])})"
        
        # 檢查預訂按鈕或連結
        if not availability_found and not negative_words_found:
            booking_selectors = [
                'a[href*="book"]',
                'button[data-testid*="book"]',
                'button[class*="book"]',
                '.availability_form_button',
                '[data-testid="availability-cta-btn"]',
                'input[value*="預訂"]',
                'button[class*="reserve"]'
            ]
            
            button_found = False
            for selector in booking_selectors:
                elements = soup.select(selector)
                for element in elements:
                    # 檢查元素文本
                    text = element.get_text().strip().lower()
                    if any(word in text for word in ['book', 'reserve', 'select', '預訂', '選擇']):
                        availability_found = True
                        availability_message = "找到預訂按鈕"
                        button_found = True
                        break
                if button_found:
                    break
        
        # 檢查價格信息（通常表示有房間可訂）
        if not availability_found and not negative_words_found:
            price_selectors = [
                '[class*="price"]',
                '[data-testid*="price"]',
                '.bui-price-display__value',
                '.priceview',
                '[class*="rate"]'
            ]
            
            price_found = False
            for selector in price_selectors:
                elements = soup.select(selector)
                for element in elements:
                    text = element.get_text().strip()
                    # 檢查是否包含價格數字和貨幣符號
                    if re.search(r'\d+', text) and any(currency in text for currency in ['$', '€', '£', '¥', 'NT', 'TWD', ',', '.']):
                        # 排除一些明顯不是價格的數字
                        if not re.search(r'(評分|評價|review|rating|km|公里)', text, re.IGNORECASE):
                            availability_found = True
                            availability_message = f"找到房間價格: {text[:30]}..."
                            price_found = True
                            break
                if price_found:
                    break
        
        # 檢查房間選擇區域
        if not availability_found and not negative_words_found:
            room_selectors = [
                '.hprt-table',
                '.roomstable',
                '[data-testid="availability-table"]',
                '.room-table',
                '.availability-table'
            ]
            
            for selector in room_selectors:
                room_table = soup.select_one(selector)
                if room_table:
                    table_text = room_table.get_text().lower()
                    if any(word in table_text for word in ['book', 'select', 'available', '預訂', '選擇', '可用']):
                        availability_found = True
                        availability_message = "在房間表格中找到可預訂選項"
                        break
        
        # 如果仍然無法確定，檢查頁面標題和基本結構
        if not availability_found and not negative_words_found and availability_message == "目前無空房":
            title = soup.title.get_text().lower() if soup.title else ""
            
            # 檢查頁面是否正常載入（不是錯誤頁面）
            if any(word in title for word in ['booking', 'hotel', '飯店']) and not any(word in title for word in ['error', '錯誤', '404']):
                # 如果頁面正常載入但沒有明確指標，改為保守的正面回應
                if len(page_text) > 1000:  # 頁面內容充足
                    availability_found = True  # 改為 True，避免漏報
                    availability_message = "頁面正常載入，建議手動確認空房狀況"
                else:
                    availability_message = "頁面載入不完整，無法確定空房狀況"
            elif any(error in page_text for error in ['error', '錯誤', '404', 'not found']):
                availability_message = "頁面載入錯誤，無法檢查空房"
        
        nights = calculate_nights(checkin_date, checkout_date)
        final_message = f"{availability_message} ({nights}晚住宿)"
        
        logger.info(f"檢查結果: {'有空房' if availability_found else '無空房'} - {final_message}")
        
        return availability_found, final_message
        
    except requests.exceptions.RequestException as e:
        logger.error(f"網路請求錯誤: {e}")
        nights = calculate_nights(checkin_date, checkout_date)
        return False, f"網路連線錯誤: {str(e)} ({nights}晚住宿)"
    except Exception as e:
        logger.error(f"檢查空房時發生錯誤: {e}")
        nights = calculate_nights(checkin_date, checkout_date)
        return False, f"檢查失敗: {str(e)} ({nights}晚住宿)"

@app.route("/", methods=['GET'])
def home():
    return "🏨 飯店空房查詢 LINE Bot 正在運行中... (優化版本 v2.0)"

@app.route("/test-connection", methods=['GET'])
def test_connection():
    """測試網路連線和解析功能"""
    try:
        # 測試基本網路連線
        test_url = "https://www.booking.com"
        session = create_session()
        response = session.get(test_url, timeout=10)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            title = soup.title.get_text() if soup.title else "無標題"
            return f"✅ 網路連線正常！頁面標題: {title[:50]}..."
        else:
            return f"⚠️ 網路連線異常，狀態碼: {response.status_code}"
    except Exception as e:
        return f"❌ 連線測試失敗: {str(e)}"

@app.route("/test-hotel", methods=['GET'])
def test_hotel():
    """測試飯店爬取功能"""
    test_url = request.args.get('url', 'https://www.booking.com/Share-eOW41e')
    try:
        hotel_name, full_url = get_hotel_info_from_url(test_url)
        return f"✅ 飯店名稱: {hotel_name}<br>🔗 完整網址: {full_url}"
    except Exception as e:
        return f"❌ 測試失敗: {str(e)}"

@app.route("/test-availability", methods=['GET'])
def test_availability():
    """測試空房檢查功能"""
    test_url = request.args.get('url', 'https://www.booking.com/Share-1NHUep')
    checkin = request.args.get('checkin', '2025-10-10')
    checkout = request.args.get('checkout', '2025-10-15')
    guests = int(request.args.get('guests', '2'))
    room_type = request.args.get('room_type', '豪華雙床間')
    
    try:
        available, message = check_hotel_availability(test_url, checkin, checkout, guests, room_type)
        return f"🔍 檢查結果: {'✅ 有空房' if available else '❌ 無空房'}<br>📝 詳細: {message}<br><br>🔗 測試網址: {test_url}"
    except Exception as e:
        return f"❌ 測試失敗: {str(e)}"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        user_id = event.source.user_id
        message_text = event.message.text
        
        logger.info(f"收到訊息: {message_text} from {user_id}")
        
        # 處理測試指令
        if message_text.startswith('測試 '):
            test_url = message_text[3:]  # 移除「測試 」前綴
            try:
                available, message = check_hotel_availability(
                    test_url, "2025-10-10", "2025-10-15", 2, "測試房型"
                )
                reply_message = f"🔍 測試結果:\n{'✅ 有空房' if available else '❌ 無空房'}\n📝 {message}"
            except Exception as e:
                reply_message = f"❌ 測試失敗: {str(e)}"
            """
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_message)]
                    )
                )
            return
            
        elif message_text.lower() in ['查看', 'list', '我的查詢']:
            bookings = get_user_bookings(user_id)
            if not bookings:
                reply_message = "📋 您目前沒有進行中的空房監控。\n\n輸入「開始」來設定新的查詢！"
            else:
                reply_message = "📋 您目前的空房監控：\n\n"
                for i, booking in enumerate(bookings, 1):
                    # 處理新舊資料庫格式
                    if len(booking) >= 9:  # 新格式包含 hotel_name
                        booking_id, _, hotel_url, hotel_name, checkin_date, checkout_date, guests, room_type, _, created_at = booking
                    else:  # 舊格式不包含 hotel_name
                        booking_id, _, hotel_url, checkin_date, checkout_date, guests, room_type, _, created_at = booking
                        hotel_name = None
                    
                    nights = calculate_nights(checkin_date, checkout_date)
                    
                    # 如果沒有飯店名稱，顯示簡化版本
                    if hotel_name and hotel_name != "未知飯店":
                        hotel_display = hotel_name
                    else:
                        hotel_display = "訂房網站飯店"
                    
                    reply_message += f"{i}. 🏨 {hotel_display}\n"
                    reply_message += f"   📅 {checkin_date} ~ {checkout_date} ({nights}晚)\n"
                    reply_message += f"   👥 {guests}人 | 🛏️ {room_type}\n\n"
                reply_message += "💡 輸入「開始」設定新的查詢"
            
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_message)]
                    )
                )
            return

        # 初始化用戶狀態
        if user_id not in user_states:
            user_states[user_id] = BookingSession(user_id)
        
        session = user_states[user_id]
        logger.info(f"用戶 {user_id} 目前在步驟 {session.step}")
        
        # 處理對話流程
        if session.step == 0:
            # 處理開始指令或直接輸入URL
            if message_text.lower() in ["開始", "start"]:
                session.step = 0
                reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址\n支援 Booking.com 短網址 (例如: https://www.booking.com/Share-eOW41e)："
            elif 'http' in message_text and ('booking.com' in message_text or 'hotel' in message_text or 'Share-' in message_text or any(site in message_text for site in ['agoda', 'hotels', 'expedia'])):
                session.hotel_url = message_text
                
                # 在背景獲取飯店資訊
                reply_message = "🔍 正在獲取飯店資訊，請稍候..."
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=reply_message)]
                        )
                    )
                
                # 獲取飯店資訊
                hotel_name, full_url = get_hotel_info_from_url(message_text)
                session.hotel_name = hotel_name
                session.hotel_url = full_url  # 更新為完整 URL
                session.step = 1
                
                # 發送飯店資訊和下一步指示
                follow_up_message = f"✅ 已收到飯店資訊：{hotel_name}\n\n📅 請輸入入住時間（格式：YYYY-MM-DD）\n例如：2025-08-15"
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    push_request = PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=follow_up_message)]
                    )
                    line_bot_api.push_message_with_http_info(push_request)
                return
            else:
                reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址 (需包含 http)，或輸入「說明」查看使用指南\n\n支援格式:\n• https://www.booking.com/Share-xxx\n• 其他訂房網站完整網址\n\n💡 快速測試：輸入「測試 [網址]」"
        
        elif session.step == 1:
            # 接收入住時間
            try:
                # 檢查日期格式
                check_date = datetime.strptime(message_text, '%Y-%m-%d')
                # 檢查日期不能是過去
                if check_date.date() < datetime.now().date():
                    reply_message = "⚠️ 入住日期不能是過去的日期，請重新輸入："
                else:
                    session.checkin_date = message_text
                    session.step = 2
                    reply_message = f"✅ 已設定入住時間：{message_text}\n\n📅 請輸入退房時間（格式：YYYY-MM-DD）\n例如：2025-08-17"
            except ValueError:
                reply_message = "❌ 日期格式錯誤，請使用 YYYY-MM-DD 格式\n例如：2025-08-15"
        
        elif session.step == 2:
            # 接收退房時間
            try:
                # 檢查日期格式
                checkout_date = datetime.strptime(message_text, '%Y-%m-%d')
                checkin_date = datetime.strptime(session.checkin_date, '%Y-%m-%d')
                
                # 檢查退房日期必須晚於入住日期
                if checkout_date <= checkin_date:
                    reply_message = "⚠️ 退房日期必須晚於入住日期，請重新輸入："
                else:
                    session.checkout_date = message_text
                    nights = (checkout_date - checkin_date).days
                    session.step = 3
                    reply_message = f"✅ 已設定退房時間：{message_text}\n📊 住宿天數：{nights} 晚\n\n👥 請輸入住宿人數："
            except ValueError:
                reply_message = "❌ 日期格式錯誤，請使用 YYYY-MM-DD 格式\n例如：2025-08-17"
        
        elif session.step == 3:
            # 接收人數
            try:
                guests = int(message_text)
                if guests > 0 and guests <= 10:
                    session.guests = guests
                    session.step = 4
                    reply_message = f"✅ 已設定人數：{guests} 人\n\n🛏️ 請輸入指定的房型名稱\n例如：標準雙人房、豪華套房、任何房型"
                else:
                    reply_message = "⚠️ 人數請輸入 1-10 之間的數字："
            except ValueError:
                reply_message = "❌ 請輸入有效的數字（1-10）："
        
        elif session.step == 4:
            # 接收房型名稱並完成設定
            session.room_type = message_text
            
            # 計算住宿天數
            nights = calculate_nights(session.checkin_date, session.checkout_date)
            
            # 儲存到資料庫
            save_booking(
                user_id,
                session.hotel_url,
                session.hotel_name or "訂房網站飯店",
                session.checkin_date,
                session.checkout_date,
                session.guests,
                session.room_type
            )
            
            # 重置會話
            user_states[user_id] = BookingSession(user_id)
            
            reply_message = f"""
✅ 空房查詢設定完成！

🏨 飯店：{session.hotel_name or "訂房網站飯店"}
📅 入住時間：{session.checkin_date}
📅 退房時間：{session.checkout_date}
🌙 住宿天數：{nights} 晚
👥 住宿人數：{session.guests} 人
🛏️ 房型：{session.room_type}

⏰ 系統將每30分鐘檢查一次空房狀況
🔔 有空房時會立即通知您！

💡 其他指令：
• 查看 - 查看所有監控項目
• 開始 - 設定新的查詢
• 說明 - 使用說明
• 測試 [網址] - 快速測試

🌟 優化版本 v2.0 - 提高檢測準確度
            """
        
        else:
            # 未知狀態，重置
            user_states[user_id] = BookingSession(user_id)
            reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址，或輸入「說明」查看使用指南"
        
        # 發送回覆訊息
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_message)]
                )
            )
        
    except Exception as e:
        logger.error(f"處理訊息時發生錯誤: {e}")
        try:
            error_message = "❌ 處理訊息時發生錯誤，請輸入「重新開始」重新設定"
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=error_message)]
                    )
                )
        except:
            pass

def check_all_bookings():
    """檢查所有活躍的預訂查詢"""
    bookings = get_active_bookings()
    logger.info(f"檢查 {len(bookings)} 個預訂查詢")
    
    for booking in bookings:
        try:
            # 處理新舊資料庫格式
            if len(booking) >= 10:  # 新格式包含 hotel_name
                booking_id, user_id, hotel_url, hotel_name, checkin_date, checkout_date, guests, room_type, is_active, created_at = booking
            else:  # 舊格式不包含 hotel_name
                booking_id, user_id, hotel_url, checkin_date, checkout_date, guests, room_type, is_active, created_at = booking
                hotel_name = "訂房網站飯店"
            
            nights = calculate_nights(checkin_date, checkout_date)
            logger.info(f"檢查預訂 {booking_id}: {hotel_name} - {room_type} ({nights}晚)")
            
            # 檢查空房
            available, message = check_hotel_availability(hotel_url, checkin_date, checkout_date, guests, room_type)
            
            if available:
                # 發送通知
                notification_message = f"""
🎉 好消息！找到空房了！

🏨 飯店：{hotel_name}
📅 入住時間：{checkin_date}
📅 退房時間：{checkout_date}
🌙 住宿天數：{nights} 晚
👥 人數：{guests} 人
🛏️房型：{room_type}

💬 檢查結果：{message}

🚀 請盡快前往預訂！
🔗 {hotel_url}

⏰ 檢查時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                """
                
                try:
                    with ApiClient(configuration) as api_client:
                        line_bot_api = MessagingApi(api_client)
                        push_request = PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text=notification_message)]
                        )
                        line_bot_api.push_message_with_http_info(push_request)
                    
                    # 將此預訂標記為非活躍（已通知）
                    conn = sqlite3.connect('hotel_bookings.db')
                    cursor = conn.cursor()
                    cursor.execute('UPDATE bookings SET is_active = 0 WHERE id = ?', (booking_id,))
                    conn.commit()
                    conn.close()
                    
                    logger.info(f"已發送空房通知給用戶 {user_id}")
                    
                except Exception as e:
                    logger.error(f"發送通知失敗: {e}")
            else:
                # 記錄檢查結果但不發送通知
                logger.info(f"預訂 {booking_id} 檢查結果: {message}")
            
            # 在檢查之間稍作停頓，避免過於頻繁的請求
            time.sleep(20)  # 增加間隔時間
            
        except Exception as e:
            logger.error(f"檢查預訂 {booking_id if 'booking_id' in locals() else 'unknown'} 時發生錯誤: {e}")
            continue

def start_scheduler():
    """啟動定時檢查"""
    # 每30分鐘檢查一次
    schedule.every(30).minutes.do(check_all_bookings)
    
    # 測試用：每10分鐘檢查一次（測試完成後請改回30分鐘）
    # schedule.every(10).minutes.do(check_all_bookings)
    
    logger.info("定時檢查器已啟動 - 每30分鐘檢查一次空房")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    # 初始化資料庫
    init_db()
    print("✅ 資料庫初始化完成")
    
    # 測試網路連線
    try:
        session = create_session()
        response = session.get("https://www.google.com", timeout=10)
        if response.status_code == 200:
            print("✅ 網路連線測試成功")
        else:
            print(f"⚠️ 網路連線狀況: {response.status_code}")
    except Exception as e:
        print(f"⚠️ 網路連線測試失敗: {e}")
        print("程序將繼續運行，但網路功能可能受限")
    
    # 在背景執行定時檢查
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ 背景檢查器已啟動 (優化版本)")
    
    # 啟動 Flask 應用
    print("🚀 啟動 Flask 應用... (優化版本 v2.0)")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
        
        # 處理系統指令
        if message_text.lower() in ['取消', 'cancel', '重新開始', 'reset']:
            user_states[user_id] = BookingSession(user_id)
            reply_message = "✅ 已重新開始。\n\n🏨 飯店空房查詢服務\n\n請輸入飯店預訂網址："
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_message)]
                    )
                )
            return
            
        elif message_text.lower() in ['幫助', 'help', '說明']:
            reply_message = """
🏨 飯店空房查詢 LINE Bot 使用說明 (v2.0)

📝 設定查詢：
1️⃣ 輸入飯店預訂網址 (支援 Booking.com 短網址)
2️⃣ 輸入入住日期 (YYYY-MM-DD)
3️⃣ 輸入退房日期 (YYYY-MM-DD)
4️⃣ 輸入住宿人數
5️⃣ 輸入房型名稱

🔧 指令：
• 開始 - 開始新的查詢設定
• 查看 - 查看目前的監控項目
• 取消 - 重新開始設定
• 說明 - 顯示此說明
• 測試 [網址] - 快速測試空房檢查

⏰ 系統每30分鐘自動檢查空房，有空房時會立即通知您！

📋 支援的網站：
• Booking.com (包含短網址 Share-xxx)
• 其他主要訂房網站

💡 本版本使用優化的網頁解析技術，提高檢測準確度