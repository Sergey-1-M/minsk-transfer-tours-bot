
import os
from datetime import date
import traceback
from flask import Flask, request
import telebot
from telebot import types
from supabase import create_client

# Настройки из Render
TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_ID = 1255152509

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не заданы BOT_TOKEN, SUPABASE_URL или SUPABASE_KEY в Render")

# Обрабатываем обновления синхронно внутри webhook, чтобы ошибки попадали в лог Render.
bot = telebot.TeleBot(TOKEN, threaded=False)
db = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

# Временные состояния диалогов
user_states = {}


def is_admin(user_id):
    return user_id == ADMIN_ID


def get_route(route_id):
    result = db.table("routes").select("*").eq("id", route_id).execute()
    return result.data[0] if result.data else None


def get_routes():
    result = (
        db.table("routes")
        .select("*")
        .eq("is_active", True)
        .order("id")
        .execute()
    )
    return result.data or []


def get_departure(departure_id):
    result = (
        db.table("departures")
        .select("*")
        .eq("id", departure_id)
        .execute()
    )
    return result.data[0] if result.data else None


def get_taken_places(departure_id):
    result = (
        db.table("bookings")
        .select("tourist_count")
        .eq("departure_id", departure_id)
        .in_("status", ["pending", "confirmed"])
        .execute()
    )
    return sum(int(item["tourist_count"]) for item in (result.data or []))


def main_menu(chat_id):
    try:
        routes = get_routes()
        keyboard = types.InlineKeyboardMarkup(row_width=1)

        for route in routes:
            keyboard.add(
                types.InlineKeyboardButton(
                    route["name"],
                    callback_data=f'route:{route["id"]}'
                )
            )

        if not routes:
            bot.send_message(
                chat_id,
                "Список маршрутов пока пуст. Пожалуйста, свяжитесь с организатором."
            )
            return

        bot.send_message(
            chat_id,
            "Здравствуйте! Выберите экскурсионный маршрут:",
            reply_markup=keyboard
        )
    except Exception:
        print("ОШИБКА main_menu / чтение маршрутов Supabase:", flush=True)
        traceback.print_exc()
        bot.send_message(
            chat_id,
            "Не удалось загрузить маршруты. Организатор уже может проверить журнал ошибки. Попробуйте позже."
        )


@bot.message_handler(commands=["start"])
def start(message):
    user_states.pop(message.from_user.id, None)
    main_menu(message.chat.id)


@bot.message_handler(commands=["admin"])
def admin_menu(message):
    if not is_admin(message.from_user.id):
        return

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Добавить дату поездки", callback_data="admin:add"),
        types.InlineKeyboardButton("Новые заявки", callback_data="admin:requests"),
        types.InlineKeyboardButton("Расписание", callback_data="admin:dates")
    )
    bot.send_message(message.chat.id, "Панель организатора:", reply_markup=keyboard)


def show_departures(chat_id, route_id):
    route = get_route(route_id)

    if not route:
        bot.send_message(chat_id, "Маршрут не найден. Попробуйте ещё раз: /start")
        return

    result = (
        db.table("departures")
        .select("*")
        .eq("route_id", route_id)
        .eq("is_active", True)
        .gte("departure_date", str(date.today()))
        .order("departure_date")
        .execute()
    )

    departures = result.data or []
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for departure in departures:
        taken = get_taken_places(departure["id"])
        available = int(departure["capacity"]) - taken

        if available <= 0:
            continue

        time_text = departure.get("departure_time") or ""
        button_text = (
            f'{departure["departure_date"]} '
            f'{time_text} — свободно мест: {available}'
        )

        keyboard.add(
            types.InlineKeyboardButton(
                button_text,
                callback_data=f'dep:{departure["id"]}'
            )
        )

    keyboard.add(
        types.InlineKeyboardButton(
            "Оставить заявку на новую дату",
            callback_data=f"new:{route_id}"
        )
    )
    keyboard.add(
        types.InlineKeyboardButton("Назад к маршрутам", callback_data="back")
    )

    bot.send_message(
        chat_id,
        f'Маршрут: {route["name"]}\n\nВыберите дату или оставьте заявку на новую поездку:',
        reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    user_id = call.from_user.id
    data = call.data

    bot.answer_callback_query(call.id)

    if data == "back":
        main_menu(call.message.chat.id)
        return

    if data.startswith("route:"):
        route_id = int(data.split(":")[1])
        show_departures(call.message.chat.id, route_id)
        return

    if data.startswith("dep:"):
        departure_id = int(data.split(":")[1])
        departure = get_departure(departure_id)

        if not departure or not departure["is_active"]:
            bot.send_message(call.message.chat.id, "Эта поездка больше недоступна.")
            return

        taken = get_taken_places(departure_id)
        available = int(departure["capacity"]) - taken

        if available <= 0:
            bot.send_message(call.message.chat.id, "К сожалению, свободных мест уже нет.")
            return

        user_states[user_id] = {
            "stage": "count",
            "route_id": departure["route_id"],
            "departure_id": departure_id,
            "available": available
        }

        bot.send_message(
            call.message.chat.id,
            f"Свободно мест: {available}.\nСколько туристов хотите записать?"
        )
        return

    if data.startswith("new:"):
        route_id = int(data.split(":")[1])

        user_states[user_id] = {
            "stage": "count",
            "route_id": route_id,
            "departure_id": None,
            "available": 20
        }

        bot.send_message(
            call.message.chat.id,
            "Вы выбрали заявку на новую дату.\nСколько туристов планирует поездку?"
        )
        return

    if data == "admin:add" and is_admin(user_id):
        routes = get_routes()
        keyboard = types.InlineKeyboardMarkup(row_width=1)

        for route in routes:
            keyboard.add(
                types.InlineKeyboardButton(
                    route["name"],
                    callback_data=f'addroute:{route["id"]}'
                )
            )

        bot.send_message(
            call.message.chat.id,
            "Для какого маршрута добавить дату?",
            reply_markup=keyboard
        )
        return

    if data.startswith("addroute:") and is_admin(user_id):
        route_id = int(data.split(":")[1])
        user_states[user_id] = {
            "stage": "admin_date",
            "route_id": route_id
        }
        bot.send_message(
            call.message.chat.id,
            "Введите дату и время поездки в формате:\n2026-12-20 09:00"
        )
        return

    if data == "admin:requests" and is_admin(user_id):
        show_pending_requests(call.message.chat.id)
        return

    if data == "admin:dates" and is_admin(user_id):
        show_admin_dates(call.message.chat.id)
        return

    if data.startswith("confirm:") and is_admin(user_id):
        booking_id = int(data.split(":")[1])
        db.table("bookings").update(
            {"status": "confirmed"}
        ).eq("id", booking_id).execute()

        bot.send_message(call.message.chat.id, f"Заявка №{booking_id} подтверждена.")
        return

    if data.startswith("cancel:") and is_admin(user_id):
        booking_id = int(data.split(":")[1])
        db.table("bookings").update(
            {"status": "cancelled"}
        ).eq("id", booking_id).execute()

        bot.send_message(call.message.chat.id, f"Заявка №{booking_id} отменена.")
        return


def show_pending_requests(chat_id):
    result = (
        db.table("bookings")
        .select("*")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )

    bookings = result.data or []

    if not bookings:
        bot.send_message(chat_id, "Новых заявок пока нет.")
        return

    for booking in bookings:
        route = get_route(booking["route_id"])
        route_name = route["name"] if route else "Маршрут"

        departure_text = "Заявка на новую дату"
        if booking.get("departure_id"):
            departure = get_departure(booking["departure_id"])
            if departure:
                departure_text = departure["departure_date"]

        text = (
            f'Заявка №{booking["id"]}\n'
            f'Маршрут: {route_name}\n'
            f'Дата: {departure_text}\n'
            f'Туристов: {booking["tourist_count"]}\n'
            f'Имя: {booking["name"]}\n'
            f'Контакт: {booking["contact"]}'
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(
                "Подтвердить",
                callback_data=f'confirm:{booking["id"]}'
            ),
            types.InlineKeyboardButton(
                "Отменить",
                callback_data=f'cancel:{booking["id"]}'
            )
        )

        bot.send_message(chat_id, text, reply_markup=keyboard)


def show_admin_dates(chat_id):
    result = (
        db.table("departures")
        .select("*")
        .gte("departure_date", str(date.today()))
        .eq("is_active", True)
        .order("departure_date")
        .execute()
    )

    departures = result.data or []

    if not departures:
        bot.send_message(chat_id, "Будущих поездок пока нет.")
        return

    for departure in departures:
        route = get_route(departure["route_id"])
        route_name = route["name"] if route else "Маршрут"
        taken = get_taken_places(departure["id"])
        available = int(departure["capacity"]) - taken

        bot.send_message(
            chat_id,
            f'№{departure["id"]} — {route_name}\n'
            f'Дата: {departure["departure_date"]}\n'
            f'Вместимость: {departure["capacity"]}\n'
            f'Занято: {taken}, свободно: {available}'
        )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    state = user_states.get(user_id)

    if not state:
        bot.send_message(message.chat.id, "Чтобы начать, нажмите /start.")
        return

    stage = state.get("stage")

    # Администратор добавляет дату поездки
    if stage == "admin_date" and is_admin(user_id):
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError()

            departure_date = parts[0]
            departure_time = parts[1]

            date.fromisoformat(departure_date)
            if len(departure_time.split(":")) != 2:
                raise ValueError()

            state["departure_date"] = departure_date
            state["departure_time"] = departure_time
            state["stage"] = "admin_capacity"

            bot.send_message(
                message.chat.id,
                "Сколько всего мест доступно в этой поездке?"
            )
        except ValueError:
            bot.send_message(
                message.chat.id,
                "Неверный формат. Введите, например:\n2026-12-20 09:00"
            )
        return

    if stage == "admin_capacity" and is_admin(user_id):
        try:
            capacity = int(text)
            if capacity < 1:
                raise ValueError()

            db.table("departures").insert({
                "route_id": state["route_id"],
                "departure_date": state["departure_date"],
                "departure_time": state["departure_time"],
                "capacity": capacity
            }).execute()

            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "Дата поездки успешно добавлена.")
        except ValueError:
            bot.send_message(message.chat.id, "Введите целое число больше нуля.")
        return

    # Турист указывает количество человек
    if stage == "count":
        try:
            count = int(text)
            if count < 1 or count > state["available"]:
                raise ValueError()

            state["tourist_count"] = count
            state["stage"] = "name"

            bot.send_message(message.chat.id, "Как вас зовут?")
        except ValueError:
            bot.send_message(
                message.chat.id,
                f'Введите число от 1 до {state["available"]}.'
            )
        return

    # Турист указывает имя
    if stage == "name":
        state["name"] = text
        state["stage"] = "contact"
        bot.send_message(
            message.chat.id,
            "Оставьте номер телефона или другой удобный способ связи."
        )
        return

    # Турист указывает контакт, заявка сохраняется в базе
    if stage == "contact":
        try:
            booking = {
                "route_id": state["route_id"],
                "departure_id": state["departure_id"],
                "tourist_count": state["tourist_count"],
                "name": state["name"],
                "contact": text,
                "telegram_id": user_id,
                "status": "pending"
            }

            db.table("bookings").insert(booking).execute()

            route = get_route(state["route_id"])
            route_name = route["name"] if route else "Маршрут"

            departure_text = "Новая дата — по согласованию"
            if state["departure_id"]:
                departure = get_departure(state["departure_id"])
                if departure:
                    departure_text = departure["departure_date"]

            bot.send_message(
                message.chat.id,
                "Спасибо! Ваша заявка принята.\n"
                "Организатор свяжется с вами для подтверждения."
            )

            admin_text = (
                "Новая заявка на экскурсию!\n\n"
                f"Маршрут: {route_name}\n"
                f"Дата: {departure_text}\n"
                f'Туристов: {state["tourist_count"]}\n'
                f'Имя: {state["name"]}\n'
                f"Контакт: {text}"
            )

            bot.send_message(ADMIN_ID, admin_text)
            user_states.pop(user_id, None)

        except Exception as error:
            print("Ошибка сохранения заявки:", error, flush=True)
            bot.send_message(
                message.chat.id,
                "Не удалось сохранить заявку. Пожалуйста, попробуйте позже или свяжитесь с организатором."
            )
        return


@app.route("/", methods=["GET"])
def home():
    return "Minsk Transfer Tours bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            print(
                "Webhook получил запрос не в JSON:",
                request.headers.get("Content-Type"),
                flush=True
            )
            return "Expected application/json", 400

        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)

        if update is None:
            print("Webhook: Telegram прислал пустое или нераспознанное обновление.", flush=True)
            return "Invalid update", 400

        message_text = None
        if update.message:
            message_text = update.message.text
        elif update.edited_message:
            message_text = update.edited_message.text

        print(
            f"Webhook: update_id={update.update_id}, message={message_text!r}",
            flush=True
        )

        # /start обрабатываем прямо в webhook. Это исключает зависимость
        # стартового меню от диспетчеризации message_handler в библиотеке.
        if update.message and update.message.text:
            command = update.message.text.split()[0].split("@")[0]
            if command == "/start":
                print("Webhook: прямой вызов обработчика /start.", flush=True)
                user_states.pop(update.message.from_user.id, None)
                main_menu(update.message.chat.id)
            elif command == "/admin":
                print("Webhook: прямой вызов обработчика /admin.", flush=True)
                admin_menu(update.message)
            else:
                bot.process_new_updates([update])
        else:
            bot.process_new_updates([update])

        print(f"Webhook: update_id={update.update_id} обработан.", flush=True)
        return "OK", 200

    except Exception:
        print("КРИТИЧЕСКАЯ ОШИБКА обработки Telegram webhook:", flush=True)
        traceback.print_exc()
        return "Internal Server Error", 500


# Установка Telegram webhook на Render
webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
if webhook_url:
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url + "/webhook")
    print("Webhook установлен:", webhook_url + "/webhook", flush=True)
