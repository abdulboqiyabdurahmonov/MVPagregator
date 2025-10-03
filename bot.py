# -*- coding: utf-8 -*-
"""
TripleA Feedback Bot — Telegram bot for collecting structured partner feedback.
Stack: FastAPI (webhook), Aiogram v3, gspread (Google Sheets), Render-ready.

ENV VARS REQUIRED
-----------------
BOT_TOKEN=...                           # Telegram bot token
WEBHOOK_SECRET=supersecret              # secret token to verify webhook
WEBHOOK_URL=https://your-service.onrender.com/webhook
SHEET_ID=...                            # Google Sheet spreadsheet ID
GOOGLE_SERVICE_ACCOUNT_JSON=...         # raw JSON of service account (one line)

OPTIONAL
--------
ADMINS=123456789,987654321              # comma-separated Telegram user IDs to receive alerts
LOCALE=ru                               # default locale text (ru/uz)
"""

import os
import json
import logging
import asyncio
import time
from collections import Counter
import re
from typing import Dict, Any, Optional, Tuple
from html import escape as html_escape
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User
)

import gspread
from gspread.exceptions import APIError

# --------------- Config & Globals ---------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()]
DEFAULT_LOCALE = os.getenv("LOCALE", "ru").lower().strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not SHEET_ID:
    raise RuntimeError("SHEET_ID is required")
if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is required")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is required")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("feedback-bot")

# --------------- i18n ---------------

TXT: Dict[str, Dict[str, str]] = {
    "ru": {
        "choose_lang": "Выбери язык интерфейса:",
        "lang_ru": "🇷🇺 Русский",
        "lang_uz": "🇺🇿 O‘zbekcha",
        "hello": "Привет! Это тестовый бот для партнёров автопроката.\n"
                 "Помоги нам улучшить агрегатор — ответь на 5 быстрых вопросов (2–3 минуты).",
        "start_btn": "Начать опрос",
        "cancel": "Отменить",
        "thanks": "Спасибо! Ответы сохранены. 🎉\nЕсли готовы — напишите, созвонимся по деталям.",
        "err": "Ой! Что-то пошло не так. Попробуй ещё раз /start",
        "q1": "1/5. Сколько времени ушло на регистрацию и добавление первой машины?\n\n"
              "Можно нажать кнопку или написать свой вариант.",
        "q1_opt1": "до 15 минут",
        "q1_opt2": "15–30 минут",
        "q1_opt3": "более 30 минут",
        "q2": "2/5. Насколько понятны статусы заявок и уведомления?\n\n"
              "Оцени по шкале 1–10 (где 10 — идеально). Можно ввести число вручную.",
        "q3": "3/5. Что показалось неудобным? (свободный ответ)",
        "q4": "4/5. Каких функций не хватает в первую очередь? (например: онлайн-оплата, шаблоны цен, импорт)",
        "q5": "5/5. Готовы ли рекомендовать коллегам? Укажи оценку 1–10.\nМожно нажать кнопку или ввести число.",
        "ask_company": "Укажи название компании (как у вас в Telegram/Instagram/юр. название)",
        "done": "Готово ✅",
        "back": "⬅️ Назад",
        "skip": "Пропустить",
        "change_lang_hint": "Чтобы сменить язык позже, используй /lang",
        "lang_switched": "Язык переключён.",
        "form_started": "Погнали! Сначала уточним компанию:",
        "diag_ok": "Диагностика OK: запись в таблицу работает.",
        "diag_fail": "Диагностика: запись в таблицу не удалась.",
        "no_data": "Пока нет данных для статистики.",
        "stats_title": "Сводка по ответам",
        "stats_n": "Всего ответов: {n}",
        "stats_q1_dist": "Q1 — время на старт:\n{dist}",
        "stats_avg": "Средние значения:\n• Q2 (понятность статусов): {avg_q2}\n• Q5 (NPS): {avg_q5}",
        "stats_top_keywords": "Топ слов из свободных полей (Q3+Q4):\n{words}",
    },
    "uz": {
        "choose_lang": "Интерфейс тилини танланг:",
        "lang_ru": "🇷🇺 Русча",
        "lang_uz": "🇺🇿 O‘zbekcha",
        "hello": "Салом! Бу тест бот — автопрокат ҳамкорлари учун.\n"
                 "Агрегаторни яхшилашга ёрдам беринг: 5 та қисқа савол (2–3 дақиқа).",
        "start_btn": "Сўровномани бошлаш",
        "cancel": "Бекор қилиш",
        "thanks": "Раҳмат! Жавоблар сақланди. 🎉",
        "err": "Уй! Нимадир хато. Қайта /start қилинг.",
        "q1": "1/5. Рўйхатдан ўтиш ва биринчи машинани қўшишга қанча вақт кетди?\n\n"
              "Кнопкани босинг ёки ўз вариантини ёзинг.",
        "q1_opt1": "15 дақиқагача",
        "q1_opt2": "15–30 дақиқа",
        "q1_opt3": "30 дақиқадан кўпроқ",
        "q2": "2/5. Аризалар статуслари ва хабарномалар қай даражада тушунарли?\n1–10 баҳоланг (қўлдан ёзиш мумкин).",
        "q3": "3/5. Нима ноқулай туюлди? (эркин жавоб)",
        "q4": "4/5. Қайси функциялар етишмайди? (масалан: онлайн тўлов, нарх шаблонлари, импорт)",
        "q5": "5/5. Ҳамкасбларга тавсия қиласизми? 1–10 баҳоланг (кнопка ёки рақам).",
        "ask_company": "Компания номини киритинг (TG/Instagram/ёки юр. ном)",
        "done": "Тайёр ✅",
        "back": "⬅️ Орқага",
        "skip": "Ўтказиб юбориш",
        "change_lang_hint": "Кейинроқ тилни /lang орқали ўзгартиришингиз мумкин.",
        "lang_switched": "Тил ўзгартирилди.",
        "form_started": "Бошладик! Аввало компания номини аниқлаймиз:",
        "diag_ok": "Диагностика OK: жадвалга ёзиш ишлаяпти.",
        "diag_fail": "Диагностика: жадвалга ёзиш муваффақиятсиз.",
        "no_data": "Ҳали статистика учун маълумот йўқ.",
        "stats_title": "Жавоблар бўйича қисқа ҳисобот",
        "stats_n": "Жами жавоблар: {n}",
        "stats_q1_dist": "Q1 — стартга кетган вақт:\n{dist}",
        "stats_avg": "Ўртача қийматлар:\n• Q2 (статус тушунарлилиги): {avg_q2}\n• Q5 (NPS): {avg_q5}",
        "stats_top_keywords": "Эркин жавоблардан калит сўзлар (Q3+Q4):\n{words}",
    }
}

# ----------- Google Sheets helpers (feedback + users) -----------

def _open_spreadsheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    gc = gspread.service_account_from_dict(info)
    return gc.open_by_key(SHEET_ID)

def _get_or_create_ws(sh, title: str, headers: Optional[list] = None):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=20)
        if headers:
            ws.append_row(headers, value_input_option="RAW")
    return ws

_SPREAD = _open_spreadsheet()
WS_FEEDBACK = _get_or_create_ws(_SPREAD, "feedback", [
    "timestamp", "user_id", "username", "full_name", "company",
    "q1_time_to_setup", "q2_statuses_score", "q3_what_inconvenient",
    "q4_missing_features", "q5_nps_recommend", "raw_json"
])
WS_USERS = _get_or_create_ws(_SPREAD, "users", ["user_id", "lang", "updated_at"])

# ---------- Async wrappers for blocking gspread ----------

async def _io_to_sheets(fn, *args, timeout: float = 6.0, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)

async def append_feedback_row(user: User, data: Dict[str, Any]) -> bool:
    row = [
        datetime.now(timezone.utc).astimezone().isoformat(),
        user.id,
        user.username or "",
        f"{user.first_name or ''} {user.last_name or ''}".strip(),
        data.get("company", ""),
        data.get("q1", ""),
        data.get("q2", ""),
        data.get("q3", ""),
        data.get("q4", ""),
        data.get("q5", ""),
        json.dumps(data, ensure_ascii=False),
    ]
    for attempt in range(1, 4):
        try:
            spread = await _io_to_sheets(_open_spreadsheet)
            ws = _get_or_create_ws(spread, "feedback", [
                "timestamp", "user_id", "username", "full_name", "company",
                "q1_time_to_setup", "q2_statuses_score", "q3_what_inconvenient",
                "q4_missing_features", "q5_nps_recommend", "raw_json"
            ])
            await _io_to_sheets(ws.append_row, row, value_input_option="USER_ENTERED")
            log.info("Sheets append OK (attempt %s)", attempt)
            return True
        except (APIError, asyncio.TimeoutError) as e:
            log.warning("Sheets append API/Timeout (attempt %s/3): %s", attempt, e)
        except Exception as e:
            log.warning("Sheets append error (attempt %s/3): %s", attempt, e)
        await asyncio.sleep(0.7 * attempt)
    return False

async def fetch_feedback_records() -> list[dict]:
    """Возвращает список словарей по листу 'feedback' (может быть пустым)."""
    spread = await _io_to_sheets(_open_spreadsheet)
    ws = _get_or_create_ws(spread, "feedback", [
        "timestamp", "user_id", "username", "full_name", "company",
        "q1_time_to_setup", "q2_statuses_score", "q3_what_inconvenient",
        "q4_missing_features", "q5_nps_recommend", "raw_json"
    ])
    # get_all_records медленный — но безопасный; запускаем вне евент-лупа
    return await _io_to_sheets(ws.get_all_records, head=1, default_blank="")

# ----------- Persistent language store ------------

_lang_cache: Dict[int, str] = {}

async def set_user_lang(user_id: int, lang: str):
    lang = "uz" if lang == "uz" else "ru"
    _lang_cache[user_id] = lang
    try:
        spread = await _io_to_sheets(_open_spreadsheet)
        ws = _get_or_create_ws(spread, "users", ["user_id", "lang", "updated_at"])
        cell = await _io_to_sheets(ws.find, str(user_id))
        if cell:
            await _io_to_sheets(ws.update_cell, cell.row, 2, lang)
            await _io_to_sheets(ws.update_cell, cell.row, 3, datetime.now(timezone.utc).astimezone().isoformat())
            return
    except Exception:
        pass
    try:
        spread = await _io_to_sheets(_open_spreadsheet)
        ws = _get_or_create_ws(spread, "users", ["user_id", "lang", "updated_at"])
        await _io_to_sheets(
            ws.append_row,
            [str(user_id), lang, datetime.now(timezone.utc).astimezone().isoformat()],
            value_input_option="USER_ENTERED",
        )
    except Exception as e:
        log.warning("Failed to persist user lang: %s", e)

async def get_user_lang_persist(user_id: int) -> Optional[str]:
    try:
        spread = await _io_to_sheets(_open_spreadsheet)
        ws = _get_or_create_ws(spread, "users", ["user_id", "lang", "updated_at"])
        cell = await _io_to_sheets(ws.find, str(user_id))
        if cell:
            lang = (await _io_to_sheets(ws.cell, cell.row, 2)).value or ""
            lang = lang.strip().lower()
            if lang in ("ru", "uz"):
                _lang_cache[user_id] = lang
                return lang
    except Exception:
        return None
    return None

def get_lang(user_id: Optional[int]) -> str:
    if not user_id:
        return "uz" if DEFAULT_LOCALE == "uz" else "ru"
    if user_id in _lang_cache:
        return _lang_cache[user_id]
    return "uz" if DEFAULT_LOCALE == "uz" else "ru"

def t(user_id: Optional[int], key: str) -> str:
    lang = get_lang(user_id)
    return TXT.get(lang, TXT["ru"]).get(key, key)

# ---------- Safe sender (HTML → escaped HTML → plain) ----------

async def send_text_safe(message: Message, user_id: Optional[int], key: str):
    txt = t(user_id, key)
    try:
        return await message.answer(txt)
    except TelegramBadRequest as e1:
        log.warning("HTML send failed for key=%s: %s; fallback to escaped", key, e1)
        try:
            return await message.answer(html_escape(txt), parse_mode="HTML")
        except Exception as e2:
            log.warning("Escaped HTML send failed for key=%s: %s; fallback to plain", key, e2)
            return await message.answer(txt, parse_mode=None)

# --------------- Bot & FSM ---------------

router = Router()

class Form(StatesGroup):
    company = State()
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()

# ---------- Keyboards (selection + nav) ----------

def nav_row(user_id: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=t(user_id, "back"), callback_data="nav:back"),
        InlineKeyboardButton(text=t(user_id, "skip"), callback_data="nav:skip"),
    ]

def kb_q1(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(user_id, "q1_opt1"), callback_data="ans:q1:opt1")],
        [InlineKeyboardButton(text=t(user_id, "q1_opt2"), callback_data="ans:q1:opt2")],
        [InlineKeyboardButton(text=t(user_id, "q1_opt3"), callback_data="ans:q1:opt3")],
        nav_row(user_id),
    ])

def kb_scale(user_id: int, question_key: str) -> InlineKeyboardMarkup:
    nums = [str(i) for i in range(1, 11)]
    row1 = [InlineKeyboardButton(text=n, callback_data=f"ans:{question_key}:{n}") for n in nums[:5]]
    row2 = [InlineKeyboardButton(text=n, callback_data=f"ans:{question_key}:{n}") for n in nums[5:]]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, nav_row(user_id)])

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TXT["ru"]["lang_ru"], callback_data="lang_ru"),
         InlineKeyboardButton(text=TXT["uz"]["lang_uz"], callback_data="lang_uz")]
    ])

def start_keyboard(user_id: Optional[int]) -> InlineKeyboardMarkup:
    lang = get_lang(user_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(user_id, "start_btn"), callback_data=f"start_form:{lang}")]]
    )

# ---------- Helpers for flow ----------

async def ask_next(message: Message, user_id: int, next_state: State):
    if next_state is Form.q1:
        await send_text_safe(message, user_id, "q1")
        await message.answer(".", reply_markup=kb_q1(user_id))
    elif next_state is Form.q2:
        await send_text_safe(message, user_id, "q2")
        await message.answer(".", reply_markup=kb_scale(user_id, "q2"))
    elif next_state is Form.q3:
        await send_text_safe(message, user_id, "q3")
    elif next_state is Form.q4:
        await send_text_safe(message, user_id, "q4")
    elif next_state is Form.q5:
        await send_text_safe(message, user_id, "q5")
        await message.answer(".", reply_markup=kb_scale(user_id, "q5"))

def prev_state_of(state: State) -> Optional[State]:
    order = [Form.company, Form.q1, Form.q2, Form.q3, Form.q4, Form.q5]
    try:
        i = order.index(state)
        return order[i-1] if i > 0 else None
    except ValueError:
        return None

# ---------- /start, /lang, /cancel ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t(message.from_user.id, "choose_lang"), reply_markup=lang_keyboard())

@router.message(Command("lang"))
async def cmd_lang(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t(message.from_user.id, "choose_lang"), reply_markup=lang_keyboard())

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(t(message.from_user.id, "done"))

# ---------- Language choice ----------

@router.callback_query(F.data.in_(("lang_ru", "lang_uz")))
async def cb_lang(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    try:
        await call.answer(
            TXT["ru"]["lang_switched"] if call.data.endswith("ru") else TXT["uz"]["lang_switched"]
        )
    except Exception:
        pass
    try:
        await state.clear()
    except Exception:
        pass

    chosen = "ru" if call.data.endswith("ru") else "uz"
    await set_user_lang(uid, chosen)

    welcome = t(uid, "hello") + "\n\n" + t(uid, "change_lang_hint")
    kb = start_keyboard(uid)
    try:
        if call.message:
            try:
                await call.message.edit_text(welcome, reply_markup=kb)
            except Exception:
                await call.message.answer(welcome, reply_markup=kb)
        else:
            await call.bot.send_message(uid, welcome, reply_markup=kb)
    except Exception as e:
        log.exception("Failed to send language-switched welcome: %s", e)

@router.callback_query(F.data.startswith("start_form"))
async def cb_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id

    # 0) мгновенно закрываем спиннер, чтобы пользователь не видел "ожидание"
    try:
        await call.answer()
    except Exception:
        pass

    # 1) НЕ блокируемся на шитах: сохраняем язык фоном, если он пришёл в callback_data
    try:
        parts = call.data.split(":", 1)
        if len(parts) == 2 and parts[1] in ("ru", "uz"):
            # fire-and-forget — без await
            asyncio.create_task(set_user_lang(uid, parts[1]))
    except Exception:
        pass

    # 2) безопасно двигаем FSM и шлём два сообщения
    try:
        await state.clear()
        await state.set_state(Form.company)

        # тут только отправка сообщений (никаких сетевых блокировок)
        await send_text_safe(call.message, uid, "form_started")
        await send_text_safe(call.message, uid, "ask_company")
    except Exception as e:
        log.exception("cb_start failed: %s", e)
        # на крайний случай — через прямую отправку
        try:
            await call.bot.send_message(uid, t(uid, "form_started"))
            await call.bot.send_message(uid, t(uid, "ask_company"))
        except Exception:
            pass

# ---------- Company (free text) ----------

@router.message(Form.company)
async def ask_company(message: Message, state: FSMContext):
    await state.update_data(company=(message.text or "").strip())
    await state.set_state(Form.q1)
    await ask_next(message, message.from_user.id, Form.q1)

# ---------- Answer via buttons (q1, q2, q5) + nav ----------

def parse_answer(data: str) -> Tuple[str, Optional[str]]:
    # "ans:q1:opt2" -> ("q1", "opt2") ; "nav:back" -> ("nav", "back")
    if ":" not in data:
        return data, None
    parts = data.split(":", 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1]
    return data, None

@router.callback_query(F.data.startswith(("ans:", "nav:")))
async def cb_answers(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cur_state = await state.get_state()

    key, val = parse_answer(call.data)

    # navigation
    if key == "nav":
        if val == "back":
            if cur_state is None:
                await call.answer()
                return
            prev = prev_state_of(StatesGroup.get_state(cur_state))
            if prev:
                await state.set_state(prev)
                await ask_next(call.message, uid, prev)
        elif val == "skip":
            # move forward without saving current
            next_map = {
                Form.company.state: Form.q1,
                Form.q1.state: Form.q2,
                Form.q2.state: Form.q3,
                Form.q3.state: Form.q4,
                Form.q4.state: Form.q5,
            }
            nxt = next_map.get(cur_state)
            if nxt:
                await state.set_state(nxt)
                await ask_next(call.message, uid, nxt)
        try:
            await call.answer()
        except Exception:
            pass
        return

    # answers
    if key == "q1":
        mapping = {"opt1": TXT[get_lang(uid)]["q1_opt1"],
                   "opt2": TXT[get_lang(uid)]["q1_opt2"],
                   "opt3": TXT[get_lang(uid)]["q1_opt3"]}
        await state.update_data(q1=mapping.get(val, val))
        await state.set_state(Form.q2)
        await ask_next(call.message, uid, Form.q2)

    elif key == "q2":
        await state.update_data(q2=val)
        await state.set_state(Form.q3)
        await ask_next(call.message, uid, Form.q3)

    elif key == "q5":
        await state.update_data(q5=val)
        data = await state.get_data()
        ok = await append_feedback_row(call.from_user, data)
        await state.clear()
        if ok:
            await call.message.answer(t(uid, "thanks"))
        else:
            await send_text_safe(call.message, uid, "err")
        # notify admins
        if ok:
            for admin_id in ADMINS:
                try:
                    uname = f"@{call.from_user.username}" if call.from_user.username else str(call.from_user.id)
                    await call.bot.send_message(
                        admin_id,
                        f"✅ Новый фидбэк: {uname}\nКомпания: {data.get('company','')}\nNPS: {data.get('q5','')}"
                    )
                except Exception:
                    pass
    try:
        await call.answer()
    except Exception:
        pass

# ---------- Free-text handlers for each state ----------

@router.message(Form.q1)
async def q1_text(message: Message, state: FSMContext):
    await state.update_data(q1=(message.text or "").strip())
    await state.set_state(Form.q2)
    await ask_next(message, message.from_user.id, Form.q2)

@router.message(Form.q2)
async def q2_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    # принимаем всё, но если это число 1..10 — нормализуем
    if text.isdigit() and 1 <= int(text) <= 10:
        value = text
    else:
        value = text  # произвольный ответ
    await state.update_data(q2=value)
    await state.set_state(Form.q3)
    await ask_next(message, message.from_user.id, Form.q3)

@router.message(Form.q3)
async def q3_text(message: Message, state: FSMContext):
    await state.update_data(q3=(message.text or "").strip())
    await state.set_state(Form.q4)
    await ask_next(message, message.from_user.id, Form.q4)

@router.message(Form.q4)
async def q4_text(message: Message, state: FSMContext):
    await state.update_data(q4=(message.text or "").strip())
    await state.set_state(Form.q5)
    await ask_next(message, message.from_user.id, Form.q5)

@router.message(Form.q5)
async def q5_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.isdigit() and 1 <= int(text) <= 10:
        value = text
    else:
        value = text
    await state.update_data(q5=value)
    data = await state.get_data()
    ok = False
    try:
        ok = await append_feedback_row(message.from_user, data)
    except Exception as e:
        log.exception("append_feedback_row raised: %s", e)

    await state.clear()

    if ok:
        await message.answer(t(message.from_user.id, "thanks"))
        for admin_id in ADMINS:
            try:
                uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
                await message.bot.send_message(
                    admin_id,
                    f"✅ Новый фидбэк: {uname}\nКомпания: {data.get('company','')}\nNPS: {data.get('q5','')}"
                )
            except Exception:
                pass
    else:
        await send_text_safe(message, message.from_user.id, "err")
        for admin_id in ADMINS:
            try:
                await message.bot.send_message(
                    admin_id,
                    "⚠️ Не удалось записать ответ в Google Sheets после 3 попыток.\n"
                    f"User: {message.from_user.id} @{message.from_user.username or '—'}\n"
                    f"Company: {data.get('company','')}\n"
                    f"Payload: {json.dumps(data, ensure_ascii=False)[:1000]}"
                )
            except Exception:
                pass

# ---------- Optional: /diag — проверка записи в шит ----------

@router.message(Command("diag"))
async def cmd_diag(message: Message):
    ok = await append_feedback_row(
        message.from_user,
        {"company": "diag", "q1": "diag", "q2": "1", "q3": "diag", "q4": "diag", "q5": "1"},
    )
    await message.answer(t(message.from_user.id, "diag_ok") if ok else t(message.from_user.id, "diag_fail"))

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    uid = message.from_user.id
    rows = await fetch_feedback_records()
    if not rows:
        await message.answer(t(uid, "no_data"))
        return

    # распределение Q1
    q1_vals = [r.get("q1_time_to_setup", "").strip() for r in rows if r.get("q1_time_to_setup", "").strip()]
    dist = Counter(q1_vals)
    dist_lines = [f"• {k} — {v}" for k, v in dist.most_common()]
    dist_text = "\n".join(dist_lines) if dist_lines else "—"

    # средние по Q2 и Q5 (берём только числа 1..10)
    def _nums(field):
        res = []
        for r in rows:
            s = str(r.get(field, "")).strip().replace(",", ".")
            try:
                v = float(s)
                if 1 <= v <= 10:
                    res.append(v)
            except Exception:
                pass
        return res

    q2_nums = _nums("q2_statuses_score")
    q5_nums = _nums("q5_nps_recommend")
    avg_q2 = f"{(sum(q2_nums)/len(q2_nums)):.2f}" if q2_nums else "—"
    avg_q5 = f"{(sum(q5_nums)/len(q5_nums)):.2f}" if q5_nums else "—"

    # топ слов из Q3 и Q4 (очистка и простые стоп-слова)
    free_texts = []
    for r in rows:
        free_texts += [str(r.get("q3_what_inconvenient", "")), str(r.get("q4_missing_features", ""))]
    text = " ".join(free_texts).lower()

    # простые стоп-листы (ru/uz + общие)
    stop = {
        "и","в","на","что","как","или","за","до","после","для","это","его","ее","мы","но","же","из","у","по","от","не",
        "сиз","ва","билан","бир","эмас","учун","ҳам","лекин","бўлди","ки","қилса","қандай",
        "the","a","an","to","of","in","on","is","are","be"
    }
    # токенизация по небуквенным
    words = re.split(r"[^\w’ʼʼʼ'-]+", text, flags=re.UNICODE)
    words = [w for w in words if len(w) > 2 and w not in stop]
    top = Counter(words).most_common(10)
    words_text = "\n".join([f"• {w} — {c}" for w, c in top]) if top else "—"

    # ответ
    parts = [
        f"📊 <b>{t(uid, 'stats_title')}</b>",
        t(uid, "stats_n").format(n=len(rows)),
        t(uid, "stats_q1_dist").format(dist=html_escape(dist_text)),
        t(uid, "stats_avg").format(avg_q2=avg_q2, avg_q5=avg_q5),
        t(uid, "stats_top_keywords").format(words=html_escape(words_text)),
    ]
    await message.answer("\n\n".join(parts))

# --------------- FastAPI + Aiogram Webhook ---------------

app = FastAPI()

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)

dp = Dispatcher()
dp.include_router(router)

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=["message", "callback_query"]
    )
    log.info("Webhook set: %s", WEBHOOK_URL)

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    log.info("Webhook deleted")

@app.post("/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    # СЕКРЕТ ОТКЛЮЧЕН: не проверяем заголовок, принимаем все апдейты
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    return PlainTextResponse("TripleA Feedback Bot: alive")

@app.head("/")
async def root_head():
    return PlainTextResponse("", status_code=200)

@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "ok": True,
        "service": "TripleA Feedback Bot",
        "webhook": WEBHOOK_URL,
        "time": datetime.now(timezone.utc).astimezone().isoformat(),
        "env_locale": DEFAULT_LOCALE,
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
