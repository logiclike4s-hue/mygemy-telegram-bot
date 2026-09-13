"""Telegram-бот с поддержкой Google Gemini.

Бот отвечает в личных чатах, а в группах — при обращении по имени/username
или в ответ на сообщение самого бота.
"""

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from typing import Deque

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
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
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))

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
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
bot_username: str | None = None
bot_display_name: str | None = None


def is_private_chat(message: Message) -> bool:
    """Возвращает True для личного диалога с ботом."""
    return message.chat.type == ChatType.PRIVATE


def was_bot_mentioned(message: Message) -> bool:
    """Проверяет обращение по username или отображаемому имени бота."""
    text = message.text or message.caption or ""
    normalized_text = text.casefold()
    if bot_username and f"@{bot_username.casefold()}" in normalized_text:
        return True

    if bot_display_name:
        # Имя считается обращением только как отдельное слово, а не как часть URL.
        words = normalized_text.replace(",", " ").replace(":", " ").split()
        return bot_display_name.casefold() in words
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


async def stream_gemini(chat_id: int, prompt: str):
    """Потоково получает ответ Gemini и возвращает его частями."""
    history = chat_histories[chat_id]
    history.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    answer_parts: list[str] = []

    try:
        stream = await gemini_client.aio.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=list(history),
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


async def get_gemini_answer(chat_id: int, prompt: str) -> str:
    """Запасной непотоковый запрос, если поток Gemini недоступен."""
    history = chat_histories[chat_id]
    history.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=list(history),
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
        return answer
    except Exception:
        history.pop()
        logger.exception("Резервный запрос к Gemini также завершился ошибкой")
        raise


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Напиши мне сообщение в личном чате. В группе упомяни меня "
        "по имени или username либо ответь на моё сообщение."
    )


@router.message(F.text | F.caption)
async def message_handler(message: Message) -> None:
    if not is_private_chat(message) and not (
        was_bot_mentioned(message) or is_reply_to_bot(message)
    ):
        return

    prompt = clean_prompt(message)
    if not prompt:
        await message.answer("Напиши вопрос или текст, который нужно обработать.")
        return

    response_message: Message | None = None
    answer_parts: list[str] = []
    last_edit = 0.0
    displayed_answer = "Готовлю ответ..."
    try:
        await bot.send_chat_action(message.chat.id, "typing")
        response_message = await message.answer("Готовлю ответ...")
        try:
            async for part in stream_gemini(message.chat.id, prompt):
                answer_parts.append(part)
                current_answer = "".join(answer_parts).strip()
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
            answer_parts = [await get_gemini_answer(message.chat.id, prompt)]
    except Exception:
        logger.exception("Не удалось обработать сообщение Telegram")
        error_text = "Не удалось получить ответ от Gemini. Попробуйте повторить чуть позже."
        if response_message:
            try:
                await response_message.edit_text(error_text)
            except Exception:
                logger.exception("Не удалось обновить сообщение об ошибке")
                await message.answer(error_text)
        else:
            await message.answer(error_text)
        return

    answer = "".join(answer_parts).strip()
    if len(answer) > 4096:
        await response_message.edit_text(answer[:4096])
        for offset in range(4096, len(answer), 4096):
            await message.answer(answer[offset : offset + 4096])
    elif answer and response_message and answer != displayed_answer:
        try:
            await response_message.edit_text(answer)
        except Exception:
            # Если Telegram уже содержит такой же текст, ответ всё равно оставлен видимым.
            logger.exception("Не удалось показать финальный ответ потокового запроса")


async def main() -> None:
    global bot_username, bot_display_name
    me = await bot.get_me()
    bot_username = me.username
    bot_display_name = me.first_name
    logger.info("Бот запущен: @%s", bot_username or me.first_name)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
