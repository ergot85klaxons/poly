import aiohttp
import asyncio
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")          # имя переменной, а не сам токен
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def main():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data={
            "chat_id": CHAT_ID,
            "text": "Бот подключён. Готов двигаться дальше.",
            "disable_web_page_preview": True
        }) as r:
            print(await r.json())

asyncio.run(main())
