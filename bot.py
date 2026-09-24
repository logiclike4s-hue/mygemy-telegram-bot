"""Telegram-бот с поддержкой Google Gemini.

Бот отвечает в личных чатах, а в группах — при обращении по имени/username
или в ответ на сообщение самого бота.
"""

import asyncio
import html
import io
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Literal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.types import Message
from dotenv import load_dotenv
from google import genai
from google.genai import types


load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
configured_model = os.getenv("GEMINI_MODEL")
# Gemini больше не принимает gemini-2.5-flash для новых пользователей.
GEMINI_MODEL = (
    "gemini-3.5-flash-lite"
    if not configured_model or configured_model == "gemini-2.5-flash"
    else configured_model
)
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
MEMORY_DB_PATH = Path(os.getenv("MEMORY_DB_PATH", "bot_memory.sqlite3"))
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "5955636722"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
INPUT_PRICE_PER_MILLION = float(os.getenv("INPUT_PRICE_PER_MILLION", "0"))
OUTPUT_PRICE_PER_MILLION = float(os.getenv("OUTPUT_PRICE_PER_MILLION", "0"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задана переменная окружения GEMINI_API_KEY")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# История хранится отдельно для каждого чата и ограничивается последними сообщениями.
chat_histories: dict[int, Deque[types.Content]] = defaultdict(
    lambda: deque(maxlen=HISTORY_LIMIT)
)
group_modes: dict[int, Literal["all", "one"]] = {}
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
database_lock = asyncio.Lock()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
bot_username: str | None = None
bot_display_name: str | None = None


def initialize_memory() -> None:
    """Создаёт локальное хранилище памяти и режимов групп."""
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'model')),
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                requests INTEGER NOT NULL DEFAULT 0,
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_chars INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS group_modes (
                chat_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('all', 'one'))
            )
            """
        )
        connection.commit()


def register_request_sync(message: Message, prompt: str, answer: str) -> None:
    user = message.from_user
    if user is None:
        return
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, username, first_name, requests, input_chars, output_chars)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                requests = users.requests + 1,
                input_chars = users.input_chars + excluded.input_chars,
                output_chars = users.output_chars + excluded.output_chars
            """,
            (
                user.id,
                user.username,
                user.first_name,
                len(prompt),
                len(answer),
            ),
        )
        connection.commit()


async def register_request(message: Message, prompt: str, answer: str) -> None:
    await asyncio.to_thread(register_request_sync, message, prompt, answer)


def is_user_banned(user_id: int) -> bool:
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        row = connection.execute(
            "SELECT banned FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row[0])


def estimate_cost(input_chars: int, output_chars: int) -> float:
    """Грубая оценка: четыре символа считаются одним токеном."""
    input_tokens = input_chars / 4
    output_tokens = output_chars / 4
    return (
        input_tokens / 1_000_000 * INPUT_PRICE_PER_MILLION
        + output_tokens / 1_000_000 * OUTPUT_PRICE_PER_MILLION
    )


def load_chat_history(chat_id: int) -> Deque[types.Content]:
    """Загружает последние сообщения чата из SQLite."""
    history: Deque[types.Content] = deque(maxlen=HISTORY_LIMIT)
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT role, text FROM chat_memory
            WHERE chat_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, HISTORY_LIMIT),
        ).fetchall()
    for role, text in reversed(rows):
        history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return history


def get_group_mode(chat_id: int) -> Literal["all", "one"]:
    if chat_id not in group_modes:
        with sqlite3.connect(MEMORY_DB_PATH) as connection:
            row = connection.execute(
                "SELECT mode FROM group_modes WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        group_modes[chat_id] = row[0] if row else "one"
    return group_modes[chat_id]


async def save_memory(chat_id: int, role: str, text: str) -> None:
    async with database_lock:
        await asyncio.to_thread(
            _save_memory_sync, chat_id, role, text
        )


def _save_memory_sync(chat_id: int, role: str, text: str) -> None:
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute(
            "INSERT INTO chat_memory (chat_id, role, text) VALUES (?, ?, ?)",
            (chat_id, role, text),
        )
        connection.execute(
            """
            DELETE FROM chat_memory
            WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_memory WHERE chat_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (chat_id, chat_id, HISTORY_LIMIT),
        )
        connection.commit()


async def set_group_mode(chat_id: int, mode: Literal["all", "one"]) -> None:
    group_modes[chat_id] = mode
    async with database_lock:
        await asyncio.to_thread(_set_group_mode_sync, chat_id, mode)


def _set_group_mode_sync(chat_id: int, mode: str) -> None:
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO group_modes (chat_id, mode) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET mode = excluded.mode
            """,
            (chat_id, mode),
        )
        connection.commit()


def is_private_chat(message: Message) -> bool:
    """Возвращает True для личного диалога с ботом."""
    return message.chat.type == ChatType.PRIVATE


def was_bot_mentioned(message: Message) -> bool:
    """Проверяет упоминание бота по username."""
    text = message.text or message.caption or ""
    normalized_text = text.casefold()
    if bot_username and f"@{bot_username.casefold()}" in normalized_text:
        return True
    return False


def is_reply_to_bot(message: Message) -> bool:
    """Проверяет, является ли сообщение ответом на сообщение бота."""
    replied = message.reply_to_message
    return bool(replied and replied.from_user and replied.from_user.id == bot.id)


def clean_prompt(message: Message) -> str:
    """Убирает username бота из запроса, сохраняя сам вопрос."""
    text = (message.text or message.caption or "").strip()
    if bot_username:
        text = text.replace(f"@{bot_username}", "").replace(
            f"@{bot_username.casefold()}", ""
        )
    return " ".join(text.split()).strip()


def format_markdown_tables(text: str) -> str:
    """Преобразует Markdown-таблицы в выровненные HTML-блоки."""
    lines = text.splitlines()
    formatted: list[str] = []
    index = 0

    while index < len(lines):
        if (
            index + 1 < len(lines)
            and "|" in lines[index]
            and "|" in lines[index + 1]
            and all(
                re.fullmatch(r"\s*:?-{3,}:?\s*", cell)
                for cell in lines[index + 1].strip().strip("|").split("|")
            )
        ):
            table_lines = [lines[index]]
            index += 1
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1

            rows = [
                [cell.strip() for cell in line.strip().strip("|").split("|")]
                for line in table_lines
            ]
            column_count = max(len(row) for row in rows)
            rows = [row + [""] * (column_count - len(row)) for row in rows]
            widths = [
                max(len(row[column]) for row in rows)
                for column in range(column_count)
            ]
            rendered_rows = [
                " | ".join(cell.ljust(widths[column]) for column, cell in enumerate(row))
                for row in rows[:1]
            ]
            rendered_rows.append("-+-".join("-" * width for width in widths))
            rendered_rows.extend(
                " | ".join(cell.ljust(widths[column]) for column, cell in enumerate(row))
                for row in rows[2:]
            )
            formatted.append("<pre>" + html.escape("\n".join(rendered_rows)) + "</pre>")
            continue

        formatted.append(html.escape(lines[index]))
        index += 1

    return "\n".join(formatted)


def should_process_message(message: Message) -> bool:
    """Проверяет, должен ли бот отвечать на сообщение в текущем режиме."""
    return is_private_chat(message) or (
        get_group_mode(message.chat.id) == "all"
        or was_bot_mentioned(message)
        or is_reply_to_bot(message)
    )


async def get_attachment(message: Message) -> tuple[types.Part | None, str]:
    """Скачивает фото, файл или голосовое сообщение для передачи Gemini."""
    file_id: str | None = None
    mime_type: str | None = None
    label = ""

    if message.voice:
        file_id = message.voice.file_id
        mime_type = message.voice.mime_type or "audio/ogg"
        label = "голосовое сообщение"
    elif message.audio:
        file_id = message.audio.file_id
        mime_type = message.audio.mime_type or "audio/mpeg"
        label = "аудиофайл"
    elif message.document:
        file_id = message.document.file_id
        mime_type = message.document.mime_type or "application/octet-stream"
        label = f"файл {message.document.file_name or ''}".strip()
    elif message.photo:
        file_id = message.photo[-1].file_id
        mime_type = "image/jpeg"
        label = "изображение"

    if not file_id or not mime_type:
        return None, ""

    downloaded = await bot.download(file_id, destination=io.BytesIO())
    if downloaded is None:
        raise RuntimeError("Telegram не вернул содержимое вложения")
    data = downloaded.getvalue()
    if not data:
        raise RuntimeError("Вложение оказалось пустым")
    return types.Part.from_bytes(data=data, mime_type=mime_type), label


async def stream_gemini(
    chat_id: int, prompt: str, attachment: types.Part | None = None
):
    """Потоково получает ответ Gemini и возвращает его частями."""
    if chat_id not in chat_histories:
        chat_histories[chat_id] = load_chat_history(chat_id)
    history = chat_histories[chat_id]
    current_parts = [types.Part(text=prompt)]
    if attachment:
        current_parts.append(attachment)
    history.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    answer_parts: list[str] = []

    try:
        stream = await gemini_client.aio.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=[*list(history)[:-1], types.Content(
                role="user", parts=current_parts
            )],
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Ты полезный Telegram-ассистент. Отвечай на языке пользователя "
                    "кратко и по существу."
                )
            ),
        )
        async for response in stream:
            part = (response.text or "")
            if part:
                answer_parts.append(part)
                yield part
    except Exception:
        history.pop()
        logger.exception("Ошибка при обращении к Gemini")
        raise

    answer = "".join(answer_parts).strip()
    if not answer:
        history.pop()
        raise RuntimeError("Gemini вернул пустой ответ")

    history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
    await save_memory(chat_id, "user", prompt)
    await save_memory(chat_id, "model", answer)


async def get_gemini_answer(
    chat_id: int, prompt: str, attachment: types.Part | None = None
) -> str:
    """Запасной непотоковый запрос, если поток Gemini недоступен."""
    if chat_id not in chat_histories:
        chat_histories[chat_id] = load_chat_history(chat_id)
    history = chat_histories[chat_id]
    history.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    current_parts = [types.Part(text=prompt)]
    if attachment:
        current_parts.append(attachment)
    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=[*list(history)[:-1], types.Content(
                role="user", parts=current_parts
            )],
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Ты полезный Telegram-ассистент. Отвечай на языке пользователя "
                    "кратко и по существу."
                )
            ),
        )
        answer = (response.text or "").strip()
        if not answer:
            raise RuntimeError("Gemini вернул пустой ответ")
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        await save_memory(chat_id, "user", prompt)
        await save_memory(chat_id, "model", answer)
        return answer
    except Exception:
        history.pop()
        logger.exception("Резервный запрос к Gemini также завершился ошибкой")
        raise


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    if not is_private_chat(message) and not (
        was_bot_mentioned(message) or is_reply_to_bot(message)
    ):
        return
    await message.reply(
        "Привет! В группе упомяни меня через @username или ответь на моё "
        "сообщение."
    )


@router.message(Command("mode"))
async def mode_handler(message: Message) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply("Эта команда работает только в групповом чате.")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.reply("Переключать режим может только администратор группы.")
        return

    command_parts = (message.text or "").split()
    requested_mode = command_parts[1].casefold() if len(command_parts) > 1 else ""
    if requested_mode not in ("all", "one"):
        await message.reply(
            "Использование:\n/mode all — отвечать на все сообщения\n"
            "/mode one — отвечать только на упоминания и ответы боту"
        )
        return

    mode = requested_mode
    await set_group_mode(message.chat.id, mode)
    description = (
        "всем сообщениям" if mode == "all" else "упоминаниям и ответам боту"
    )
    await message.reply(f"Режим изменён: отвечаю на {description}.")


@router.message(Command("image"))
async def image_handler(message: Message) -> None:
    if not should_process_message(message):
        return

    prompt = (message.text or "").partition(" ")[2].strip()
    if not prompt:
        await message.reply("Использование: /image описание изображения")
        return

    try:
        await bot.send_chat_action(message.chat.id, "upload_photo")
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=IMAGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )
        image_part = next(
            (
                part
                for candidate in (response.candidates or [])
                for part in (candidate.content.parts if candidate.content else [])
                if part.inline_data and part.inline_data.data
            ),
            None,
        )
        if image_part is None:
            raise RuntimeError("Модель не вернула изображение")

        image_bytes = image_part.inline_data.data
        await message.reply_photo(
            io.BytesIO(image_bytes),
            caption=(response.text or "").strip()[:1024] or None,
        )
    except Exception as error:
        logger.exception("Ошибка генерации изображения")
        error_message = "Не удалось создать изображение."
        response_error = str(error)
        if "RESOURCE_EXHAUSTED" in response_error or "429" in response_error:
            error_message = (
                "Генерация изображения временно недоступна: Gemini API "
                "вернул 429 — квота для этой модели исчерпана или равна нулю. "
                "Подключите биллинг/доступную квоту в Google AI Studio и "
                "повторите команду позже."
            )
        elif "NOT_FOUND" in response_error or "404" in response_error:
            error_message = (
                f"Модель генерации {IMAGE_MODEL} недоступна для этого API-ключа. "
                "Укажите доступную IMAGE_MODEL в переменных окружения."
            )
        await message.reply(error_message)


@router.message()
async def message_handler(message: Message) -> None:
    # Один чат обрабатывается последовательно, чтобы ответы и история не смешивались.
    async with chat_locks[message.chat.id]:
        await process_message(message)


async def process_message(message: Message) -> None:
    if message.from_user and is_user_banned(message.from_user.id):
        return
    if not should_process_message(message):
        return

    prompt = clean_prompt(message)
    attachment: types.Part | None = None
    attachment_label = ""

    response_message: Message | None = None
    answer_parts: list[str] = []
    last_edit = 0.0
    displayed_answer = ""
    try:
        attachment, attachment_label = await get_attachment(message)
        if not prompt:
            prompt = (
                f"Пользователь отправил {attachment_label or message.content_type}. "
                "Проанализируй его и ответь кратко."
            )
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            async for part in stream_gemini(
                message.chat.id, prompt, attachment
            ):
                answer_parts.append(part)
                current_answer = "".join(answer_parts).strip()
                if response_message is None and current_answer:
                    response_message = await message.reply(current_answer)
                    displayed_answer = current_answer
                    last_edit = time.monotonic()
                    continue

                # Редактируем не чаще раза в секунду, чтобы не упереться в лимиты Telegram.
                now = time.monotonic()
                if (
                    len(current_answer) <= 4096
                    and current_answer
                    and current_answer != displayed_answer
                    and (not last_edit or now - last_edit >= 0.8)
                ):
                    await response_message.edit_text(current_answer)
                    last_edit = now
                    displayed_answer = current_answer
        except Exception:
            # Повторяем запрос без streaming: ответ будет получен даже при сбое потока.
            answer_parts = [
                await get_gemini_answer(message.chat.id, prompt, attachment)
            ]
    except Exception:
        logger.exception("Не удалось обработать сообщение Telegram")
        error_text = "Не удалось получить ответ от Gemini. Попробуйте повторить чуть позже."
        if response_message:
            try:
                await response_message.edit_text(error_text)
            except Exception:
                logger.exception("Не удалось обновить сообщение об ошибке")
                await message.reply(error_text)
        else:
            await message.reply(error_text)
        return

    answer = "".join(answer_parts).strip()
    if answer and message.from_user:
        await register_request(message, prompt, answer)
    formatted_answer = format_markdown_tables(answer)
    if len(formatted_answer) > 4096:
        if response_message:
            await response_message.edit_text(
                formatted_answer[:4096], parse_mode="HTML"
            )
        else:
            await message.reply(formatted_answer[:4096], parse_mode="HTML")
        for offset in range(4096, len(formatted_answer), 4096):
            await message.reply(
                formatted_answer[offset : offset + 4096], parse_mode="HTML"
            )
    elif answer:
        if response_message and formatted_answer != displayed_answer:
            try:
                await response_message.edit_text(
                    formatted_answer, parse_mode="HTML"
                )
            except Exception:
                # Если Telegram уже содержит такой же текст, ответ всё равно оставлен видимым.
                logger.exception("Не удалось показать финальный ответ потокового запроса")
        elif not response_message:
            await message.reply(formatted_answer, parse_mode="HTML")


admin_bot: Bot | None = Bot(token=ADMIN_BOT_TOKEN) if ADMIN_BOT_TOKEN else None
admin_dp = Dispatcher()
admin_authenticated = False
pending_ban: tuple[int, int] | None = None


def admin_allowed(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id == ADMIN_USER_ID
        and admin_authenticated
    )


def find_user_id(identifier: str) -> int | None:
    value = identifier.strip().lstrip("@").casefold()
    if value.isdigit():
        return int(value)
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        row = connection.execute(
            "SELECT user_id FROM users WHERE lower(username) = ?", (value,)
        ).fetchone()
    return int(row[0]) if row else None


@admin_dp.message(Command("start"))
async def admin_start(message: Message) -> None:
    if message.from_user and message.from_user.id == ADMIN_USER_ID:
        await message.answer("Введите пароль командой: /login <пароль>")
    else:
        await message.answer("Доступ запрещён.")


@admin_dp.message(Command("login"))
async def admin_login(message: Message) -> None:
    global admin_authenticated
    if not message.from_user or message.from_user.id != ADMIN_USER_ID:
        await message.answer("Доступ запрещён.")
        return
    if not ADMIN_PASSWORD:
        await message.answer("ADMIN_PASSWORD не настроен на сервере.")
        return
    password = (message.text or "").partition(" ")[2].strip()
    admin_authenticated = password == ADMIN_PASSWORD
    await message.answer(
        "Вход выполнен. Доступны /stats, /top, /ban, /unban и /confirm."
        if admin_authenticated
        else "Неверный пароль."
    )


@admin_dp.message(Command("stats"))
async def admin_stats(message: Message) -> None:
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return
    identifier = (message.text or "").partition(" ")[2].strip()
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        if identifier:
            user_id = find_user_id(identifier)
            row = connection.execute(
                "SELECT user_id, username, first_name, requests, input_chars, output_chars, banned "
                "FROM users WHERE user_id = ?",
                (user_id or -1,),
            ).fetchone()
            if not row:
                await message.answer("Пользователь не найден.")
                return
            await message.answer(
                f"ID: {row[0]}\nUsername: @{row[1] or '-'}\nИмя: {row[2]}\n"
                f"Запросов: {row[3]}\nСимволов ввода: {row[4]}\n"
                f"Символов ответа: {row[5]}\nЗаблокирован: {'да' if row[6] else 'нет'}"
                f"\nПримерная стоимость: ${estimate_cost(row[4], row[5]):.6f}"
            )
            return
        totals = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(requests), 0), COALESCE(SUM(input_chars), 0), "
            "COALESCE(SUM(output_chars), 0) FROM users"
        ).fetchone()
    await message.answer(
        f"Пользователей: {totals[0]}\nЗапросов: {totals[1]}\n"
        f"Символов ввода: {totals[2]}\nСимволов ответа: {totals[3]}\n"
        f"Примерная стоимость: ${estimate_cost(totals[2], totals[3]):.6f}"
    )


@admin_dp.message(Command("top"))
async def admin_top(message: Message) -> None:
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return
    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        rows = connection.execute(
            "SELECT username, first_name, requests FROM users ORDER BY requests DESC LIMIT 10"
        ).fetchall()
    text = "\n".join(
        f"{index}. @{username or '-'} ({first_name}) — {requests}"
        for index, (username, first_name, requests) in enumerate(rows, 1)
    )
    await message.answer(text or "Запросов пока нет.")


@admin_dp.message(Command("ban"))
async def admin_ban(message: Message) -> None:
    global pending_ban
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /ban <chat_id> <user_id или @username>")
        return
    user_id = find_user_id(parts[2])
    if user_id is None:
        await message.answer("Пользователь не найден в статистике.")
        return
    pending_ban = (int(parts[1]), user_id)
    await message.answer(
        f"Подтвердите бан: /confirm\nЧат: {parts[1]}, пользователь: {user_id}"
    )


@admin_dp.message(Command("block"))
async def admin_block(message: Message) -> None:
    """Глобально запрещает пользователю обращаться к основному боту."""
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return

    identifier = (message.text or "").partition(" ")[2].strip()
    user_id = find_user_id(identifier)
    if user_id is None:
        await message.answer(
            "Пользователь не найден. Он должен хотя бы один раз написать "
            "основному боту, чтобы появился в статистике."
        )
        return

    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
        connection.commit()
    await message.answer(
        f"Пользователь {user_id} заблокирован для личных сообщений боту."
    )


@admin_dp.message(Command("unblock"))
async def admin_unblock(message: Message) -> None:
    """Снимает глобальную блокировку пользователя."""
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return

    identifier = (message.text or "").partition(" ")[2].strip()
    user_id = find_user_id(identifier)
    if user_id is None:
        await message.answer("Пользователь не найден.")
        return

    with sqlite3.connect(MEMORY_DB_PATH) as connection:
        connection.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
        connection.commit()
    await message.answer(f"Пользователь {user_id} разблокирован.")


@admin_dp.message(Command("confirm"))
async def admin_confirm(message: Message) -> None:
    global pending_ban
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return
    if not pending_ban or not admin_bot:
        await message.answer("Нет ожидающего действия.")
        return
    chat_id, user_id = pending_ban
    try:
        await admin_bot.ban_chat_member(chat_id, user_id)
        with sqlite3.connect(MEMORY_DB_PATH) as connection:
            connection.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
            connection.commit()
        await message.answer(f"Пользователь {user_id} заблокирован.")
    except Exception:
        logger.exception("Ошибка бана пользователя")
        await message.answer("Не удалось заблокировать пользователя.")
    finally:
        pending_ban = None


@admin_dp.message(Command("unban"))
async def admin_unban(message: Message) -> None:
    if not admin_allowed(message):
        await message.answer("Сначала выполните /login <пароль>.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /unban <chat_id> <user_id>")
        return
    user_id = find_user_id(parts[2])
    if user_id is None:
        await message.answer("Пользователь не найден.")
        return
    if not admin_bot:
        return
    try:
        await admin_bot.unban_chat_member(int(parts[1]), user_id, only_if_banned=True)
        with sqlite3.connect(MEMORY_DB_PATH) as connection:
            connection.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
            connection.commit()
        await message.answer(f"Пользователь {user_id} разблокирован.")
    except Exception:
        logger.exception("Ошибка разбана пользователя")
        await message.answer("Не удалось разблокировать пользователя.")


async def main() -> None:
    global bot_username, bot_display_name
    initialize_memory()
    me = await bot.get_me()
    bot_username = me.username
    bot_display_name = me.first_name
    logger.info("Бот запущен: @%s", bot_username or me.first_name)

    try:
        polling_tasks = [dp.start_polling(bot)]
        if admin_bot and ADMIN_PASSWORD:
            polling_tasks.append(admin_dp.start_polling(admin_bot))
        await asyncio.gather(*polling_tasks)
    finally:
        await bot.session.close()
        if admin_bot:
            await admin_bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
