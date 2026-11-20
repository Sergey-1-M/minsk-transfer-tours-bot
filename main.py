
import os
from datetime import date, datetime
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


def format_departure_time(value, default="уточняется"):
    return str(value)[:5] if value else default


def report_manual_contact(booking, reason):
    try:
        bot.send_message(
            ADMIN_ID,
            "Не удалось доставить туристу сообщение в Telegram. Свяжитесь вручную.\n"
            f"Заявка №{booking.get('id', '—')}\n"
            f"Имя: {booking.get('name') or 'не указано'}\n"
            f"Телефон/контакт: {booking.get('contact') or 'не указан'}\n"
            f"Причина: {reason}"
        )
    except Exception as error:
        print(f"Не удалось уведомить администратора о ручном контакте: {error}", flush=True)


def notify_booking_status(booking_id, status):
    result = db.table("bookings").select("*").eq("id", booking_id).limit(1).execute()
    if not result.data:
        return
    booking = result.data[0]
    route = get_route(booking["route_id"])
    route_name = route["name"] if route else "Экскурсия"
    departure = get_departure(booking["departure_id"]) if booking.get("departure_id") else None
    date_text = departure["departure_date"] if departure else (booking.get("requested_date") or "уточняется")
    time_text = format_departure_time(departure.get("departure_time") if departure else None)
    status_text = "Организатор подтвердил вашу заявку." if status == "confirmed" else "Ваша заявка отменена. Если это произошло по ошибке, свяжитесь с организатором."
    body = (f"{status_text}\nМаршрут: {route_name}\nДата: {date_text}\nВремя начала: {time_text}\n"
            f"Количество туристов: {booking['tourist_count']}\n\nMinsk Transfer Tours — местный организатор экскурсий.")
    telegram_id = booking.get("telegram_id")
    if telegram_id:
        try:
            bot.send_message(int(telegram_id), body)
        except Exception as error:
            print(f"Не удалось отправить статусное уведомление {telegram_id}: {error}", flush=True)
            report_manual_contact(booking, str(error))


def notify_departure_time(departure_id):
    departure = get_departure(departure_id)
    if not departure:
        return
    result = (db.table("bookings").select("id, name, contact, telegram_id").eq("departure_id", departure_id)
              .in_("status", ["pending", "confirmed"]).execute())
    route = get_route(departure["route_id"])
    route_name = route["name"] if route else "Экскурсия"
    body = (f"В поездке «{route_name}» изменилось время начала.\n"
            f"Дата: {departure['departure_date']}\nНовое время: {format_departure_time(departure.get('departure_time'))}\n\n"
            "Организатор Minsk Transfer Tours согласует с вами остальные детали.")
    for item in result.data or []:
        if item.get("telegram_id"):
            try:
                bot.send_message(int(item["telegram_id"]), body)
            except Exception as error:
                print(f"Не удалось отправить Telegram-уведомление: {error}", flush=True)
                report_manual_contact(item, str(error))


def notify_group_price(departure_id, exclude_telegram_id=None):
    departure = get_departure(departure_id)
    if not departure:
        return
    result = (db.table("bookings").select("id, name, contact, tourist_count, telegram_id").eq("departure_id", departure_id)
              .in_("status", ["pending", "confirmed"]).execute())
    bookings = result.data or []
    people = sum(int(x["tourist_count"]) for x in bookings)
    route = get_route(departure["route_id"])
    route_name = route["name"] if route else "Экскурсия"
    if people == 0:
        if "Группа создана туристом" in (departure.get("notes") or ""):
            db.table("departures").update({"is_active": False}).eq("id", departure_id).execute()
        try:
            bot.send_message(ADMIN_ID, f"В группе №{departure_id} больше нет активных участников: {route_name}, {departure['departure_date']}.")
        except Exception as error:
            print(f"Не удалось уведомить администратора: {error}", flush=True)
        return
    price = get_group_price(departure["route_id"], people)
    if price is None:
        print(f"Не найден тариф route_id={departure['route_id']}, group_size={people}", flush=True)
        return
    body = (f"Обновление поездки «{route_name}».\nДата: {departure['departure_date']}\n"
            f"Время начала: {format_departure_time(departure.get('departure_time'))}\n"
            f"Участников: {people} из 4, свободно мест: {max(0, 4-people)}\n"
            f"Стоимость: {price} BYN с человека.")
    try:
        bot.send_message(ADMIN_ID, f"Обновление группы №{departure_id}\n" + body)
    except Exception as error:
        print(f"Не удалось уведомить администратора: {error}", flush=True)
    for item in bookings:
        tid = item.get("telegram_id")
        if tid and (exclude_telegram_id is None or int(tid) != int(exclude_telegram_id)):
            try:
                bot.send_message(int(tid), body)
            except Exception as error:
                print(f"Не удалось уведомить участника {tid}: {error}", flush=True)
                report_manual_contact(item, str(error))


def create_departure(route_id, requested_date, requested_time):
    # Каждый инициатор открывает отдельную группу; группы одной даты не объединяются.
    result = db.table("departures").insert({
        "route_id": route_id,
        "departure_date": requested_date,
        "departure_time": requested_time,
        "capacity": 4,
        "is_active": True,
        "notes": "Группа создана туристом; время предварительное"
    }).execute()
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
            "Добро пожаловать в Minsk Transfer Tours!\n\n"
            "Мы работаем напрямую, без агрегаторов и посредников: предлагаем выгодные цены, "
            "мини-группы до 4 человек и внимательное отношение к каждому туристу.\n"
            "Без предоплаты. Напрямую с организатором. С заботой о вашем путешествии.\n\n"
            "Выбирайте доступную дату или откройте собственную группу.\n\n"
            "Выберите маршрут:",
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
    if is_admin(message.from_user.id):
        admin_menu(message)
    else:
        main_menu(message.chat.id)


@bot.message_handler(commands=["tours"])
def tours_command(message):
    user_states.pop(message.from_user.id, None)
    if is_admin(message.from_user.id):
        admin_menu(message)
    else:
        main_menu(message.chat.id)


def format_join_prices(route_id, taken, available):
    """Показывает цену в зависимости от числа туристов, которые присоединятся."""
    price_lines = []
    for joining_count in range(1, available + 1):
        total_group_size = taken + joining_count
        price = get_group_price(route_id, total_group_size)
        price_text = f"{price} BYN с человека" if price is not None else "уточняется"
        noun = "человек" if joining_count == 1 else "человека"
        price_lines.append(f"Если присоединится {joining_count} {noun} — {price_text}")
    return "\n".join(price_lines)


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

        available_departures = []
        for departure in departures:
            taken = get_taken_places(departure["id"])
            available = min(4, int(departure["capacity"])) - taken
            if available <= 0:
                continue

            route = get_route(departure["route_id"])
            route_name = route["name"] if route else "Экскурсия"
            available_departures.append({
                "departure": departure,
                "taken": taken,
                "available": available,
                "route_name": route_name,
                "time": str(departure.get("departure_time") or "")[:5]
            })

        if not available_departures:
            bot.send_message(chat_id, "Сейчас нет поездок со свободными местами. Выберите маршрут через /tours, чтобы оставить заявку на другую дату.")
            return

        # Сортировка по дате, затем по названию маршрута и времени.
        available_departures.sort(key=lambda item: (
            str(item["departure"]["departure_date"]),
            item["route_name"].casefold(),
            item["time"]
        ))

        month_names = {
            "01": "января", "02": "февраля", "03": "марта",
            "04": "апреля", "05": "мая", "06": "июня",
            "07": "июля", "08": "августа", "09": "сентября",
            "10": "октября", "11": "ноября", "12": "декабря"
        }

        lines = ["ДОСТУПНЫЕ ПОЕЗДКИ"]
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        current_date = None

        for item in available_departures:
            departure = item["departure"]
            departure_date = str(departure["departure_date"])
            year, month, day = departure_date.split("-")
            date_label = f"{int(day)} {month_names[month]}"

            if departure_date != current_date:
                current_date = departure_date
                lines.append(f"\n{date_label}")

            route_name = item["route_name"]
            taken = item["taken"]
            available = item["available"]
            time_text = f' · {item["time"]}' if item["time"] else ""
            prices_text = format_join_prices(departure["route_id"], taken, available)
            lines.append(
                f"🏰 {route_name}{time_text}\n"
                f"В группе: {taken}/4, свободно: {available}\n"
                f"{prices_text}"
            )
            keyboard.add(types.InlineKeyboardButton(
                f"Присоединиться — {route_name}, {date_label}",
                callback_data=f'dep:{departure["id"]}'
            ))

        bot.send_message(chat_id, "\n\n".join(lines), reply_markup=keyboard)
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
        "Выберите маршрут, затем доступную поездку или откройте собственную группу. "
        "Для своей группы укажите дату и предварительное время начала. "
        "При бронировании потребуются имя и номер телефона. "
        "Подтверждения и изменения по поездке приходят в Telegram. "
        "Если Telegram для связи недоступен, организатор свяжется с вами по телефону."
    )


@bot.message_handler(commands=["admin"])
def admin_menu(message):
    if not is_admin(message.from_user.id):
        return
    show_admin_menu(message.chat.id)


def show_admin_menu(chat_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Группы и участники", callback_data="admin:groups"),
        types.InlineKeyboardButton("Новые заявки", callback_data="admin:requests"),
        types.InlineKeyboardButton("Расписание", callback_data="admin:dates"),
        types.InlineKeyboardButton("Добавить дату поездки", callback_data="admin:add")
    )
    bot.send_message(chat_id, "Панель организатора Minsk Transfer Tours:", reply_markup=keyboard)


def show_admin_groups(chat_id):
    result = (db.table("departures").select("*").eq("is_active", True)
              .gte("departure_date", str(date.today())).order("departure_date").execute())
    departures = result.data or []
    if not departures:
        bot.send_message(chat_id, "Активных будущих групп пока нет.")
        return
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    lines = ["ГРУППЫ И УЧАСТНИКИ"]
    for dep in departures:
        route = get_route(dep["route_id"])
        name = route["name"] if route else "Маршрут"
        taken = get_taken_places(dep["id"])
        lines.append(f"\n№{dep['id']} — {name}\n{dep['departure_date']} · {format_departure_time(dep.get('departure_time'))}\nЗанято: {taken}, свободно: {max(0, int(dep.get('capacity',4))-taken)}")
        keyboard.add(types.InlineKeyboardButton(f"Открыть группу №{dep['id']}", callback_data=f"admin:group:{dep['id']}"))
    keyboard.add(types.InlineKeyboardButton("Главное меню", callback_data="admin:home"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)


def show_admin_group(chat_id, departure_id):
    dep = get_departure(departure_id)
    if not dep:
        bot.send_message(chat_id, "Группа не найдена.")
        return
    route = get_route(dep["route_id"])
    route_name = route["name"] if route else "Маршрут"
    result = (db.table("bookings").select("*").eq("departure_id", departure_id)
              .in_("status", ["pending", "confirmed"]).order("created_at").execute())
    bookings = result.data or []
    people = sum(int(x["tourist_count"]) for x in bookings)
    cap = int(dep.get("capacity",4))
    price = get_group_price(dep["route_id"], people) if people else None
    lines = [f"ГРУППА №{departure_id}", f"Маршрут: {route_name}", f"Дата: {dep['departure_date']}",
             f"Время: {format_departure_time(dep.get('departure_time'))}", f"Участников: {people} из {cap}",
             f"Свободно мест: {max(0,cap-people)}", f"Цена: {price} BYN с человека" if price is not None else "Цена: пока не рассчитывается", ""]
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for booking in bookings:
        status = "подтверждена" if booking.get("status") == "confirmed" else "ожидает подтверждения"
        tg = "Telegram доступен" if booking.get("telegram_id") else "Telegram нет — связаться вручную"
        lines.append(f"Заявка №{booking['id']} — {booking.get('name') or 'без имени'}\nМест: {booking['tourist_count']} · {status}\nКонтакт: {booking.get('contact') or 'не указан'}\n{tg}")
        keyboard.add(
            types.InlineKeyboardButton(f"Изменить число мест — №{booking['id']}", callback_data=f"admin:editcount:{booking['id']}"),
            types.InlineKeyboardButton(f"Отменить заявку №{booking['id']}", callback_data=f"cancel:{booking['id']}")
        )
    keyboard.add(types.InlineKeyboardButton("🗑 Удалить группу", callback_data=f"admin:deletegroup:{departure_id}"))
    keyboard.add(types.InlineKeyboardButton("Назад к группам", callback_data="admin:groups"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)


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
    date_lines = [
        f'Маршрут: {route["name"]}',
        '',
        'Выберите дату или оставьте заявку на новую поездку:'
    ]

    for departure in departures:
        taken = get_taken_places(departure["id"])
        available = min(4, int(departure["capacity"])) - taken

        if available <= 0:
            continue

        prices_text = format_join_prices(route_id, taken, available)
        time_text = format_departure_time(departure.get("departure_time"), "")
        date_lines.append(
            f'\n{departure["departure_date"]} {time_text} — в группе {taken}/4, свободно {available}\n'
            f'{prices_text}'
        )
        button_text = (
            f'{departure["departure_date"]} {time_text} — '
            f'занято {taken}/4, свободно {available}'
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
        "\n".join(date_lines),
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
            "departure_time": departure.get("departure_time"),
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

    if data.startswith("admin:time:") and is_admin(user_id):
        departure_id = int(data.split(":")[2])
        departure = get_departure(departure_id)
        if not departure:
            bot.send_message(call.message.chat.id, "Поездка не найдена.")
            return
        user_states[user_id] = {"stage": "admin_edit_time", "departure_id": departure_id}
        bot.send_message(
            call.message.chat.id,
            f"Текущее время: {departure.get('departure_time') or 'не указано'}.\n"
            "Введите новое время в формате ЧЧ:ММ, например 09:30."
        )
        return

    if data == "admin:home" and is_admin(user_id):
        show_admin_menu(call.message.chat.id)
        return

    if data == "admin:groups" and is_admin(user_id):
        show_admin_groups(call.message.chat.id)
        return

    if data.startswith("admin:group:") and is_admin(user_id):
        show_admin_group(call.message.chat.id, int(data.split(":")[2]))
        return

    if data.startswith("admin:deletegroup:") and is_admin(user_id):
        departure_id = int(data.split(":")[2])
        dep = get_departure(departure_id)
        if not dep or not dep.get("is_active"):
            bot.send_message(call.message.chat.id, "Активная группа не найдена.")
            return
        route = get_route(dep["route_id"])
        route_name = route["name"] if route else "Маршрут"
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("Да, удалить", callback_data=f"admin:confirmdelete:{departure_id}"),
            types.InlineKeyboardButton("Назад к группе", callback_data=f"admin:group:{departure_id}")
        )
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Удалить группу №{departure_id}?\\n\\nМаршрут: {route_name}\\nДата: {dep['departure_date']}\\n\\nВсе участники получат уведомление об отмене, группа перестанет отображаться.",
            reply_markup=keyboard
        )
        return

    if data.startswith("admin:confirmdelete:") and is_admin(user_id):
        departure_id = int(data.split(":")[2])
        dep = get_departure(departure_id)
        if not dep or not dep.get("is_active"):
            bot.send_message(call.message.chat.id, "Группа уже удалена или неактивна.")
            return
        route = get_route(dep["route_id"])
        route_name = route["name"] if route else "Маршрут"
        bookings = (db.table("bookings").select("*").eq("departure_id", departure_id)
                    .in_("status", ["pending", "confirmed"]).execute()).data or []
        db.table("departures").update({"is_active": False}).eq("id", departure_id).execute()
        db.table("bookings").update({"status": "cancelled"}).eq("departure_id", departure_id).in_("status", ["pending", "confirmed"]).execute()
        for booking in bookings:
            tid = booking.get("telegram_id")
            if tid:
                try:
                    bot.send_message(
                        int(tid),
                        f"⚠️ Поездка «{route_name}» на {dep['departure_date']} отменена организатором.\\nГруппа расформирована."
                    )
                except Exception as error:
                    print(f"Не удалось уведомить участника об отмене группы {departure_id}: {error}", flush=True)
                    report_manual_contact(booking, str(error))
        bot.send_message(call.message.chat.id, f"Группа №{departure_id} удалена из активных. Участники уведомлены.")
        show_admin_groups(call.message.chat.id)
        return

    if data.startswith("admin:editcount:") and is_admin(user_id):
        bid = int(data.split(":")[2])
        res = db.table("bookings").select("*").eq("id", bid).limit(1).execute()
        if not res.data or res.data[0].get("status") not in ["pending", "confirmed"]:
            bot.send_message(call.message.chat.id, "Активная заявка не найдена.")
            return
        user_states[user_id] = {"stage": "admin_edit_count", "booking_id": bid}
        bot.send_message(call.message.chat.id, f"Сейчас мест в заявке: {res.data[0]['tourist_count']}. Введите новое количество (от 1 до 4).")
        return

    if data == "admin:dates" and is_admin(user_id):
        show_admin_dates(call.message.chat.id)
        return

    if data.startswith("confirm:") and is_admin(user_id):
        booking_id = int(data.split(":")[1])
        db.table("bookings").update(
            {"status": "confirmed"}
        ).eq("id", booking_id).execute()
        notify_booking_status(booking_id, "confirmed")

        bot.send_message(call.message.chat.id, f"Заявка №{booking_id} подтверждена. Турист получил уведомление.")
        return

    if data.startswith("cancel:") and is_admin(user_id):
        booking_id = int(data.split(":")[1])
        selected = db.table("bookings").select("*").eq("id", booking_id).limit(1).execute()
        if not selected.data:
            bot.send_message(call.message.chat.id, "Заявка не найдена.")
            return
        booking = selected.data[0]
        if booking.get("status") == "cancelled":
            bot.send_message(call.message.chat.id, f"Заявка №{booking_id} уже отменена.")
            return
        departure_id = booking.get("departure_id")
        db.table("bookings").update({"status": "cancelled"}).eq("id", booking_id).execute()
        notify_booking_status(booking_id, "cancelled")
        if departure_id:
            notify_group_price(departure_id)
        bot.send_message(call.message.chat.id, f"Заявка №{booking_id} отменена. Места освобождены, цена пересчитана.")
        if departure_id:
            show_admin_group(call.message.chat.id, departure_id)
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
        time_text = format_departure_time(departure.get("departure_time"), "не указано")
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("Изменить время", callback_data=f"admin:time:{departure['id']}"),
            types.InlineKeyboardButton("Управлять группой", callback_data=f"admin:group:{departure['id']}")
        )
        bot.send_message(
            chat_id,
            f'№{departure["id"]} — {route_name}\n'
            f'Дата: {departure["departure_date"]}\n'
            f'Время начала: {time_text}\n'
            f'Вместимость: {departure["capacity"]}\n'
            f'Занято: {taken}, свободно: {available}',
            reply_markup=keyboard
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

    # Организатор корректирует предварительное время группы.
    if stage == "admin_edit_time" and is_admin(user_id):
        try:
            parsed_time = datetime.strptime(text, "%H:%M").time()
            departure_id = state["departure_id"]
            db.table("departures").update({
                "departure_time": parsed_time.strftime("%H:%M:%S")
            }).eq("id", departure_id).execute()
            user_states.pop(user_id, None)
            notify_departure_time(departure_id)
            bot.send_message(
                message.chat.id,
                "Время поездки обновлено. Участники получили уведомления по Telegram и Email (если почта настроена)."
            )
        except ValueError:
            bot.send_message(message.chat.id, "Укажите время в формате ЧЧ:ММ, например 09:30.")
        except Exception as error:
            print(f"Не удалось изменить время поездки: {error}", flush=True)
            bot.send_message(message.chat.id, "Не удалось изменить время. Проверьте журнал Render.")
        return

    if stage == "admin_edit_count" and is_admin(user_id):
        try:
            new_count = int(text)
            bid = state["booking_id"]
            res = db.table("bookings").select("*").eq("id", bid).limit(1).execute()
            if not res.data:
                raise ValueError("Заявка не найдена.")
            booking = res.data[0]
            if booking.get("status") not in ["pending", "confirmed"]:
                raise ValueError("Изменять можно только активную заявку.")
            did = booking.get("departure_id")
            dep = get_departure(did) if did else None
            if not dep:
                raise ValueError("Группа не найдена.")
            other_places = get_taken_places(did) - int(booking["tourist_count"])
            maximum = min(4, int(dep.get("capacity",4))) - other_places
            if new_count < 1 or new_count > maximum:
                raise ValueError(f"Для этой заявки можно указать от 1 до {maximum} мест.")
            db.table("bookings").update({"tourist_count": new_count}).eq("id", bid).execute()
            user_states.pop(user_id, None)
            notify_group_price(did)
            bot.send_message(message.chat.id, f"Количество мест в заявке №{bid} изменено. Состав группы и цена пересчитаны.")
            show_admin_group(message.chat.id, did)
        except ValueError as error:
            bot.send_message(message.chat.id, str(error) or "Введите целое число.")
        except Exception as error:
            print(f"Ошибка изменения количества мест: {error}", flush=True)
            bot.send_message(message.chat.id, "Не удалось изменить заявку. Проверьте логи Render.")
        return

    # Администратор добавляет дату поездки

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
            state["stage"] = "requested_time"
            bot.send_message(
                message.chat.id,
                "На какое время вы хотели бы начать экскурсию?\n"
                "Укажите предварительное время в формате ЧЧ:ММ, например 09:00.\n"
                "Организатор сможет согласовать и немного скорректировать его."
            )
        except ValueError:
            bot.send_message(
                message.chat.id,
                "Пожалуйста, укажите будущую дату в формате ГГГГ-ММ-ДД, например: 2026-12-20."
            )
        return

    # Первый турист выбирает предварительное время начала своей группы.
    if stage == "requested_time":
        try:
            parsed_time = datetime.strptime(text, "%H:%M").time()
            state["requested_time"] = parsed_time.strftime("%H:%M:%S")
            state["stage"] = "count"
            bot.send_message(message.chat.id, "Время записано предварительно.\nСколько туристов планирует поездку? (от 1 до 4)")
        except ValueError:
            bot.send_message(message.chat.id, "Укажите время в формате ЧЧ:ММ, например 09:00.")
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

    # После телефона заявка сразу сохраняется; Email не требуется.
    if stage == "contact":
        state["contact"] = text
        try:
            departure_id = state.get("departure_id")
            if not departure_id and state.get("requested_date"):
                dep = create_departure(state["route_id"], state["requested_date"], state["requested_time"])
                if not dep:
                    raise RuntimeError("Не удалось создать группу на выбранную дату")
                departure_id = dep["id"]
            booking = {
                "route_id": state["route_id"], "departure_id": departure_id,
                "requested_date": state.get("requested_date"), "tourist_count": state["tourist_count"],
                "name": state["name"], "contact": state["contact"],
                "telegram_id": user_id, "status": "pending"
            }
            inserted = db.table("bookings").insert(booking).execute()
            if not inserted.data:
                raise RuntimeError("Supabase не вернул созданную заявку")
            route = get_route(state["route_id"])
            route_name = route["name"] if route else "Маршрут"
            group_total = get_taken_places(departure_id) if departure_id else state["tourist_count"]
            current_price = get_group_price(state["route_id"], group_total)
            price_text = f"{current_price} BYN с человека" if current_price is not None else "уточняется организатором"
            if departure_id:
                notify_group_price(departure_id, exclude_telegram_id=user_id)
            dep = get_departure(departure_id) if departure_id else None
            departure_text = dep["departure_date"] if dep else (state.get("requested_date") or "Дата не указана")
            time_text = format_departure_time(dep.get("departure_time") if dep else state.get("requested_time"))
            if not is_admin(user_id):
                bot.send_message(message.chat.id,
                    "Спасибо! Ваша заявка принята и ожидает подтверждения организатором.\n"
                    f"Маршрут: {route_name}\nДата: {departure_text}\nВремя: {time_text}\n"
                    f"Туристов: {state['tourist_count']}\nСтоимость: {price_text}\n\n"
                    "Обновления по поездке будут приходить сюда, в Telegram.")
            bot.send_message(ADMIN_ID,
                "Новая заявка на экскурсию!\n\n"
                f"Группа №{departure_id or 'не создана'}\nМаршрут: {route_name}\nДата: {departure_text}\n"
                f"Время: {time_text}\nТуристов: {state['tourist_count']}\nИмя: {state['name']}\n"
                f"Контакт: {state['contact']}\nTelegram ID: {user_id}")
            user_states.pop(user_id, None)
        except Exception as error:
            print("Ошибка сохранения заявки:", error, flush=True)
            bot.send_message(message.chat.id, "Не удалось сохранить заявку. Пожалуйста, попробуйте позже или свяжитесь с организатором.")
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
                if is_admin(update.message.from_user.id):
                    admin_menu(update.message)
                else:
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
