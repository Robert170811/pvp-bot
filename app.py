# app.py
import os
import threading
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from models import init_db, SessionLocal, User, Gift, InventoryItem, Match, Bet, Currency, MatchStatus
from logic import (
    get_or_create_user, add_stars, inventory_delta, parse_gifts_blob,
    create_match, place_bet_stars, place_bet_gifts, resolve_match, can_start_match
)
from config import get_config

# Telegram bot
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
cfg = get_config()

app = Flask(__name__, static_folder="webapp", static_url_path="")

# --------- API для мини-приложения ---------

def get_session() -> Session:
    return SessionLocal()

def resolve_tg_user_from_webapp(initData) -> int:
    """
    Упрощённо: из initDataUnsafe берём user.id, здесь не проводим подпись/проверку.
    Для продакшена проверь хэш подписи по документу Telegram WebApp.
    """
    # для MVP принимаем tg_id из заглушки (небезопасно, но быстро)
    # на фронте мы не шлём весь initDataUnsafe — в реальном проекте реализуй проверку подписи!
    return int(os.getenv("ADMIN_USER_ID", "0"))  # fallback: твой аккаунт

@app.post("/api/me")
def api_me():
    s = get_session()
    try:
        tg_user_id = resolve_tg_user_from_webapp(request.json.get("initData"))
        me = get_or_create_user(s, tg_user_id)
        # собрать подарки
        inv = s.query(InventoryItem).filter_by(user_id=me.id).all()
        gifts = []
        for i in inv:
            gifts.append({"code": i.gift.code, "title": i.gift.title, "qty": i.qty, "value": i.gift.value_stars})
        return jsonify({"ok": True, "me": {"stars": me.stars_balance, "gifts": gifts}})
    finally:
        s.close()

@app.post("/api/start_fight")
def api_start_fight():
    payload = request.json.get("payload") or {}
    currency = payload.get("currency")
    bet = payload.get("bet") or {}
    s = get_session()
    try:
        tg_user_id = resolve_tg_user_from_webapp(request.json.get("initData"))
        user = get_or_create_user(s, tg_user_id)

        if not can_start_match(s, user):
            return jsonify({"ok": False, "error": "Слишком часто. Подождите несколько секунд."})

        if currency not in ("stars", "gifts"):
            return jsonify({"ok": False, "error": "Неверная валюта."})
        cur = Currency.STARS if currency == "stars" else Currency.GIFTS

        m = create_match(s, cur)
        ok, msg = (False, "Ошибка")
        if cur == Currency.STARS:
            amount = int(bet.get("amount", 0))
            ok, msg = place_bet_stars(s, m, user, amount)
        else:
            gifts_blob = bet.get("gifts", "")
            gifts = parse_gifts_blob(gifts_blob)
            ok, msg = place_bet_gifts(s, m, user, gifts)

        if not ok:
            return jsonify({"ok": False, "error": msg})

        # В этой простой версии сразу автоподбор соперника (бот-соперник) и завершение матча:
        # чтобы пользователь увидел мгновенный результат в мини-приложении.
        bot_user = get_or_create_user(s, tg_user_id + 1, username="BotOpponent")
        if m.currency == Currency.STARS:
            # бот ставит примерно такую же сумму
            from random import randint
            bot_amount = max(1, int(m.bets[0].value_stars * (0.8 + randint(0, 40)/100)))
            add_stars(s, bot_user, bot_amount)  # чтобы точно хватило
            place_bet_stars(s, m, bot_user, bot_amount)
        else:
            # бот поставит дешевый подарок
            inventory_delta(s, bot_user, "ROSE", 2)  # выдать боту розы
            place_bet_gifts(s, m, bot_user, {"ROSE": 2})

        winner_id, pool, commission, detail = resolve_match(s, m)
        message = f"Матч #{m.id} разыгран. Пул: {pool}⭐️, комиссия: {commission}⭐️."
        if detail.get("type") == "gift":
            message += f" Комиссия взята подарком {detail['gift_code']} (⭐️{detail['gift_value']})."
        message += (" Победа за вами! 🎉" if winner_id == user.id else " Увы, вы проиграли.")
        return jsonify({"ok": True, "message": message})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    finally:
        s.close()

# отдаём статику мини-приложения
@app.get("/")
def index():
    return send_from_directory("webapp", "index.html")

# --------- Telegram Bot ---------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session()
    try:
        u = get_or_create_user(s, update.effective_user.id, update.effective_user.username)
        await update.message.reply_text(
            "Привет! Это PvP-бот.\n"
            "Команды:\n"
            "/balance — баланс\n"
            "/fight — быстрый бой с ботом\n"
            "/addstars 50 — выдать себе звезды (для теста)\n"
            "/gifts — мои подарки\n"
            "/mini — открыть мини-приложение"
        )
    finally:
        s.close()

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session()
    try:
        u = get_or_create_user(s, update.effective_user.id, update.effective_user.username)
        inv = s.query(InventoryItem).filter_by(user_id=u.id).all()
        gifts_str = ", ".join([f"{i.gift.title} x{i.qty}" for i in inv]) or "нет"
        await update.message.reply_text(f"⭐️ Звёзды: {u.stars_balance}\n🎁 Подарки: {gifts_str}")
    finally:
        s.close()

async def cmd_addstars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите кол-во: /addstars 50")
        return
    amount = int(context.args[0])
    s = get_session()
    try:
        u = get_or_create_user(s, update.effective_user.id, update.effective_user.username)
        add_stars(s, u, amount)
        await update.message.reply_text(f"Начислено {amount}⭐️. Текущий баланс: {u.stars_balance}")
    finally:
        s.close()

async def cmd_gifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_session()
    try:
        u = get_or_create_user(s, update.effective_user.id, update.effective_user.username)
        inv = s.query(InventoryItem).filter_by(user_id=u.id).all()
        if not inv:
            await update.message.reply_text("Подарков нет. Для теста выдам ROSE x3.")
            inventory_delta(s, u, "ROSE", +3)
            return
        gifts_str = "\n".join([f"{i.gift.title} ({i.gift.code}) x{i.qty} (⭐️{i.gift.value_stars})" for i in inv])
        await update.message.reply_text("Ваши подарки:\n" + gifts_str)
    finally:
        s.close()

async def cmd_fight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрый бой с ботом: игрок ставит 10⭐️, бот — случайно 8-12⭐️, мгновенное разрешение.
    """
    s = get_session()
    try:
        user = get_or_create_user(s, update.effective_user.id, update.effective_user.username)
        if not can_start_match(s, user):
            await update.message.reply_text("Слишком часто. Подождите несколько секунд.")
            return

        from random import randint
        # создаём матч на звёзды
        m = create_match(s, Currency.STARS)
        ok, msg = place_bet_stars(s, m, user, 10)
        if not ok:
            await update.message.reply_text("Ошибка: " + msg)
            return

        bot_user = get_or_create_user(s, user.tg_id + 1, username="BotOpponent")
        bot_amount = randint(8, 12)
        add_stars(s, bot_user, bot_amount)
        place_bet_stars(s, m, bot_user, bot_amount)

        winner_id, pool, commission, detail = resolve_match(s, m)
        text = f"Матч #{m.id}: вы поставили 10⭐️, соперник — {bot_amount}⭐️.\nПул: {pool}⭐️, комиссия: {commission}⭐️."
        if detail.get("type") == "gift":
            text += f" Комиссия подарком {detail['gift_code']} (⭐️{detail['gift_value']})."
        text += "\nИтог: " + ("🎉 Победа!" if winner_id == user.id else "Поражение 😔")
        await update.message.reply_text(text)
    finally:
        s.close()

async def cmd_mini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = os.getenv("WEBAPP_URL", "http://localhost:5000")
    await update.message.reply_text(f"Открыть мини-приложение: {url}")

def run_bot():
    app_ = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("balance", cmd_balance))
    app_.add_handler(CommandHandler("addstars", cmd_addstars))
    app_.add_handler(CommandHandler("gifts", cmd_gifts))
    app_.add_handler(CommandHandler("fight", cmd_fight))
    app_.add_handler(CommandHandler("mini", cmd_mini))
    app_.run_polling(close_loop=False)

if __name__ == "__main__":
    init_db()
    # запускаем бота в отдельном потоке, Flask — в главном
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=True)
