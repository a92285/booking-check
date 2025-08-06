import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

app = Flask(__name__)

# LINE Bot 設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("❌ 請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")
    print("請檢查 .env 文件是否正確設置")
    exit(1)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

print("✅ LINE Bot 設定完成")
print(f"✅ Token 前6碼: {LINE_CHANNEL_ACCESS_TOKEN[:6]}...")

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
        print("❌ Invalid signature")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    message_text = event.message.text
    print(f"收到訊息: {message_text}")
    
    # 簡單的回覆測試
    if message_text.lower() in ["測試", "test", "hello", "hi"]:
        reply_text = "🎉 LINE Bot 運作正常！\n\n可用指令:\n• 測試 - 測試連線\n• 開始 - 開始設定空房查詢"
    elif message_text.lower() in ["開始", "start"]:
        reply_text = "🏨 歡迎使用飯店空房查詢服務！\n\n請輸入飯店預訂網址 (例如 Booking.com 的飯店頁面)："
    else:
        reply_text = f"收到您的訊息：{message_text}\n\n輸入「測試」檢查系統狀態\n輸入「開始」設定空房查詢"
    
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    print("🚀 啟動 Flask 應用...")
    app.run(debug=True, host='0.0.0.0', port=5000)