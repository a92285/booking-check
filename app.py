from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import os
import re
import threading
import time
from datetime import datetime
from room_checker import RoomChecker

app = Flask(__name__)

# LINE Bot 設定
configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
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
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=message)]
                )
            )
        print(f"通知已發送給用戶: {user_id}")
    except Exception as e:
        print(f"發送通知失敗: {e}")

def process_room_query_background(user_id, checkin_date, checkout_date, adults):
    """背景處理房間查詢"""
    try:
        print(f"背景處理用戶 {user_id} 查詢房間：{checkin_date} 到 {checkout_date}，{adults}人")
        result = room_checker.check_room_by_dates(checkin_date, checkout_date, adults)
        
        if result['available']:
            # 已經有空房，立即通知
            print(f"發現空房！立即通知用戶 {user_id}")
            reply_text = f"""🎉 好消息！房間現在就有空！

📅 入住日期：{checkin_date}
📅 退房日期：{checkout_date}
👥 入住人數：{adults}人

🔗 立即預訂：
{result['url']}

✨ 趕快下訂吧！"""
            
            # 用 push_message 主動推送
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
        else:
            # 沒有空房，開始監控
            print(f"目前沒有空房，為用戶 {user_id} 開始監控")
            monitoring_tasks[user_id] = {
                'checkin': checkin_date,
                'checkout': checkout_date,
                'adults': adults,
                'active': True
            }
            
            reply_text = f"""❌ 目前沒有空房，但別擔心！

📅 入住日期：{checkin_date}
📅 退房日期：{checkout_date}
👥 入住人數：{adults}人

🔍 已開始自動監控
⏰ 每30分鐘檢查一次
📱 一有空房就立即通知您

輸入「狀態」查看監控狀態
輸入「停止」取消監控"""
            
            # 用 push_message 主動推送
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
                
    except Exception as e:
        print(f"背景處理房間查詢時發生錯誤: {e}")
        # 如果查詢失敗，也要通知用戶
        error_text = f"查詢房間時發生錯誤：{str(e)}\n請稍後再試或聯繫客服"
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=error_text)]
                    )
                )
        except:
            print(f"發送錯誤通知失敗")

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
    app.logger.info("Request body: " + body)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error handling webhook: {e}")
        abort(500)
    
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    try:
        user_id = event.source.user_id
        user_message = event.message.text.strip()
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            
            # 第一步：立即回覆「收到訊息，開始處理」
            quick_reply = "✅ 收到訊息，開始處理中..."
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=quick_reply)]
                )
            )
            
            # 第二步：根據不同指令在背景處理
            if user_message.lower() in ['說明', 'help', '幫助']:
                def send_help():
                    help_text = """📖 使用說明

輸入格式：
入住日期 退房日期 人數

範例：
2025-12-25 2025-12-27 2

其他指令：
• 狀態 - 查看監控狀態
• 停止 - 停止監控
• 說明 - 查看此說明"""
                    
                    try:
                        with ApiClient(configuration) as api_client:
                            line_bot_api = MessagingApi(api_client)
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=[TextMessage(text=help_text)]
                                )
                            )
                    except Exception as e:
                        print(f"發送說明失敗: {e}")
                
                # 在背景執行
                threading.Thread(target=send_help, daemon=True).start()
                return
            
            if user_message == '狀態':
                def send_status():
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
                    
                    try:
                        with ApiClient(configuration) as api_client:
                            line_bot_api = MessagingApi(api_client)
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=[TextMessage(text=status_text)]
                                )
                            )
                    except Exception as e:
                        print(f"發送狀態失敗: {e}")
                
                # 在背景執行
                threading.Thread(target=send_status, daemon=True).start()
                return
            
            if user_message == '停止':
                def stop_monitoring():
                    if user_id in monitoring_tasks:
                        monitoring_tasks[user_id]['active'] = False
                        reply_text = "✅ 監控已停止"
                    else:
                        reply_text = "目前沒有進行中的監控任務"
                    
                    try:
                        with ApiClient(configuration) as api_client:
                            line_bot_api = MessagingApi(api_client)
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=[TextMessage(text=reply_text)]
                                )
                            )
                    except Exception as e:
                        print(f"發送停止通知失敗: {e}")
                
                # 在背景執行
                threading.Thread(target=stop_monitoring, daemon=True).start()
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
                
                # 在背景執行房間查詢
                query_thread = threading.Thread(
                    target=process_room_query_background, 
                    args=(user_id, checkin_date, checkout_date, adults),
                    daemon=True
                )
                query_thread.start()
                
            except ValueError as e:
                def send_error():
                    error_text = f"""❌ 輸入格式錯誤

正確格式：
入住日期 退房日期 人數

範例：
2025-12-25 2025-12-27 2

錯誤原因：{str(e)}
輸入「說明」查看詳細使用方法"""
                    
                    try:
                        with ApiClient(configuration) as api_client:
                            line_bot_api = MessagingApi(api_client)
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=[TextMessage(text=error_text)]
                                )
                            )
                    except Exception as e:
                        print(f"發送錯誤訊息失敗: {e}")
                
                # 在背景執行
                threading.Thread(target=send_error, daemon=True).start()
            
            except Exception as e:
                print(f"處理訊息時發生錯誤: {e}")
                def send_general_error():
                    try:
                        with ApiClient(configuration) as api_client:
                            line_bot_api = MessagingApi(api_client)
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=[TextMessage(text=f"發生錯誤：{str(e)}")]
                                )
                            )
                    except Exception as e:
                        print(f"發送錯誤訊息失敗: {e}")
                
                # 在背景執行
                threading.Thread(target=send_general_error, daemon=True).start()
    
    except Exception as e:
        print(f"handle_message 發生錯誤: {e}")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)