import os
import telebot
from flask import Flask, request

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 1255152509

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

ROUTES = [
    "Мир и Несвиж",
    "Сула",
    "Гродно",
    "Брест и Брестская крепость",
    "Витебск",
    "Полоцк"
]

@bot.message_handler(commands=["start"])
def start(message):
    keyboard = telebot.types.InlineKeyboardMarkup()
    for route in ROUTES:
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                route, callback_data="route:" + route
            )
        )
    bot.send_message(
        message.chat.id,
        "Здравствуйте! Это бот Minsk Transfer Tours.\n\n"
        "Выберите направление:",
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("route:"))
def choose_route(call):
    route = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"Вы выбрали: {route}.\n\n"
        "Напишите, пожалуйста, сколько туристов планирует поездку "
        "и как с вами связаться."
    )
    bot.send_message(
        ADMIN_ID,
        f"Пользователь выбрал маршрут: {route}.\n"
        f"Telegram: @{call.from_user.username or 'не указан'}"
    )

@app.route("/", methods=["GET"])
def home():
    return "Minsk Transfer Tours bot is running"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200

if __name__ == "__main__":
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    if webhook_url:
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url + "/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
