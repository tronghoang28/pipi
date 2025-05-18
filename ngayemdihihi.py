import html
from telegram import Update
import subprocess 
import random
import nest_asyncio
import asyncio
nest_asyncio.apply()

import os
import json
import logging
import time
import pytz
from datetime import datetime, timedelta, timezone
from functools import wraps
from tinydb import TinyDB, Query
import phonenumbers
from phonenumbers import carrier, geocoder
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ============ CONFIG ============
BOT_TOKEN = "7599849151:AAG6QDQD2_vp1TDY1Ci79kQlsbAU38j0_FE"

DATA_DIR = "/root/ngayemdi/data"
os.makedirs(DATA_DIR, exist_ok=True)

ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
NUM_FILE = os.path.join(DATA_DIR, "num.json")
VIPTRIAL_FILE = os.path.join(DATA_DIR, "viptrial.json")
VIPTRIAL_DAYS = 3  # Thời hạn VIP trial (3 ngà
ONE_SCRIPTS = ["smsvip1.py", "smsvip2.py", "smsvip3.py"]
COOLDOWN_SECONDS = 120
COOLDOWN_WHITELIST = ["/auto", "/list", "/delso"]
MAX_PHONE_NUMBERS = 5

# TinyDB setup for cooldown
COOLDOWN_DB = TinyDB(os.path.join(DATA_DIR, "cooldown.json"))
Cooldown = Query()

PROCESSING_NUMBERS = set()

# In-memory cache
phone_cache = {}  # user_id: list_sdt
data_lock = asyncio.Lock()  # đảm bảo đồng bộ

# Biến trạng thái bảo trì
IS_MAINTENANCE = False


# ============ JSON UTILS ============
def doc_json(file, bypass_cache=False):
    """Đọc dữ liệu từ file JSON"""
    if not os.path.exists(file):
        return {}
    try:
        with open(file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"Lỗi đọc {file}: {e}")
        return {}

def ghi_json(file, data):
    """Ghi dữ liệu vào file JSON"""
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def lay_thoi_gian_vn():
    """Lấy thời gian hiện tại theo múi giờ Việt Nam"""
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.now(tz_vn)
    time_str = now.strftime("%H:%M:%S")
    today_str = now.strftime("%d/%m/%Y")
    return time_str, today_str


def tinh_thoi_gian_con_lai(user_id):
    """Tính thời gian còn lại để hết cooldown"""
    record = COOLDOWN_DB.get(Cooldown.user_id == str(user_id))
    if not record:
        return None

    last_dt = datetime.fromisoformat(record['last_used'])
    now = datetime.now()
    delta = COOLDOWN_SECONDS - (now - last_dt).total_seconds()
    if delta <= 0:
        return None

    return f"{int(delta // 60)} phút" if delta >= 60 else f"{int(delta)} giây"

def get_current_timestamp():
    """Lấy timestamp hiện tại"""
    return int(time.time())

# Hàm tính thời gian hết hạn
def calculate_expiry_timestamp(days):
    """Tính timestamp hết hạn dựa trên số ngày"""
    return get_current_timestamp() + (days * 24 * 60 * 60)


def escape_html(text):
    """Escape HTML characters"""
    return str(text).replace("<", "&lt;").replace(">", "&gt;")

async def send_blockquote(update: Update, text: str, reply_markup=None):
    """Gửi tin nhắn dạng blockquote"""
    return await update.message.reply_text(
        f"<blockquote>{text.strip()}</blockquote>",
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    
# ============ ADMIN / VIP ============
def is_admin(user_id):
    """Kiểm tra xem user có phải là admin không"""
    data = doc_json(ADMIN_FILE)
    return str(user_id) in data and data[str(user_id)].get("role") == "admin"

def is_vip(user_id):
    """Kiểm tra xem user có phải là VIP không (bao gồm VIP trial)"""
    # Kiểm tra VIP thường
    data = doc_json(ADMIN_FILE)
    if str(user_id) in data and data[str(user_id)].get("role") in ("vip", "admin"):
        return True
    
    # Kiểm tra VIP trial
    return is_vip_trial(user_id)

def them_vip(user_id, name):
    """Thêm một user vào danh sách VIP"""
    data = doc_json(ADMIN_FILE)
    data[str(user_id)] = {"name": name, "role": "vip"}
    ghi_json(ADMIN_FILE, data)

# ============ PHONE NUMBER ============
def is_valid_phone_number(s):
    """Kiểm tra xem chuỗi có phải là số điện thoại hợp lệ không"""
    try:
        number = phonenumbers.parse(s, "VN")
        return phonenumbers.is_valid_number(number)
    except:
        return False

def is_duplicate(s, user_id):
    """Kiểm tra xem số điện thoại đã tồn tại trong danh sách của user chưa"""
    data = doc_json(NUM_FILE)
    return s in data.get(str(user_id), [])

def is_max_phone_limit_reached(user_id):
    """Kiểm tra xem user đã đạt giới hạn số điện thoại chưa"""
    data = doc_json(NUM_FILE)
    return len(data.get(str(user_id), [])) >= MAX_PHONE_NUMBERS

async def them_so_user_cache(user_id: int, new_phones: list[str]) -> tuple[list, list]:
    """
    Thêm số điện thoại vào cache, ghi xuống JSON sau.
    Trả về (đã thêm, vượt giới hạn).
    """
    async with data_lock:
        # Lấy từ cache trước, nếu không có thì lấy từ file
        if user_id not in phone_cache:
            data = doc_json(NUM_FILE)
            phone_cache[user_id] = data.get(str(user_id), [])

        phones = phone_cache[user_id]
        added = []
        over_limit = []

        for phone in new_phones:
            if phone not in phones:
                if len(phones) < MAX_PHONE_NUMBERS:
                    phones.append(phone)
                    added.append(phone)
                else:
                    over_limit.append(phone)

        phone_cache[user_id] = phones
        return added, over_limit

async def them_so_user_safe(user_id: int, new_phones: list[str]) -> tuple[list, list]:
    """
    Thêm số điện thoại vào cache và cập nhật file.
    Trả về (đã thêm, vượt giới hạn).
    """
    added, over_limit = await them_so_user_cache(user_id, new_phones)
    
    # Cập nhật file với cache
    data = doc_json(NUM_FILE)
    data[str(user_id)] = phone_cache.get(user_id, [])
    ghi_json(NUM_FILE, data)
    
    return added, over_limit

def lay_so_user(user_id):
    """Lấy danh sách số điện thoại của user"""
    return doc_json(NUM_FILE).get(str(user_id), [])

def xoa_so_user(user_id, danh_sach):
    """Remove phone numbers from user's list"""
    async def _async_delete():
        async with data_lock:
            # Update cache
            if user_id in phone_cache:
                phone_cache[user_id] = [s for s in phone_cache[user_id] 
                                        if s not in danh_sach]
                
                # Remove empty entries
                if not phone_cache[user_id]:
                    del phone_cache[user_id]
            
            # Update disk
            data = doc_json(NUM_FILE)
            uid = str(user_id)
            if uid in data:
                data[uid] = [s for s in data[uid] if s not in danh_sach]
                if not data[uid]:
                    del data[uid]
                ghi_json(NUM_FILE, data)
    
    # Execute asynchronously if possible
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(_async_delete())
    else:
        # We're in a sync context
        asyncio.run(_async_delete())

async def flush_cache_to_file():
    """Định kỳ lưu cache xuống file"""
    while True:
        await asyncio.sleep(300)  # 5 phút
        async with data_lock:
            if not phone_cache:
                continue

            # Đọc file gốc, cập nhật từ cache
            data = doc_json(NUM_FILE)
            for user_id, phones in phone_cache.items():
                data[str(user_id)] = phones
            ghi_json(NUM_FILE, data)

# ============ VIP TRIAL FUNCTIONS ============
def is_vip_trial(user_id):
    """Kiểm tra xem user có phải là VIP trial không và còn hạn không"""
    data = doc_json(VIPTRIAL_FILE)
    str_id = str(user_id)
    if str_id in data:
        # Kiểm tra hết hạn
        if data[str_id].get("expiry", 0) > get_current_timestamp():
            return True
        else:
            # Đã hết hạn, xóa khỏi danh sách
            del data[str_id]
            ghi_json(VIPTRIAL_FILE, data)
    return False

def add_vip_trial(user_id, name):
    """Thêm một user vào danh sách VIP Trial"""
    data = doc_json(VIPTRIAL_FILE)
    data[str(user_id)] = {
        "name": name,
        "added_at": get_current_timestamp(),
        "expiry": calculate_expiry_timestamp(VIPTRIAL_DAYS)
    }
    ghi_json(VIPTRIAL_FILE, data)

def remove_expired_trials():
    """Xóa tất cả các VIP trial đã hết hạn"""
    data = doc_json(VIPTRIAL_FILE)
    current_time = get_current_timestamp()
    
    # Lọc ra những VIP trial còn hạn
    valid_trials = {
        user_id: info for user_id, info in data.items()
        if info.get("expiry", 0) > current_time
    }
    
    # Nếu có sự thay đổi thì lưu lại
    if len(valid_trials) != len(data):
        ghi_json(VIPTRIAL_FILE, valid_trials)

# ============ DECORATORS ============
def cooldown_user(func):
    """Decorator để kiểm soát cooldown giữa các lệnh"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        cmd = (update.message or update.callback_query.message).text.split()[0].lower()
        if cmd in COOLDOWN_WHITELIST:
            return await func(update, context)

        record = COOLDOWN_DB.get(Cooldown.user_id == user_id)
        now = datetime.now()
        if record:
            last_used = datetime.fromisoformat(record['last_used'])
            elapsed = (now - last_used).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - elapsed)
                return await (update.message or update.callback_query.message).reply_text(
                    f"<blockquote>🐸➤Bạn cần chờ {remaining}s để dùng lại\nHạn chế spam để tránh bị chặn hành vi!</blockquote>",
                    parse_mode="HTML"
                )

        COOLDOWN_DB.upsert({'user_id': user_id, 'last_used': now.isoformat()}, Cooldown.user_id == user_id)
        return await func(update, context)
    return wrapper

def only_group(func):
    """Decorator để giới hạn lệnh chỉ sử dụng trong nhóm"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_type = update.message.chat.type
        if is_admin(user_id):
            return await func(update, context)
       
        if chat_type == "private":
            return await update.message.reply_text(
                "<blockquote>Lệnh này chỉ được sử dụng trong nhóm\nt.me/spamcallsmsooo</blockquote>", 
                parse_mode="HTML"
            )
        
        return await func(update, context)
    return wrapper

def vip_only(func):
    """Decorator để giới hạn lệnh chỉ cho VIP sử dụng"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_vip(update.effective_user.id):
            return await update.message.reply_text(
                "<blockquote>🐸➤Lệnh này chỉ dành cho VIP hoặc Admin</blockquote>",
                parse_mode="HTML"
            )
        return await func(update, context)
    return wrapper

def admin_only(func):
    """Decorator để giới hạn lệnh chỉ cho Admin sử dụng"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return await update.message.reply_text(
                "<blockquote>🐸➤Lệnh này chỉ dành cho Admin</blockquote>",
                parse_mode="HTML"
            )
        return await func(update, context)
    return wrapper

def block_during_maintenance(func):
    """Decorator để chặn lệnh khi bot đang bảo trì"""
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        if IS_MAINTENANCE:
            await update.message.reply_text(
                "<blockquote>⛔ Bot đang trong chế độ bảo trì.<br>Vui lòng thử lại sau.</blockquote>",
                parse_mode="HTML"
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def lock_during_maintenance(func):
    """Decorator để chặn lệnh khi bot đang bảo trì (alias)"""
    return block_during_maintenance(func)

async def run_spam_command(command):
    """Run a command asynchronously using asyncio subprocess"""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        logger.error(f"Command failed: {command}")
        logger.error(f"Error: {stderr.decode('utf-8')}")
    
    return proc.returncode == 0

def get_user_badge(user_id):
    """Lấy biểu tượng/nhãn dựa trên vai trò người dùng"""
    if is_admin(user_id):
        return "A͟͟D͟͟M͟͟I͟͟N"
    elif is_vip(user_id) and not is_vip_trial(user_id):
        return "V.I.P"
    elif is_vip_trial(user_id):
        return "TRIAL"
    else:
        return "👤 USER"


# ===================== COMMAND HANDLERS =====================

@only_group
@block_during_maintenance
async def auto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler cho lệnh /auto - thêm số điện thoại vào danh sách tự động"""
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    time_str, today_str = lay_thoi_gian_vn()

    # Kiểm tra VIP
    if not is_vip(int(user_id)):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Đầu Khấc! 💸", url="https://t.me/hoangtrong288")]
        ])
        return await update.message.reply_text(
            "<blockquote>🐸➤Hiện tại bạn chưa có quyền VIP\n"
            "Ấn vào nút bên dưới để liên hệ Admin</blockquote>",
            parse_mode="HTML", 
            reply_markup=kb
        )
    
    # Kiểm tra cú pháp
    if not context.args:
        return await update.message.reply_text(
            "<blockquote>🐸➤Vui lòng nhập số ĐT sau lệnh\n/auto 0989xxxxxx←</blockquote>",
            parse_mode="HTML"
        )

    # Kiểm tra giới hạn
    if is_max_phone_limit_reached(user_id):
        return await update.message.reply_text(
            "<blockquote>🐸➤Bạn chỉ được thêm tối đa\n5 số điện thoại trong list←</blockquote>",
            parse_mode="HTML"
        )

    # Tách xử lý đầu vào
    valid_phones = []
    dup = []
    inv = []

    for phone in context.args:
        phone = phone.strip()
        if not is_valid_phone_number(phone):
            inv.append(phone)
        elif is_duplicate(phone, user_id):
            dup.append(phone)
        else:
            valid_phones.append(phone)

    # Gọi thêm số an toàn
    added, over_limit = await them_so_user_safe(user_id, valid_phones)

    def esc(x): return x.replace("<", "&lt;").replace(">", "&gt;")
    added_str = ' | '.join([esc(x) for x in added]) if added else 'Không có'
    dup_str = ', '.join([esc(x) for x in dup]) if dup else 'Không có'
    inv_str = ', '.join([esc(x) for x in inv]) if inv else 'Không có'
    over_str = ', '.join([esc(x) for x in over_limit]) if over_limit else 'Không có'

    msg = f"""
<blockquote>
╭━━『⭓ Triển Khai ⭓』━━╮
┃»User  : {esc(full_name)}
┃»ID    : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━: ➛
│»Đã Thêm     : {len(added)}
│+ {added_str}
│»Vượt giới hạn: {over_str}
│»Trùng lặp   : {dup_str}
│»Sai số      : {inv_str}
│»Time   : {time_str}
│»Today  : {today_str}
╰━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━卐
╭━『📢 Thông báo Lệnh』━╮
┃»Lệnh sẽ thực thi sau 20 phút
┃»AE lưu ý theo dõi tiến trình
╰━━━━━━━━━━━━━✦❂✦
</blockquote>
"""
    await update.message.reply_text(msg, parse_mode="HTML")


@only_group
@block_during_maintenance
async def list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /list command - shows a user's registered phone numbers."""
    try:
        user = update.effective_user
        user_id = user.id
        full_name = user.full_name
        time_str, today_str = lay_thoi_gian_vn()

        if not is_vip(user_id):
            return await update.message.reply_text(
                "<blockquote>🐸➤Bạn không có quyền VIP\n"
                "Để sử dụng chức năng này.←\n"
                "Liên hệ admin: @hoangtrong288\n"
                "Để lên VIP miễn phí nhé AE</blockquote>", 
                parse_mode="HTML"
            )

        user_nums = lay_so_user(user_id)
       
        if not user_nums:
            return await update.message.reply_text(
                "<blockquote>🐸➤Bạn chưa thêm số điện thoại nào\nvào danh sách của mình!←</blockquote>",
                parse_mode="HTML"
            )
            
        # Format nội dung danh sách số điện thoại
        content = ""
        for i, num in enumerate(user_nums, 1):
            content += f"> │»{i}. {num}\n"
            
        msg = f"""
<blockquote>
╭━━『⭓ Danh Sách ⭓』━━╮
┃»User  : {escape_html(full_name)}
┃»ID    : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Tổng số: {len(user_nums)}
│»{content}
│
│»Time   : {time_str}
│»Today  : {today_str}
╰━━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
"""
        await update.message.reply_text(msg, parse_mode="HTML")

        # Log the action for monitoring purposes
        logging.info(f"User {user_id} ({user.full_name}) listed their phone numbers. Count: {len(user_nums)}")

    except Exception as e:
        # Log the error
        logging.error(f"Error in list_handler: {str(e)}", exc_info=True)
        # Inform the user
        await update.message.reply_text(
            "<blockquote>🐸➤Đã xảy ra lỗi khi xử lý yêu cầu của bạn←</blockquote>",
            parse_mode="HTML"
        )
        
@only_group
@block_during_maintenance
async def delso_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler cho lệnh /delso - xóa số điện thoại khỏi danh sách"""
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    user_numbers = lay_so_user(user_id)
    time_str, today_str = lay_thoi_gian_vn()

    if not is_vip(user_id):
        return await update.message.reply_text(
            "<blockquote>🐸➤Bạn không có quyền VIP\n"
            "Để sử dụng chức năng này.←\n"
            "Liên hệ admin:@hoangtrong288\n"
            "Để lên VIP miễn phí nhé AE</blockquote>", 
            parse_mode="HTML"
        )

    if not context.args:
        return await update.message.reply_text(
            "<blockquote>🐸➤Cách dùng: /delso 1,2 hoặc /delso all←</blockquote>",
            parse_mode="HTML"
        )

    arg = context.args[0].strip().lower()

    if arg == "all":
        if user_numbers:
            xoa_so_user(user_id, user_numbers)
            context.chat_data[f"keep_{update.message.message_id}"] = True
            
            msg = f"""
<blockquote>
╭━━『⭓ Xoá Toàn Bộ Số ⭓』━━╮
┃»User  : {full_name}
┃»ID    : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Đã xoá toàn bộ số
│»Time   : {time_str}
│»Today  : {today_str}
╰━━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
"""
            await update.message.reply_text(msg, parse_mode="HTML")
        else:
            await update.message.reply_text(
                "<blockquote>🐸➤Danh sách của bạn đang trống←</blockquote>",
                parse_mode="HTML"
            )
    else:
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(user_numbers):
                phone_to_remove = user_numbers[idx]
                xoa_so_user(user_id, [phone_to_remove])
                context.chat_data[f"keep_{update.message.message_id}"] = True
                
                msg = f"""
<blockquote>
╭━━『⭓ Xoá Số Thứ {idx + 1} ⭓』━━╮
┃»User  : {full_name}
┃»ID    : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Đã xoá: {escape_html(phone_to_remove)}
│»Time  : {time_str}
│»Today : {today_str}
╰━━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
"""
                await update.message.reply_text(msg, parse_mode="HTML")
            else:
                await update.message.reply_text(
                    "<blockquote>🐸➤Số thứ tự không hợp lệ←</blockquote>",
                    parse_mode="HTML"
                )
        except ValueError:
            await update.message.reply_text(
                "<blockquote>🐸➤Sai cú pháp /delso 1,2 hoặc all←</blockquote>",
                parse_mode="HTML"
            )


@block_during_maintenance
async def viptrial_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler cho lệnh /viptrial - kích hoạt VIP trial cho người dùng mới"""
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    time_str, today_str = lay_thoi_gian_vn()
    
    # Kiểm tra xem user đã là VIP chưa
    if is_vip(user_id):
        return await update.message.reply_text(
            "<blockquote>🐸➤Bạn đã là VIP hoặc VIP trial rồi!</blockquote>",
            parse_mode="HTML"
        )
    
    # Thêm user vào danh sách VIP trial
    add_vip_trial(user_id, full_name)
    
    # Tính ngày hết hạn
    viptrial_data = doc_json(VIPTRIAL_FILE)
    expiry_timestamp = viptrial_data[str(user_id)].get("expiry", 0)
    expiry_date = datetime.fromtimestamp(expiry_timestamp).strftime("%d/%m/%Y %H:%M:%S")
    
    # Gửi thông báo thành công
    msg = f"""
<blockquote>
┃»User    : {escape_html(full_name)}
┃»ID      : {user_id}
│»Trạng thái: Đã kích hoạt VIP trial
│»Thời hạn  : {VIPTRIAL_DAYS} ngày
│»Hết hạn   : {expiry_date}
│»Time      : {time_str}
│»Today     : {today_str}
</blockquote>
""".strip()
    
    await update.message.reply_text(msg, parse_mode="HTML")


@cooldown_user
@only_group
@block_during_maintenance
async def smscall_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler cho lệnh /smscall - gửi SMS và cuộc gọi tới số điện thoại"""
    user = update.effective_user
    user_id = str(user.id)
    full_name = user.full_name
    current_time = time.time()

    time_str, today_str = lay_thoi_gian_vn()

    if len(context.args) != 1:
        return await update.message.reply_text(
            "<blockquote>🐸➤Cú pháp: /smscall SỐ</blockquote>",
            parse_mode="HTML"
        )

    phone_number = context.args[0]

    if not (phone_number.isdigit() and len(phone_number) == 10):
        return await update.message.reply_text(
            "<blockquote>🐸➤Số ĐT phải đủ 10 số!</blockquote>",
            parse_mode="HTML"
        )

    try:
        parsed_number = phonenumbers.parse(phone_number, "VN")
        carrier_name = carrier.name_for_number(parsed_number, "vi")
        region = geocoder.description_for_number(parsed_number, "vi")
        if not carrier_name:
            return await update.message.reply_text(
                "<blockquote>🐸➤Số không hợp lệ bạn ơi!</blockquote>",
                parse_mode="HTML"
            )
    except phonenumbers.phonenumberutil.NumberParseException:
        return await update.message.reply_text(
            "<blockquote>🐸➤Lỗi khi xử lý số điện thoại!</blockquote>",
            parse_mode="HTML"
        )

    # Kiểm tra trùng lặp
    if is_duplicate(phone_number, user_id):
        return await update.message.reply_text(
            "<blockquote>🐸➤Số ĐT này đang trong thời gian xử lý\nVui lòng đợi hoặc chọn số khác</blockquote>",
            parse_mode="HTML"
        )

    # Random 1 trong 3 script - Sử dụng asyncio thay vì os.system để cải thiện hiệu năng
    script_name = random.choice(ONE_SCRIPTS)
    
    # Sử dụng asyncio.create_subprocess_shell thay vì os.system
    await asyncio.create_subprocess_shell(
        f"screen -dmS smsvip_{user_id} bash -c 'timeout 500s python3 {script_name} {phone_number} 1000'",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    context.chat_data[f"keep_{update.message.message_id}"] = True
  
    # Gửi thông tin phản hồi
    msg = f"""
<blockquote>
╭━━『⭓ CALL SMS ⭓』━━╮
┃»User    : {full_name}
┃»ID      : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Phone   : {phone_number}
│»Round   : 6688
│»Nhà mạng: {carrier_name}
│»Khu vực : {region}
│»Time    : {time_str}
│»Today   : {today_str}
╰━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
""".strip()

    await update.message.reply_text(msg, parse_mode="HTML")
    
@cooldown_user
@only_group
@block_during_maintenance
async def vipsms_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    time_str, today_str = lay_thoi_gian_vn()

    if len(context.args) != 1:
        return await update.message.reply_text(
            "<blockquote>🐸➤Cú pháp: /vipsms SỐ Spam liên tục 6h←</blockquote>", 
            parse_mode="HTML"
        )

    phone_number = context.args[0].strip()
    try:
        parsed = phonenumbers.parse(phone_number, "VN")
        carrier_name = carrier.name_for_number(parsed, "vi") or "Không rõ"
        region = geocoder.description_for_number(parsed, "vi") or "Không rõ"
        
        if not phonenumbers.is_valid_number(parsed):
            return await update.message.reply_text(
                "<blockquote>🐸➤Số không hợp lệ bạn ơi!←</blockquote>", 
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Error parsing phone number: {e}")
        return await update.message.reply_text(
            "<blockquote>🐸➤Số không hợp lệ bạn ơi!←</blockquote>", 
            parse_mode="HTML"
        )

    context.chat_data[f"keep_{update.message.message_id}"] = True
  
    msg = f"""
<blockquote>
╭━━『⭓ VIP SMS ⭓』━━╮
┃»User    : {html.escape(full_name)}
┃»ID      : {user_id}
╰━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Phone   : {phone_number}
│»Round   : 1Giờ + 999count
│»Nhà mạng: {carrier_name}
│»Khu vực : {region}
│»Time    : {time_str}
│»Today   : {today_str}
╰━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
""".strip()

    await update.message.reply_text(msg, parse_mode="HTML")

    try:
        # Use asyncio.create_subprocess_shell instead of subprocess.run
        await asyncio.create_subprocess_shell(
            f"screen -dm bash -c 'timeout 259s python3 vipbot.py {phone_number} 1000'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    except Exception as e:
        await update.message.reply_text(
            "<blockquote>🐸 Lỗi khi thực thi lệnh spam!</blockquote>",
            parse_mode="HTML"
        )
        logging.error(f"Lỗi shell command: {e}")

@cooldown_user
@only_group
@block_during_maintenance
async def ngl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    time_str, today_str = lay_thoi_gian_vn()

    if len(context.args) != 3:
        return await update.message.reply_text(
            "<blockquote>🐸➤ Cách dùng: /ngl [username] [nội_dung] [số_lần]</blockquote>",
            parse_mode="HTML"
        )

    username, noidung, repeat = context.args
    try:
        repeat_count = int(repeat)
        if repeat_count > 100:
            return await update.message.reply_text(
                "<blockquote>Số lần tối đa là 100.</blockquote>",
                parse_mode="HTML"
            )
    except ValueError:
        return await update.message.reply_text(
            "<blockquote>Số lần phải là số nguyên.</blockquote>",
            parse_mode="HTML"
        )

    # Use asyncio.create_subprocess_shell instead of os.system
    await asyncio.create_subprocess_shell(
        f"screen -dm bash -c 'timeout 250s python3 spamngl.py {username} \"{noidung}\" {repeat_count}'",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    context.chat_data[f"keep_{update.message.message_id}"] = True
 
    msg = f"""
<blockquote>
╭━━『⭓ NGL SPAM ⭓』━━╮
┃»User    : {full_name}
┃»ID      : {user_id}
╰━━━━━━━━━━━━✦❂✦
╭━━━━━━━: ̗̀➛
│»Username: {escape_html(username)}
│»Nội dung: {escape_html(noidung)}
│»Số lần  : {repeat_count}
│»Time    : {time_str}
│»Today   : {today_str}
╰━━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━━卐
</blockquote>
""".strip()

    await update.message.reply_text(msg, parse_mode="HTML")
    
@cooldown_user
@only_group
@block_during_maintenance
async def spam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    full_name = user.full_name
    time_str, today_str = lay_thoi_gian_vn()

    if len(context.args) != 1:
        return await update.message.reply_text(
            "<blockquote>🐸➤Cú pháp: /spam Số, liên tục trong 1h</blockquote>", 
            parse_mode="HTML"
        )

    phone_number = context.args[0].strip()
    try:
        parsed = phonenumbers.parse(phone_number, "VN")
        carrier_name = carrier.name_for_number(parsed, "vi") or "Không rõ"
        region = geocoder.description_for_number(parsed, "vi") or "Không rõ"
        
        if not phonenumbers.is_valid_number(parsed):
            return await update.message.reply_text(
                "<blockquote>🐸➤Số không hợp lệ bạn ơi!←</blockquote>", 
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Error parsing phone number: {e}")
        return await update.message.reply_text(
            "<blockquote>🐸➤Số không hợp lệ bạn ơi!←</blockquote>", 
            parse_mode="HTML"
        )

    context.chat_data[f"keep_{update.message.message_id}"] = True
   
    msg = f"""
<blockquote>
╭━━『⭓ SPAMER ⭓』━━╮
┃»User    : {html.escape(full_name)}
┃»ID      : {user_id}
╰━━━━━━━━━━━✦❂✦
╭━━━━━━: ̗̀➛
│»Phone   : {phone_number}
│»Round   : 30 phút
│»Nhà mạng: {carrier_name}
│»Khu vực : {region}
│»Time    : {time_str}
│»Today   : {today_str}
╰━━━━⋆༺𓆩☠︎︎𓆪༻⋆━━━卐
</blockquote>
""".strip()

    await update.message.reply_text(msg, parse_mode="HTML")

    try:
        # Sử dụng asyncio.create_subprocess_shell thay vì subprocess.run
        await asyncio.create_subprocess_shell(
            f"screen -dm bash -c 'timeout 250s python3 viptest.py {phone_number} 1000'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    except Exception as e:
        await update.message.reply_text(
            "<blockquote>🐸 Lỗi khi thực thi lệnh spam!</blockquote>",
            parse_mode="HTML"
        )
        logging.error(f"Lỗi shell command: {e}")


# ==== LIST_VIP HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def list_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = doc_json(ADMIN_FILE)
    content = json.dumps(data, indent=2, ensure_ascii=False)

    await update.message.reply_text(f"<blockquote>{html.escape(content)}</blockquote>", parse_mode="HTML")


# ==== THEM_VIP HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def them_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text(
            "<blockquote>🐸 Cú pháp: /themvip [ID]</blockquote>",
            parse_mode="HTML"
        )

    user_id = context.args[0]
    data = doc_json(ADMIN_FILE)
    data[user_id] = {"role": "vip"}
    ghi_json(ADMIN_FILE, data)

    return await update.message.reply_text(
        f"<blockquote>Đã thêm VIP với ID: {user_id}</blockquote>",
        parse_mode="HTML"
    )

# ==== XOA_VIP HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def xoa_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "<blockquote>🐸 Cú pháp: /xoavip [ID]</blockquote>",
            parse_mode="HTML"
        )

    user_id = context.args[0]
    data = doc_json(ADMIN_FILE)
    if user_id in data:
        del data[user_id]
        ghi_json(ADMIN_FILE, data)
        return await update.message.reply_text(
            f"<blockquote>Đã xoá VIP với ID: {user_id}</blockquote>",
            parse_mode="HTML"
        )
    else:
        return await update.message.reply_text(
            f"<blockquote>🐸 ID không tồn tại trong danh sách VIP</blockquote>",
            parse_mode="HTML"
        )

# ==== CHECK_ID HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def check_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "<blockquote>Cú pháp: /checkid [ID]</blockquote>",
            parse_mode="HTML"
        )

    username = context.args[0].lstrip('@')
    try:
        user = await context.bot.get_chat(username)
        return await update.message.reply_text(
            f"<blockquote>ID của @{html.escape(username)} là: {user.id}</blockquote>", 
            parse_mode="HTML"
        )
    except Exception:
        return await update.message.reply_text(
            "<blockquote>Không tìm thấy user hoặc bot chưa từng liên hệ.</blockquote>",
            parse_mode="HTML"
        )

# Global variable for maintenance mode
IS_MAINTENANCE = False

# ==== LIST_ALL_NUMBERS HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def list_all_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Đọc dữ liệu từ num.json
    data = doc_json(NUM_FILE)
    if not data:
        await update.message.reply_text(
            "<blockquote>Không có số điện thoại nào trong hệ thống.</blockquote>",
            parse_mode="HTML"
        )
        return

    # Liệt kê tất cả các số điện thoại
    numbers = []
    for uid, numbers_list in data.items():
        for number in numbers_list:
            numbers.append(f"Số: {html.escape(number)}")
    
    # Gửi danh sách các số điện thoại
    if numbers:
        text = "<blockquote>" + "\n".join(numbers) + "</blockquote>"
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(
            "<blockquote>Không có số nào trong hệ thống.</blockquote>",
            parse_mode="HTML"
        )

# ==== XOA_ALL_NUMBERS HANDLER IMPROVEMENTS ====
@admin_only
@block_during_maintenance
async def xoa_all_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Xoá toàn bộ dữ liệu trong num.json
    ghi_json(NUM_FILE, {})
    await update.message.reply_text("<blockquote>OK.</blockquote>", parse_mode="HTML")

# Define the audit_and_repair function that was missing
async def audit_and_repair():
    """Audit and repair database files"""
    # Load data files
    admin_data = doc_json(ADMIN_FILE)
    num_data = doc_json(NUM_FILE)
    
    # Fix phone number database
    fixed_num_data = {}
    for user_id, phones in num_data.items():
        valid_phones = []
        for phone in phones:
            try:
                parsed = phonenumbers.parse(phone, "VN")
                if phonenumbers.is_valid_number(parsed):
                    valid_phones.append(phone)
            except:
                pass
        
        if valid_phones:
            fixed_num_data[user_id] = valid_phones
    
    # Write fixed data back
    ghi_json(NUM_FILE, fixed_num_data)
    
    # Clear cooldown database to avoid issues
    COOLDOWN_DB.truncate()
    
    # Return report
    return {
        "admin_entries": len(admin_data),
        "num_entries_before": len(num_data),
        "num_entries_after": len(fixed_num_data)
    }

# ==== REPAIR COMMAND HANDLER ====
@admin_only
async def repair_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global IS_MAINTENANCE
    IS_MAINTENANCE = True

    await update.message.reply_text(
        "<blockquote>🚧 Đang tạm dừng hoạt động\nBot đang sửa chữa dữ liệu...</blockquote>",
        parse_mode="HTML"
    )

    try:
        result = await audit_and_repair()
        await update.message.reply_text(
            f"<blockquote>Sửa chữa hoàn tất\nBot đã hoạt động trở lại.</blockquote>"
            f"Kết quả: {html.escape(str(result))}</blockquote>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"<blockquote>Có lỗi xảy ra trong quá trình sửa chữa\n{html.escape(str(e))}</blockquote>",
            parse_mode="HTML"
        )
    finally:
        IS_MAINTENANCE = False

# ==== AUTO_DELETE_INVALID HANDLER ====
async def auto_delete_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # Skip if message is None (could happen with edits/channel posts)
    if not message:
        return
        
    msg_id = message.message_id

    # Nếu bot đã đánh dấu message này là đã xử lý thành công → không xoá
    if context.chat_data.get(f"keep_{msg_id}"):
        return

    try:
        await message.delete()
    except Exception as e:
        logging.error(f"Không thể xoá tin nhắn: {e}")

async def check_expired_trials():
    """Định kỳ kiểm tra và xóa các VIP trial đã hết hạn"""
    while True:
        try:
            remove_expired_trials()
        except Exception as e:
            logging.error(f"Lỗi khi kiểm tra VIP trial hết hạn: {e}")
        
        # Kiểm tra mỗi giờ
        await asyncio.sleep(3600)

async def main():
    # Setup logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("auto", auto_handler))
    app.add_handler(CommandHandler("list", list_handler))
    app.add_handler(CommandHandler("delso", delso_handler))
    app.add_handler(CommandHandler("smscall", smscall_handler))
    app.add_handler(CommandHandler("ngl", ngl_handler))
    app.add_handler(CommandHandler("vipsms", vipsms_handler))
    app.add_handler(CommandHandler("spam", spam_handler))
    app.add_handler(CommandHandler("themvip", them_vip_cmd))
    app.add_handler(CommandHandler("xoavip", xoa_vip_cmd))
    app.add_handler(CommandHandler("listvip", list_vip_cmd))
    app.add_handler(CommandHandler("checkid", check_id_cmd))
    app.add_handler(CommandHandler("listnums", list_all_numbers))
    app.add_handler(CommandHandler("xoanums", xoa_all_numbers))
    app.add_handler(CommandHandler("repair", repair_command))
    app.add_handler(CommandHandler("viptrial", viptrial_handler))
    
    # Message handler for auto-deleting messages
    app.add_handler(MessageHandler(filters.ALL, auto_delete_invalid))
    
    # Start background task for cache flushing
    asyncio.create_task(flush_cache_to_file())
    
    # Thêm tác vụ kiểm tra VIP trial hết hạn
    asyncio.create_task(check_expired_trials())
    
    print("Bot đang chạy...")
    await app.run_polling(poll_interval=0, timeout=10, drop_pending_updates=True)

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()  # DÙNG ĐỂ FIX LỖI EVENT LOOP (cần cài: pip install nest_asyncio)

    asyncio.run(main())