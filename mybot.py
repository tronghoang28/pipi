import re
import asyncio
import logging
from datetime import datetime
from functools import wraps
import pytz
import aiosqlite
from typing import Dict, Any, Callable, Awaitable, Union

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage

# Giả định import từ module db_utils
from db_utils import init_db, them_admin_user, process_phone_numbers, Users, Phones, DB_PATH

# Cấu hình logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Hằng số
TOKEN = "7429154754:AAFS2W0yN7962i8VTbQumokVipnpzuW4HFw"
MAX_PHONE_NUMBERS = 5
COOLDOWN_SECONDS = 90
COOLDOWN_WHITELIST = ["/auto", "/list", "/delso"]

# Hàm escape markdown
def escape_markdown_v2(text):
    if text is None:
        return ""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


# Middleware trong aiogram 3.x
class UserDataMiddleware:
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        data["users"] = Users()
        data["phones"] = Phones()
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id
            data["is_vip"] = await data["users"].is_vip(user_id)
        return await handler(event, data)


# Filter cooldown
class CooldownFilter:
    def __init__(self, seconds=COOLDOWN_SECONDS, whitelist=None):
        self.seconds = seconds
        self.whitelist = whitelist or []

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return True

        command = message.text.split()[0].lower()
        if command in self.whitelist:
            return True

        user_id = message.from_user.id
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT last_used FROM cooldowns WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            now = datetime.now()
            if row:
                last = datetime.fromisoformat(row[0])
                if (now - last).total_seconds() < self.seconds:
                    remain = int(self.seconds - (now - last).total_seconds())
                    await message.reply(f"⏳ Vui lòng chờ {remain}s rồi dùng lại")
                    return False

            await db.execute(
                "REPLACE INTO cooldowns (user_id, last_used) VALUES (?, ?)", 
                (user_id, now.isoformat())
            )
            await db.commit()
        return True


# Filter nhóm thay cho decorator cũ
class GroupOnlyFilter:
    async def __call__(self, message: Message, data: Dict[str, Any]) -> bool:
        user_id = message.from_user.id
        chat_type = message.chat.type
        users = data["users"]  # Lấy instance Users từ middleware

        # Nếu là admin thì cho phép
        if await users.is_admin(user_id):
            return True

        # Nếu là private thì từ chối
        if chat_type == "private":
            kb = InlineKeyboardBuilder()
            kb.button(text="Vào nhóm", url="https://t.me/spamcallsmsooo")
            await message.answer(
                "Lệnh này chỉ dùng trong nhóm: https://t.me/spamcallsmsooo",
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return False

        return True


# Filter VIP
class VipFilter:
    async def __call__(self, message: Message, data: Dict[str, Any]) -> bool:
        if not data.get("is_vip", False):
            kb = InlineKeyboardBuilder()
            kb.button(text="Liên hệ Admin", url="https://t.me/hoangtrong288")
            await message.answer(
                escape_markdown_v2("> 🤡➤Bạn không có quyền VIP\n> Để sử dụng chức năng này.←📌\n> Ấn nút bên dưới để xin quyền hoặc mua VIP."),
                reply_markup=kb.as_markup(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return False
        return True


async def send_quote(message: Message, text: str, reply_markup=None) -> Message:
    """Gửi tin nhắn với định dạng Markdown V2 dạng quote"""
    # Fix 4: Use escape_markdown_v2 instead
    try:
        escaped_text = escape_markdown_v2(text)
        return await message.answer(
            escaped_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
    except TelegramBadRequest as e:
        logger.error(f"Error sending message: {e}")
        # Fallback nếu có lỗi định dạng
        return await message.answer(
            text,
            reply_markup=reply_markup
        )


# Định nghĩa các bộ lọc
cooldown = CooldownFilter(seconds=COOLDOWN_SECONDS, whitelist=COOLDOWN_WHITELIST)
group_only = GroupOnlyFilter()
vip_only = VipFilter()


router = Router(name="main_router")
router.message.middleware(UserDataMiddleware())

@router.message(Command("admin"))
async def admin_handler(message: Message, data: Dict[str, Any]):
    user_id = message.from_user.id
    
    # Kiểm tra xem người dùng có phải admin không
    is_admin = await data["users"].is_admin(user_id)
    
    if is_admin:
        args = message.text.strip().split()[1:]
        if len(args) >= 2 and args[0] == "add":
            try:
                new_admin_id = int(args[1])
                # Lấy thông tin user nếu được đề cập
                name = args[2] if len(args) > 2 else f"Admin_{new_admin_id}"
                
                # Thêm admin mới
                await them_admin_user(new_admin_id, name)
                
                return await message.answer(
                    escape_markdown_v2(f"✅ Đã thêm admin mới: {name} (ID: {new_admin_id})"),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except ValueError:
                return await message.answer(
                    escape_markdown_v2("❌ ID không hợp lệ. Cú pháp: /admin add [user_id] [tên]"),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        else:
            return await message.answer(
                escape_markdown_v2("✅ Bạn là admin. Cú pháp thêm admin: /admin add [user_id] [tên]"),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    else:
        return await message.answer(
            escape_markdown_v2("❌ Bạn không phải là admin."),
            parse_mode=ParseMode.MARKDOWN_V2
        )


@router.message(Command("auto"), group_only, vip_only, cooldown)
async def auto_handler(message: Message, data: Dict[str, Any]):
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    args = message.text.strip().split()[1:]

    if not args:
        return await send_quote(
            message,
            "> Vui lòng nhập số điện thoại\n> Ví dụ: /auto 0989xxxxxx"
        )

    # Kiểm tra giới hạn lưu
    count = await data["phones"].get_phone_count(user_id)
    if count >= MAX_PHONE_NUMBERS:
        return await send_quote(
            message,
            f"🤡 Bạn chỉ được lưu tối đa {MAX_PHONE_NUMBERS} số."
        )

    # Xử lý danh sách số điện thoại
    added, dup, inv = await process_phone_numbers(data["phones"], user_id, args)

    # Chuẩn bị thời gian Việt Nam
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.now(tz_vn)
    time_str = now.strftime("%H\\:%M\\:%S \\- %d/%m/%Y")
    today_str = now.strftime("%d/%m/%Y")

    # Escape MarkdownV2
    full_name_escaped = escape_markdown_v2(full_name)
    added_str = ' \\| '.join([escape_markdown_v2(x) for x in added]) if added else 'Không có'
    dup_str = ', '.join([escape_markdown_v2(x) for x in dup]) if dup else 'Không có'
    inv_str = ', '.join([escape_markdown_v2(x) for x in inv]) if inv else 'Không có'

    # MarkdownV2 quote message
    msg = fr"""
> ╭━━『⭓ Triển Khai ⭓』━━╮
> ┃»User  : {full_name_escaped}
> ┃»ID    : {user_id}
> ╰━━━━━━━⇣
> ╭─────────────: ̗̀➛
> │»Đã Thêm: {len(added)}
> │\\+ {added_str}
> │
> │»Trùng lặp: {dup_str}
> │»Sai số : {inv_str}
> │
> │»Time   : {time_str}
> │»Today  : {today_str}
> ╰─────────────────────────✦
"""

    await message.answer(msg.strip(), parse_mode=ParseMode.MARKDOWN_V2)


@router.message(Command("list"), group_only, vip_only, cooldown)
async def list_handler(message: Message, data: Dict[str, Any]):
    user = message.from_user
    user_id = user.id
    full_name = user.full_name or "Người dùng"
    
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.now(tz_vn)
    time_str = now.strftime("%H:%M:%S - %d/%m/%Y")
    today_str = now.strftime("%d/%m/%Y")

    # Lấy danh sách số kèm trạng thái
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT phone, status, last_processed FROM user_numbers 
            WHERE user_id = ? ORDER BY last_processed DESC
        """, (user_id,))
        rows = await cur.fetchall()
    
    if not rows:
        return await message.answer(
            escape_markdown_v2("🤡➤Bạn chưa thêm số điện thoại nào\n vào danh sách của mình!←📌"),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    # Format phản hồi - escape cho Markdown V2
    phone_lines = []
    for i, (phone, status, last_proc) in enumerate(rows, 1):
        status_emoji = "🟢" if status == "success" else "🟡" if status == "pending" else "🔴"
        processed_time = "Chưa xử lý" if not last_proc else datetime.fromisoformat(last_proc).strftime("%H:%M:%S %d/%m")
        phone_line = f"{i}\\. {escape_markdown_v2(phone)} {status_emoji} {escape_markdown_v2(processed_time)}"
        phone_lines.append(phone_line)
    
    formatted_numbers = "\n> │» ".join(phone_lines)
    full_name_escaped = escape_markdown_v2(full_name)

    msg = fr"""
> ╭━━『⭓ Danh Sách ⭓』━━╮
> ┃»User  : {full_name_escaped}
> ┃»ID    : {user_id}
> ╰━━━━━━━⇣
> ╭─────────────: ̗̀➛
> │»Tổng số: {len(rows)}
> │» {formatted_numbers}
> │»
> │»Time   : {time_str}
> │»Today  : {today_str}
> ╰─────────────────────────✦
"""

    return await message.answer(msg, parse_mode=ParseMode.MARKDOWN_V2)


@router.message(Command("delso"), group_only, vip_only, cooldown)
async def delso_handler(message: Message, data: Dict[str, Any]):
    user = message.from_user
    user_id = user.id
    full_name = escape_markdown_v2(user.full_name or user.username or "Không tên")

    # Lấy thời gian VN
    now = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh"))
    time_str = now.strftime("%H:%M:%S - %d/%m/%Y")
    today_str = now.strftime("%d/%m/%Y")

    args = message.text.split()[1:]
    if not args:
        return await message.answer(
            escape_markdown_v2("> 🤡➤ Cách dùng: /delso số thứ tự hoặc delso all"),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    user_list = await data["phones"].get_phones(user_id)
    if not user_list:
        return await message.answer(
            escape_markdown_v2("> 🤡➤ Danh sách của bạn đang trống"),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    arg = args[0].lower()

    if arg == "all":
        removed = list(user_list)
        for phone in removed:
            await data["phones"].delete_phone(user_id, phone)

        msg = fr"""
> ╭━━『⭓ Xoá Toàn Bộ Số ⭓』━━╮
> ┃»User  : {full_name}
> ┃»ID    : {user_id}
> ╰━━━━━━━⇣
> ╭─────────────: ̗̀➛
> │»Đã xoá toàn bộ *{len(removed)}* số
> │»Time   : {time_str}
> │»Today  : {today_str}
> ╰─────────────────────────✦
""".strip()
    else:
        try:
            idx = int(arg) - 1
            if not (0 <= idx < len(user_list)):
                raise IndexError
            removed = [user_list[idx]]
            await data["phones"].delete_phone(user_id, removed[0])
            removed_escaped = escape_markdown_v2(removed[0])

            msg = fr"""
> ╭━━『⭓ Xoá Số Thứ {idx+1} ⭓』━━╮
> ┃»User  : {full_name}
> ┃»ID    : {user_id}
> ╰━━━━━━━⇣
> ╭─────────────: ̗̀➛
> │»Đã xoá: *{removed_escaped}*
> │»Time  : {time_str}
> │»Today : {today_str}
> ╰─────────────────────────✦
""".strip()
        except (ValueError, IndexError):
            return await message.answer(
                escape_markdown_v2("> 🤡➤ Số thứ tự không hợp lệ\n> Vui lòng dùng /delso all hoặc /delso <số_thứ_tự>"),
                parse_mode=ParseMode.MARKDOWN_V2
            )

    await message.answer(msg, parse_mode=ParseMode.MARKDOWN_V2)


@router.message(Command("admin_startus"))
async def admin_status(message: Message):
    user_id = message.from_user.id
    
    async with aiosqlite.connect('/root/bibibot/bot_data.db') as db:
        # Lấy danh sách tất cả các bảng
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table';") as cursor:
            tables = await cursor.fetchall()
        
        found = False
        for table in tables:
            table_name = table['name']
            
            # Lấy thông tin về cấu trúc bảng
            async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                columns = await cursor.fetchall()
            
            # Tìm các cột có thể chứa user ID
            id_columns = []
            for col in columns:
                col_name = col[1]  # Tên cột thường ở vị trí thứ 2
                if 'id' in col_name.lower() or 'user' in col_name.lower():
                    id_columns.append(col_name)
            
            # Kiểm tra từng cột
            for col_name in id_columns:
                try:
                    query = f"SELECT * FROM {table_name} WHERE {col_name} = ?"
                    async with db.execute(query, (user_id,)) as cursor:
                        result = await cursor.fetchone()
                        
                    if result:
                        found = True
                        await message.answer(f"Tìm thấy ID của bạn trong bảng {table_name}, cột {col_name}")
                        # Chuyển result thành dictionary để dễ đọc
                        col_names = [description[0] for description in cursor.description]
                        result_dict = {col_names[i]: result[i] for i in range(len(col_names))}
                        await message.answer(f"Dữ liệu: {result_dict}")
                except Exception as e:
                    continue  # Bỏ qua lỗi và tiếp tục kiểm tra
        
        if not found:
            await message.answer("Không tìm thấy ID của bạn trong database.")

async def main():
    await init_db()
    
    # Thêm admin mặc định
    await them_admin_user(7325753720, "Đầu Khấc !")
   
    # Khởi tạo bot và dispatcher
    bot = Bot(token=TOKEN)
    
    # Sử dụng memory storage cho FSM (nếu cần)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    
    # Đăng ký middleware
    router.message.middleware(UserDataMiddleware())
    
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())