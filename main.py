import asyncio
import logging
import os
import json
import sys
from aiogram.filters import CommandObject
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, PollAnswer
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 5273553942  # ТВОЙ_TELEGRAM_ID (число, без кавычек)

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    print("Перед запуском выполни в PowerShell:")
    print('  $env:BOT_TOKEN="твой_токен_от_BotFather"')
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
    "first_blood_taken": False,  # Успел ли кто-то ответить первым
    "scores": {}  # Структура: { user_id: {"name": "Имя", "score": 10} }
}


# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("✅ Бот работает")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 **Список команд:**\n\n"
        "/start — проверить, что бот работает\n"
        "/help — показать это сообщение\n"
        "/packs — показать доступные JSON-паки с вопросами (только админ)\n"
        "/loadpack имя_файла.json — загрузить пак вопросов (только админ)\n"
        "/start_quiz — начать викторину, сбросить баллы (только админ)\n"
        "/next — отправить следующий вопрос (только админ)\n"
        "/stats — показать таблицу лидеров (только админ)"
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    # Ищем все файлы с расширением .json в текущей папке
    files = [f for f in os.listdir('.') if f.endswith('.json')]

    if not files:
        await message.answer("📭 Паков с вопросами (JSON файлов) в папке не найдено.")
        return

    text = "📚 **Доступные паки викторин:**\n\n"
    for f in files:
        text += f"▫️ `{f}`\n"

    text += "\nЧтобы загрузить, отправь команду:\n`/loadpack имя_файла.json`"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("loadpack"))
async def cmd_loadpack(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return

    # Проверяем, передал ли админ имя файла
    if not command.args:
        await message.answer("⚠️ Укажи имя файла! Пример: `/loadpack quiz.json`", parse_mode="Markdown")
        return

    filename = command.args.strip()

    # Проверяем, существует ли такой файл
    if not os.path.exists(filename):
        await message.answer(f"❌ Файл `{filename}` не найден!", parse_mode="Markdown")
        return

    # Читаем файл и загружаем в quiz_state
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            data = json.load(file)

            # Проверяем, что в файле действительно список (массив)
            if not isinstance(data, list):
                await message.answer("❌ Ошибка формата: JSON должен содержать список вопросов (массив).")
                return

            quiz_state["questions"] = data
            await message.answer(f"✅ Пак `{filename}` успешно загружен! Вопросов в паке: {len(data)}\n"
                                 f"Теперь можешь писать `/start_quiz`.")
    except json.JSONDecodeError:
        await message.answer("❌ Ошибка чтения файла! Проверь синтаксис JSON (запятые, кавычки).")
    except Exception as e:
        await message.answer(f"❌ Произошла непредвиденная ошибка: {e}")


@dp.message(Command("start_quiz"))
async def cmd_start_quiz(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    # Проверка на то, загружены ли вопросы
    if not quiz_state["questions"]:
        await message.answer(
            "⚠️ Ошибка: Вопросы не загружены!\nСначала загрузи пак через команду `/loadpack имя_файла.json`",
            parse_mode="Markdown")
        return

    quiz_state["current_index"] = 0
    quiz_state["scores"] = {}
    quiz_state["active_poll_id"] = None

    await message.answer("🎮 Викторина инициализирована! Баллы сброшены.\n"
                         "Введи /next, чтобы запустить первый вопрос.")


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    # 1. Закрываем предыдущий опрос, если он был отправлен
    if quiz_state["active_poll_id"]:
        try:
            await bot.stop_poll(
                chat_id=quiz_state["active_chat_id"],
                message_id=quiz_state["active_message_id"]
            )
        except TelegramBadRequest:
            # Опрос уже мог закрыться сам по истечении 30 секунд
            pass
        quiz_state["active_poll_id"] = None

    # 2. Проверяем, есть ли еще вопросы (ИСПРАВЛЕНО)
    if quiz_state["current_index"] >= len(quiz_state["questions"]):
        await message.answer("🏁 Вопросы закончились! Введи /stats для подведения итогов.")
        return

    # 3. Достаем текущий вопрос и отправляем опрос (ИСПРАВЛЕНО)
    q = quiz_state["questions"][quiz_state["current_index"]]

    sent_msg = await bot.send_poll(
        chat_id=message.chat.id,
        question=q["question"],
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct_index"],
        is_anonymous=False,
        open_period=15  # Таймер: опрос закроется через 30 секунд
    )

    # 4. Обновляем состояние
    quiz_state["active_poll_id"] = sent_msg.poll.id
    quiz_state["active_chat_id"] = sent_msg.chat.id
    quiz_state["active_message_id"] = sent_msg.message_id
    quiz_state["first_blood_taken"] = False  # Сбрасываем флаг первого ответившего

    # Сдвигаем индекс для следующего раза
    quiz_state["current_index"] += 1


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    if not quiz_state["scores"]:
        text = "🤷‍♂️ Пока никто не заработал баллов."
    else:
        # Сортируем игроков по убыванию баллов
        sorted_scores = sorted(quiz_state["scores"].values(), key=lambda x: x["score"], reverse=True)

        text = "🏆 **Таблица лидеров:**\n\n"
        for i, user_data in enumerate(sorted_scores, 1):
            text += f"{i}. {user_data['name']} — {user_data['score']} баллов\n"

    # Отправляем результат в личку админу
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except TelegramBadRequest:
        await message.answer("⚠️ Не могу отправить таблицу! Сначала напиши боту в личных сообщениях.")
        return

    # Если команда была вызвана не в личке, даём знать в чате, что результат отправлен
    if message.chat.id != ADMIN_ID:
        await message.answer("📩 Таблица лидеров отправлена ведущему в личные сообщения.")


# Хэндлер, который ловит КАЖДЫЙ ответ юзера в опросе
@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    # Убеждаемся, что ответ прилетел именно в текущий активный опрос
    if poll_answer.poll_id != quiz_state["active_poll_id"]:
        return

    # Находим правильный ответ для текущего вопроса (ИСПРАВЛЕНО)
    current_q = quiz_state["questions"][quiz_state["current_index"] - 1]
    chosen_option = poll_answer.option_ids[0]  # В викторине всегда только 1 вариант

    # Если ответ правильный
    if chosen_option == current_q["correct_index"]:
        user_id = poll_answer.user.id
        user_name = poll_answer.user.first_name

        # Регистрируем юзера в таблице, если его там еще нет
        if user_id not in quiz_state["scores"]:
            quiz_state["scores"][user_id] = {"name": user_name, "score": 0}

        # Начисляем баллы
        if not quiz_state["first_blood_taken"]:
            quiz_state["scores"][user_id]["score"] += 2
            quiz_state["first_blood_taken"] = True
        else:
            quiz_state["scores"][user_id]["score"] += 1


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())