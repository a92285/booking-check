from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
import re
import threading
import time
from datetime import datetime
from room_checker import RoomChecker

app = Flask(__name__)

# LINE Bot 設定
line_bot_api = LineBotApi(os.getenv('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# 房間檢查器
room_checker = RoomChecker()

# 監控任務列表 {user_id: {'checkin': date, 'checkout': date, 'adults': int, 'active': bool}}
monitoring_tasks = {}

def send_notification(user_id, checkin, checkout, adults, url):
    """發送通知給用戶"""
    message = f"""🎉 好消息！房間有空了！

📅 入住日期：{checkin}
📅 退房日期：{checkout}
👥 入住人數：{adults}人

🔗 立即預訂：
{url}

監控已自動停止。"""
    
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=message))
        print(f"通知已發送給用戶: {user_id}")
    except Exception as e:
        print(f"發送通知失敗: {e}")

def monitor_rooms():
    """背景監控任務"""
    while True:
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{current_time}] 開始檢查所有監控任務...")
            
            for user_id, task in list(monitoring_tasks.items()):
                if not task.get('active', False):
                    continue
                
                print(f"檢查用戶 {user_id} 的房間...")
                result = room_checker.check_room_by_dates(
                    task['checkin'], 
                    task['checkout'], 
                    task['adults']
                )
                
                if result['available']:
                    print(f"找到空房！通知用戶 {user_id}")
                    send_notification(
                        user_id, 
                        task['checkin'], 
                        task['checkout'], 
                        task['adults'],
                        result['url']
                    )
                    # 停止該用戶的監控
                    monitoring_tasks[user_id]['active'] = False
                else:
                    print(f"用戶 {user_id} 的房間仍無空房")
            
            print(f"[{current_time}] 檢查完成，30分鐘後再次檢查")
            
        except Exception as e:
            print(f"監控過程發生錯誤: {e}")
        
        # 等待30分鐘
        time.sleep(1800)  # 30分鐘 = 1800秒

# 啟動背景監控線程
monitoring_thread = threading.Thread(target=monitor_rooms, daemon=True)
monitoring_thread.start()

@app.route("/", methods=['GET'])
def home():
    return "房間監控 LINE Bot 正在運行中！"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    
    if user_message.lower() in ['說明', 'help', '幫助']:
        help_text = """📖 使用說明

輸入格式：
入住日期 退房日期 人數

範例：
2025-12-25 2025-12-27 2

其他指令：
• 狀態 - 查看監控狀態
• 停止 - 停止監控
• 說明 - 查看此說明"""
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=help_text)
        )
        return
    
    if user_message == '狀態':
        if user_id in monitoring_tasks and monitoring_tasks[user_id].get('active'):
            task = monitoring_tasks[user_id]
            status_text = f"""📊 監控狀態：運行中

📅 入住日期：{task['checkin']}
📅 退房日期：{task['checkout']}
👥 入住人數：{task['adults']}人

⏰ 每30分鐘檢查一次
💡 輸入「停止」可取消監控"""
        else:
            status_text = "目前沒有進行中的監控任務"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=status_text)
        )
        return
    
    if user_message == '停止':
        if user_id in monitoring_tasks:
            monitoring_tasks[user_id]['active'] = False
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="✅ 監控已停止")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="目前沒有進行中的監控任務")
            )
        return
    
    # 解析監控指令：入住日期 退房日期 人數
    try:
        parts = user_message.split()
        if len(parts) < 2:
            raise ValueError("格式不正確")
        
        checkin_date = parts[0]
        checkout_date = parts[1]
        adults = int(parts[2]) if len(parts) > 2 else 2
        
        # 驗證日期格式 YYYY-MM-DD
        date_pattern = r'^\d{4}-\d{2}-\d{2}$'
        if not re.match(date_pattern, checkin_date) or not re.match(date_pattern, checkout_date):
            raise ValueError("日期格式必須是 YYYY-MM-DD")
        
        if adults < 1 or adults > 10:
            raise ValueError("人數必須在1-10之間")
        
        # 先檢查一次當前狀態
        result = room_checker.check_room_by_dates(checkin_date, checkout_date, adults)
        
        if result['available']:
            # 已經有空房，直接通知
            reply_text = f"""🎉 好消息！房間現在就有空！

📅 入住日期：{checkin_date}
📅 退房日期：{checkout_date}
👥 入住人數：{adults}人

🔗 立即預訂：
{result['url']}"""
        else:
            # 沒有空房，開始監控
            monitoring_tasks[user_id] = {
                'checkin': checkin_date,
                'checkout': checkout_date,
                'adults': adults,
                'active': True
            }
            
            reply_text = f"""🔍 開始監控房間狀態

📅 入住日期：{checkin_date}
📅 退房日期：{checkout_date}
👥 入住人數：{adults}人

⏰ 每30分鐘檢查一次
📱 有空房時會立即通知您

輸入「狀態」查看監控狀態
輸入「停止」取消監控"""
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        
    except ValueError as e:
        error_text = f"""❌ 輸入格式錯誤

正確格式：
入住日期 退房日期 人數

範例：
2025-12-25 2025-12-27 2

錯誤原因：{str(e)}
輸入「說明」查看詳細使用方法"""
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=error_text)
        )
    
    except Exception as e:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"發生錯誤：{str(e)}")
        )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
