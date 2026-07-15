"""Gera uma StringSession do Telethon para guardar no .env (TELEGRAM_SESSION).

Rode uma única vez: ./.venv/Scripts/python gerar_session_string.py
Vai pedir o código de login enviado para TELEGRAM_PHONE (e a senha de 2FA, se houver).
Copie a string impressa no final e cole no .env como TELEGRAM_SESSION=<string>.

ATENÇÃO: essa string dá acesso completo à conta do Telegram, igual a uma senha.
Nunca compartilhe nem versione o .env com esse valor preenchido.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")


async def main() -> None:
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        print("\nLogin ok. Adicione esta linha ao seu .env:\n")
        print(f"TELEGRAM_SESSION={client.session.save()}")


if __name__ == "__main__":
    asyncio.run(main())
