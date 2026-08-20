import asyncio
import logging
import os
import json
import sys
from aiohttp import web
from aiogram.filters import CommandObject
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PollAnswer, ChatMemberAdministrator, ChatMemberOwner
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = 5273553942  # Твой ID (как главный создатель)

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЕ ИГРЫ ---
quiz_state = {
    "questions": [],
    "current_index": 0,
    "active_poll_id": None,
    "active_chat_id": None,
    "active_message_id": None,
    "first_blood_taken": False,
    "scores": {}
}


# --- ХЕЛПЕРЫ ПРОВЕРКИ ПРАВ ---
async def is_admin(message: Message) -> bool:
    """Разрешает команды в ЛС хозяину/админу, либо админам текущей группы"""
    if message.chat.type == "private":
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)) or message.from_user.id == OWNER_ID
    except Exception:
        return False


# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("✅ Бот-викторина работает! Отправь мне .json файл с вопросами в ЛС для загрузки.")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Список команд:</b>\n\n"
        "▫️ <b>Загрузка вопросов:</b> Отправь .json файл боту в ЛС!\n"
        "▫️ <b>/packs</b> — показать доступные JSON-паки\n"
        "▫️ <b>/loadpack имя_файла.json</b> — загрузить пак вопросов\n"
        "▫️ <b>/start_quiz</b> — начать викторину, сбросить баллы\n"
        "▫️ <b>/next</b> — отправить следующий вопрос\n"
        "▫️ <b>/stats</b> — показать таблицу лидеров"
    )
    await message.answer(text, parse_mode="HTML")


# Прием JSON файлов в ЛС от админов
@dp.message(F.document)
async def handle_json_upload(message: Message):
    if not await is_admin(message):
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

        # Проверка структуры каждого вопроса
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
    files = [f for f in os.listdir('.') if f.endswith('.json')]
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
    quiz_state["active_poll_id"] = None
    await message.answer("🎮 Викторина инициализирована! Баллы сброшены.\nВведи /next, чтобы запустить первый вопрос.")


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

    sent_msg = await bot.send_poll(
        chat_id=message.chat.id,
        question=q["question"],
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct_index"],
        is_anonymous=False,
        open_period=15
    )

    quiz_state["active_poll_id"] = sent_msg.poll.id
    quiz_state["active_chat_id"] = sent_msg.chat.id
    quiz_state["active_message_id"] = sent_msg.message_id
    quiz_state["first_blood_taken"] = False
    quiz_state["current_index"] += 1


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_admin(message): return

    if not quiz_state["scores"]:
        text = "🤷‍♂️ Пока никто не заработал баллов."
    else:
        sorted_scores = sorted(quiz_state["scores"].values(), key=lambda x: x["score"], reverse=True)
        text = "🏆 <b>Таблица лидеров:</b>\n\n"
        for i, user_data in enumerate(sorted_scores, 1):
            text += f"{i}. {user_data['name']} — {user_data['score']} баллов\n"

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

    current_q = quiz_state["questions"][quiz_state["current_index"] - 1]
    chosen_option = poll_answer.option_ids[0]

    if chosen_option == current_q["correct_index"]:
        user_id = poll_answer.user.id
        user_name = poll_answer.user.first_name

        if user_id not in quiz_state["scores"]:
            quiz_state["scores"][user_id] = {"name": user_name, "score": 0}

        if not quiz_state["first_blood_taken"]:
            quiz_state["scores"][user_id]["score"] += 2
            quiz_state["first_blood_taken"] = True
        else:
            quiz_state["scores"][user_id]["score"] += 1


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
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())