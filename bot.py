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
import time
from gspread.exceptions import APIError
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User

import gspread
from datetime import datetime, timezone

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
              "Варианты: до 15 минут / 15–30 минут / более 30 минут",
        "q2": "2/5. Насколько понятны статусы заявок и уведомления?\n\n"
              "Оцени по шкале 1–10 (где 10 — идеально).",
        "q3": "3/5. Что показалось неудобным? (свободный ответ)",
        "q4": "4/5. Каких функций не хватает в первую очередь? (например: онлайн-оплата, шаблоны цен, импорт)",
        "q5": "5/5. Готовы ли рекомендовать коллегам? Укажи оценку 1–10.",
        "ask_company": "Укажи название компании (как у вас в Telegram/Instagram/юр. название)",
        "done": "Готово ✅",
        "back": "⬅️ Назад",
        "skip": "Пропустить",
        "change_lang_hint": "Чтобы сменить язык позже, используй /lang",
        "lang_switched": "Язык переключён.",
        "form_started": "Погнали! Сначала уточним компанию:",
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
              "Вариантлар: 15 дақиқагача / 15–30 дақиқа / 30 дақиқадан кўпроқ",
        "q2": "2/5. Аризалар статуслари ва хабарномалар қай даражада тушунарли?\n1–10 баҳоланг.",
        "q3": "3/5. Нима ноқулай туюлди? (эркин жавоб)",
        "q4": "4/5. Қайси функциялар етишмайди? (масалан: онлайн тўлов, нарх шаблонлари, импорт)",
        "q5": "5/5. Ҳамкасбларга тавсия қиласизми? 1–10 баҳоланг.",
        "ask_company": "Компания номини киритинг (TG/Instagram/ёки юр. ном)",
        "done": "Тайёр ✅",
        "back": "⬅️ Орқага",
        "skip": "Ўтказиб юбориш",
        "change_lang_hint": "Кейинроқ тилни /lang орқали ўзгартиришингиз мумкин.",
        "lang_switched": "Тил ўзгартирилди.",
        "form_started": "Бошладик! Аввало компания номини аниқлаймиз:",
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

def append_feedback_row(user: User, data: Dict[str, Any]) -> bool:
    """Надёжный апенд: reopen + 3 retries + USER_ENTERED. Возвращает True/False."""
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
            # Переоткрываем книгу/лист на каждую попытку — меньше шансов на “протухший” хэндл
            spread = _open_spreadsheet()
            ws = _get_or_create_ws(spread, "feedback", [
                "timestamp", "user_id", "username", "full_name", "company",
                "q1_time_to_setup", "q2_statuses_score", "q3_what_inconvenient",
                "q4_missing_features", "q5_nps_recommend", "raw_json"
            ])
            ws.append_row(row, value_input_option="USER_ENTERED")
            return True
        except APIError as e:
            log.warning("Google Sheets APIError on append (attempt %s/3): %s", attempt, e)
        except Exception as e:
            log.warning("Append to Sheets failed (attempt %s/3): %s", attempt, e)
        time.sleep(0.7 * attempt)  # простой прогрессивный бэкофф
    return False

# ----------- Persistent language store ------------

_lang_cache: Dict[int, str] = {}

def set_user_lang(user_id: int, lang: str):
    lang = "uz" if lang == "uz" else "ru"
    _lang_cache[user_id] = lang
    try:
        cell = WS_USERS.find(str(user_id))
        if cell:
            WS_USERS.update_cell(cell.row, 2, lang)
            WS_USERS.update_cell(cell.row, 3, datetime.now(timezone.utc).astimezone().isoformat())
            return
    except Exception:
        pass
    # append new
    try:
        WS_USERS.append_row([str(user_id), lang, datetime.now(timezone.utc).astimezone().isoformat()],
                            value_input_option="RAW")
    except Exception as e:
        log.warning("Failed to persist user lang: %s", e)

def get_user_lang_persist(user_id: int) -> Optional[str]:
    try:
        cell = WS_USERS.find(str(user_id))
        if cell:
            lang = WS_USERS.cell(cell.row, 2).value or ""
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
    # try persisted
    lang = get_user_lang_persist(user_id)
    if lang:
        return lang
    # fallback to env default
    return "uz" if DEFAULT_LOCALE == "uz" else "ru"

def t(user_id: Optional[int], key: str) -> str:
    lang = get_lang(user_id)
    return TXT.get(lang, TXT["ru"]).get(key, key)

# --------------- Bot & FSM ---------------

router = Router()

class Form(StatesGroup):
    company = State()
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TXT["ru"]["lang_ru"], callback_data="lang_ru"),
         InlineKeyboardButton(text=TXT["uz"]["lang_uz"], callback_data="lang_uz")]
    ])

def start_keyboard(user_id: Optional[int]) -> InlineKeyboardMarkup:
    # Вшиваем текущий язык в callback_data для страховки
    lang = get_lang(user_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(user_id, "start_btn"), callback_data=f"start_form:{lang}")]]
    )

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
    set_user_lang(uid, chosen)

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
    # Подхватим язык из callback_data (start_form:uz|ru) — ещё один ремень безопасности
    try:
        parts = call.data.split(":", 1)
        if len(parts) == 2 and parts[1] in ("ru", "uz"):
            set_user_lang(uid, parts[1])
    except Exception:
        pass

    await state.clear()
    await state.set_state(Form.company)
    await call.message.answer(t(uid, "form_started"))
    await call.message.answer(t(uid, "ask_company"))
    await call.answer()

@router.message(Form.company)
async def ask_company(message: Message, state: FSMContext):
    await state.update_data(company=(message.text or "").strip())
    await state.set_state(Form.q1)
    await message.answer(t(message.from_user.id, "q1"))

@router.message(Form.q1)
async def ask_q1(message: Message, state: FSMContext):
    await state.update_data(q1=(message.text or "").strip())
    await state.set_state(Form.q2)
    await message.answer(t(message.from_user.id, "q2"))

@router.message(Form.q2)
async def ask_q2(message: Message, state: FSMContext):
    await state.update_data(q2=(message.text or "").strip())
    await state.set_state(Form.q3)
    await message.answer(t(message.from_user.id, "q3"))

@router.message(Form.q3)
async def ask_q3(message: Message, state: FSMContext):
    await state.update_data(q3=(message.text or "").strip())
    await state.set_state(Form.q4)
    await message.answer(t(message.from_user.id, "q4"))

@router.message(Form.q4)
async def ask_q4(message: Message, state: FSMContext):
    await state.update_data(q4=(message.text or "").strip())
    await state.set_state(Form.q5)
    await message.answer(t(message.from_user.id, "q5"))

@router.message(Form.q5)
async def finalize(message: Message, state: FSMContext):
    await state.update_data(q5=(message.text or "").strip())
    data = await state.get_data()

    ok = False
    try:
        ok = append_feedback_row(message.from_user, data)
    except Exception as e:
        log.exception("append_feedback_row raised: %s", e)

    await state.clear()

    if ok:
        await message.answer(t(message.from_user.id, "thanks"))
    else:
        # Сообщаем пользователю мягко, а админам — подробно
        await send_text_safe(message, message.from_user.id, "err")
        for admin_id in ADMINS:
            try:
                await message.bot.send_message(
                    admin_id,
                    "⚠️ Не удалось записать ответ в Google Sheets после 3 попыток.\n"
                    f"User: {message.from_user.id} @{message.from_user.username or '—'}\n"
                    f"Company: {data.get('company','')}\n"
                    f"Payload: {json.dumps(data, ensure_ascii=False)[:2000]}"
                )
            except Exception:
                pass
        return

    # notify admins при успехе
    for admin_id in ADMINS:
        try:
            uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
            await message.bot.send_message(
                admin_id,
                f"✅ Новый фидбэк: {uname}\nКомпания: {data.get('company','')}\nNPS: {data.get('q5','')}"
            )
        except Exception:
            pass

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
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"]
    )
    log.info("Webhook set: %s", WEBHOOK_URL)

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    log.info("Webhook deleted")

@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    # удобный корневой пинг, чтобы Render/uptime-боты не сыпали 404
    return PlainTextResponse("TripleA Feedback Bot: alive")

@app.get("/healthz")
async def healthz():
    # подробный хелсчек, если понадобится для мониторинга
    return JSONResponse({
        "ok": True,
        "service": "TripleA Feedback Bot",
        "webhook": WEBHOOK_URL,
        "time": datetime.now(timezone.utc).astimezone().isoformat(),
        "env_locale": DEFAULT_LOCALE,
    })
