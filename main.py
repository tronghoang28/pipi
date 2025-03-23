
import asyncio
import logging
from telethon import TelegramClient
from flask import Flask
from threading import Thread

# Thông tin đăng nhập Telegram API
API_ID = 20976448
API_HASH = "9c550766b2ad538f194d27d0c30d8678"
PHONE_NUMBER = "84983286226"
CHAT_ID = -1002442620965

# Khoảng thời gian giữa các lần gửi tin nhắn
MESSAGE_INTERVAL = 12 * 60  # 12 phút

# Danh sách tin nhắn
MESSAGES = [
    "⚔️👺⚔️Phiên bản SpamCall đã chạy. Xác định đi mấy con lợn 🐷🐷🐷\nÉc éc éc !!!",
    "/vip 0367590613",
    "/vip 0339955467", 
    "/vip 0903727909",
    "/vip 0984328184",
    "/call 0367590613",
    "/call 0339955467",
    "/call 0903727909",
    "/call 0984328184",
]

# Cấu hình logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask server
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Telegram đang chạy"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    server = Thread(target=run)
    server.start()

async def send_message(client, message):
    """Gửi tin nhắn đến nhóm chỉ định"""
    retries = 3
    while retries > 0:
        try:
            await client.send_message(CHAT_ID, message)
            logger.info(f"Đã gửi tin nhắn: {message}")
            break
        except Exception as e:
            retries -= 1
            if retries == 0:
                logger.error(f"Lỗi khi gửi tin nhắn sau 3 lần thử: {e}")
            else:
                logger.warning(f"Lỗi khi gửi tin nhắn, thử lại lần {3-retries}: {e}")
                await asyncio.sleep(2)

async def scheduled_message(client):
    """Gửi tất cả tin nhắn mỗi 12 phút và xóa tin nhắn cũ"""
    while True:
        try:
            # Xóa tin nhắn cũ trước khi gửi tin mới
            logger.info("Đang xóa tin nhắn cũ...")
            async for message in client.iter_messages(CHAT_ID):
                await message.delete()
            logger.info("Đã xóa tin nhắn cũ")
            
            # Gửi tin nhắn mới
            logger.info("Bắt đầu gửi tất cả tin nhắn...")
            for message in MESSAGES:
                await send_message(client, message)
                await asyncio.sleep(2)
            logger.info(f"Đã gửi tất cả tin nhắn. Đợi {MESSAGE_INTERVAL//60} phút trước khi gửi lại...")
            await asyncio.sleep(MESSAGE_INTERVAL)
        except Exception as e:
            logger.error(f"Lỗi trong chu trình gửi/xóa tin nhắn: {e}")
            await asyncio.sleep(60)  # Đợi 1 phút trước khi thử lại nếu có lỗi

async def main():
    """Hàm chính của ứng dụng"""
    if not PHONE_NUMBER:
        logger.error("Vui lòng cung cấp TELEGRAM_PHONE_NUMBER trong biến môi trường")
        return

    client = TelegramClient('session_name', API_ID, API_HASH)
    await client.start(phone=PHONE_NUMBER)

    if not await client.is_user_authorized():
        logger.error("Phiên đăng nhập không hợp lệ. Vui lòng kiểm tra lại thông tin đăng nhập.")
        return

    logger.info("Đã đăng nhập thành công!")
    await scheduled_message(client)

if __name__ == "__main__":
    logger.info("Client Telegram đang khởi động...")
    keep_alive() # Khởi động web server
    asyncio.run(main())
