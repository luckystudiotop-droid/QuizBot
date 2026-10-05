import asyncio
import html
import json
import logging
import os
import sys
import time

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, PollAnswer

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
TOKEN = os.environ.get("BOT_TOKEN")

# Список админов через запятую: ADMIN_IDS="5273553942,123456789"
# Если переменная не задана — используется твой ID по умолчанию.
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "5273553942").split(",") if x.strip()
}

PACKS_DIR = os.environ.get("PACKS_DIR", "packs")      # папка с JSON-паками
STATE_FILE = os.environ.get("STATE_FILE", "state.json")  # файл сохранения игры
OPEN_PERIOD = 15          # сколько секунд открыт опрос
FINAL_TO_CHAT = True      # True: итоги после последнего вопроса идут в чат; False: админу в личку
MAX_LEADERBOARD_ROWS = 50
QUIET_SECONDS = 5            # сколько секунд после вопроса удалять сообщения в чате
DELETE_ADMIN_MESSAGES = False  # True: удалять и сообщения админов тоже

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    print("Перед запуском выполни в PowerShell:")
    print('  $env:BOT_TOKEN="твой_токен_от_BotFather"')
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

bot = Bot(token=TOKEN)
dp = Dispatcher()

next_lock = asyncio.Lock()      # защита от двойного /next
background_tasks = set()        # чтобы фоновые задачи не собирались сборщиком мусора


# =====================================================================
# СОСТОЯНИЕ ИГРЫ (сохраняется в STATE_FILE)
# =====================================================================
def default_state() -> dict:
    return {
        "pack_name": None,
        "questions": [],
        "current_index": 0,          # сколько вопросов уже задано
        "active_poll_id": None,
        "active_chat_id": None,
        "active_message_id": None,
        "active_correct": None,      # правильный индекс текущего опроса
        "active_until": 0.0,         # unix-время закрытия текущего опроса
        "quiet_until": 0.0,          # до какого времени удаляем сообщения в чате
        "first_blood_taken": False,
        # { "user_id": {"name": "Имя", "username": "nick" | None, "score": 10} }
        "scores": {},
    }


quiz_state = default_state()


def save_state() -> None:
    """Атомарная запись: сначала во временный файл, потом подмена."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(quiz_state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.error("Не удалось сохранить состояние: %s", e)


def load_state() -> None:
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            quiz_state.update({k: v for k, v in saved.items() if k in quiz_state})
            log.info("Состояние восстановлено: вопросов %d, игроков %d",
                     len(quiz_state["questions"]), len(quiz_state["scores"]))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Не удалось прочитать состояние, начинаю с нуля: %s", e)


# =====================================================================
# ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ADMIN_IDS


def poll_is_active() -> bool:
    return bool(quiz_state["active_poll_id"]) and time.time() < quiz_state["active_until"]


def validate_pack(data) -> list:
    """Возвращает список ошибок. Пустой список = пак корректный."""
    if not isinstance(data, list):
        return ["JSON должен содержать список вопросов (массив)."]
    if not data:
        return ["Пак пустой."]

    errors = []
    for i, q in enumerate(data, 1):
        if not isinstance(q, dict):
            errors.append(f"Вопрос №{i}: это не объект.")
            continue

        question = q.get("question")
        if not isinstance(question, str) or not (1 <= len(question) <= 300):
            errors.append(f"Вопрос №{i}: поле question должно быть строкой до 300 символов.")

        options = q.get("options")
        if not isinstance(options, list) or not (2 <= len(options) <= 10):
            errors.append(f"Вопрос №{i}: options должен быть списком из 2–10 вариантов.")
        elif any(not isinstance(o, str) or not (1 <= len(o) <= 100) for o in options):
            errors.append(f"Вопрос №{i}: каждый вариант — строка до 100 символов.")

        idx = q.get("correct_index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            errors.append(f"Вопрос №{i}: correct_index должен быть целым числом.")
        elif isinstance(options, list) and not (0 <= idx < len(options)):
            errors.append(f"Вопрос №{i}: correct_index={idx} выходит за границы вариантов.")
    return errors


def player_label(user_id: str, p: dict) -> str:
    """@username (Telegram сам делает его кликабельным).
    Если username нет — кликабельная ссылка на профиль по имени."""
    if p.get("username"):
        return "@" + html.escape(p["username"])
    return f'<a href="tg://user?id={user_id}">{html.escape(p.get("name") or "Игрок")}</a>'


def build_leaderboard(title: str = "🏆 <b>Таблица лидеров:</b>") -> str:
    if not quiz_state["scores"]:
        return "🤷‍♂️ Пока никто не заработал баллов."

    rows = sorted(
        quiz_state["scores"].items(),
        key=lambda kv: (-kv[1]["score"], (kv[1].get("name") or "").lower()),
    )
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [title, ""]
    for place, (uid, p) in enumerate(rows[:MAX_LEADERBOARD_ROWS], 1):
        mark = medals.get(place, f"{place}.")
        lines.append(f"{mark} {player_label(uid, p)} — {p['score']} баллов")
    if len(rows) > MAX_LEADERBOARD_ROWS:
        lines.append(f"…и ещё {len(rows) - MAX_LEADERBOARD_ROWS}")
    return "\n".join(lines)


async def close_active_poll() -> None:
    """Закрывает активный опрос (если он ещё открыт) и сбрасывает его в состоянии."""
    if quiz_state["active_poll_id"]:
        try:
            await bot.stop_poll(
                chat_id=quiz_state["active_chat_id"],
                message_id=quiz_state["active_message_id"],
            )
        except TelegramBadRequest:
            pass  # опрос уже закрылся сам по таймеру
    quiz_state["active_poll_id"] = None
    quiz_state["active_until"] = 0.0
    quiz_state["quiet_until"] = 0.0
    save_state()


async def send_final_results(chat_id: int, poll_id: str) -> None:
    """После последнего вопроса ждёт закрытия опроса и отправляет итоги."""
    await asyncio.sleep(OPEN_PERIOD + 2)
    if quiz_state["active_poll_id"] != poll_id:
        return  # ведущий уже сделал что-то другое (/next, /start_quiz, /loadpack)
    text = build_leaderboard("🏁 <b>Викторина окончена! Итоги:</b>")
    target = chat_id if FINAL_TO_CHAT else next(iter(ADMIN_IDS))
    try:
        await bot.send_message(target, text, parse_mode="HTML")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.error("Не удалось отправить итоги: %s", e)


# =====================================================================
# ХЭНДЛЕРЫ
# =====================================================================
class QuietWindowMiddleware(BaseMiddleware):
    """Первые QUIET_SECONDS секунд после вопроса удаляет сообщения в игровом чате,
    чтобы вариант ответа не «уезжал» из-под пальца."""

    def __init__(self):
        self.warned = False

    async def __call__(self, handler, event: Message, data):
        in_window = (
            time.time() < quiz_state["quiet_until"]
            and event.chat.id == quiz_state["active_chat_id"]
        )
        from_admin = event.from_user is not None and event.from_user.id in ADMIN_IDS

        if in_window and (DELETE_ADMIN_MESSAGES or not from_admin):
            try:
                await event.delete()
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                log.warning("Не удалось удалить сообщение: %s", e)
                if not self.warned:  # предупреждаем админов один раз
                    self.warned = True
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                "⚠️ Не могу удалять сообщения в игровом чате. "
                                "Сделай бота админом с правом «Удаление сообщений».",
                            )
                        except (TelegramBadRequest, TelegramForbiddenError):
                            pass
            return None  # сообщение не передаём дальше в хэндлеры

        return await handler(event, data)


dp.message.outer_middleware(QuietWindowMiddleware())


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("✅ Бот работает")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Список команд:</b>\n\n"
        "/start — проверить, что бот работает\n"
        "/help — показать это сообщение\n"
        "/packs — показать доступные JSON-паки (только админ)\n"
        "/loadpack <code>имя_файла.json</code> — загрузить пак (только админ)\n"
        "/start_quiz — начать викторину, сбросить баллы (только админ)\n"
        "/next — отправить следующий вопрос (только админ)\n"
        "/stats — таблица лидеров в личку (только админ)"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    if not is_admin(message):
        return

    files = sorted(f for f in os.listdir(PACKS_DIR) if f.endswith(".json"))
    if not files:
        await message.answer(f"📭 В папке <code>{html.escape(PACKS_DIR)}/</code> нет JSON-паков.",
                             parse_mode="HTML")
        return

    text = "📚 <b>Доступные паки викторин:</b>\n\n"
    text += "".join(f"▫️ <code>{html.escape(f)}</code>\n" for f in files)
    text += "\nЧтобы загрузить, отправь:\n<code>/loadpack имя_файла.json</code>"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("loadpack"))
async def cmd_loadpack(message: Message, command: CommandObject):
    if not is_admin(message):
        return

    if not command.args:
        await message.answer("⚠️ Укажи имя файла! Пример: <code>/loadpack quiz.json</code>",
                             parse_mode="HTML")
        return

    if poll_is_active():
        await message.answer("⚠️ Сейчас идёт опрос. Дождись его закрытия и повтори команду.")
        return

    # basename не даёт выйти из папки packs/ через ../
    filename = os.path.basename(command.args.strip())
    path = os.path.join(PACKS_DIR, filename)

    if not os.path.isfile(path):
        await message.answer(f"❌ Файл <code>{html.escape(filename)}</code> не найден в "
                             f"<code>{html.escape(PACKS_DIR)}/</code>!", parse_mode="HTML")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        await message.answer(f"❌ Ошибка синтаксиса JSON: {html.escape(str(e))}")
        return
    except (OSError, UnicodeDecodeError) as e:
        await message.answer(f"❌ Не удалось прочитать файл: {html.escape(str(e))}")
        return

    errors = validate_pack(data)
    if errors:
        shown = "\n".join(f"• {html.escape(e)}" for e in errors[:10])
        more = f"\n…и ещё ошибок: {len(errors) - 10}" if len(errors) > 10 else ""
        await message.answer(f"❌ Пак не загружен, найдены ошибки:\n{shown}{more}", parse_mode="HTML")
        return

    quiz_state["questions"] = data
    quiz_state["pack_name"] = filename
    quiz_state["current_index"] = 0
    quiz_state["active_poll_id"] = None
    save_state()

    await message.answer(
        f"✅ Пак <code>{html.escape(filename)}</code> загружен! Вопросов: {len(data)}\n"
        f"Теперь можешь писать /start_quiz.",
        parse_mode="HTML",
    )


@dp.message(Command("start_quiz"))
async def cmd_start_quiz(message: Message):
    if not is_admin(message):
        return

    if not quiz_state["questions"]:
        await message.answer(
            "⚠️ Ошибка: вопросы не загружены!\n"
            "Сначала загрузи пак: <code>/loadpack имя_файла.json</code>",
            parse_mode="HTML",
        )
        return

    async with next_lock:
        await close_active_poll()
        quiz_state["current_index"] = 0
        quiz_state["scores"] = {}
        quiz_state["first_blood_taken"] = False
        save_state()

    await message.answer("🎮 Викторина инициализирована! Баллы сброшены.\n"
                         "Введи /next, чтобы запустить первый вопрос.")


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if not is_admin(message):
        return

    if not quiz_state["questions"]:
        await message.answer("⚠️ Сначала загрузи пак: /loadpack")
        return

    # Lock: второй /next, пришедший одновременно, дождётся первого
    async with next_lock:
        await close_active_poll()

        if quiz_state["current_index"] >= len(quiz_state["questions"]):
            await message.answer("🏁 Вопросы закончились! Введи /stats для подведения итогов.")
            return

        number = quiz_state["current_index"] + 1
        q = quiz_state["questions"][quiz_state["current_index"]]

        try:
            sent = await bot.send_poll(
                chat_id=message.chat.id,
                question=q["question"],
                options=q["options"],
                type="quiz",
                correct_option_id=q["correct_index"],
                is_anonymous=False,
                open_period=OPEN_PERIOD,
            )
        except TelegramBadRequest as e:
            # Не зацикливаемся на кривом вопросе: пропускаем его и сообщаем
            quiz_state["current_index"] += 1
            save_state()
            await message.answer(
                f"❌ Вопрос №{number} не отправился и пропущен: {html.escape(str(e))}\n"
                f"Введи /next, чтобы продолжить.",
                parse_mode="HTML",
            )
            return

        quiz_state["active_poll_id"] = sent.poll.id
        quiz_state["active_chat_id"] = sent.chat.id
        quiz_state["active_message_id"] = sent.message_id
        quiz_state["active_correct"] = q["correct_index"]
        quiz_state["active_until"] = time.time() + OPEN_PERIOD
        quiz_state["quiet_until"] = time.time() + QUIET_SECONDS
        quiz_state["first_blood_taken"] = False
        quiz_state["current_index"] += 1
        save_state()

        # Был последний вопрос — итоги придут сами
        if quiz_state["current_index"] >= len(quiz_state["questions"]):
            task = asyncio.create_task(send_final_results(sent.chat.id, sent.poll.id))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return

    text = build_leaderboard()

    try:
        await bot.send_message(message.from_user.id, text, parse_mode="HTML")
    except TelegramForbiddenError:
        await message.answer("⚠️ Не могу написать тебе в личку! Сначала отправь боту /start в личных сообщениях.")
        return
    except TelegramBadRequest as e:
        log.error("Ошибка отправки таблицы: %s", e)
        await message.answer(f"⚠️ Не удалось отправить таблицу: {html.escape(str(e))}", parse_mode="HTML")
        return

    if message.chat.type != "private":
        await message.answer("📩 Таблица лидеров отправлена ведущему в личные сообщения.")


# Ловит КАЖДЫЙ ответ игрока в опросе
@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    if poll_answer.poll_id != quiz_state["active_poll_id"]:
        return
    if not poll_answer.option_ids:  # ответ отозван
        return

    if poll_answer.option_ids[0] != quiz_state["active_correct"]:
        return

    user = poll_answer.user
    uid = str(user.id)

    player = quiz_state["scores"].setdefault(uid, {"name": user.first_name, "username": None, "score": 0})
    # Обновляем имя и username при каждом правильном ответе
    player["name"] = user.first_name
    player["username"] = user.username

    if not quiz_state["first_blood_taken"]:
        player["score"] += 2
        quiz_state["first_blood_taken"] = True
    else:
        player["score"] += 1

    save_state()


# =====================================================================
async def main():
    os.makedirs(PACKS_DIR, exist_ok=True)
    load_state()
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())