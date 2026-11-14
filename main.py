
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


def get_group_price(route_id, group_size):
    if group_size < 1 or group_size > 4:
        return None
    result = (
        db.table("route_prices")
        .select("price_per_person")
        .eq("route_id", route_id)
        .eq("group_size", group_size)
        .limit(1)
        .execute()
    )
    return int(result.data[0]["price_per_person"]) if result.data else None


def notify_group_price(departure_id):
    departure = get_departure(departure_id)
    if not departure:
        return
    result = (
        db.table("bookings")
        .select("tourist_count, telegram_id")
        .eq("departure_id", departure_id)
        .in_("status", ["pending", "confirmed"])
        .execute()
    )
    bookings = result.data or []
    people = sum(int(item["tourist_count"]) for item in bookings)
    if not people:
        return
    price = get_group_price(departure["route_id"], people)
    if price is None:
        print(f"Не найден тариф route_id={departure['route_id']}, group_size={people}", flush=True)
        return
    route = get_route(departure["route_id"])
    route_name = route["name"] if route else "Экскурсия"
    group_message = (
        f"Группа на экскурсию «{route_name}»\\n"
        f"Дата: {departure['departure_date']}\\n"
        f"Участников: {people} из 4\\n"
        f"Свободно мест: {max(0, 4 - people)}\\n"
        f"Стоимость: {price} BYN с человека."
    )

    # Организатор получает отдельное уведомление о составе группы и актуальной цене.
    try:
        bot.send_message(ADMIN_ID, "Обновление группы\\n" + group_message)
    except Exception as error:
        print(f"Не удалось отправить уведомление организатору: {error}", flush=True)

    # Участники видят только состав группы и цену, без чужих контактов.
    for item in bookings:
        telegram_id = item.get("telegram_id")
        if telegram_id:
            try:
                bot.send_message(
                    int(telegram_id),
                    f"Изменение стоимости поездки «{route_name}» на {departure['departure_date']}.\\n"
                    f"В группе: {people} из 4 человек.\\n"
                    f"Новая стоимость: {price} BYN с человека."
                )
            except Exception as error:
                print(f"Не удалось отправить уведомление участнику {telegram_id}: {error}", flush=True)


def get_or_create_departure(route_id, requested_date):
    result = (
        db.table("departures")
        .select("*")
        .eq("route_id", route_id)
        .eq("departure_date", requested_date)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    result = (
        db.table("departures")
        .insert({
            "route_id": route_id,
            "departure_date": requested_date,
            "capacity": 4,
            "is_active": True,
            "notes": "Группа создана по заявке туриста"
        })
        .execute()
    )
    return result.data[0] if result.data else None


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


@bot.message_handler(commands=["tours"])
def tours_command(message):
    user_states.pop(message.from_user.id, None)
    main_menu(message.chat.id)


def show_all_dates(chat_id):
    try:
        result = (
            db.table("departures")
            .select("*")
            .eq("is_active", True)
            .gte("departure_date", str(date.today()))
            .order("departure_date")
            .execute()
        )
        departures = result.data or []

        if not departures:
            bot.send_message(chat_id, "Пока нет опубликованных дат поездок. Выберите маршрут через /tours или оставьте заявку на новую дату.")
            return

        lines = ["Доступные даты поездок:"]
        for departure in departures:
            taken = get_taken_places(departure["id"])
            available = min(4, int(departure["capacity"])) - taken
            if available <= 0:
                continue

            route = get_route(departure["route_id"])
            route_name = route["name"] if route else "Экскурсия"
            departure_time = departure.get("departure_time") or ""
            price = get_group_price(departure["route_id"], max(1, taken))
            price_text = f"{price} BYN/чел." if price is not None else "цена уточняется"
            lines.append(
                f'• {route_name} — {departure["departure_date"]} {departure_time}; '
                f'занято: {taken}/4, свободно: {available}; цена: {price_text}'
            )

        if len(lines) == 1:
            bot.send_message(chat_id, "Сейчас нет поездок со свободными местами. Выберите маршрут через /tours, чтобы оставить заявку на другую дату.")
        else:
            bot.send_message(chat_id, "\n".join(lines))
    except Exception:
        print("ОШИБКА /dates при загрузке дат из Supabase:", flush=True)
        traceback.print_exc()
        bot.send_message(chat_id, "Не удалось загрузить даты поездок. Попробуйте позже.")


@bot.message_handler(commands=["dates"])
def dates_command(message):
    show_all_dates(message.chat.id)


@bot.message_handler(commands=["help"])
def help_command(message):
    bot.send_message(
        message.chat.id,
        "Как пользоваться ботом:\n"
        "/tours — посмотреть экскурсионные маршруты и выбрать поездку.\n"
        "/dates — посмотреть опубликованные даты и свободные места.\n"
        "Выберите маршрут, затем дату или оставьте заявку на новую дату. "
        "Укажите количество туристов, имя и контакт для связи. "
        "Организатор свяжется с вами для подтверждения."
    )


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
        available = min(4, int(departure["capacity"])) - taken

        if available <= 0:
            continue

        price = get_group_price(route_id, max(1, taken))
        price_text = f"{price} BYN/чел." if price is not None else "цена уточняется"
        time_text = departure.get("departure_time") or ""
        button_text = (
            f'{departure["departure_date"]} {time_text} — '
            f'занято {taken}/4, свободно {available}, {price_text}'
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
        available = min(4, int(departure["capacity"])) - taken

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
            "stage": "requested_date",
            "route_id": route_id,
            "departure_id": None,
            "available": 4
        }

        bot.send_message(
            call.message.chat.id,
            "На какую дату вы хотели бы поездку?\nВведите дату в формате ГГГГ-ММ-ДД, например: 2026-12-20."
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
        selected = db.table("bookings").select("departure_id").eq("id", booking_id).limit(1).execute()
        db.table("bookings").update(
            {"status": "cancelled"}
        ).eq("id", booking_id).execute()
        departure_id = selected.data[0].get("departure_id") if selected.data else None
        if departure_id:
            notify_group_price(departure_id)

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

        departure_text = booking.get("requested_date") or "Заявка на новую дату"
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
            if capacity < 1 or capacity > 4:
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
            bot.send_message(message.chat.id, "Укажите количество мест от 1 до 4.")
        return

    # Турист предлагает желаемую дату для новой поездки
    if stage == "requested_date":
        try:
            requested_date = date.fromisoformat(text)
            if requested_date < date.today():
                raise ValueError()

            state["requested_date"] = requested_date.isoformat()
            state["stage"] = "count"
            bot.send_message(
                message.chat.id,
                "Спасибо! Желаемая дата записана.\nСколько туристов планирует поездку? (от 1 до 4)"
            )
        except ValueError:
            bot.send_message(
                message.chat.id,
                "Пожалуйста, укажите будущую дату в формате ГГГГ-ММ-ДД, например: 2026-12-20."
            )
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
            departure_id = state.get("departure_id")
            if not departure_id and state.get("requested_date"):
                departure = get_or_create_departure(state["route_id"], state["requested_date"])
                if not departure:
                    raise RuntimeError("Не удалось создать группу на выбранную дату")
                departure_id = departure["id"]

            booking = {
                "route_id": state["route_id"],
                "departure_id": departure_id,
                "requested_date": state.get("requested_date"),
                "tourist_count": state["tourist_count"],
                "name": state["name"],
                "contact": text,
                "telegram_id": user_id,
                "status": "pending"
            }

            db.table("bookings").insert(booking).execute()

            route = get_route(state["route_id"])
            route_name = route["name"] if route else "Маршрут"
            group_total = get_taken_places(departure_id) if departure_id else state["tourist_count"]
            current_price = get_group_price(state["route_id"], group_total)
            price_text = f"{current_price} BYN с человека" if current_price is not None else "уточняется организатором"
            if departure_id:
                notify_group_price(departure_id)

            departure_text = state.get("requested_date") or "Дата не указана"
            if state["departure_id"]:
                departure = get_departure(state["departure_id"])
                if departure:
                    departure_text = departure["departure_date"]

            bot.send_message(
                message.chat.id,
                "Спасибо! Ваша заявка принята.\n"
                f"Организатор свяжется с вами для подтверждения.\n"
                f"Желаемая дата: {departure_text}\n"
                f"Текущая стоимость: {price_text}"
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
            elif command == "/tours":
                print("Webhook: прямой вызов обработчика /tours.", flush=True)
                user_states.pop(update.message.from_user.id, None)
                main_menu(update.message.chat.id)
            elif command == "/dates":
                print("Webhook: прямой вызов обработчика /dates.", flush=True)
                show_all_dates(update.message.chat.id)
            elif command == "/help":
                print("Webhook: прямой вызов обработчика /help.", flush=True)
                help_command(update.message)
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
