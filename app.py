import os
import time
import schedule
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import sqlite3
import threading
import logging
from dotenv import load_dotenv

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

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
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
            checkin_date TEXT NOT NULL,
            checkout_date TEXT NOT NULL,
            guests INTEGER NOT NULL,
            room_type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# 用戶狀態管理
user_states = {}

class BookingSession:
    def __init__(self, user_id):
        self.user_id = user_id
        self.step = 0  # 0: 等待URL, 1: 等待入住時間, 2: 等待退房時間, 3: 等待人數, 4: 等待房型
        self.hotel_url = None
        self.checkin_date = None
        self.checkout_date = None
        self.guests = None
        self.room_type = None

def save_booking(user_id, hotel_url, checkin_date, checkout_date, guests, room_type):
    """儲存預訂查詢到資料庫"""
    conn = sqlite3.connect('hotel_bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO bookings (user_id, hotel_url, checkin_date, checkout_date, guests, room_type)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, hotel_url, checkin_date, checkout_date, guests, room_type))
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

def get_hotel_name_from_url(url):
    """從網址中提取飯店名稱或簡化顯示"""
    try:
        # 嘗試從 URL 中提取有用的部分
        if 'booking.com' in url:
            # 從 booking.com URL 中提取飯店名稱
            parts = url.split('/')
            for part in parts:
                if 'hotel' in part and len(part) > 5:
                    # 替換連字符為空格，首字母大寫
                    hotel_name = part.replace('-', ' ').title()
                    return f"Booking.com - {hotel_name[:20]}"
            # 如果沒找到hotel部分，就顯示booking.com
            return "Booking.com"
        
        # 如果是其他網站或無法解析，顯示域名
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.hostname or 'Unknown'
        
        # 移除 www. 前綴
        if domain.startswith('www.'):
            domain = domain[4:]
            
        return f"{domain[:25]}"
        
    except:
        # 如果解析失敗，返回截斷的 URL
        return url[:35] + "..." if len(url) > 35 else url
    """計算住宿天數"""
    try:
        checkin = datetime.strptime(checkin_date, '%Y-%m-%d')
        checkout = datetime.strptime(checkout_date, '%Y-%m-%d')
        nights = (checkout - checkin).days
        return nights
    except:
        return 0

# 簡化的空房檢查 (模擬功能)
def check_hotel_availability(hotel_url, checkin_date, checkout_date, guests, room_type):
    """
    簡化版的空房檢查
    在實際應用中，這裡會使用 Selenium 爬蟲或 API 來檢查真實的空房狀況
    現在返回模擬結果
    """
    try:
        # 模擬檢查過程
        import random
        time.sleep(2)  # 模擬網路請求時間
        
        nights = calculate_nights(checkin_date, checkout_date)
        
        # 30% 機率有空房 (用於測試)
        has_availability = random.random() < 0.3
        
        if has_availability:
            return True, f"找到空房：{room_type} 可預訂！({nights}晚住宿)"
        else:
            return False, f"目前無空房 ({nights}晚住宿)"
            
    except Exception as e:
        logger.error(f"檢查空房時發生錯誤: {e}")
        return False, f"檢查失敗: {str(e)}"

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_id = event.source.user_id
        message_text = event.message.text
        
        logger.info(f"收到訊息: {message_text} from {user_id}")
        
        # 處理系統指令
        if message_text.lower() in ['取消', 'cancel', '重新開始', 'reset']:
            user_states[user_id] = BookingSession(user_id)
            reply_message = "✅ 已重新開始。\n\n🏨 飯店空房查詢服務\n\n請輸入飯店預訂網址："
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
            return
            
        elif message_text.lower() in ['幫助', 'help', '說明']:
            reply_message = """
🏨 飯店空房查詢 LINE Bot 使用說明

📝 設定查詢：
1️⃣ 輸入飯店預訂網址
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
            """
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
            return
            
        elif message_text.lower() in ['查看', 'list', '我的查詢']:
            bookings = get_user_bookings(user_id)
            if not bookings:
                reply_message = "📋 您目前沒有進行中的空房監控。\n\n輸入「開始」來設定新的查詢！"
            else:
                reply_message = "📋 您目前的空房監控：\n\n"
                for i, booking in enumerate(bookings, 1):
                    booking_id, _, hotel_url, checkin_date, checkout_date, guests, room_type, _, created_at = booking
                    nights = calculate_nights(checkin_date, checkout_date)
                    hotel_display = get_hotel_name_from_url(hotel_url)
                    reply_message += f"{i}. 🏨 {hotel_display}\n"
                    reply_message += f"   📅 {checkin_date} ~ {checkout_date} ({nights}晚)\n"
                    reply_message += f"   👥 {guests}人 | 🛏️ {room_type}\n\n"
                reply_message += "輸入「開始」設定新的查詢"
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
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
                reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址 (例如 Booking.com 的飯店頁面)："
            elif 'http' in message_text:
                session.hotel_url = message_text
                session.step = 1
                reply_message = "✅ 已收到飯店網址！\n\n📅 請輸入入住時間（格式：YYYY-MM-DD）\n例如：2024-12-25"
            else:
                reply_message = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址 (需包含 http)，或輸入「說明」查看使用指南"
        
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
                session.checkin_date,
                session.checkout_date,
                session.guests,
                session.room_type
            )
            
            # 重置會話
            user_states[user_id] = BookingSession(user_id)
            
            reply_message = f"""
✅ 空房查詢設定完成！

🏨 飯店：{get_hotel_name_from_url(session.hotel_url)}
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
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
        
    except Exception as e:
        logger.error(f"處理訊息時發生錯誤: {e}")
        try:
            error_message = "❌ 處理訊息時發生錯誤，請輸入「重新開始」重新設定"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_message))
        except:
            pass

def check_all_bookings():
    """檢查所有活躍的預訂查詢"""
    bookings = get_active_bookings()
    logger.info(f"檢查 {len(bookings)} 個預訂查詢")
    
    for booking in bookings:
        booking_id, user_id, hotel_url, checkin_date, checkout_date, guests, room_type, is_active, created_at = booking
        
        nights = calculate_nights(checkin_date, checkout_date)
        logger.info(f"檢查預訂 {booking_id}: {room_type} ({nights}晚)")
        
        # 檢查空房
        available, message = check_hotel_availability(hotel_url, checkin_date, checkout_date, guests, room_type)
        
        if available:
            # 發送通知
            notification_message = f"""
🎉 好消息！找到空房了！

🏨 飯店：{get_hotel_name_from_url(hotel_url)}
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
                line_bot_api.push_message(user_id, TextSendMessage(text=notification_message))
                
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
        time.sleep(5)

def start_scheduler():
    """啟動定時檢查"""
    # 每30分鐘檢查一次
    schedule.every(30).minutes.do(check_all_bookings)
    
    # 也可以設定每小時檢查（測試時可改為每分鐘）
    # schedule.every().hour.do(check_all_bookings)
    # schedule.every().minute.do(check_all_bookings)  # 測試用
    
    logger.info("定時檢查器已啟動 - 每30分鐘檢查一次空房")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    # 初始化資料庫
    init_db()
    print("✅ 資料庫初始化完成")
    
    # 在背景執行定時檢查
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ 背景檢查器已啟動")
    
    # 啟動 Flask 應用
    print("🚀 啟動 Flask 應用...")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)