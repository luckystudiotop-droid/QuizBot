import asyncio
import html
import json
import logging
import os
import random
import sys
import time

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


# =====================================================================
# НАСТРОЙКИ
# =====================================================================
TOKEN = os.environ.get("BOT_TOKEN")

# Список админов через запятую: ADMIN_IDS="5273553942,123456789"
# Если переменная не задана — используется твой ID по умолчанию.
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "5273553942").split(",")
    if x.strip()
}

PACKS_DIR = os.environ.get("PACKS_DIR", "packs")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

OPEN_PERIOD = 15                  # сколько секунд открыт опрос
QUIET_SECONDS = 5                 # сколько секунд удалять сообщения после вопроса
DELETE_ADMIN_MESSAGES = False     # True: удалять и сообщения админов тоже
FINAL_TO_CHAT = True              # True: итоги в игровой чат; False: админу в ЛС
MAX_LEADERBOARD_ROWS = 50

# Пауза между вопросами AutoMod.
PAUSE_MIN, PAUSE_MAX = 3, 60

# Telegram ограничивает длину вопроса. Резервируем место под "[999/999] ".
MAX_QUESTION_LEN = 288

if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не задана!")
    print("Перед запуском выполни в PowerShell:")
    print('  $env:BOT_TOKEN="твой_токен_от_BotFather"')
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("quizbot")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Защита от одновременной отправки вопросов.
next_lock = asyncio.Lock()

# Сильные ссылки на фоновые задачи.
background_tasks = set()

# Одна AutoMod-задача для всего процесса.
automod_task = None


# =====================================================================
# СОСТОЯНИЕ ИГРЫ
# =====================================================================
def default_settings() -> dict:
    return {
        "automod": False,
        "pause": 7,
        "shuffle": False,
    }


def default_state() -> dict:
    return {
        "pack_name": None,
        "questions": [],
        "order": [],
        "current_index": 0,

        "active_poll_id": None,
        "active_chat_id": None,
        "active_message_id": None,
        "active_correct": None,
        "active_until": 0.0,
        "quiet_until": 0.0,

        "first_blood_taken": False,

        # Чат, в котором работает AutoMod.
        "automod_chat_id": None,

        "settings": default_settings(),

        # {
        #   "user_id": {
        #       "name": "Имя",
        #       "username": "nick" | None,
        #       "score": 10
        #   }
        # }
        "scores": {},
    }


quiz_state = default_state()


def save_state() -> None:
    """Атомарно сохраняет состояние."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                quiz_state,
                f,
                ensure_ascii=False,
                indent=2,
            )
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

    # Обновляем только известные ключи, чтобы старый/повреждённый
    # state.json не ломал новую версию бота.
    for key, value in saved.items():
        if key == "settings" and isinstance(value, dict):
            for setting_key in quiz_state["settings"]:
                if setting_key in value:
                    quiz_state["settings"][setting_key] = value[setting_key]
        elif key in quiz_state:
            quiz_state[key] = value

    # Нормализация настроек.
    try:
        quiz_state["settings"]["pause"] = max(
            PAUSE_MIN,
            min(PAUSE_MAX, int(quiz_state["settings"]["pause"])),
        )
    except (TypeError, ValueError):
        quiz_state["settings"]["pause"] = 7

    quiz_state["settings"]["automod"] = bool(
        quiz_state["settings"]["automod"]
    )
    quiz_state["settings"]["shuffle"] = bool(
        quiz_state["settings"]["shuffle"]
    )

    # Старые сохранения могли не содержать order.
    questions_count = len(quiz_state["questions"])
    order = quiz_state["order"]

    if (
        not isinstance(order, list)
        or len(order) != questions_count
        or sorted(order) != list(range(questions_count))
    ):
        quiz_state["order"] = list(range(questions_count))

    # Защита от некорректного current_index.
    try:
        quiz_state["current_index"] = max(
            0,
            min(int(quiz_state["current_index"]), questions_count),
        )
    except (TypeError, ValueError):
        quiz_state["current_index"] = 0

    log.info(
        "Состояние восстановлено: вопросов %d, игроков %d",
        len(quiz_state["questions"]),
        len(quiz_state["scores"]),
    )


# =====================================================================
# ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def is_admin(message: Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in ADMIN_IDS
    )


def poll_is_active() -> bool:
    return (
        bool(quiz_state["active_poll_id"])
        and time.time() < quiz_state["active_until"]
    )


def automod_on() -> bool:
    return bool(quiz_state["settings"]["automod"])


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

        if not isinstance(question, str) or not (
            1 <= len(question) <= MAX_QUESTION_LEN
        ):
            errors.append(
                f"Вопрос №{i}: question должен быть строкой до "
                f"{MAX_QUESTION_LEN} символов."
            )

        options = q.get("options")

        if not isinstance(options, list) or not (
            2 <= len(options) <= 10
        ):
            errors.append(
                f"Вопрос №{i}: options должен быть списком из 2–10 вариантов."
            )
        elif any(
            not isinstance(o, str) or not (1 <= len(o) <= 100)
            for o in options
        ):
            errors.append(
                f"Вопрос №{i}: каждый вариант — строка до 100 символов."
            )

        idx = q.get("correct_index")

        if isinstance(idx, bool) or not isinstance(idx, int):
            errors.append(
                f"Вопрос №{i}: correct_index должен быть целым числом."
            )
        elif isinstance(options, list) and not (
            0 <= idx < len(options)
        ):
            errors.append(
                f"Вопрос №{i}: correct_index={idx} выходит за границы вариантов."
            )

    return errors


def player_label(user_id: str, player: dict) -> str:
    """Формирует кликабельное имя игрока."""
    if player.get("username"):
        return "@" + html.escape(player["username"])

    return (
        f'<a href="tg://user?id={user_id}">'
        f'{html.escape(player.get("name") or "Игрок")}'
        f"</a>"
    )


def build_leaderboard(
    title: str = "🏆 <b>Таблица лидеров:</b>",
) -> str:
    if not quiz_state["scores"]:
        return "🤷‍♂️ Пока никто не заработал баллов."

    rows = sorted(
        quiz_state["scores"].items(),
        key=lambda kv: (
            -kv[1]["score"],
            (kv[1].get("name") or "").lower(),
        ),
    )

    medals = {
        1: "🥇",
        2: "🥈",
        3: "🥉",
    }

    lines = [title, ""]

    for place, (uid, player) in enumerate(
        rows[:MAX_LEADERBOARD_ROWS],
        1,
    ):
        mark = medals.get(place, f"{place}.")
        lines.append(
            f"{mark} {player_label(uid, player)} — "
            f"{player['score']} баллов"
        )

    if len(rows) > MAX_LEADERBOARD_ROWS:
        lines.append(
            f"…и ещё {len(rows) - MAX_LEADERBOARD_ROWS}"
        )

    return "\n".join(lines)


def reset_active_poll_state() -> None:
    """Сбрасывает локальное состояние текущего poll."""
    quiz_state["active_poll_id"] = None
    quiz_state["active_chat_id"] = None
    quiz_state["active_message_id"] = None
    quiz_state["active_correct"] = None
    quiz_state["active_until"] = 0.0
    quiz_state["quiet_until"] = 0.0
    quiz_state["first_blood_taken"] = False


def reset_quiz_state_for_new_pack() -> None:
    """Полностью сбрасывает игровое состояние после загрузки нового пака."""
    quiz_state["order"] = list(range(len(quiz_state["questions"])))
    quiz_state["current_index"] = 0
    quiz_state["scores"] = {}
    reset_active_poll_state()


async def close_active_poll(save: bool = True) -> None:
    """Закрывает активный poll, если он ещё существует."""
    poll_id = quiz_state["active_poll_id"]

    if poll_id:
        try:
            await bot.stop_poll(
                chat_id=quiz_state["active_chat_id"],
                message_id=quiz_state["active_message_id"],
            )
        except TelegramBadRequest:
            # Poll мог закрыться сам по open_period.
            pass
        except TelegramForbiddenError:
            log.warning(
                "Нет прав на закрытие poll в чате %s.",
                quiz_state["active_chat_id"],
            )

    reset_active_poll_state()

    if save:
        save_state()


async def send_final_results(
    chat_id: int,
    poll_id: str,
) -> None:
    """
    После последнего вопроса ждёт закрытия poll и отправляет итоги.
    Если за это время была запущена новая игра — старые итоги не отправляет.
    """
    await asyncio.sleep(OPEN_PERIOD + 2)

    # Проверяем, что именно этот последний poll всё ещё является
    # актуальным состоянием игры.
    if quiz_state["active_poll_id"] != poll_id:
        return

    text = build_leaderboard(
        "🏁 <b>Викторина окончена! Итоги:</b>"
    )

    target = (
        chat_id
        if FINAL_TO_CHAT
        else next(iter(ADMIN_IDS))
    )

    try:
        await bot.send_message(
            target,
            text,
            parse_mode="HTML",
        )
    except (
        TelegramBadRequest,
        TelegramForbiddenError,
    ) as e:
        log.error("Не удалось отправить итоги: %s", e)


# =====================================================================
# ОТПРАВКА ВОПРОСОВ
# =====================================================================
async def send_next_question(chat_id: int):
    """
    Отправляет следующий вопрос.

    Возвращает:
      "sent"      — обычный вопрос отправлен;
      "sent_last" — отправлен последний вопрос;
      "finished"  — вопросы закончились;
      "error"     — вопрос не отправился.
    """
    async with next_lock:
        # Для ручного /next закрываем предыдущий poll.
        # Для AutoMod к этому моменту он уже обычно закрыт сам.
        if quiz_state["active_poll_id"]:
            await close_active_poll()

        total = len(quiz_state["questions"])

        if total == 0:
            return (
                "finished",
                "🏁 Вопросы не загружены.",
            )

        if quiz_state["current_index"] >= total:
            return (
                "finished",
                "🏁 Вопросы закончились! Введи /stats для подведения итогов.",
            )

        # Проверяем order на случай повреждения state.json.
        order = quiz_state["order"]

        if (
            not isinstance(order, list)
            or len(order) != total
            or sorted(order) != list(range(total))
        ):
            quiz_state["order"] = list(range(total))
            order = quiz_state["order"]

        current_index = quiz_state["current_index"]
        number = current_index + 1

        q = quiz_state["questions"][
            order[current_index]
        ]

        poll_question = (
            f"[{number}/{total}] {q['question']}"
        )

        try:
            sent = await bot.send_poll(
                chat_id=chat_id,
                question=poll_question,
                options=q["options"],
                type="quiz",
                correct_option_id=q["correct_index"],
                is_anonymous=False,
                open_period=OPEN_PERIOD,
            )
        except TelegramBadRequest as e:
            # Вопрос не отправился — пропускаем его, чтобы AutoMod
            # не застрял навсегда.
            quiz_state["current_index"] += 1
            save_state()

            return (
                "error",
                f"❌ Вопрос №{number} не отправился и пропущен: "
                f"{html.escape(str(e))}",
            )

        quiz_state["active_poll_id"] = sent.poll.id
        quiz_state["active_chat_id"] = sent.chat.id
        quiz_state["active_message_id"] = sent.message_id
        quiz_state["active_correct"] = q["correct_index"]
        quiz_state["active_until"] = (
            time.time() + OPEN_PERIOD
        )
        quiz_state["quiet_until"] = (
            time.time() + QUIET_SECONDS
        )

        # Индекс увеличивается только после успешной отправки.
        quiz_state["current_index"] += 1

        save_state()

        if quiz_state["current_index"] >= total:
            task = asyncio.create_task(
                send_final_results(
                    sent.chat.id,
                    sent.poll.id,
                )
            )

            background_tasks.add(task)
            task.add_done_callback(
                background_tasks.discard
            )

            return "sent_last", ""

        return "sent", ""


# =====================================================================
# AUTOMOD
# =====================================================================
async def automod_sleep(seconds: float) -> bool:
    """
    Спит, но каждую секунду проверяет, не выключили ли AutoMod.
    Возвращает False, если AutoMod выключен.
    """
    end = time.time() + max(0, seconds)

    while time.time() < end:
        if not automod_on():
            return False

        remaining = end - time.time()

        await asyncio.sleep(
            max(
                0.05,
                min(1.0, remaining),
            )
        )

    return automod_on()


async def automod_loop() -> None:
    log.info("AutoMod запущен")

    try:
        while automod_on():
            try:
                chat_id = quiz_state["automod_chat_id"]

                if not chat_id:
                    log.error(
                        "AutoMod включён, но automod_chat_id отсутствует."
                    )
                    quiz_state["settings"]["automod"] = False
                    save_state()
                    break

                pause = quiz_state["settings"]["pause"]

                # Если poll ещё идёт — ждём его окончания.
                remaining = (
                    quiz_state["active_until"]
                    - time.time()
                )

                if remaining > 0:
                    if not await automod_sleep(
                        remaining
                    ):
                        break

                # После закрытия вопроса — заданная пауза.
                if pause > 0:
                    if not await automod_sleep(pause):
                        break

                status, text = await send_next_question(
                    chat_id
                )

                if status == "error":
                    if text:
                        await bot.send_message(
                            chat_id,
                            text,
                            parse_mode="HTML",
                        )

                    # После ошибки тоже выдерживаем паузу.
                    if not await automod_sleep(pause):
                        break

                elif status == "sent_last":
                    # Последний вопрос уже отправлен.
                    # После его закрытия send_final_results отправит итоги.
                    quiz_state["settings"]["automod"] = False
                    save_state()
                    break

                elif status == "finished":
                    quiz_state["settings"]["automod"] = False
                    save_state()
                    break

            except TelegramForbiddenError:
                log.error(
                    "AutoMod: у бота нет доступа к чату, выключаю."
                )

                quiz_state["settings"]["automod"] = False
                save_state()
                break

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "AutoMod: ошибка в цикле, повтор через 5 секунд."
                )

                if not await automod_sleep(5):
                    break

    finally:
        log.info("AutoMod остановлен")


def ensure_automod_task() -> None:
    global automod_task

    if automod_task is None or automod_task.done():
        automod_task = asyncio.create_task(
            automod_loop()
        )


async def stop_automod_task() -> None:
    """
    Останавливает существующую AutoMod-задачу.
    Это нужно, чтобы после выключения/перезапуска режима
    не осталось двух циклов одновременно.
    """
    global automod_task

    task = automod_task

    if task is None:
        return

    if not task.done():
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    automod_task = None


async def set_automod(
    enabled: bool,
    chat_id: int,
) -> str:
    """Включает/выключает AutoMod."""
    settings = quiz_state["settings"]

    if not enabled:
        settings["automod"] = False
        save_state()

        # Не ждём завершения здесь, чтобы callback/команда
        # быстро получила ответ.
        current_task = automod_task

        if (
            current_task is not None
            and not current_task.done()
            and current_task is not asyncio.current_task()
        ):
            current_task.cancel()

        return (
            "⏹ AutoMod выключен. "
            "Текущий опрос продолжит идти до конца, "
            "дальше вопросы автоматически отправляться не будут."
        )

    if not quiz_state["questions"]:
        return (
            "⚠️ Сначала загрузи пак: "
            "/loadpack имя_файла.json"
        )

    if quiz_state["current_index"] >= len(
        quiz_state["questions"]
    ):
        return (
            "⚠️ Вопросы закончились. "
            "Сделай /start_quiz, чтобы начать заново."
        )

    # Если AutoMod уже работает — просто переносим его
    # в текущий чат, без создания второго цикла.
    settings["automod"] = True
    quiz_state["automod_chat_id"] = chat_id

    save_state()
    ensure_automod_task()

    return (
        f"🤖 AutoMod включён: вопросы идут сами, "
        f"пауза между ними {settings['pause']} с."
    )


# =====================================================================
# УДАЛЕНИЕ СООБЩЕНИЙ ПОСЛЕ ВОПРОСА
# =====================================================================
class QuietWindowMiddleware(BaseMiddleware):
    """
    Первые QUIET_SECONDS секунд после вопроса удаляет сообщения
    в игровом чате, чтобы варианты ответа не «уезжали» из-под пальца.
    """

    def __init__(self):
        self.warned = False

    async def __call__(
        self,
        handler,
        event: Message,
        data,
    ):
        in_window = (
            time.time() < quiz_state["quiet_until"]
            and event.chat.id
            == quiz_state["active_chat_id"]
        )

        from_admin = (
            event.from_user is not None
            and event.from_user.id in ADMIN_IDS
        )

        if in_window and (
            DELETE_ADMIN_MESSAGES
            or not from_admin
        ):
            try:
                await event.delete()

            except (
                TelegramBadRequest,
                TelegramForbiddenError,
            ) as e:
                log.warning(
                    "Не удалось удалить сообщение: %s",
                    e,
                )

                if not self.warned:
                    self.warned = True

                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                "⚠️ Не могу удалять сообщения "
                                "в игровом чате. Сделай бота админом "
                                "с правом «Удаление сообщений».",
                            )
                        except (
                            TelegramBadRequest,
                            TelegramForbiddenError,
                        ):
                            pass

            return None

        return await handler(event, data)


dp.message.outer_middleware(
    QuietWindowMiddleware()
)


# =====================================================================
# ХЭНДЛЕРЫ
# =====================================================================
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
        "/loadpack <code>имя_файла.json</code> — загрузить пак "
        "(только админ)\n"
        "/start_quiz — начать викторину, сбросить баллы "
        "(только админ)\n"
        "/next — отправить следующий вопрос "
        "(только админ)\n"
        "/automod — включить/выключить AutoMod "
        "(только админ)\n"
        "/automod on — включить AutoMod\n"
        "/automod off — выключить AutoMod\n"
        "/settings — меню настроек (только админ)\n"
        "/stats — таблица лидеров в личку (только админ)"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    if not is_admin(message):
        return

    try:
        files = sorted(
            f
            for f in os.listdir(PACKS_DIR)
            if f.endswith(".json")
        )
    except OSError as e:
        await message.answer(
            f"❌ Не удалось прочитать папку с паками: "
            f"{html.escape(str(e))}"
        )
        return

    if not files:
        await message.answer(
            f"📭 В папке "
            f"<code>{html.escape(PACKS_DIR)}/</code> "
            f"нет JSON-паков.",
            parse_mode="HTML",
        )
        return

    text = "📚 <b>Доступные паки викторин:</b>\n\n"
    text += "".join(
        f"▫️ <code>{html.escape(f)}</code>\n"
        for f in files
    )
    text += (
        "\nЧтобы загрузить, отправь:\n"
        "<code>/loadpack имя_файла.json</code>"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


@dp.message(Command("loadpack"))
async def cmd_loadpack(
    message: Message,
    command: CommandObject,
):
    if not is_admin(message):
        return

    if not command.args:
        await message.answer(
            "⚠️ Укажи имя файла! "
            "Пример: <code>/loadpack quiz.json</code>",
            parse_mode="HTML",
        )
        return

    if automod_on():
        await message.answer(
            "⚠️ Сначала выключи AutoMod: /automod off"
        )
        return

    if poll_is_active():
        await message.answer(
            "⚠️ Сейчас идёт опрос. "
            "Дождись его закрытия и повтори команду."
        )
        return

    filename = os.path.basename(
        command.args.strip()
    )

    path = os.path.join(
        PACKS_DIR,
        filename,
    )

    if not os.path.isfile(path):
        await message.answer(
            f"❌ Файл <code>{html.escape(filename)}</code> "
            f"не найден в "
            f"<code>{html.escape(PACKS_DIR)}/</code>!",
            parse_mode="HTML",
        )
        return

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

    except json.JSONDecodeError as e:
        await message.answer(
            f"❌ Ошибка синтаксиса JSON: "
            f"{html.escape(str(e))}"
        )
        return

    except (
        OSError,
        UnicodeDecodeError,
    ) as e:
        await message.answer(
            f"❌ Не удалось прочитать файл: "
            f"{html.escape(str(e))}"
        )
        return

    errors = validate_pack(data)

    if errors:
        shown = "\n".join(
            f"• {html.escape(e)}"
            for e in errors[:10]
        )

        more = (
            f"\n…и ещё ошибок: {len(errors) - 10}"
            if len(errors) > 10
            else ""
        )

        await message.answer(
            f"❌ Пак не загружен, найдены ошибки:\n"
            f"{shown}{more}",
            parse_mode="HTML",
        )
        return

    # Загружаем новый пак.
    quiz_state["questions"] = data
    quiz_state["pack_name"] = filename

    # ВАЖНО: полностью сбрасываем старую игру,
    # порядок и баллы.
    reset_quiz_state_for_new_pack()

    save_state()

    await message.answer(
        f"✅ Пак <code>{html.escape(filename)}</code> "
        f"загружен! Вопросов: {len(data)}\n"
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
            "Сначала загрузи пак: "
            "<code>/loadpack имя_файла.json</code>",
            parse_mode="HTML",
        )
        return

    if automod_on():
        await message.answer(
            "⚠️ Сначала выключи AutoMod: /automod off"
        )
        return

    async with next_lock:
        await close_active_poll()

        total = len(quiz_state["questions"])
        order = list(range(total))

        if quiz_state["settings"]["shuffle"]:
            random.shuffle(order)

        quiz_state["order"] = order
        quiz_state["current_index"] = 0
        quiz_state["scores"] = {}
        quiz_state["first_blood_taken"] = False

        save_state()

    shuffle_note = (
        " Порядок вопросов перемешан."
        if quiz_state["settings"]["shuffle"]
        else ""
    )

    await message.answer(
        "🎮 Викторина инициализирована! "
        f"Баллы сброшены.{shuffle_note}\n"
        "Введи /next для ручного режима "
        "или /automod для автоматического."
    )


@dp.message(Command("next"))
async def cmd_next(message: Message):
    if not is_admin(message):
        return

    if automod_on():
        await message.answer(
            "🤖 AutoMod включён, вопросы идут сами. "
            "Выключи его: /automod off"
        )
        return

    if not quiz_state["questions"]:
        await message.answer(
            "⚠️ Сначала загрузи пак: /loadpack"
        )
        return

    status, text = await send_next_question(
        message.chat.id
    )

    if text:
        suffix = (
            "\nВведи /next, чтобы продолжить."
            if status == "error"
            else ""
        )

        await message.answer(
            text + suffix,
            parse_mode="HTML",
        )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return

    text = build_leaderboard()

    try:
        await bot.send_message(
            message.from_user.id,
            text,
            parse_mode="HTML",
        )

    except TelegramForbiddenError:
        await message.answer(
            "⚠️ Не могу написать тебе в личку! "
            "Сначала отправь боту /start "
            "в личных сообщениях."
        )
        return

    except TelegramBadRequest as e:
        log.error(
            "Ошибка отправки таблицы: %s",
            e,
        )

        await message.answer(
            f"⚠️ Не удалось отправить таблицу: "
            f"{html.escape(str(e))}",
            parse_mode="HTML",
        )
        return

    if message.chat.type != "private":
        await message.answer(
            "📩 Таблица лидеров отправлена ведущему "
            "в личные сообщения."
        )


# =====================================================================
# AUTOMOD КОМАНДОЙ
# /automod
# /automod on
# /automod off
# =====================================================================
@dp.message(Command("automod"))
async def cmd_automod(
    message: Message,
    command: CommandObject,
):
    if not is_admin(message):
        return

    arg = (
        command.args or ""
    ).strip().lower()

    if arg in ("on", "вкл", "1"):
        enabled = True

    elif arg in ("off", "выкл", "0"):
        enabled = False

    else:
        enabled = not automod_on()

    result = await set_automod(
        enabled,
        message.chat.id,
    )

    await message.answer(result)


# =====================================================================
# МЕНЮ НАСТРОЕК
# =====================================================================
def settings_text() -> str:
    settings = quiz_state["settings"]

    return (
        "⚙️ <b>Настройки викторины</b>\n\n"
        f"🤖 AutoMod: "
        f"<b>{'включён' if settings['automod'] else 'выключен'}</b>\n"
        f"⏱ Пауза между вопросами: "
        f"<b>{settings['pause']} с</b>\n"
        f"🔀 Перемешивание вопросов: "
        f"<b>{'включено' if settings['shuffle'] else 'выключено'}</b>\n\n"
        "Перемешивание применяется при следующем "
        "/start_quiz."
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    settings = quiz_state["settings"]

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "🤖 AutoMod: "
                        f"{'ВКЛ ✅' if settings['automod'] else 'ВЫКЛ ❌'}"
                    ),
                    callback_data="set:automod",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "🔀 Перемешивание: "
                        f"{'ВКЛ ✅' if settings['shuffle'] else 'ВЫКЛ ❌'}"
                    ),
                    callback_data="set:shuffle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="−5",
                    callback_data="set:pause:-5",
                ),
                InlineKeyboardButton(
                    text="−1",
                    callback_data="set:pause:-1",
                ),
                InlineKeyboardButton(
                    text=f"⏱ {settings['pause']} с",
                    callback_data="set:noop",
                ),
                InlineKeyboardButton(
                    text="+1",
                    callback_data="set:pause:1",
                ),
                InlineKeyboardButton(
                    text="+5",
                    callback_data="set:pause:5",
                ),
            ],
        ]
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        settings_text(),
        reply_markup=settings_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("set:"))
async def on_settings_button(
    cb: CallbackQuery,
):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer(
            "Только для ведущего.",
            show_alert=True,
        )
        return

    parts = cb.data.split(":")
    action = parts[1]
    settings = quiz_state["settings"]
    note = None

    if action == "automod":
        chat_id = (
            cb.message.chat.id
            if cb.message
            else cb.from_user.id
        )

        note = await set_automod(
            not automod_on(),
            chat_id,
        )

    elif action == "shuffle":
        settings["shuffle"] = not settings["shuffle"]
        save_state()

        note = (
            "🔀 Перемешивание включено."
            if settings["shuffle"]
            else "🔀 Перемешивание выключено."
        )

    elif action == "pause":
        try:
            delta = int(parts[2])
        except (IndexError, ValueError):
            delta = 0

        old_pause = settings["pause"]

        settings["pause"] = max(
            PAUSE_MIN,
            min(
                PAUSE_MAX,
                old_pause + delta,
            ),
        )

        save_state()

        if settings["pause"] != old_pause:
            note = (
                f"⏱ Пауза: {settings['pause']} с."
            )
        else:
            note = (
                f"⏱ Минимум: {PAUSE_MIN} с."
                if delta < 0
                else f"⏱ Максимум: {PAUSE_MAX} с."
            )

    elif action == "noop":
        note = (
            f"⏱ Сейчас пауза "
            f"{settings['pause']} с."
        )

    await cb.answer(
        note[:190] if note else None
    )

    if cb.message:
        try:
            await cb.message.edit_text(
                settings_text(),
                reply_markup=settings_keyboard(),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            # Например: message is not modified.
            pass


# =====================================================================
# ОТВЕТЫ ИГРОКОВ
# =====================================================================
@dp.poll_answer()
async def handle_poll_answer(
    poll_answer: PollAnswer,
):
    if (
        poll_answer.poll_id
        != quiz_state["active_poll_id"]
    ):
        return

    if not poll_answer.option_ids:
        # Ответ отозван.
        return

    if (
        poll_answer.option_ids[0]
        != quiz_state["active_correct"]
    ):
        return

    user = poll_answer.user
    uid = str(user.id)

    player = quiz_state["scores"].setdefault(
        uid,
        {
            "name": user.first_name,
            "username": None,
            "score": 0,
        },
    )

    # Обновляем данные игрока.
    player["name"] = user.first_name
    player["username"] = user.username

    if not quiz_state["first_blood_taken"]:
        player["score"] += 2
        quiz_state["first_blood_taken"] = True
    else:
        player["score"] += 1

    save_state()


# =====================================================================
# ЗАПУСК
# =====================================================================
async def main():
    os.makedirs(
        PACKS_DIR,
        exist_ok=True,
    )

    load_state()

    # Если бот перезапустился во время AutoMod —
    # продолжаем с сохранённого места.
    if (
        automod_on()
        and quiz_state["automod_chat_id"]
    ):
        ensure_automod_task()

    log.info("Бот запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        await stop_automod_task()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
