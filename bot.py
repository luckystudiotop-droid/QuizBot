import asyncio
import html
import json
import logging
import os
import random
import sys
import time

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PollAnswer,
)

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "5273553942").split(",") if x.strip()
}
PACKS_DIR = os.environ.get("PACKS_DIR", "packs")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
OPEN_PERIOD = 15
QUIET_SECONDS = 5
DELETE_ADMIN_MESSAGES = False
FINAL_TO_CHAT = True
MAX_LEADERBOARD_ROWS = 50
PAUSE_MIN, PAUSE_MAX = 3, 60
MAX_QUESTION_LEN = 288

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    print("Перед запуском выполни в PowerShell:")
    print('  $env:BOT_TOKEN="твой_токен_от_BotFather"')
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

bot = Bot(token=TOKEN)
dp = Dispatcher()

next_lock = asyncio.Lock()
background_tasks = set()
automod_task = None
PORT = int(os.environ.get("PORT", 10000))


async def health_check(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()
    log.info("HTTP-сервер запущен на порту %s", PORT)

    return runner

def default_settings() -> dict:
    return {"automod": False, "pause": 7, "shuffle": False}


def default_state() -> dict:
    return {
        "pack_name": None, "questions": [], "order": [], "current_index": 0,
        "active_poll_id": None, "active_chat_id": None, "active_message_id": None,
        "active_correct": None, "active_until": 0.0, "quiet_until": 0.0,
        "first_blood_taken": False, "automod_chat_id": None,
        "settings": default_settings(), "scores": {},
    }


quiz_state = default_state()


def save_state() -> None:
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
    except (OSError, json.JSONDecodeError) as e:
        log.error("Не удалось прочитать состояние, начинаю с нуля: %s", e)
        return
    if not isinstance(saved, dict):
        return

    # Загружаем только известные ключи и сохраняем безопасные значения настроек.
    for key, value in saved.items():
        if key == "settings" and isinstance(value, dict):
            quiz_state["settings"].update(
                {k: v for k, v in value.items() if k in quiz_state["settings"]}
            )
        elif key in quiz_state:
            quiz_state[key] = value

    if not isinstance(quiz_state["questions"], list):
        quiz_state["questions"] = []
    if not isinstance(quiz_state["scores"], dict):
        quiz_state["scores"] = {}
    if not isinstance(quiz_state["settings"], dict):
        quiz_state["settings"] = default_settings()

    quiz_state["settings"]["pause"] = max(
        PAUSE_MIN, min(PAUSE_MAX, int(quiz_state["settings"].get("pause", 7)))
    )
    quiz_state["settings"]["automod"] = bool(quiz_state["settings"].get("automod", False))
    quiz_state["settings"]["shuffle"] = bool(quiz_state["settings"].get("shuffle", False))

    if len(quiz_state["order"]) != len(quiz_state["questions"]):
        quiz_state["order"] = list(range(len(quiz_state["questions"])))

    log.info("Состояние восстановлено: вопросов %d, игроков %d",
             len(quiz_state["questions"]), len(quiz_state["scores"]))


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ADMIN_IDS


def poll_is_active() -> bool:
    return bool(quiz_state["active_poll_id"]) and time.time() < quiz_state["active_until"]


def automod_on() -> bool:
    return quiz_state["settings"]["automod"]


def validate_pack(data) -> list:
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
        if not isinstance(question, str) or not (1 <= len(question) <= MAX_QUESTION_LEN):
            errors.append(
                f"Вопрос №{i}: question должен быть строкой до {MAX_QUESTION_LEN} символов "
                f"(остальное резервируется под номер вопроса)."
            )

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
    if quiz_state["active_poll_id"]:
        try:
            await bot.stop_poll(
                chat_id=quiz_state["active_chat_id"],
                message_id=quiz_state["active_message_id"],
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    quiz_state["active_poll_id"] = None
    quiz_state["active_chat_id"] = None
    quiz_state["active_message_id"] = None
    quiz_state["active_correct"] = None
    quiz_state["active_until"] = 0.0
    quiz_state["quiet_until"] = 0.0
    save_state()


async def send_final_results(chat_id: int, poll_id: str) -> None:
    await asyncio.sleep(OPEN_PERIOD + 2)
    if quiz_state["active_poll_id"] != poll_id:
        return
    text = build_leaderboard("🏁 <b>Викторина окончена! Итоги:</b>")
    target = chat_id if FINAL_TO_CHAT else next(iter(ADMIN_IDS))
    try:
        await bot.send_message(target, text, parse_mode="HTML")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.error("Не удалось отправить итоги: %s", e)


async def send_next_question(chat_id: int):
    async with next_lock:
        await close_active_poll()

        total = len(quiz_state["questions"])
        if quiz_state["current_index"] >= total:
            return "finished", "🏁 Вопросы закончились! Введи /stats для подведения итогов."

        number = quiz_state["current_index"] + 1
        q = quiz_state["questions"][quiz_state["order"][quiz_state["current_index"]]]

        try:
            sent = await bot.send_poll(
                chat_id=chat_id,
                question=f"[{number}/{total}] {q['question']}",
                options=q["options"],
                type="quiz",
                correct_option_id=q["correct_index"],
                is_anonymous=False,
                open_period=OPEN_PERIOD,
            )
        except TelegramBadRequest as e:
            quiz_state["current_index"] += 1
            save_state()
            return "error", f"❌ Вопрос №{number} не отправился и пропущен: {html.escape(str(e))}"

        quiz_state["active_poll_id"] = sent.poll.id
        quiz_state["active_chat_id"] = sent.chat.id
        quiz_state["active_message_id"] = sent.message_id
        quiz_state["active_correct"] = q["correct_index"]
        quiz_state["active_until"] = time.time() + OPEN_PERIOD
        quiz_state["quiet_until"] = time.time() + QUIET_SECONDS
        quiz_state["first_blood_taken"] = False
        quiz_state["current_index"] += 1
        save_state()

        if quiz_state["current_index"] >= total:
            task = asyncio.create_task(send_final_results(sent.chat.id, sent.poll.id))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            return "sent_last", ""
        return "sent", ""


async def automod_sleep(seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not automod_on():
            return False
        await asyncio.sleep(max(0.05, min(1.0, end - time.time())))
    return automod_on()


async def automod_loop() -> None:
    log.info("AutoMod запущен")
    try:
        while automod_on():
            try:
                chat_id = quiz_state["automod_chat_id"]
                pause = quiz_state["settings"]["pause"]
                remaining = quiz_state["active_until"] - time.time()
                if remaining > 0:
                    if not await automod_sleep(remaining + pause):
                        break

                status, text = await send_next_question(chat_id)

                if status == "error":
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                    if not await automod_sleep(pause):
                        break
                elif status in ("sent_last", "finished"):
                    quiz_state["settings"]["automod"] = False
                    save_state()
                    break
            except TelegramForbiddenError:
                log.error("AutoMod: у бота нет доступа к чату, выключаю")
                quiz_state["settings"]["automod"] = False
                save_state()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("AutoMod: ошибка в цикле, повтор через 5 сек")
                await asyncio.sleep(5)
    finally:
        log.info("AutoMod остановлен")


def ensure_automod_task() -> None:
    global automod_task
    if automod_task is None or automod_task.done():
        automod_task = asyncio.create_task(automod_loop())


async def set_automod(enabled: bool, chat_id: int) -> str:
    settings = quiz_state["settings"]

    if not enabled:
        settings["automod"] = False
        save_state()
        return "⏹ AutoMod выключен. Текущий опрос закроется сам, дальше — вручную через /next."

    if not quiz_state["questions"]:
        return "⚠️ Сначала загрузи пак: /loadpack имя_файла.json"
    if quiz_state["current_index"] >= len(quiz_state["questions"]):
        return "⚠️ Вопросы закончились. Сделай /start_quiz, чтобы начать заново."

    settings["automod"] = True
    quiz_state["automod_chat_id"] = chat_id
    save_state()
    ensure_automod_task()
    return f"🤖 AutoMod включён: вопросы идут сами, пауза между ними {settings['pause']} с."


class QuietWindowMiddleware(BaseMiddleware):
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
                if not self.warned:
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
            return None

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
        "/automod — включить/выключить AutoMod (только админ)\n"
        "/settings — меню настроек (только админ)\n"
        "/stats — таблица лидеров в личку (только админ)"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    if not is_admin(message):
        return
    files = sorted(f for f in os.listdir(PACKS_DIR) if f.endswith(".json"))
    if not files:
        await message.answer(
            f"📭 В папке <code>{html.escape(PACKS_DIR)}/</code> нет JSON-паков.",
            parse_mode="HTML",
        )
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
        await message.answer(
            "⚠️ Укажи имя файла! Пример: <code>/loadpack quiz.json</code>",
            parse_mode="HTML",
        )
        return
    if automod_on():
        await message.answer("⚠️ Сначала выключи AutoMod: /automod")
        return
    if poll_is_active():
        await message.answer("⚠️ Сейчас идёт опрос. Дождись его закрытия и повтори команду.")
        return

    filename = os.path.basename(command.args.strip())
    path = os.path.join(PACKS_DIR, filename)
    if not os.path.isfile(path):
        await message.answer(
            f"❌ Файл <code>{html.escape(filename)}</code> не найден в "
            f"<code>{html.escape(PACKS_DIR)}/</code>!",
            parse_mode="HTML",
        )
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
    quiz_state["order"] = list(range(len(data)))
    quiz_state["pack_name"] = filename
    quiz_state["current_index"] = 0
    quiz_state["active_poll_id"] = None
    quiz_state["active_chat_id"] = None
    quiz_state["active_message_id"] = None
    quiz_state["active_correct"] = None
    quiz_state["active_until"] = 0.0
    quiz_state["quiet_until"] = 0.0
    quiz_state["first_blood_taken"] = False
    quiz_state["scores"] = {}
    save_state()

    await message.answer(
        f"✅ Пак <code>{html.escape(filename)}</code> загружен! Вопросов: {len(data)}\n"
        "Теперь можешь писать /start_quiz.",
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
    if automod_on():
        await message.answer("⚠️ Сначала выключи AutoMod: /automod")
        return

    async with next_lock:
        await close_active_poll()
        order = list(range(len(quiz_state["questions"])))
        if quiz_state["settings"]["shuffle"]:
            random.shuffle(order)
        quiz_state["order"] = order
        quiz_state["current_index"] = 0
        quiz_state["scores"] = {}
        quiz_state["first_blood_taken"] = False
        quiz_state["automod_chat_id"] = None
        save_state()

    shuffle_note = " Порядок вопросов перемешан." if quiz_state["settings"]["shuffle"] else ""
    await message.answer(
        f"🎮 Викторина инициализирована! Баллы сброшены.{shuffle_note}\n"
        "Введи /next для ручного режима или /automod для автоматического."
    )


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if not is_admin(message):
        return
    if automod_on():
        await message.answer("🤖 AutoMod включён, вопросы идут сами. Выключи его: /automod")
        return
    if not quiz_state["questions"]:
        await message.answer("⚠️ Сначала загрузи пак: /loadpack")
        return

    status, text = await send_next_question(message.chat.id)
    if text:
        await message.answer(
            text + ("\nВведи /next, чтобы продолжить." if status == "error" else ""),
            parse_mode="HTML",
        )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    text = build_leaderboard()
    try:
        await bot.send_message(message.from_user.id, text, parse_mode="HTML")
    except TelegramForbiddenError:
        await message.answer("⚠️ Не могу написать тебе в личку! Сначала отправь боту /start в личных сообщениях.")
    except TelegramBadRequest as e:
        log.error("Ошибка отправки таблицы: %s", e)
        await message.answer(f"⚠️ Не удалось отправить таблицу: {html.escape(str(e))}", parse_mode="HTML")
        return
    if message.chat.type != "private":
        await message.answer("📩 Таблица лидеров отправлена ведущему в личные сообщения.")


@dp.message(Command("automod"))
async def cmd_automod(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "1"):
        enabled = True
    elif arg in ("off", "выкл", "0"):
        enabled = False
    else:
        enabled = not automod_on()
    await message.answer(await set_automod(enabled, message.chat.id))


def settings_text() -> str:
    s = quiz_state["settings"]
    return (
        "⚙️ <b>Настройки викторины</b>\n\n"
        f"🤖 AutoMod: <b>{'включён' if s['automod'] else 'выключен'}</b>\n"
        f"⏱ Пауза между вопросами: <b>{s['pause']} с</b>\n"
        f"🔀 Перемешивание вопросов: <b>{'включено' if s['shuffle'] else 'выключено'}</b>\n\n"
        "Перемешивание применяется при следующем /start_quiz."
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    s = quiz_state["settings"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🤖 AutoMod: {'ВКЛ ✅' if s['automod'] else 'ВЫКЛ ❌'}",
            callback_data="set:automod")],
        [InlineKeyboardButton(
            text=f"🔀 Перемешивание: {'ВКЛ ✅' if s['shuffle'] else 'ВЫКЛ ❌'}",
            callback_data="set:shuffle")],
        [
            InlineKeyboardButton(text="−5", callback_data="set:pause:-5"),
            InlineKeyboardButton(text="−1", callback_data="set:pause:-1"),
            InlineKeyboardButton(text=f"⏱ {s['pause']} с", callback_data="set:noop"),
            InlineKeyboardButton(text="+1", callback_data="set:pause:1"),
            InlineKeyboardButton(text="+5", callback_data="set:pause:5"),
        ],
    ])


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_admin(message):
        return
    await message.answer(settings_text(), reply_markup=settings_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("set:"))
async def on_settings_button(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для ведущего.", show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]
    settings = quiz_state["settings"]
    note = None

    if action == "automod":
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        note = await set_automod(not automod_on(), chat_id)
    elif action == "shuffle":
        settings["shuffle"] = not settings["shuffle"]
        save_state()
    elif action == "pause":
        try:
            delta = int(parts[2])
        except (IndexError, ValueError):
            delta = 0
        settings["pause"] = max(PAUSE_MIN, min(PAUSE_MAX, settings["pause"] + delta))
        save_state()

    await cb.answer(note[:190] if note else None)

    if cb.message:
        try:
            await cb.message.edit_text(
                settings_text(), reply_markup=settings_keyboard(), parse_mode="HTML"
            )
        except TelegramBadRequest:
            pass


@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    if poll_answer.poll_id != quiz_state["active_poll_id"]:
        return
    if not poll_answer.option_ids:
        return
    if poll_answer.option_ids[0] != quiz_state["active_correct"]:
        return

    user = poll_answer.user
    uid = str(user.id)
    player = quiz_state["scores"].setdefault(
        uid, {"name": user.first_name, "username": None, "score": 0}
    )
    player["name"] = user.first_name
    player["username"] = user.username

    if not quiz_state["first_blood_taken"]:
        player["score"] += 2
        quiz_state["first_blood_taken"] = True
    else:
        player["score"] += 1
    save_state()


async def main():
    os.makedirs(PACKS_DIR, exist_ok=True)
    load_state()

    if automod_on() and quiz_state["automod_chat_id"]:
        ensure_automod_task()

    web_runner = await start_web_server()

    print("Бот запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        if automod_task and not automod_task.done():
            automod_task.cancel()
            try:
                await automod_task
            except asyncio.CancelledError:
                pass

        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
