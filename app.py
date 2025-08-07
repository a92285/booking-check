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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import re
from urllib.parse import urlparse, parse_qs, unquote

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

# Selenium 設定 - 針對Railway雲端環境優化
def create_webdriver():
    """建立 Chrome WebDriver - 針對Railway雲端環境"""
    chrome_options = Options()
    
    # 雲端環境必需的參數
    chrome_options.add_argument('--headless=new')  # 使用新的headless模式
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--disable-plugins')
    chrome_options.add_argument('--disable-images')
    chrome_options.add_argument('--disable-javascript')  # 可以提高速度，但可能影響某些網站
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Linux; x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    chrome_options.add_argument('--remote-debugging-port=9222')
    chrome_options.add_argument('--disable-background-timer-throttling')
    chrome_options.add_argument('--disable-backgrounding-occluded-windows')
    chrome_options.add_argument('--disable-renderer-backgrounding')
    chrome_options.add_argument('--disable-features=TranslateUI')
    chrome_options.add_argument('--disable-web-security')
    chrome_options.add_argument('--allow-running-insecure-content')
    
    # 設置頁面載入策略
    chrome_options.page_load_strategy = 'eager'  # 不等待所有資源載入完成
    
    # Railway特定設定
    chrome_binary = os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_binary:
        chrome_options.binary_location = chrome_binary
        logger.info(f"使用Chrome binary: {chrome_binary}")
    
    # ChromeDriver路徑設定
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    try:
        if chromedriver_path and os.path.exists(chromedriver_path):
            logger.info(f"使用指定的ChromeDriver路徑: {chromedriver_path}")
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            # 嘗試使用系統中的chromedriver
            logger.info("嘗試使用系統ChromeDriver")
            driver = webdriver.Chrome(options=chrome_options)
        
        # 設置超時時間
        driver.set_page_load_timeout(30)
        driver.implicitly_wait(10)
        
        logger.info("WebDriver 創建成功")
        return driver
        
    except Exception as e:
        logger.error(f"無法建立 WebDriver: {e}")
        
        # 如果還是失敗，嘗試不使用Service
        try:
            logger.info("嘗試使用默認ChromeDriver設定")
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_page_load_timeout(30)
            driver.implicitly_wait(10)
            logger.info("使用默認設定成功創建WebDriver")
            return driver
        except Exception as e2:
            logger.error(f"使用默認設定也失敗: {e2}")
            return None

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

def resolve_short_url(short_url):
    """解析短網址，獲取真實網址"""
    try:
        # 設定 requests session
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Linux; x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        
        # 跟隨重定向但不下載內容
        response = session.head(short_url, allow_redirects=True, timeout=15)
        logger.info(f"短網址 {short_url} 解析為: {response.url}")
        return response.url
    except Exception as e:
        logger.error(f"無法解析短網址 {short_url}: {e}")
        return short_url

def get_hotel_info_from_url(url):
    """從網址獲取飯店資訊 - 簡化版本適合雲端環境"""
    driver = None
    try:
        # 如果是短網址，先解析
        if 'booking.com/Share-' in url or len(url) < 50:
            logger.info(f"解析短網址: {url}")
            full_url = resolve_short_url(url)
            logger.info(f"解析後網址: {full_url}")
        else:
            full_url = url
        
        # 嘗試從URL中提取飯店名稱（備用方案）
        hotel_name_from_url = "Booking.com 飯店"
        if 'booking.com' in full_url:
            # 嘗試從URL參數或路徑中提取信息
            parsed = urlparse(full_url)
            if '/hotel/' in parsed.path:
                path_parts = parsed.path.split('/hotel/')
                if len(path_parts) > 1:
                    hotel_part = path_parts[1].split('.')[0]
                    hotel_name_from_url = hotel_part.replace('-', ' ').title()[:50]
        
        driver = create_webdriver()
        if not driver:
            logger.warning("無法創建WebDriver，使用URL解析結果")
            return hotel_name_from_url, full_url
        
        logger.info(f"正在獲取飯店資訊: {full_url}")
        
        try:
            driver.get(full_url)
            
            # 減少等待時間，提高效率
            wait = WebDriverWait(driver, 10)
            time.sleep(3)  # 減少等待時間
            
            # 嘗試獲取飯店名稱
            hotel_name = hotel_name_from_url  # 默認值
            
            try:
                # 方法1: 尋找標題
                title_text = driver.title
                if title_text and len(title_text) > 5:
                    # 清理標題文本
                    cleaned_title = re.sub(r'\s*-.*$', '', title_text)
                    cleaned_title = re.sub(r'預訂.*', '', cleaned_title)
                    cleaned_title = cleaned_title.strip()
                    if len(cleaned_title) > 3:
                        hotel_name = cleaned_title[:50]
            except:
                pass
            
            try:
                # 方法2: 尋找 h1 標籤 (簡化選擇器)
                h1_elements = driver.find_elements(By.TAG_NAME, "h1")
                for h1 in h1_elements[:3]:  # 只檢查前3個h1元素
                    text = h1.text.strip()
                    if text and len(text) > 3 and len(text) < 100:
                        hotel_name = text[:50]
                        break
            except:
                pass
            
            logger.info(f"找到飯店名稱: {hotel_name}")
            return hotel_name, full_url
            
        except Exception as e:
            logger.error(f"獲取頁面內容時發生錯誤: {e}")
            return hotel_name_from_url, full_url
        
    except Exception as e:
        logger.error(f"獲取飯店資訊時發生錯誤: {e}")
        return "Booking.com 飯店", url
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

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
    """檢查飯店空房狀況 - 簡化版本適合雲端環境"""
    driver = None
    try:
        logger.info(f"開始檢查空房: {hotel_url}")
        
        # 如果是短網址，先解析
        if 'booking.com/Share-' in hotel_url or len(hotel_url) < 50:
            full_url = resolve_short_url(hotel_url)
        else:
            full_url = hotel_url
        
        driver = create_webdriver()
        if not driver:
            return False, "無法啟動瀏覽器"
        
        # 構建帶有日期和人數的搜尋 URL
        checkin_dt = datetime.strptime(checkin_date, '%Y-%m-%d')
        checkout_dt = datetime.strptime(checkout_date, '%Y-%m-%d')
        
        checkin_str = checkin_dt.strftime('%Y-%m-%d')
        checkout_str = checkout_dt.strftime('%Y-%m-%d')
        
        if '?' in full_url:
            search_url = f"{full_url}&checkin={checkin_str}&checkout={checkout_str}&group_adults={guests}"
        else:
            search_url = f"{full_url}?checkin={checkin_str}&checkout={checkout_str}&group_adults={guests}"
        
        logger.info(f"搜尋網址: {search_url}")
        
        try:
            driver.get(search_url)
            time.sleep(3)  # 減少等待時間
            
            # 簡化的可用性檢查
            availability_found = False
            availability_message = "目前無空房"
            
            # 檢查頁面標題和基本內容
            try:
                title = driver.title.lower()
                if any(word in title for word in ['available', 'book', 'reserve', '可預訂']):
                    availability_found = True
                    availability_message = "找到可預訂選項"
            except:
                pass
            
            # 簡化的元素檢查
            try:
                # 尋找預訂相關按鈕或連結
                book_elements = driver.find_elements(By.CSS_SELECTOR, 
                    "button, a, input[type='submit']")
                
                for element in book_elements[:10]:  # 只檢查前10個元素
                    try:
                        text = element.text.strip().lower()
                        if any(word in text for word in ['book', 'reserve', 'select', '預訂', '選擇', '立即']):
                            if element.is_displayed() and element.is_enabled():
                                availability_found = True
                                availability_message = "找到可預訂房間"
                                break
                    except:
                        continue
            except:
                pass
            
            # 檢查是否有錯誤頁面
            try:
                page_source = driver.page_source.lower()
                if any(word in page_source for word in ['no availability', 'sold out', '無空房', '已滿房']):
                    availability_found = False
                    availability_message = "確認無空房"
            except:
                pass
            
            nights = calculate_nights(checkin_date, checkout_date)
            final_message = f"{availability_message} ({nights}晚住宿)"
            
            logger.info(f"檢查結果: {'有空房' if availability_found else '無空房'} - {final_message}")
            
            return availability_found, final_message
            
        except Exception as e:
            logger.error(f"頁面處理錯誤: {e}")
            nights = calculate_nights(checkin_date, checkout_date)
            return False, f"檢查失敗: 頁面載入問題 ({nights}晚住宿)"
        
    except Exception as e:
        logger.error(f"檢查空房時發生錯誤: {e}")
        nights = calculate_nights(checkin_date, checkout_date)
        return False, f"檢查失敗: {str(e)} ({nights}晚住宿)"
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

@app.route("/", methods=['GET'])
def home():
    return "🏨 飯店空房查詢 LINE Bot 正在運行中..."

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
🏨 飯店空房查詢 LINE Bot 使用說明

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

⏰ 系統每30分鐘自動檢查空房，有空房時會立即通知您！

📋 支援的網站：
• Booking.com (包含短網址 Share-xxx)
• 其他主要訂房網站
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
                    
                    # 如果沒有飯店名稱，顯示 URL
                    if hotel_name and hotel_name != "未知飯店":
                        hotel_display = hotel_name
                    else:
                        hotel_display = "Booking.com 飯店"
                    
                    reply_message += f"{i}. 🏨 {hotel_display}\n"
                    reply_message += f"   📅 {checkin_date} ~ {checkout_date} ({nights}晚)\n"
                    reply_message += f"   👥 {guests}人 | 🛏️ {room_type}\n"
                    reply_message += f"   🔗 {hotel_url}\n\n"
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
            elif 'http' in message_text and ('booking.com' in message_text or 'hotel' in message_text or 'Share-' in message_text):
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
                
                # 發送飯店資訊和下一步指示 - 修正API調用
                follow_up_message = f"✅ 已收到飯店資訊：{hotel_name}\n\n📅 請輸入入住時間（格式：YYYY-MM-DD）\n例如：2024-12-25"
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    push_request = PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=follow_up_message)]
                    )
                    line_bot_api.push_message_with_http_info(push_request)
                return
            else:
                reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址 (需包含 http)，或輸入「說明」查看使用指南\n\n支援格式:\n• https://www.booking.com/Share-xxx\n• 其他訂房網站完整網址"
        
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
                    reply_message = f"✅ 已設定入住時間：{message_text}\n\n📅 請輸入退房時間（格式：YYYY-MM-DD）\n例如：2024-12-27"
            except ValueError:
                reply_message = "❌ 日期格式錯誤，請使用 YYYY-MM-DD 格式\n例如：2024-12-25"
        
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
                reply_message = "❌ 日期格式錯誤，請使用 YYYY-MM-DD 格式\n例如：2024-12-27"
        
        elif session.step == 3:
            # 接收人數
            try:
                guests = int(message_text)
                if guests > 0 and guests <= 10:
                    session.guests = guests
                    session.step = 4
                    reply_message = f"✅ 已設定人數：{guests} 人\n\n🛏️ 請輸入指定的房型名稱\n例如：標準雙人房、豪華套房"
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
                session.hotel_name or "未知飯店",
                session.checkin_date,
                session.checkout_date,
                session.guests,
                session.room_type
            )
            
            # 重置會話
            user_states[user_id] = BookingSession(user_id)
            
            reply_message = f"""
✅ 空房查詢設定完成！

🏨 飯店：{session.hotel_name or "未知飯店"}
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
                hotel_name = "未知飯店"
            
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

💬 {message}

🚀 請盡快前往預訂！
🔗 {hotel_url}
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
            
            # 在檢查之間稍作停頓，避免過於頻繁的請求
            time.sleep(15)  # 增加間隔時間，減輕服務器負擔
            
        except Exception as e:
            logger.error(f"檢查預訂 {booking_id if 'booking_id' in locals() else 'unknown'} 時發生錯誤: {e}")
            continue

def start_scheduler():
    """啟動定時檢查"""
    # 每30分鐘檢查一次
    schedule.every(30).minutes.do(check_all_bookings)
    
    # 測試用：每10分鐘檢查一次（上線前請改回30分鐘）
    # schedule.every(10).minutes.do(check_all_bookings)
    
    logger.info("定時檢查器已啟動 - 每30分鐘檢查一次空房")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

# 添加測試WebDriver的路由
@app.route("/test-webdriver", methods=['GET'])
def test_webdriver():
    """測試WebDriver是否正常工作"""
    try:
        driver = create_webdriver()
        if driver:
            driver.get("https://www.google.com")
            title = driver.title
            driver.quit()
            return f"✅ WebDriver 測試成功！頁面標題: {title}"
        else:
            return "❌ WebDriver 創建失敗"
    except Exception as e:
        return f"❌ WebDriver 測試失敗: {str(e)}"

if __name__ == "__main__":
    # 初始化資料庫
    init_db()
    print("✅ 資料庫初始化完成")
    
    # 測試 WebDriver 是否正常工作
    try:
        test_driver = create_webdriver()
        if test_driver:
            test_driver.quit()
            print("✅ WebDriver 測試成功")
        else:
            print("⚠️ WebDriver 初始化失敗，但程序將繼續運行")
    except Exception as e:
        print(f"⚠️ WebDriver 測試失敗: {e}")
        print("程序將繼續運行，但網頁爬取功能可能受限")
    
    # 在背景執行定時檢查
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ 背景檢查器已啟動")
    
    # 啟動 Flask 應用
    print("🚀 啟動 Flask 應用...")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)