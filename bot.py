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
GOOGLE_SERVICE_ACCOUNT_BASE64=...       # base64-encoded service account JSON

OPTIONAL:
ADMINS=123456789,987654321              # comma-separated Telegram user IDs to receive alerts
LOCALE=ru                               # default locale text (ru/uz)
"""

import os
import json
import base64
import logging
from typing import Dict, Any, List, Optional

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
LOCALE = os.getenv("LOCALE", "ru")

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

TXT = {
    "ru": {
        "hello": "Привет! Это тестовый бот для партнёров автопроката.\n"
                 "Помоги нам улучшить агрегатор — ответь на 5 быстрых вопросов (2–3 минуты).",
        "start_btn": "Начать опрос",
        "cancel": "Отменить",
        "thanks": "Спасибо! Ответы сохранены. 🎉\nЕсли готовы — напишите, созвонимся по деталям.",
        "err": "Ой! Что-то пошло не так. Попробуй ещё раз /start",
        "q1": "1/5. Сколько времени ушло на регистрацию и добавление первой машины?\n\n"
              "Варианты: <15 минут / 15–30 минут / >30 минут",
        "q2": "2/5. Насколько понятны статусы заявок и уведомления?\n\n"
              "Оцени по шкале 1–10 (где 10 — идеально).",
        "q3": "3/5. Что показалось неудобным? (свободный ответ)",
        "q4": "4/5. Каких функций не хватает в первую очередь? (например: онлайн-оплата, шаблоны цен, импорт)",
        "q5": "5/5. Готовы ли рекомендовать коллегам? Укажи оценку 1–10.",
        "ask_company": "Укажи название компании (как у вас в Telegram/Instagram/юр. название)",
        "done": "Готово ✅",
        "back": "⬅️ Назад",
        "skip": "Пропустить",
    },
    "uz": {
        "hello": "Салом! Бу тест бот — автопрокат ҳамкорларига мўлжалланган.\n"
                 "Агрегаторни яхшилашга ёрдам беринг: 5 та қисқа савол (2–3 дақиқа).",
        "start_btn": "Сўровномани бошлаш",
        "cancel": "Бекор қилиш",
        "thanks": "Раҳмат! Жавоблар сақланди. 🎉",
        "err": "Уй! Нимадир хато. Қайта /start қилинг.",
        "q1": "1/5. Рўйхатдан ўтиш ва биринчи машинани қўшишга қанча вақт кетди?\n\n"
              "Вариантлар: <15 дақиқа / 15–30 дақиқа / >30 дақиқа",
        "q2": "2/5. Аризалар статусклари ва хабарномалар қанчалик тушунарли?\n1–10 баҳоланг.",
        "q3": "3/5. Нима ноқулай туюлди? (эркин жавоб)",
        "q4": "4/5. Қайси функциялар етишмайди? (масалан: онлайн тўлов, нарх шаблонлари, импорт)",
        "q5": "5/5. Ҳамкасбларга тавсия қиласизми? 1–10 баҳоланг.",
        "ask_company": "Компания номини киритинг (TG/Instagram/ёки юр. ном)",
        "done": "Тайёр ✅",
        "back": "⬅️ Орқага",
        "skip": "Ўтказиб юбориш",
    }
}

def t(key: str) -> str:
    return TXT.get(LOCALE, TXT["ru"]).get(key, key)

# --------------- Google Sheets client ---------------

def make_sheets_client():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("feedback")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="feedback", rows=2000, cols=20)
        ws.append_row([
            "timestamp", "user_id", "username", "full_name", "company",
            "q1_time_to_setup", "q2_statuses_score", "q3_what_inconvenient",
            "q4_missing_features", "q5_nps_recommend", "raw_json"
        ])
    return ws

SHEET = make_sheets_client()

def append_feedback_row(user: User, data: Dict[str, Any]):
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
        json.dumps(data, ensure_ascii=False)
    ]
    SHEET.append_row(row, value_input_option="RAW")

# --------------- Bot & FSM ---------------

router = Router()

class Form(StatesGroup):
    company = State()
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        [InlineKeyboardButton(text=t("start_btn"), callback_data="start_form")]
    ]])
    await message.answer(t("hello"), reply_markup=kb)

@router.callback_query(F.data == "start_form")
async def cb_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Form.company)
    await call.message.answer(t("ask_company"))
    await call.answer()

@router.message(Form.company)
async def ask_company(message: Message, state: FSMContext):
    await state.update_data(company=message.text.strip())
    await state.set_state(Form.q1)
    await message.answer(t("q1"))

@router.message(Form.q1)
async def ask_q1(message: Message, state: FSMContext):
    await state.update_data(q1=message.text.strip())
    await state.set_state(Form.q2)
    await message.answer(t("q2"))

@router.message(Form.q2)
async def ask_q2(message: Message, state: FSMContext):
    await state.update_data(q2=message.text.strip())
    await state.set_state(Form.q3)
    await message.answer(t("q3"))

@router.message(Form.q3)
async def ask_q3(message: Message, state: FSMContext):
    await state.update_data(q3=message.text.strip())
    await state.set_state(Form.q4)
    await message.answer(t("q4"))

@router.message(Form.q4)
async def ask_q4(message: Message, state: FSMContext):
    await state.update_data(q4=message.text.strip())
    await state.set_state(Form.q5)
    await message.answer(t("q5"))

@router.message(Form.q5)
async def finalize(message: Message, state: FSMContext):
    await state.update_data(q5=message.text.strip())
    data = await state.get_data()
    try:
        append_feedback_row(message.from_user, data)
    except Exception as e:
        logging.exception("Failed to append to sheet")
        await message.answer(t("err"))
        return
    await state.clear()
    await message.answer(t("thanks"))
    # notify admins
    for admin_id in ADMINS:
        try:
            await message.bot.send_message(
                admin_id,
                f"Новый фидбэк от @{message.from_user.username or message.from_user.id} — {data.get('company','')}"
            )
        except Exception:
            pass

# --------------- FastAPI + Aiogram Webhook ---------------

app = FastAPI()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(router)

@app.on_event("startup")
async def on_startup():
    # Set webhook
    await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    log.info("Webhook set")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

@app.post("/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return JSONResponse({"ok": True})

@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)

