
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

pending_requests = {}


@bot.message_handler(commands=["start"])
def start(message):
    keyboard = telebot.types.InlineKeyboardMarkup()

    for i, route in enumerate(ROUTES):
        keyboard.add(
            telebot.types.InlineKeyboardButton(
                route, callback_data=f"route:{i}"
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
    try:
        index = int(call.data.split(":", 1)[1])
        route = ROUTES[index]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Не удалось выбрать маршрут")
        return

    bot.answer_callback_query(call.id)
    pending_requests[call.message.chat.id] = route

    bot.send_message(
        call.message.chat.id,
        f"Вы выбрали: {route}.\n\n"
        "Напишите, пожалуйста, сколько туристов планирует поездку "
        "и оставьте контакт для связи."
    )


@bot.message_handler(content_types=["text", "contact"])
def receive_request(message):
    chat_id = message.chat.id

    if chat_id not in pending_requests:
        return

    route = pending_requests.pop(chat_id)

    if message.contact:
        contact = message.contact.phone_number
    else:
        contact = message.text or "Контакт не указан"

    username = message.from_user.username or "не указан"

    bot.send_message(
        ADMIN_ID,
        "Новая заявка на экскурсию!\n\n"
        f"Маршрут: {route}\n"
        f"Данные туриста: {contact}\n"
        f"Имя: {message.from_user.first_name}\n"
        f"Telegram: @{username}"
    )

    bot.send_message(
        chat_id,
        "Спасибо! Ваша заявка отправлена организатору. "
        "Мы свяжемся с вами для подтверждения."
    )


@app.route("/", methods=["GET"])
def home():
    return "Minsk Transfer Tours bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(
        request.get_data().decode("utf-8")
    )
    bot.process_new_updates([update])
    return "OK", 200


webhook_url = os.environ.get("RENDER_EXTERNAL_URL")

if webhook_url:
    bot.set_webhook(url=webhook_url + "/webhook")
