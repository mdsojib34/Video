import asyncio
import html
import os
import secrets
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "6994836801"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "-1003743608328"))
MONETAG_ZONE_ID = os.getenv("MONETAG_ZONE_ID", "11404425").strip()
MONETAG_SDK_FUNC = os.getenv("MONETAG_SDK_FUNC", f"show_{MONETAG_ZONE_ID}").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://your-domain.example").rstrip("/")
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "1200"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))
SESSION_TTL = 10 * 60

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set BOT_TOKEN.")

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"

DEFAULTS = {
    "welcome_text": (
        "🔥 <b>ভিডিও জোনে স্বাগতম!</b>\n\n"
        "নতুন ভিডিও দেখতে নিচের বাটনে ক্লিক করুন।\n"
        "ভিডিও চালুর আগে একটি বিজ্ঞাপন দেখানো হবে।\n\n"
        "👇 শুরু করতে ক্লিক করুন"
    ),
    "watch_button": "🎬 ভিডিও দেখুন",
    "new_button": "🎬 নতুন ভিডিও দেখুন",
    "previous_button": "⬅️ আগের ভিডিও দেখুন",
    "maintenance": "0",
    "auto_delete_seconds": str(AUTO_DELETE_SECONDS),
}

router = Router()
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
dp.include_router(router)


async def db_exec(sql: str, params=(), *, fetchone=False, fetchall=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        result = None
        if fetchone:
            result = await cur.fetchone()
        elif fetchall:
            result = await cur.fetchall()
        await db.commit()
        return result


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for key, value in DEFAULTS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value)
            )
        await db.execute(
            "INSERT OR IGNORE INTO admins(telegram_id, role) VALUES(?, 'owner')",
            (OWNER_ID,),
        )
        await db.commit()


async def setting(key: str) -> str:
    row = await db_exec("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    return row["value"] if row else DEFAULTS.get(key, "")


async def set_setting(key: str, value: str):
    await db_exec(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def upsert_user(message_or_query):
    user = message_or_query.from_user
    await db_exec(
        """
        INSERT INTO users(telegram_id, username, first_name, last_active)
        VALUES(?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(telegram_id) DO UPDATE SET
          username=excluded.username,
          first_name=excluded.first_name,
          last_active=CURRENT_TIMESTAMP
        """,
        (user.id, user.username or "", user.first_name or ""),
    )


async def user_blocked(telegram_id: int) -> bool:
    row = await db_exec(
        "SELECT is_blocked FROM users WHERE telegram_id=?", (telegram_id,), fetchone=True
    )
    return bool(row and row["is_blocked"])


async def is_admin(user_id: int) -> bool:
    row = await db_exec(
        "SELECT 1 FROM admins WHERE telegram_id=?", (user_id,), fetchone=True
    )
    return bool(row)


def home_keyboard(watch_text: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=watch_text, callback_data="watch:first")]]
    )


async def video_keyboard():
    new_text = await setting("new_button")
    prev_text = await setting("previous_button")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=new_text, callback_data="watch:new")],
            [InlineKeyboardButton(text=prev_text, callback_data="watch:prev")],
        ]
    )


def admin_keyboard(maintenance: bool):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats"),
                InlineKeyboardButton(text="🎬 Videos", callback_data="admin:videos"),
            ],
            [
                InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
                InlineKeyboardButton(text="📣 Broadcast", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton(text="📝 Text Settings", callback_data="admin:texts"),
                InlineKeyboardButton(text="📡 Channel", callback_data="admin:channel"),
            ],
            [
                InlineKeyboardButton(
                    text=f"🛠 Maintenance: {'ON' if maintenance else 'OFF'}",
                    callback_data="admin:maintenance",
                )
            ],
        ]
    )


@router.message(CommandStart())
async def start(message: Message):
    await upsert_user(message)
    if await user_blocked(message.from_user.id):
        return await message.answer("🚫 আপনার Bot Access বর্তমানে বন্ধ রয়েছে।")
    if await setting("maintenance") == "1" and not await is_admin(message.from_user.id):
        return await message.answer("🛠 Bot Maintenance চলছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।")
    text = await setting("welcome_text")
    watch_text = await setting("watch_button")
    await message.answer(text, reply_markup=home_keyboard(watch_text))


async def select_target_video(telegram_id: int, direction: str):
    user = await db_exec(
        "SELECT current_video_id FROM users WHERE telegram_id=?",
        (telegram_id,),
        fetchone=True,
    )
    current = user["current_video_id"] if user else None

    if direction == "first" or not current:
        return await db_exec(
            "SELECT * FROM videos WHERE status=1 ORDER BY id DESC LIMIT 1", fetchone=True
        )

    if direction == "new":
        # Move through the feed from newest to older items. This gives a fresh item on each press.
        row = await db_exec(
            "SELECT * FROM videos WHERE status=1 AND id < ? ORDER BY id DESC LIMIT 1",
            (current,),
            fetchone=True,
        )
        return row

    if direction == "prev":
        # Go back to the video viewed immediately before the current one.
        return await db_exec(
            """
            SELECT v.* FROM user_video_history h
            JOIN videos v ON v.id=h.video_id
            WHERE h.user_id=(SELECT id FROM users WHERE telegram_id=?)
              AND v.status=1 AND v.id != ?
            ORDER BY h.id DESC LIMIT 1
            """,
            (telegram_id, current),
            fetchone=True,
        )
    return None


async def create_ad_session(telegram_id: int, video_id: int, direction: str) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    await db_exec(
        """
        INSERT INTO ad_sessions(token,telegram_id,video_id,direction,status,created_at,expires_at)
        VALUES(?,?,?,?, 'pending', ?, ?)
        """,
        (token, telegram_id, video_id, direction, now, now + SESSION_TTL),
    )
    return token


@router.callback_query(F.data.startswith("watch:"))
async def watch_video(callback: CallbackQuery):
    await upsert_user(callback)
    uid = callback.from_user.id
    if await user_blocked(uid):
        return await callback.answer("আপনার access বন্ধ আছে।", show_alert=True)
    if await setting("maintenance") == "1" and not await is_admin(uid):
        return await callback.answer("Maintenance চলছে।", show_alert=True)

    direction = callback.data.split(":", 1)[1]
    target = await select_target_video(uid, direction)
    if not target:
        if direction == "prev":
            return await callback.answer("এর আগে আর কোনো দেখা ভিডিও নেই।", show_alert=True)
        return await callback.answer("নতুন আর কোনো ভিডিও নেই।", show_alert=True)

    # Prevent multiple pending sessions for the same user/video.
    now = int(time.time())
    existing = await db_exec(
        """
        SELECT token FROM ad_sessions
        WHERE telegram_id=? AND video_id=? AND status='pending' AND expires_at>?
        ORDER BY id DESC LIMIT 1
        """,
        (uid, target["id"], now),
        fetchone=True,
    )
    token = existing["token"] if existing else await create_ad_session(uid, target["id"], direction)
    ad_url = f"{PUBLIC_BASE_URL}/ad?token={quote(token)}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📢 বিজ্ঞাপন দেখে ভিডিও খুলুন", url=ad_url)]]
    )
    await callback.message.answer(
        "🔒 <b>ভিডিও লক করা আছে</b>\n\nবিজ্ঞাপন সম্পূর্ণ দেখলে ভিডিও অটোমেটিক পাঠানো হবে।",
        reply_markup=kb,
    )
    await callback.answer()


async def auto_delete(chat_id: int, message_id: int, delay: int):
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    with suppress(Exception):
        await bot.delete_message(chat_id, message_id)


async def deliver_video(telegram_id: int, video_id: int, ad_session_id: int):
    video = await db_exec("SELECT * FROM videos WHERE id=? AND status=1", (video_id,), fetchone=True)
    if not video:
        await bot.send_message(telegram_id, "⚠️ ভিডিওটি বর্তমানে পাওয়া যাচ্ছে না।")
        return False

    caption = video["caption"] or "🎬 ভিডিও"
    sent = await bot.send_video(
        telegram_id,
        video=video["telegram_file_id"],
        caption=caption,
        protect_content=True,
        reply_markup=await video_keyboard(),
    )
    await db_exec(
        "UPDATE users SET current_video_id=?, total_views=total_views+1, total_ads=total_ads+1, last_active=CURRENT_TIMESTAMP WHERE telegram_id=?",
        (video_id, telegram_id),
    )
    await db_exec(
        "INSERT INTO user_video_history(user_id,video_id,direction) VALUES((SELECT id FROM users WHERE telegram_id=?), ?, 'view')",
        (telegram_id, video_id),
    )
    delay = int(await setting("auto_delete_seconds") or "0")
    if delay > 0:
        asyncio.create_task(auto_delete(telegram_id, sent.message_id, delay))
    return True


@router.channel_post(F.chat.id == SOURCE_CHANNEL_ID, F.video)
async def import_channel_video(message: Message):
    await db_exec(
        """
        INSERT INTO videos(channel_id,channel_message_id,telegram_file_id,caption,status,sort_order)
        VALUES(?,?,?,?,1,?)
        ON CONFLICT(channel_id,channel_message_id) DO UPDATE SET
          telegram_file_id=excluded.telegram_file_id,
          caption=excluded.caption,
          status=1
        """,
        (
            message.chat.id,
            message.message_id,
            message.video.file_id,
            message.caption or "",
            message.message_id,
        ),
    )


@router.edited_channel_post(F.chat.id == SOURCE_CHANNEL_ID, F.video)
async def edit_channel_video(message: Message):
    await db_exec(
        "UPDATE videos SET telegram_file_id=?, caption=? WHERE channel_id=? AND channel_message_id=?",
        (message.video.file_id, message.caption or "", message.chat.id, message.message_id),
    )


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        return
    maintenance = await setting("maintenance") == "1"
    await message.answer(
        "⚙️ <b>ADMIN CONTROL PANEL</b>\n\nসব গুরুত্বপূর্ণ control এখান থেকে পরিচালনা করুন।",
        reply_markup=admin_keyboard(maintenance),
    )


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Unauthorized", show_alert=True)
    action = callback.data.split(":", 1)[1]

    if action == "stats":
        users = await db_exec("SELECT COUNT(*) c FROM users", fetchone=True)
        videos = await db_exec("SELECT COUNT(*) c FROM videos", fetchone=True)
        active = await db_exec("SELECT COUNT(*) c FROM videos WHERE status=1", fetchone=True)
        ads = await db_exec("SELECT COUNT(*) c FROM ad_sessions WHERE status='used'", fetchone=True)
        views = await db_exec("SELECT COALESCE(SUM(total_views),0) c FROM users", fetchone=True)
        text = (
            "📊 <b>Statistics</b>\n\n"
            f"👥 Users: <b>{users['c']}</b>\n"
            f"🎬 Videos: <b>{videos['c']}</b>\n"
            f"✅ Active Videos: <b>{active['c']}</b>\n"
            f"▶️ Views: <b>{views['c']}</b>\n"
            f"📢 Completed Ads: <b>{ads['c']}</b>"
        )
        await callback.message.answer(text)

    elif action == "videos":
        rows = await db_exec(
            "SELECT id, channel_message_id, status, substr(caption,1,35) caption FROM videos ORDER BY id DESC LIMIT 15",
            fetchall=True,
        )
        if not rows:
            await callback.message.answer("🎬 এখনো কোনো ভিডিও import হয়নি।")
        else:
            lines = ["🎬 <b>Latest Videos</b>"]
            buttons = []
            for r in rows:
                state = "✅" if r["status"] else "❌"
                cap = html.escape(r["caption"] or "No caption")
                lines.append(f"\n{state} #{r['id']} · ch:{r['channel_message_id']} · {cap}")
                buttons.append([
                    InlineKeyboardButton(
                        text=f"{'Disable' if r['status'] else 'Enable'} #{r['id']}",
                        callback_data=f"video:toggle:{r['id']}",
                    )
                ])
            await callback.message.answer("".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    elif action == "users":
        total = await db_exec("SELECT COUNT(*) c FROM users", fetchone=True)
        blocked = await db_exec("SELECT COUNT(*) c FROM users WHERE is_blocked=1", fetchone=True)
        recent = await db_exec(
            "SELECT telegram_id, username, first_name, total_views FROM users ORDER BY last_active DESC LIMIT 10",
            fetchall=True,
        )
        lines = [f"👥 <b>Users</b>\n\nTotal: {total['c']}\nBlocked: {blocked['c']}\n"]
        for r in recent:
            name = html.escape(r["username"] or r["first_name"] or str(r["telegram_id"]))
            lines.append(f"\n• {name} — {r['telegram_id']} — {r['total_views']} views")
        await callback.message.answer("".join(lines))

    elif action == "maintenance":
        cur = await setting("maintenance")
        new = "0" if cur == "1" else "1"
        await set_setting("maintenance", new)
        await callback.message.edit_reply_markup(reply_markup=admin_keyboard(new == "1"))
        await callback.answer(f"Maintenance {'ON' if new == '1' else 'OFF'}")
        return

    elif action == "channel":
        await callback.message.answer(
            "📡 <b>Channel Settings</b>\n\n"
            f"Source Channel ID: <code>{SOURCE_CHANNEL_ID}</code>\n"
            "নতুন video post হলে auto-import হবে।\n\n"
            "⚠️ Telegram Bot API channel post deletion event দেয় না; delete হলে Admin Video list থেকে Disable করুন।"
        )

    elif action == "texts":
        await callback.message.answer(
            "📝 <b>Text Settings</b>\n\n"
            "Commands:\n"
            "<code>/setwelcome আপনার নতুন welcome text</code>\n"
            "<code>/setwatch 🎬 ভিডিও দেখুন</code>\n"
            "<code>/setnew 🎬 নতুন ভিডিও দেখুন</code>\n"
            "<code>/setprev ⬅️ আগের ভিডিও দেখুন</code>\n"
            "<code>/setdelete 1200</code> — seconds; 0 = OFF"
        )

    elif action == "broadcast":
        await callback.message.answer(
            "📣 Broadcast করতে ব্যবহার করুন:\n\n<code>/broadcast আপনার মেসেজ</code>"
        )

    await callback.answer()


@router.callback_query(F.data.startswith("video:toggle:"))
async def toggle_video(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return await callback.answer("Unauthorized", show_alert=True)
    vid = int(callback.data.rsplit(":", 1)[1])
    row = await db_exec("SELECT status FROM videos WHERE id=?", (vid,), fetchone=True)
    if not row:
        return await callback.answer("Video not found", show_alert=True)
    new = 0 if row["status"] else 1
    await db_exec("UPDATE videos SET status=? WHERE id=?", (new, vid))
    await callback.answer(f"Video #{vid} {'Enabled' if new else 'Disabled'}", show_alert=True)


async def admin_set_from_command(message: Message, key: str, command: str):
    if not await is_admin(message.from_user.id):
        return
    value = message.text.partition(" ")[2].strip()
    if not value:
        return await message.answer(f"Usage: <code>/{command} value</code>")
    await set_setting(key, value)
    await message.answer("✅ Updated")


@router.message(Command("setwelcome"))
async def setwelcome(message: Message):
    await admin_set_from_command(message, "welcome_text", "setwelcome")


@router.message(Command("setwatch"))
async def setwatch(message: Message):
    await admin_set_from_command(message, "watch_button", "setwatch")


@router.message(Command("setnew"))
async def setnew(message: Message):
    await admin_set_from_command(message, "new_button", "setnew")


@router.message(Command("setprev"))
async def setprev(message: Message):
    await admin_set_from_command(message, "previous_button", "setprev")


@router.message(Command("setdelete"))
async def setdelete(message: Message):
    if not await is_admin(message.from_user.id):
        return
    raw = message.text.partition(" ")[2].strip()
    try:
        seconds = int(raw)
        if seconds < 0 or seconds > 86400:
            raise ValueError
    except ValueError:
        return await message.answer("Usage: <code>/setdelete 1200</code> (0-86400)")
    await set_setting("auto_delete_seconds", str(seconds))
    await message.answer(f"✅ Auto delete: {'OFF' if seconds == 0 else str(seconds)+' seconds'}")


@router.message(Command("broadcast"))
async def broadcast(message: Message):
    if not await is_admin(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        return await message.answer("Usage: <code>/broadcast আপনার মেসেজ</code>")
    rows = await db_exec("SELECT telegram_id FROM users WHERE is_blocked=0", fetchall=True)
    ok = fail = 0
    status = await message.answer(f"📣 Broadcast শুরু... Total {len(rows)}")
    for r in rows:
        try:
            await bot.send_message(r["telegram_id"], text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.035)
    await status.edit_text(f"✅ Broadcast Complete\nDelivered: {ok}\nFailed: {fail}")


# ----------------------------- Monetag web gate -----------------------------

def ad_page_html(token: str) -> str:
    safe_token = html.escape(token, quote=True)
    safe_func = ''.join(c for c in MONETAG_SDK_FUNC if c.isalnum() or c == '_')
    return f"""<!doctype html>
<html lang=\"bn\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,maximum-scale=1\" />
<title>ভিডিও আনলক</title>
<script src=\"//libtl.com/sdk.js\" data-zone=\"{html.escape(MONETAG_ZONE_ID)}\" data-sdk=\"{safe_func}\"></script>
<style>
body{{margin:0;background:#0e1117;color:#fff;font-family:system-ui,-apple-system,Segoe UI,sans-serif;display:grid;min-height:100vh;place-items:center}}
.card{{width:min(92vw,420px);background:#171b23;border:1px solid #2b3240;border-radius:22px;padding:28px;box-sizing:border-box;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.35)}}
.icon{{font-size:54px}} h1{{font-size:24px;margin:12px 0 8px}} p{{color:#b7c0cf;line-height:1.55}}
button{{width:100%;border:0;border-radius:14px;padding:15px 18px;font-size:17px;font-weight:750;cursor:pointer;background:#fff;color:#111827;margin-top:14px}}
button:disabled{{opacity:.55;cursor:not-allowed}} .status{{margin-top:16px;min-height:24px;color:#d8dee9}} .small{{font-size:12px;color:#7f8a9d;margin-top:16px}}
</style>
</head>
<body>
<div class=\"card\">
  <div class=\"icon\">🎬</div>
  <h1>ভিডিও আনলক করুন</h1>
  <p>একটি বিজ্ঞাপন সম্পূর্ণ দেখুন। বিজ্ঞাপন সফলভাবে শেষ হলে ভিডিও Telegram-এ অটোমেটিক পাঠানো হবে।</p>
  <button id=\"go\">📢 বিজ্ঞাপন দেখুন</button>
  <div class=\"status\" id=\"status\"></div>
  <div class=\"small\">Ad session সুরক্ষিত ও সময়সীমাবদ্ধ।</div>
</div>
<script>
const token = {safe_token!r};
const btn = document.getElementById('go');
const statusEl = document.getElementById('status');
let running = false;
async function runAd() {{
  if (running) return; running = true; btn.disabled = true;
  statusEl.textContent = 'বিজ্ঞাপন লোড হচ্ছে...';
  try {{
    const fn = window[{safe_func!r}];
    if (typeof fn !== 'function') throw new Error('Ad SDK এখনো লোড হয়নি');
    await fn();
    statusEl.textContent = '✅ বিজ্ঞাপন সম্পূর্ণ। ভিডিও পাঠানো হচ্ছে...';
    const r = await fetch('/api/ad-complete', {{
      method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{token}})
    }});
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'Verification failed');
    statusEl.textContent = '✅ ভিডিও Telegram-এ পাঠানো হয়েছে। এখন এই পেজ বন্ধ করতে পারেন।';
    btn.style.display='none';
  }} catch (e) {{
    statusEl.textContent = '⚠️ ' + (e.message || 'Ad complete হয়নি। আবার চেষ্টা করুন।');
    btn.disabled = false; running = false;
  }}
}}
btn.addEventListener('click', runAd);
</script>
</body>
</html>"""


async def ad_page(request: web.Request):
    token = request.query.get("token", "")
    row = await db_exec(
        "SELECT status, expires_at FROM ad_sessions WHERE token=?", (token,), fetchone=True
    )
    if not row:
        return web.Response(text="Invalid ad session", status=404)
    if row["expires_at"] < int(time.time()):
        return web.Response(text="Ad session expired. Telegram-এ ফিরে গিয়ে আবার চেষ্টা করুন।", status=410)
    if row["status"] == "used":
        return web.Response(text="এই ad session ইতোমধ্যে ব্যবহার হয়েছে।", status=409)
    return web.Response(text=ad_page_html(token), content_type="text/html")


async def ad_complete(request: web.Request):
    try:
        data = await request.json()
        token = str(data.get("token", ""))
    except Exception:
        return web.json_response({"ok": False, "error": "Bad request"}, status=400)

    now = int(time.time())
    row = await db_exec(
        "SELECT * FROM ad_sessions WHERE token=?", (token,), fetchone=True
    )
    if not row:
        return web.json_response({"ok": False, "error": "Invalid session"}, status=404)
    if row["expires_at"] < now:
        return web.json_response({"ok": False, "error": "Session expired"}, status=410)
    if row["status"] == "used":
        return web.json_response({"ok": True, "already": True})

    # Atomic-ish claim: only a pending session can transition to used.
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE ad_sessions SET status='used', completed_at=?, used_at=? WHERE id=? AND status='pending'",
            (now, now, row["id"]),
        )
        await db.commit()
        if cur.rowcount != 1:
            return web.json_response({"ok": False, "error": "Session already processed"}, status=409)

    try:
        ok = await deliver_video(row["telegram_id"], row["video_id"], row["id"])
    except Exception as exc:
        # Allow retry if Telegram delivery failed unexpectedly.
        await db_exec(
            "UPDATE ad_sessions SET status='pending', used_at=NULL WHERE id=?", (row["id"],)
        )
        return web.json_response({"ok": False, "error": f"Video delivery failed: {type(exc).__name__}"}, status=500)
    return web.json_response({"ok": bool(ok)})


async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "single-video-bot"})


async def start_web_server():
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/ad", ad_page)
    app.router.add_post("/api/ad-complete", ad_complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    return runner


async def main():
    await init_db()
    runner = await start_web_server()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
