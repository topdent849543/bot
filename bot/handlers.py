from __future__ import annotations

import asyncio
import math
import re
from html import escape
from urllib.parse import urlparse, urlunparse

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Config
from bot.db import Database
from bot.states import AdminStates


TASKS_LABEL = "🟦 المهام"
WHEEL_LABEL = "🎡 دولاب الحظ"
EARNINGS_LABEL = "💰 أرباحي"
ADMIN_LABEL = "🛠 لوحة الأدمن"


def normalize_telegram_channel_url(value: str) -> str | None:
    """Accept Telegram handles and common t.me links, returning a canonical HTTPS URL."""
    candidate = value.strip().strip("<>").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", candidate):
        return f"https://t.me/{candidate[1:]}"

    if candidate.startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        candidate = f"https://{candidate}"
    elif candidate.startswith("//"):
        candidate = f"https:{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me"
    }:
        return None
    if not parsed.path or parsed.path == "/" or any(ch.isspace() for ch in parsed.path):
        return None

    # Telegram accepts HTTPS t.me links for public channels and private invite links.
    return urlunparse(("https", "t.me", parsed.path, "", parsed.query, ""))


WELCOME_TEXT = (
    "<b>أهلًا بك في بوت المكافآت 🎁</b>\n\n"
    "أكمل المهام أولًا، ثم استخدم <b>دولاب الحظ</b> حتى مرتين.\n"
    "بعدها ستجد جميع النتائج داخل قسم <b>أرباحي</b>."
)


def make_router(config: Config, db: Database) -> Router:
    router = Router(name="rewards")

    def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
        rows = [
            [KeyboardButton(text=TASKS_LABEL), KeyboardButton(text=WHEEL_LABEL)],
            [KeyboardButton(text=EARNINGS_LABEL)],
        ]
        if config.is_admin(user_id):
            rows.append([KeyboardButton(text=ADMIN_LABEL)])
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    async def allow_user(user) -> bool:
        return await db.ensure_user(user, admin_exempt=config.is_admin(user.id))

    async def require_allowed_message(message: Message) -> bool:
        if not message.from_user:
            return False
        allowed = await allow_user(message.from_user)
        if not allowed:
            await message.answer(
                "<b>تم إيقاف البوت مؤقتًا لاستقبال مستخدمين جدد.</b>\n"
                "يرجى المحاولة لاحقًا.",
                reply_markup=None,
            )
        return allowed

    async def require_allowed_callback(callback: CallbackQuery) -> bool:
        if not callback.from_user:
            return False
        allowed = await allow_user(callback.from_user)
        if not allowed:
            await callback.answer("تم إيقاف البوت للمستخدمين الجدد.", show_alert=True)
        return allowed

    async def send_or_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()

    def back_to_main_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")]]
        )

    def task_list_markup(tasks, completed: set[int]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for task in tasks:
            task_id = int(task["id"])
            state = "✅ مكتملة" if task_id in completed else "🔵 غير مكتملة"
            builder.button(
                text=f"{state} — {task['title']}",
                callback_data=f"task:detail:{task_id}",
            )
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home"))
        return builder.as_markup()

    async def show_tasks(message_or_callback, user_id: int) -> None:
        tasks = await db.get_tasks()
        if not tasks:
            text = (
                "<b>🟦 المهام</b>\n\n"
                "لا توجد مهام مضافة حاليًا. يمكنك متابعة البوت أو استخدام دولاب الحظ لاحقًا."
            )
            markup = back_to_main_markup()
        else:
            completed = await db.completed_task_ids(user_id)
            done, total = await db.task_progress(user_id)
            text = (
                "<b>🟦 قائمة المهام</b>\n\n"
                f"التقدّم: <b>{done}/{total}</b>\n"
                "اضغط على المهمة لعرض القناة وتنفيذها."
            )
            markup = task_list_markup(tasks, completed)

        if isinstance(message_or_callback, CallbackQuery):
            await send_or_edit(message_or_callback, text, markup)
        else:
            await message_or_callback.answer(text, reply_markup=markup)

    async def show_wheel(message_or_callback, user_id: int) -> None:
        done, total = await db.task_progress(user_id)
        spin_count = await db.get_spin_count(user_id)
        remaining = max(0, 2 - spin_count)

        if total > 0 and done < total:
            text = (
                "<b>🎡 دولاب الحظ مقفل</b>\n\n"
                f"يرجى إكمال المهام أولًا: <b>{done}/{total}</b> مكتملة.\n"
                "بعد إنهاء جميع المهام سيفتح لك الدولاب."
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🟦 الذهاب إلى المهام", callback_data="nav:tasks")],
                    [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")],
                ]
            )
        elif spin_count >= 2:
            text = (
                "<b>🎡 انتهت لفّاتك</b>\n\n"
                "تم استخدام اللفتين المسموحتين لهذا الحساب.\n"
                "يمكنك مراجعة النتائج من قسم <b>أرباحي</b>."
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💰 عرض أرباحي", callback_data="nav:earnings")],
                    [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")],
                ]
            )
        else:
            next_spin = spin_count + 1
            assigned = await db.get_assigned_outcome(user_id, next_spin)
            pending = "جاهزة" if assigned else "بانتظار تحديد الإدارة للنتيجة"
            text = (
                "<b>🎡 دولاب الحظ</b>\n\n"
                f"اللفّات المتبقية: <b>{remaining}</b> من 2\n"
                f"حالة اللفة القادمة: <b>{pending}</b>\n\n"
                "اضغط على الزر عندما تكون مستعدًا."
            )
            buttons = []
            if assigned:
                buttons.append([InlineKeyboardButton(text="🎲 تدوير الدولاب الآن", callback_data="wheel:spin")])
            else:
                buttons.append([InlineKeyboardButton(text="🔄 تحديث الحالة", callback_data="nav:wheel")])
            buttons.append([InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")])
            markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        if isinstance(message_or_callback, CallbackQuery):
            await send_or_edit(message_or_callback, text, markup)
        else:
            await message_or_callback.answer(text, reply_markup=markup)

    async def show_earnings(message_or_callback, user_id: int) -> None:
        rows = await db.get_earnings(user_id)
        if not rows:
            text = (
                "<b>💰 أرباحي</b>\n\n"
                "لا توجد نتائج مسجلة حتى الآن. أكمل المهام ثم جرّب دولاب الحظ."
            )
        else:
            entries = []
            for row in rows:
                entries.append(
                    f"<b>اللفة {row['spin_number']}</b>\n"
                    f"{escape(row['message'])}\n"
                    f"<i>{row['spun_at']}</i>"
                )
            text = "<b>💰 سجل أرباحي</b>\n\n" + "\n\n".join(entries)
        markup = back_to_main_markup()
        if isinstance(message_or_callback, CallbackQuery):
            await send_or_edit(message_or_callback, text, markup)
        else:
            await message_or_callback.answer(text, reply_markup=markup)

    def admin_keyboard(new_users_blocked: bool) -> InlineKeyboardMarkup:
        status = "✅ فتح تسجيل المستخدمين الجدد" if new_users_blocked else "🚫 منع المستخدمين الجدد"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ إضافة مهمة", callback_data="adm:addtask"),
                    InlineKeyboardButton(text="📋 إدارة المهام", callback_data="adm:tasks"),
                ],
                [
                    InlineKeyboardButton(text="🎁 إضافة جائزة", callback_data="adm:addprize"),
                    InlineKeyboardButton(text="🎁 إدارة الجوائز", callback_data="adm:prizes"),
                ],
                [InlineKeyboardButton(text="👥 عرض المستخدمين", callback_data="adm:users:0")],
                [InlineKeyboardButton(text=status, callback_data="adm:toggle_new")],
                [InlineKeyboardButton(text="📊 الإحصاءات", callback_data="adm:stats")],
                [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")],
            ]
        )

    async def show_admin(message_or_callback) -> None:
        blocked = await db.new_users_blocked()
        text = (
            "<b>🛠 لوحة تحكم الإدارة</b>\n\n"
            "من هنا يمكنك إدارة المهام، الجوائز، المستخدمين ونتائج دولاب الحظ.\n"
            f"استقبال مستخدمين جدد: <b>{'موقوف ⛔' if blocked else 'مفعّل ✅'}</b>"
        )
        markup = admin_keyboard(blocked)
        if isinstance(message_or_callback, CallbackQuery):
            await send_or_edit(message_or_callback, text, markup)
        else:
            await message_or_callback.answer(text, reply_markup=markup)

    async def admin_only_callback(callback: CallbackQuery) -> bool:
        if not config.is_admin(callback.from_user.id):
            await callback.answer("هذه اللوحة مخصصة للإدارة فقط.", show_alert=True)
            return False
        return True

    async def admin_only_message(message: Message) -> bool:
        if not message.from_user or not config.is_admin(message.from_user.id):
            await message.answer("هذه اللوحة مخصصة للإدارة فقط.")
            return False
        return True

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message):
            return
        await state.clear()
        await message.answer(WELCOME_TEXT, reply_markup=main_keyboard(message.from_user.id))

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not await require_allowed_message(message):
            return
        await message.answer(
            "<b>طريقة الاستخدام</b>\n\n"
            "1. افتح قسم المهام وأكمل كل مهمة.\n"
            "2. بعد اكتمالها يفتح دولاب الحظ.\n"
            "3. لكل حساب لفّتان فقط.\n"
            "4. راجع النتائج من قسم أرباحي.",
            reply_markup=main_keyboard(message.from_user.id),
        )

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        if not await require_allowed_message(message):
            return
        if not await admin_only_message(message):
            return
        await show_admin(message)

    @router.message(Command("cancel"))
    async def cancel_admin_flow(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message):
            return
        if not config.is_admin(message.from_user.id):
            return
        if await state.get_state() is not None:
            await state.clear()
            await message.answer("تم إلغاء العملية الحالية.", reply_markup=main_keyboard(message.from_user.id))
        else:
            await message.answer("لا توجد عملية قيد التنفيذ.")

    @router.message(F.text == TASKS_LABEL)
    async def tasks_from_menu(message: Message) -> None:
        if await require_allowed_message(message):
            await show_tasks(message, message.from_user.id)

    @router.message(F.text == WHEEL_LABEL)
    async def wheel_from_menu(message: Message) -> None:
        if await require_allowed_message(message):
            await show_wheel(message, message.from_user.id)

    @router.message(F.text == EARNINGS_LABEL)
    async def earnings_from_menu(message: Message) -> None:
        if await require_allowed_message(message):
            await show_earnings(message, message.from_user.id)

    @router.message(F.text == ADMIN_LABEL)
    async def admin_from_menu(message: Message) -> None:
        if not await require_allowed_message(message):
            return
        if await admin_only_message(message):
            await show_admin(message)

    @router.callback_query(F.data == "nav:home")
    async def home_callback(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback):
            return
        await send_or_edit(callback, WELCOME_TEXT, None)

    @router.callback_query(F.data == "nav:tasks")
    async def tasks_callback(callback: CallbackQuery) -> None:
        if await require_allowed_callback(callback):
            await show_tasks(callback, callback.from_user.id)

    @router.callback_query(F.data == "nav:wheel")
    async def wheel_callback(callback: CallbackQuery) -> None:
        if await require_allowed_callback(callback):
            await show_wheel(callback, callback.from_user.id)

    @router.callback_query(F.data == "nav:earnings")
    async def earnings_callback(callback: CallbackQuery) -> None:
        if await require_allowed_callback(callback):
            await show_earnings(callback, callback.from_user.id)

    @router.callback_query(F.data.startswith("task:detail:"))
    async def task_detail(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback):
            return
        try:
            task_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة المهمة.", show_alert=True)
            return
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("هذه المهمة لم تعد متاحة.", show_alert=True)
            return
        completed = task_id in await db.completed_task_ids(callback.from_user.id)
        progress_text = "<b>✅ تم تنفيذ هذه المهمة مسبقًا.</b>" if completed else "<b>الحالة: غير مكتملة</b>"
        text = (
            f"<b>🟦 {escape(task['title'])}</b>\n\n"
            "اضغط زر الاشتراك لتسجيل المهمة فورًا، ثم سيظهر زر مباشر لفتح القناة.\n"
            "لا يقوم البوت بالتحقق من العضوية، بحسب إعداد البوت المطلوب.\n\n"
            f"{progress_text}"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📣 اشترك بالقناة", callback_data=f"task:join:{task_id}")],
                [InlineKeyboardButton(text="⬅️ رجوع للمهام", callback_data="nav:tasks")],
            ]
        )
        await send_or_edit(callback, text, markup)

    @router.callback_query(F.data.startswith("task:join:"))
    async def task_join_and_complete(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback):
            return
        try:
            task_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة المهمة.", show_alert=True)
            return
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("هذه المهمة لم تعد متاحة.", show_alert=True)
            return
        newly_done = await db.complete_task(callback.from_user.id, task_id)
        done, total = await db.task_progress(callback.from_user.id)
        await callback.answer("تم تسجيل المهمة بنجاح.")
        text = (
            f"<b>{'🎉 تم تنفيذ المهمة بنجاح!' if newly_done else '✅ المهمة مسجلة بالفعل.'}</b>\n\n"
            f"المهمة: <b>{escape(task['title'])}</b>\n"
            f"تقدمك: <b>{done}/{total}</b>\n\n"
            "اضغط أدناه لفتح القناة."
        )
        buttons = [
            [InlineKeyboardButton(text="📣 فتح القناة", url=task["channel_url"])],
            [InlineKeyboardButton(text="🟦 متابعة المهام", callback_data="nav:tasks")],
        ]
        if done == total:
            buttons.insert(0, [InlineKeyboardButton(text="🎡 فتح دولاب الحظ", callback_data="nav:wheel")])
        buttons.append([InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="nav:home")])
        if callback.message:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data == "wheel:spin")
    async def spin_wheel(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback):
            return
        done, total = await db.task_progress(callback.from_user.id)
        if total > 0 and done < total:
            await callback.answer("يرجى إكمال كل المهام أولًا.", show_alert=True)
            await show_wheel(callback, callback.from_user.id)
            return

        current_count = await db.get_spin_count(callback.from_user.id)
        if current_count >= 2:
            await callback.answer("استخدمت اللفتين المسموحتين.", show_alert=True)
            await show_wheel(callback, callback.from_user.id)
            return
        if not await db.get_assigned_outcome(callback.from_user.id, current_count + 1):
            await callback.answer("النتيجة لم تُحدد من الإدارة بعد.", show_alert=True)
            await show_wheel(callback, callback.from_user.id)
            return

        await callback.answer("يتم تدوير الدولاب...")
        if callback.message:
            await callback.message.edit_text(
                "<b>🎡 جاري تدوير دولاب الحظ...</b>\n\n"
                "ثوانٍ قليلة ويظهر لك الحظ!"
            )
            await callback.message.answer_dice(emoji="🎲")
            await asyncio.sleep(1.8)

        result = await db.spin(callback.from_user.id)
        if result is None:
            if callback.message:
                await callback.message.answer(
                    "تعذر تنفيذ اللفة الآن. يرجى المحاولة من جديد.",
                    reply_markup=back_to_main_markup(),
                )
            return

        text = (
            "<b>✨ انتهى الدور!</b>\n\n"
            f"<b>نتيجة اللفة {result.spin_number}:</b>\n"
            f"{escape(result.message)}\n\n"
            "يمكنك مشاهدة هذه النتيجة في قسم <b>أرباحي</b>."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💰 عرض أرباحي", callback_data="nav:earnings")],
                [InlineKeyboardButton(text="🎡 العودة للدولاب", callback_data="nav:wheel")],
            ]
        )
        if callback.message:
            await callback.message.answer(text, reply_markup=markup)

    # ------------------------ Admin panel ------------------------

    @router.callback_query(F.data == "adm:home")
    async def admin_home_callback(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        await show_admin(callback)

    @router.callback_query(F.data == "adm:addtask")
    async def add_task_begin(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        await state.clear()
        await state.set_state(AdminStates.task_title)
        await send_or_edit(
            callback,
            "<b>➕ إضافة مهمة جديدة</b>\n\n"
            "أرسل الآن اسم المهمة الظاهر للمستخدم.\n"
            "مثال: <code>اشترك في قناة العروض</code>\n\n"
            "للإلغاء أرسل /cancel",
        )

    @router.message(AdminStates.task_title)
    async def receive_task_title(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message) or not await admin_only_message(message):
            return
        title = (message.text or "").strip()
        if not title or len(title) > 120:
            await message.answer("أرسل اسم مهمة بين 1 و120 حرفًا.")
            return
        await state.update_data(task_title=title)
        await state.set_state(AdminStates.task_url)
        await message.answer(
            "أرسل رابط قناة Telegram الآن. يقبل الرابط الكامل أو المختصر أو اسم القناة.\n"
            "أمثلة: <code>https://t.me/my_channel</code> أو <code>t.me/my_channel</code> أو <code>@my_channel</code>\n\n"
            "للإلغاء أرسل /cancel"
        )

    @router.message(AdminStates.task_url)
    async def receive_task_url(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message) or not await admin_only_message(message):
            return
        raw_url = (message.text or "").strip()
        url = normalize_telegram_channel_url(raw_url)
        if not url:
            await message.answer(
                "ما قدرت أتعرف على رابط القناة. أرسل رابط Telegram مثل "
                "<code>https://t.me/my_channel</code> أو <code>t.me/my_channel</code> "
                "أو اسم المستخدم <code>@my_channel</code>."
            )
            return
        data = await state.get_data()
        task_id = await db.add_task(data["task_title"], url)
        await state.clear()
        await message.answer(
            f"<b>✅ تمت إضافة المهمة رقم {task_id}</b>\n\n"
            f"العنوان: {escape(data['task_title'])}\n"
            f"الرابط: {escape(url)}",
            reply_markup=main_keyboard(message.from_user.id),
        )
        await show_admin(message)

    @router.callback_query(F.data == "adm:tasks")
    async def manage_tasks(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        tasks = await db.get_tasks()
        builder = InlineKeyboardBuilder()
        if not tasks:
            text = "<b>📋 إدارة المهام</b>\n\nلا توجد مهام فعالة حاليًا."
        else:
            text = "<b>📋 إدارة المهام</b>\n\nاضغط على المهمة لحذفها من الواجهة للمستخدمين."
            for task in tasks:
                builder.button(text=f"🗑 {task['title']}", callback_data=f"adm:deltask:{task['id']}")
            builder.adjust(1)
        builder.row(InlineKeyboardButton(text="➕ إضافة مهمة", callback_data="adm:addtask"))
        builder.row(InlineKeyboardButton(text="⬅️ رجوع للإدارة", callback_data="adm:home"))
        await send_or_edit(callback, text, builder.as_markup())

    @router.callback_query(F.data.startswith("adm:deltask:"))
    async def delete_task(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            task_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة المهمة.", show_alert=True)
            return
        task = await db.get_task(task_id)
        if not task:
            await callback.answer("هذه المهمة غير موجودة.", show_alert=True)
            return
        await db.deactivate_task(task_id)
        await callback.answer("تم حذف المهمة من القائمة.")
        await manage_tasks(callback)

    @router.callback_query(F.data == "adm:addprize")
    async def add_prize_begin(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        await state.clear()
        await state.set_state(AdminStates.prize_name)
        await send_or_edit(
            callback,
            "<b>🎁 إضافة جائزة</b>\n\n"
            "أرسل اسم الجائزة.\n"
            "مثال: <code>بطاقة فيزا بقيمة 10$</code>\n\n"
            "للإلغاء أرسل /cancel",
        )

    @router.message(AdminStates.prize_name)
    async def receive_prize_name(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message) or not await admin_only_message(message):
            return
        name = (message.text or "").strip()
        if not name or len(name) > 120:
            await message.answer("أرسل اسم جائزة بين 1 و120 حرفًا.")
            return
        await state.update_data(prize_name=name)
        await state.set_state(AdminStates.prize_description)
        await message.answer(
            "أرسل وصف الجائزة أو رابط استلامها.\n"
            "يمكنك إرسال عدة أسطر.\n\n"
            "للإلغاء أرسل /cancel"
        )

    @router.message(AdminStates.prize_description)
    async def receive_prize_description(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message) or not await admin_only_message(message):
            return
        description = (message.text or "").strip()
        if not description or len(description) > 1500:
            await message.answer("أرسل وصفًا بين 1 و1500 حرف.")
            return
        data = await state.get_data()
        prize_id = await db.add_prize(data["prize_name"], description)
        await state.clear()
        await message.answer(
            f"<b>✅ تمت إضافة الجائزة رقم {prize_id}</b>\n\n"
            f"<b>{escape(data['prize_name'])}</b>\n{escape(description)}",
            reply_markup=main_keyboard(message.from_user.id),
        )
        await show_admin(message)

    @router.callback_query(F.data == "adm:prizes")
    async def manage_prizes(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        prizes = await db.get_prizes()
        builder = InlineKeyboardBuilder()
        if not prizes:
            text = "<b>🎁 إدارة الجوائز</b>\n\nلا توجد جوائز فعالة حاليًا."
        else:
            text = "<b>🎁 إدارة الجوائز</b>\n\nاضغط على الجائزة لحذفها من قائمة الاختيار."
            for prize in prizes:
                builder.button(text=f"🗑 {prize['name']}", callback_data=f"adm:delprize:{prize['id']}")
            builder.adjust(1)
        builder.row(InlineKeyboardButton(text="➕ إضافة جائزة", callback_data="adm:addprize"))
        builder.row(InlineKeyboardButton(text="⬅️ رجوع للإدارة", callback_data="adm:home"))
        await send_or_edit(callback, text, builder.as_markup())

    @router.callback_query(F.data.startswith("adm:delprize:"))
    async def delete_prize(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            prize_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة الجائزة.", show_alert=True)
            return
        prize = await db.get_prize(prize_id)
        if not prize:
            await callback.answer("هذه الجائزة غير موجودة.", show_alert=True)
            return
        await db.deactivate_prize(prize_id)
        await callback.answer("تم حذف الجائزة من القائمة.")
        await manage_prizes(callback)

    @router.callback_query(F.data == "adm:toggle_new")
    async def toggle_new_users(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        currently_blocked = await db.new_users_blocked()
        await db.set_setting("new_users_blocked", "0" if currently_blocked else "1")
        await callback.answer("تم تحديث حالة استقبال المستخدمين الجدد.")
        await show_admin(callback)

    @router.callback_query(F.data == "adm:stats")
    async def admin_stats(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        stats = await db.stats()
        text = (
            "<b>📊 إحصاءات البوت</b>\n\n"
            f"👥 المستخدمون: <b>{stats['users']}</b>\n"
            f"🟦 المهام الفعالة: <b>{stats['tasks']}</b>\n"
            f"🎁 الجوائز الفعالة: <b>{stats['prizes']}</b>\n"
            f"🎲 اللفات المنفذة: <b>{stats['spins']}</b>"
        )
        await send_or_edit(
            callback,
            text,
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ رجوع للإدارة", callback_data="adm:home")]]),
        )

    async def show_users(callback: CallbackQuery, page: int) -> None:
        users, total = await db.list_users(page=page)
        per_page = 8
        pages = max(1, math.ceil(total / per_page))
        current_page = min(max(page, 0), pages - 1)
        if current_page != page:
            users, total = await db.list_users(page=current_page)
        builder = InlineKeyboardBuilder()
        if not users:
            text = "<b>👥 المستخدمون</b>\n\nلا يوجد مستخدمون حتى الآن."
        else:
            text = f"<b>👥 المستخدمون ({total})</b>\n\nاختر مستخدمًا لتحديد نتائج لفّاته."
            for user in users:
                username = f"@{user['username']}" if user['username'] else "بدون اسم مستخدم"
                display_name = (user['first_name'] or "مستخدم").strip()
                label = f"👤 {display_name} — {username}"
                builder.button(text=label[:60], callback_data=f"adm:user:{user['user_id']}")
            builder.adjust(1)
        nav = []
        if current_page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ السابق", callback_data=f"adm:users:{current_page - 1}"))
        if current_page < pages - 1:
            nav.append(InlineKeyboardButton(text="التالي ➡️", callback_data=f"adm:users:{current_page + 1}"))
        if nav:
            builder.row(*nav)
        builder.row(InlineKeyboardButton(text="⬅️ رجوع للإدارة", callback_data="adm:home"))
        await send_or_edit(callback, text, builder.as_markup())

    @router.callback_query(F.data.startswith("adm:users:"))
    async def users_callback(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            page = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            page = 0
        await show_users(callback, page)

    async def show_user_card(callback: CallbackQuery, user_id: int) -> None:
        user = await db.get_user(user_id)
        if not user:
            await callback.answer("المستخدم غير موجود.", show_alert=True)
            return
        count = await db.get_spin_count(user_id)
        assigned1 = await db.get_assigned_outcome(user_id, 1)
        assigned2 = await db.get_assigned_outcome(user_id, 2)
        username = f"@{user['username']}" if user['username'] else "غير متاح"
        name = escape(" ".join(part for part in [user['first_name'], user['last_name']] if part).strip() or "مستخدم")
        spin1 = "منفذة ✅" if count >= 1 else ("محددة 🎯" if assigned1 else "غير محددة")
        spin2 = "منفذة ✅" if count >= 2 else ("محددة 🎯" if assigned2 else "غير محددة")
        text = (
            "<b>👤 بيانات المستخدم</b>\n\n"
            f"الاسم: <b>{name}</b>\n"
            f"المعرف: <code>{user_id}</code>\n"
            f"اسم المستخدم: {escape(username)}\n\n"
            f"اللفة الأولى: <b>{spin1}</b>\n"
            f"اللفة الثانية: <b>{spin2}</b>"
        )
        buttons = []
        if count < 1:
            buttons.append([InlineKeyboardButton(text="🎯 حدّد نتيجة اللفة الأولى", callback_data=f"adm:assign:{user_id}:1")])
        if count < 2:
            buttons.append([InlineKeyboardButton(text="🎯 حدّد نتيجة اللفة الثانية", callback_data=f"adm:assign:{user_id}:2")])
        buttons.append([InlineKeyboardButton(text="⬅️ المستخدمون", callback_data="adm:users:0")])
        await send_or_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("adm:user:"))
    async def user_card_callback(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            user_id = int(callback.data.rsplit(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة المستخدم.", show_alert=True)
            return
        await show_user_card(callback, user_id)

    @router.callback_query(F.data.startswith("adm:assign:"))
    async def assign_outcome_menu(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            _, _, user_id_raw, spin_raw = callback.data.split(":")
            user_id, spin_number = int(user_id_raw), int(spin_raw)
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة بيانات اللفة.", show_alert=True)
            return
        if await db.get_spin_count(user_id) >= spin_number:
            await callback.answer("هذه اللفة تم تنفيذها ولا يمكن تغييرها.", show_alert=True)
            await show_user_card(callback, user_id)
            return
        prizes = await db.get_prizes()
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ كتابة نتيجة مخصصة", callback_data=f"adm:custom:{user_id}:{spin_number}")
        for prize in prizes:
            builder.button(
                text=f"🎁 {prize['name']}",
                callback_data=f"adm:pick:{user_id}:{spin_number}:{prize['id']}",
            )
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="⬅️ رجوع للمستخدم", callback_data=f"adm:user:{user_id}"))
        text = (
            f"<b>🎯 تحديد نتيجة اللفة {spin_number}</b>\n\n"
            "اختر جائزة محفوظة أو اكتب نتيجة مخصصة مثل: <i>حظ أوفر في المرة القادمة</i>."
        )
        await send_or_edit(callback, text, builder.as_markup())

    @router.callback_query(F.data.startswith("adm:custom:"))
    async def custom_outcome_begin(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            _, _, user_id_raw, spin_raw = callback.data.split(":")
            user_id, spin_number = int(user_id_raw), int(spin_raw)
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة بيانات اللفة.", show_alert=True)
            return
        await state.clear()
        await state.update_data(target_user_id=user_id, target_spin=spin_number)
        await state.set_state(AdminStates.custom_outcome)
        await send_or_edit(
            callback,
            f"<b>✍️ نتيجة مخصصة للفة {spin_number}</b>\n\n"
            "أرسل النص الذي يظهر للمستخدم بعد الدوران.\n"
            "مثال: <code>مبروك! ربحت بطاقة فيزا بقيمة 10$\nرابط الاستلام: https://example.com</code>\n\n"
            "للإلغاء أرسل /cancel",
        )

    @router.message(AdminStates.custom_outcome)
    async def receive_custom_outcome(message: Message, state: FSMContext) -> None:
        if not await require_allowed_message(message) or not await admin_only_message(message):
            return
        outcome = (message.text or "").strip()
        if not outcome or len(outcome) > 1800:
            await message.answer("أرسل نتيجة بين 1 و1800 حرف.")
            return
        data = await state.get_data()
        user_id = int(data["target_user_id"])
        spin_number = int(data["target_spin"])
        if await db.get_spin_count(user_id) >= spin_number:
            await state.clear()
            await message.answer("هذه اللفة نُفذت بالفعل، لا يمكن تغييرها.")
            return
        await db.assign_outcome(user_id, spin_number, outcome, message.from_user.id)
        await state.clear()
        await message.answer(
            f"<b>✅ تم تحديد نتيجة اللفة {spin_number}</b>\n\n{escape(outcome)}",
            reply_markup=main_keyboard(message.from_user.id),
        )

    @router.callback_query(F.data.startswith("adm:pick:"))
    async def pick_prize_outcome(callback: CallbackQuery) -> None:
        if not await require_allowed_callback(callback) or not await admin_only_callback(callback):
            return
        try:
            _, _, user_id_raw, spin_raw, prize_id_raw = callback.data.split(":")
            user_id, spin_number, prize_id = int(user_id_raw), int(spin_raw), int(prize_id_raw)
        except (ValueError, AttributeError):
            await callback.answer("تعذر قراءة الاختيار.", show_alert=True)
            return
        if await db.get_spin_count(user_id) >= spin_number:
            await callback.answer("هذه اللفة تم تنفيذها ولا يمكن تغييرها.", show_alert=True)
            return
        prize = await db.get_prize(prize_id)
        if not prize:
            await callback.answer("الجائزة لم تعد متاحة.", show_alert=True)
            return
        outcome = f"🎉 مبروك! لقد ربحت: {prize['name']}\n\n{prize['description']}"
        await db.assign_outcome(user_id, spin_number, outcome, callback.from_user.id)
        await callback.answer("تم تحديد الجائزة لهذه اللفة.")
        await show_user_card(callback, user_id)

    return router
