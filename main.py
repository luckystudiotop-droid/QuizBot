import asyncio
import logging
import os
import json
import sys
from aiohttp import web
from aiogram.filters import CommandObject
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, PollAnswer, ChatMemberAdministrator, ChatMemberOwner,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = 5273553942  # Только этот ID имеет доступ к управлению ботом

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

CONFIG_FILE = "config.json"

# --- СОСТОЯНИЕ ИГРЫ ---
quiz_state = {
    "questions": [],
    "current_index": 0,
    "active_poll_id": None,
    "active_chat_id": None,
    "active_message_id": None,
    "first_blood_taken": False,
    "scores": {},          # user_id -> {"name": str, "score": int}
    "eliminated": {},      # user_id -> {"name": str, "score": int, "cut_at": int}
}

# --- НАСТРОЙКИ (персистентные, храним в config.json) ---
settings = {
    "cuts": []  # список {"after_question": int, "cut_count": int}, отсортирован по after_question
}

# Ожидание ввода от владельца после нажатия "Добавить срез" (user_id -> True)
awaiting_cut_input = set()


def load_config():
    global settings
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                settings["cuts"] = data.get("cuts", [])
        except Exception as e:
            print(f"⚠️ Не удалось загрузить {CONFIG_FILE}: {e}")


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить {CONFIG_FILE}: {e}")


# --- ХЕЛПЕРЫ ПРОВЕРКИ ПРАВ ---
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def is_admin(message: Message) -> bool:
    """Разрешает управляющие команды только владельцу (везде: и в ЛС, и в группах)."""
    return is_owner(message.from_user.id)


# --- ЛОГИКА СРЕЗА ИГРОКОВ ---
def get_cut_for_question(question_number: int):
    """Возвращает конфиг среза, запланированного ровно на этот номер вопроса, либо None."""
    for cut in settings["cuts"]:
        if cut["after_question"] == question_number:
            return cut
    return None


def apply_cuts_up_to(question_number: int):
    """
    Применяет все ещё не применённые срезы с after_question <= question_number,
    в порядке возрастания after_question.
    question_number — номер только что отвеченного вопроса (1-индексация).
    """
    cuts = sorted(settings["cuts"], key=lambda c: c["after_question"])
    for cut in cuts:
        if cut["after_question"] == question_number:
            _apply_single_cut(cut["cut_count"])


def _apply_single_cut(cut_count: int):
    active = quiz_state["scores"]
    if not active or cut_count <= 0:
        return

    # Сортируем активных игроков по убыванию баллов
    ranked = sorted(active.items(), key=lambda kv: kv[1]["score"], reverse=True)

    if cut_count >= len(ranked):
        return  # срезать больше, чем есть игроков — не делаем ничего (защита от абсурдной настройки)

    # Граница: последнее проходящее место
    cutoff_score = ranked[len(ranked) - cut_count - 1][1]["score"]

    survivors = {}
    eliminated_now = []
    for user_id, data in ranked:
        if data["score"] > cutoff_score:
            survivors[user_id] = data
        elif data["score"] == cutoff_score:
            # игроки на границе — все проходят дальше (даже если их больше, чем формально нужно)
            survivors[user_id] = data
        else:
            eliminated_now.append((user_id, data))

    for user_id, data in eliminated_now:
        quiz_state["eliminated"][user_id] = {
            "name": data["name"],
            "score": data["score"],
            "cut_at": quiz_state["current_index"]
        }

    quiz_state["scores"] = survivors


# --- МЕНЮ НАСТРОЕК ---
def build_settings_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✂️ Срезы игроков", callback_data="menu_cuts")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_cuts_menu() -> InlineKeyboardMarkup:
    buttons = []
    cuts = sorted(settings["cuts"], key=lambda c: c["after_question"])
    for i, cut in enumerate(cuts):
        label = f"❌ После в.{cut['after_question']}: срезать {cut['cut_count']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"delcut_{i}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить срез", callback_data="addcut")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_root")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer("⚙️ <b>Настройки бота</b>\n\nВыбери раздел:", parse_mode="HTML",
                          reply_markup=build_settings_menu())


@dp.callback_query(F.data == "menu_root")
async def cb_menu_root(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text("⚙️ <b>Настройки бота</b>\n\nВыбери раздел:", parse_mode="HTML",
                                      reply_markup=build_settings_menu())
    await callback.answer()


@dp.callback_query(F.data == "menu_cuts")
async def cb_menu_cuts(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    cuts = sorted(settings["cuts"], key=lambda c: c["after_question"])
    if cuts:
        text = "✂️ <b>Срезы игроков</b>\n\nНажми на срез, чтобы удалить его, либо добавь новый:"
    else:
        text = "✂️ <b>Срезы игроков</b>\n\nПока не настроено ни одного среза."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_cuts_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("delcut_"))
async def cb_delcut(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    idx = int(callback.data.split("_")[1])
    cuts = sorted(settings["cuts"], key=lambda c: c["after_question"])
    if 0 <= idx < len(cuts):
        removed = cuts.pop(idx)
        settings["cuts"] = cuts
        save_config()
        await callback.answer(f"Удалено: после в.{removed['after_question']} срез {removed['cut_count']}")
    else:
        await callback.answer("Уже удалено")
    await callback.message.edit_text("✂️ <b>Срезы игроков</b>\n\nНажми на срез, чтобы удалить его, либо добавь новый:",
                                      parse_mode="HTML", reply_markup=build_cuts_menu())


@dp.callback_query(F.data == "addcut")
async def cb_addcut(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    awaiting_cut_input.add(callback.from_user.id)
    await callback.message.answer(
        "✏️ Пришли сообщением два числа через пробел:\n"
        "<code>номер_вопроса количество_срезаемых</code>\n\n"
        "Например: <code>20 5</code> — после 20-го вопроса срезать 5 последних мест.",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(F.text.regexp(r"^\d+\s+\d+$"))
async def handle_cut_input(message: Message):
    if message.from_user.id not in awaiting_cut_input:
        return  # обычное текстовое сообщение, не относящееся к вводу среза
    awaiting_cut_input.discard(message.from_user.id)

    after_q, cut_count = map(int, message.text.split())
    if after_q <= 0 or cut_count <= 0:
        await message.answer("⚠️ Оба числа должны быть больше нуля. Настройка не сохранена.")
        return

    settings["cuts"].append({"after_question": after_q, "cut_count": cut_count})
    save_config()

    await message.answer(
        f"✅ Срез добавлен: после вопроса №{after_q} срезаются последние {cut_count} мест.",
        reply_markup=None
    )
    await message.answer("✂️ <b>Срезы игроков</b>", parse_mode="HTML", reply_markup=build_cuts_menu())


# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_owner(message.from_user.id):
        await message.answer("✅ Бот-викторина работает! Отправь мне .json файл с вопросами в ЛС для загрузки, "
                              "или напиши /settings для настроек.")
    else:
        await message.answer("✅ Бот-викторина работает!")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_owner(message.from_user.id):
        return
    text = (
        "📖 <b>Список команд:</b>\n\n"
        "▫️ <b>Загрузка вопросов:</b> Отправь .json файл боту в ЛС!\n"
        "▫️ <b>/packs</b> — показать доступные JSON-паки\n"
        "▫️ <b>/loadpack имя_файла.json</b> — загрузить пак вопросов\n"
        "▫️ <b>/start_quiz</b> — начать викторину, сбросить баллы\n"
        "▫️ <b>/next</b> — отправить следующий вопрос\n"
        "▫️ <b>/stats</b> — показать таблицу лидеров\n"
        "▫️ <b>/settings</b> — меню настроек (срезы игроков и т.д.)"
    )
    await message.answer(text, parse_mode="HTML")


# Прием JSON файлов в ЛС только от владельца
@dp.message(F.document)
async def handle_json_upload(message: Message):
    if not is_owner(message.from_user.id):
        return

    document = message.document
    if not document.file_name.endswith('.json'):
        return

    file_info = await bot.get_file(document.file_id)
    destination_name = document.file_name

    try:
        downloaded_file = await bot.download_file(file_info.file_path)
        content = downloaded_file.read().decode('utf-8')
        data = json.loads(content)

        if not isinstance(data, list) or len(data) == 0:
            await message.reply("❌ Ошибка: Файл должен содержать список вопросов (массив `[...]`).")
            return

        for idx, item in enumerate(data, 1):
            if not isinstance(item, dict) or "question" not in item or "options" not in item or "correct_index" not in item:
                await message.reply(f"❌ Ошибка в вопросе №{idx}: нужны ключи `question`, `options` и `correct_index`.")
                return

        with open(destination_name, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        await message.reply(
            f"📥 <b>Файл <code>{destination_name}</code> сохранён!</b>\n"
            f"📊 Всего вопросов в паке: {len(data)}\n\n"
            f"Загрузить в викторину:\n<code>/loadpack {destination_name}</code>",
            parse_mode="HTML"
        )

    except json.JSONDecodeError:
        await message.reply("❌ Ошибка: Битный JSON файл!")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    if not await is_admin(message): return
    files = [f for f in os.listdir('.') if f.endswith('.json') and f != CONFIG_FILE]
    if not files:
        await message.answer("📭 JSON-паков в папке не найдено.")
        return
    text = "📚 <b>Доступные паки викторин:</b>\n\n"
    for f in files: text += f"▫️ <code>{f}</code>\n"
    text += "\nЧтобы загрузить:\n<code>/loadpack имя_файла.json</code>"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("loadpack"))
async def cmd_loadpack(message: Message, command: CommandObject):
    if not await is_admin(message): return
    if not command.args:
        await message.answer("⚠️ Укажи имя файла! Пример: <code>/loadpack quiz.json</code>", parse_mode="HTML")
        return

    filename = command.args.strip()
    if not os.path.exists(filename):
        await message.answer(f"❌ Файл <code>{filename}</code> не найден!", parse_mode="HTML")
        return

    try:
        with open(filename, 'r', encoding='utf-8') as file:
            data = json.load(file)
            if not isinstance(data, list):
                await message.answer("❌ Ошибка: JSON должен содержать список вопросов.")
                return
            quiz_state["questions"] = data
            await message.answer(f"✅ Пак <code>{filename}</code> загружен! Вопросов: {len(data)}\nПиши /start_quiz для старта.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения: {e}")


@dp.message(Command("start_quiz"))
async def cmd_start_quiz(message: Message):
    if not await is_admin(message): return
    if not quiz_state["questions"]:
        await message.answer("⚠️ Вопросы не загружены! Загрузи пак через /loadpack")
        return

    quiz_state["current_index"] = 0
    quiz_state["scores"] = {}
    quiz_state["eliminated"] = {}
    quiz_state["active_poll_id"] = None

    cuts_summary = ""
    if settings["cuts"]:
        cuts_sorted = sorted(settings["cuts"], key=lambda c: c["after_question"])
        cuts_summary = "\n\n✂️ Настроенные срезы:\n" + "\n".join(
            f"— после в.{c['after_question']}: срез {c['cut_count']}" for c in cuts_sorted
        )

    await message.answer(f"🎮 Викторина инициализирована! Баллы сброшены.\nВведи /next, чтобы запустить первый вопрос.{cuts_summary}")


async def scheduled_cut_task(chat_id: int, question_number: int, delay: int):
    """Ждёт закрытия опроса, потом применяет срез и присылает итог в чат, откуда шёл /next."""
    await asyncio.sleep(delay + 1)  # +1с запас, чтобы опрос точно успел закрыться в Telegram

    # Проверяем, что за это время не запустили новый вопрос раньше времени / не сбросили игру
    if quiz_state["current_index"] != question_number:
        return

    before = len(quiz_state["scores"])
    apply_cuts_up_to(question_number)
    after = len(quiz_state["scores"])
    cut = before - after

    if cut > 0:
        newly_eliminated = [
            data["name"] for data in quiz_state["eliminated"].values()
            if data["cut_at"] == question_number
        ]
        names = ", ".join(newly_eliminated)
        await bot.send_message(
            chat_id,
            f"✂️ <b>Срез после вопроса №{question_number} применён!</b>\n"
            f"Выбыли: {names}",
            parse_mode="HTML"
        )


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if not await is_admin(message): return

    if quiz_state["active_poll_id"]:
        try:
            await bot.stop_poll(
                chat_id=quiz_state["active_chat_id"],
                message_id=quiz_state["active_message_id"]
            )
        except TelegramBadRequest:
            pass
        quiz_state["active_poll_id"] = None

    if quiz_state["current_index"] >= len(quiz_state["questions"]):
        await message.answer("🏁 Вопросы закончились! Введи /stats для итогов.")
        return

    q = quiz_state["questions"][quiz_state["current_index"]]
    open_period = 15

    sent_msg = await bot.send_poll(
        chat_id=message.chat.id,
        question=q["question"],
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct_index"],
        is_anonymous=False,
        open_period=open_period
    )

    quiz_state["active_poll_id"] = sent_msg.poll.id
    quiz_state["active_chat_id"] = sent_msg.chat.id
    quiz_state["active_message_id"] = sent_msg.message_id
    quiz_state["first_blood_taken"] = False
    quiz_state["current_index"] += 1

    question_number = quiz_state["current_index"]
    upcoming_cut = get_cut_for_question(question_number)
    if upcoming_cut:
        await message.answer(
            f"⚠️ После этого вопроса (№{question_number}) произойдёт срез: "
            f"выбывают последние {upcoming_cut['cut_count']} мест."
        )
        asyncio.create_task(scheduled_cut_task(message.chat.id, question_number, open_period))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_admin(message): return

    if not quiz_state["scores"] and not quiz_state["eliminated"]:
        text = "🤷‍♂️ Пока никто не заработал баллов."
    else:
        text = ""
        if quiz_state["scores"]:
            sorted_scores = sorted(quiz_state["scores"].values(), key=lambda x: x["score"], reverse=True)
            text += "🏆 <b>Таблица лидеров:</b>\n\n"
            for i, user_data in enumerate(sorted_scores, 1):
                text += f"{i}. {user_data['name']} — {user_data['score']} баллов\n"

        if quiz_state["eliminated"]:
            sorted_elim = sorted(quiz_state["eliminated"].values(), key=lambda x: x["score"], reverse=True)
            text += "\n💀 <b>Выбывшие:</b>\n\n"
            for user_data in sorted_elim:
                text += f"▫️ {user_data['name']} — {user_data['score']} баллов (срезан после в.{user_data['cut_at']})\n"

    try:
        await bot.send_message(message.from_user.id, text, parse_mode="HTML")
        if message.chat.id != message.from_user.id:
            await message.answer("📩 Таблица лидеров отправлена в личные сообщения.")
    except TelegramBadRequest:
        await message.answer("⚠️ Не могу отправить таблицу в ЛС! Напиши боту сначала.")


@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    if poll_answer.poll_id != quiz_state["active_poll_id"]:
        return

    question_number = quiz_state["current_index"]  # номер только что отвеченного вопроса (1-индексация)
    current_q = quiz_state["questions"][question_number - 1]
    chosen_option = poll_answer.option_ids[0]
    user_id = poll_answer.user.id

    # Если игрок уже выбыл — не начисляем баллы
    if user_id in quiz_state["eliminated"]:
        return

    if chosen_option == current_q["correct_index"]:
        user_name = poll_answer.user.first_name

        if user_id not in quiz_state["scores"]:
            quiz_state["scores"][user_id] = {"name": user_name, "score": 0}

        if not quiz_state["first_blood_taken"]:
            quiz_state["scores"][user_id]["score"] += 2
            quiz_state["first_blood_taken"] = True
        else:
            quiz_state["scores"][user_id]["score"] += 1


@dp.message(Command("cutcheck"))
async def cmd_cutcheck(message: Message):
    """Ручное применение срезов на случай, если poll закрылся до истечения всех ответов
    и нужно применить срез именно сейчас (после текущего отвеченного вопроса)."""
    if not await is_admin(message): return
    question_number = quiz_state["current_index"]
    before = len(quiz_state["scores"])
    apply_cuts_up_to(question_number)
    after = len(quiz_state["scores"])
    cut = before - after
    if cut > 0:
        await message.answer(f"✂️ Применён срез: выбыло {cut} игроков после вопроса №{question_number}.")
    else:
        await message.answer(f"ℹ️ Для вопроса №{question_number} срезов не настроено или они уже применены.")


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
    return web.Response(text="Quiz Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    print("Бот викторины запущен!")
    load_config()
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())