import asyncpg
from html import escape

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from common.config import get_config

config = get_config()


async def notify_about_pr(files: str, request_url: str) -> None:
    """Notify subscribed chats about created pull or merge request."""
    conn = await asyncpg.connect(
        host='db',
        database=config.dbt_project_name,
        user=config.notifier_db_username,
        password=config.notifier_db_password,
        port=5432,
    )
    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    try:
        async with conn.transaction(): 
            async for id in conn.cursor("select tid from meta.ids"):
                if files:
                    try:
                        await bot.send_message(chat_id=id['tid'], 
                                            text=f"""
                                            При попытке теста обновленных моделей найдены ошибки в файлах {files}
                                            \nPR/MR с предположительным решением ошибки: {request_url}"""
                                            )
                    except TelegramBadRequest as e:
                        if 'chat not found' in str(e):
                            print(f"Чат с id {id['tid']} не найден. Пропускаю...")
                else:
                    await bot.send_message(chat_id=id['tid'], 
                                            text=f"""
                                            Упал пайплайн при попытке сборки коммита с обновленными моделями"""
                                            )
    finally:
        await bot.session.close()
    

async def notify_about_review(finding: str) -> None:
    """Notify subscribed chats about review findings."""
    conn = await asyncpg.connect(
        host='db',
        database=config.dbt_project_name,
        user=config.notifier_db_username,
        password=config.notifier_db_password,
        port=5432,
    )
    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    try:
        async with conn.transaction():
            async for id in conn.cursor("select tid from meta.ids"):
                try:
                    await bot.send_message(
                        chat_id=id['tid'],
                        text=f"Review finding:\n{escape(finding)}",
                    )
                except TelegramBadRequest as e:
                    if 'chat not found' in str(e):
                        print(f"Чат с id {id['tid']} не найден. Пропускаю...")
    finally:
        await bot.session.close()
