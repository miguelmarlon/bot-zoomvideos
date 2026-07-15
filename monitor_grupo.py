"""Varre grupos do Telegram em busca de mensagens com link de afiliado da Shopee + vídeo.

Para cada mensagem nova encontrada (desde a última execução): converte o link para o
link de afiliado, baixa o vídeo para a pasta data/, reposta o vídeo (com a legenda já
usando o link convertido) no grupo de destino (TELEGRAM_GROUP_ID) e remove o arquivo local.

O progresso é salvo em state.json (último ID de mensagem processado por chat), para
que execuções seguintes não reprocessem as mesmas mensagens.

No início da execução, apaga a imagem/texto de divulgação enviados na execução
anterior (IDs salvos em divulgacao_ids.json). Ao final, envia novamente a imagem
data/img_divulgacao.jpg e o texto de data/texto_divulgacao.txt para o grupo de destino.
"""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from shopee_client import converter_link

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [
    int(chat_id) for chat_id in os.getenv("TELEGRAM_MONITOR_CHAT_IDS", "").split(",") if chat_id
]
GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = BASE_DIR / "state.json"
IMAGEM_DIVULGACAO = DATA_DIR / "img_divulgacao.jpg"
TEXTO_DIVULGACAO = DATA_DIR / "texto_divulgacao.txt"
DIVULGACAO_IDS_FILE = BASE_DIR / "divulgacao_ids.json"

# Se TELEGRAM_SESSION estiver no .env, loga sem precisar de arquivo de sessão em disco
# (gerado via gerar_session_string.py). Caso contrário, usa sessão em arquivo local.
SESSION_STRING = os.getenv("TELEGRAM_SESSION")
SESSION = StringSession(SESSION_STRING) if SESSION_STRING else str(BASE_DIR / "monitor_session")

LINK_SHOPEE_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:shopee\.com\.br|shope\.ee)/\S+", re.IGNORECASE
)


def carregar_estado() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def salvar_estado(estado: dict) -> None:
    STATE_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def tem_video(mensagem) -> bool:
    if mensagem.video:
        return True
    if mensagem.document:
        mime = getattr(mensagem.document, "mime_type", "") or ""
        return mime.startswith("video/")
    return False


def extrair_link_shopee(texto: str) -> str | None:
    if not texto:
        return None
    encontrado = LINK_SHOPEE_RE.search(texto)
    return encontrado.group(0) if encontrado else None


def enviar_video_bot(caminho: str, legenda: str) -> None:
    """Envia o vídeo para o grupo de destino usando o Bot API (o bot precisa já estar no grupo)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    with open(caminho, "rb") as video:
        resposta = requests.post(
            url,
            data={"chat_id": GROUP_ID, "caption": legenda},
            files={"video": video},
            timeout=120,
        )
    resposta.raise_for_status()
    corpo = resposta.json()
    if not corpo.get("ok"):
        raise RuntimeError(f"Erro ao enviar video: {corpo}")


def apagar_mensagem_bot(message_id: int) -> None:
    resposta = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
        data={"chat_id": GROUP_ID, "message_id": message_id},
        timeout=30,
    )
    corpo = resposta.json()
    if not corpo.get("ok"):
        print(f"  aviso: nao foi possivel apagar mensagem {message_id}: {corpo.get('description')}")


def apagar_divulgacao_anterior() -> None:
    """Apaga a imagem + texto de divulgação enviados na execução anterior, se existirem."""
    if not DIVULGACAO_IDS_FILE.exists():
        return

    ids = json.loads(DIVULGACAO_IDS_FILE.read_text(encoding="utf-8"))
    for chave in ("foto_id", "texto_id"):
        message_id = ids.get(chave)
        if message_id:
            apagar_mensagem_bot(message_id)

    DIVULGACAO_IDS_FILE.unlink(missing_ok=True)
    print("divulgacao anterior removida do grupo")


def enviar_divulgacao() -> None:
    """Envia a imagem + texto de divulgação do canal para o grupo de destino.

    Enviados como duas mensagens (foto e depois texto) porque a legenda de foto do
    Telegram tem limite de 1024 caracteres, e o texto de divulgação pode ultrapassar isso.
    Os IDs das mensagens enviadas são salvos para serem apagados na próxima execução.
    """
    if not IMAGEM_DIVULGACAO.exists() or not TEXTO_DIVULGACAO.exists():
        print("divulgacao nao enviada: img_divulgacao.jpg ou texto_divulgacao.txt ausente em data/")
        return

    with open(IMAGEM_DIVULGACAO, "rb") as imagem:
        resposta = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": GROUP_ID},
            files={"photo": imagem},
            timeout=60,
        )
    resposta.raise_for_status()
    corpo = resposta.json()
    if not corpo.get("ok"):
        raise RuntimeError(f"Erro ao enviar imagem de divulgacao: {corpo}")
    foto_id = corpo["result"]["message_id"]

    legenda = TEXTO_DIVULGACAO.read_text(encoding="utf-8")
    resposta = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": GROUP_ID, "text": legenda},
        timeout=30,
    )
    resposta.raise_for_status()
    corpo = resposta.json()
    if not corpo.get("ok"):
        raise RuntimeError(f"Erro ao enviar texto de divulgacao: {corpo}")
    texto_id = corpo["result"]["message_id"]

    DIVULGACAO_IDS_FILE.write_text(
        json.dumps({"foto_id": foto_id, "texto_id": texto_id}, indent=2), encoding="utf-8"
    )
    print("mensagem de divulgacao enviada")


async def processar_chat(
    client: TelegramClient, chat_id: int, estado: dict, limite: int | None, enviados: int
) -> int:
    ultimo_id = estado.get(str(chat_id), 0)
    maior_id = ultimo_id

    async for mensagem in client.iter_messages(chat_id, min_id=ultimo_id, reverse=True):
        if limite is not None and enviados >= limite:
            break

        maior_id = max(maior_id, mensagem.id)

        legenda = mensagem.raw_text or ""
        link = extrair_link_shopee(legenda)
        if link and tem_video(mensagem):
            print(f"[{chat_id}] mensagem {mensagem.id}: link encontrado -> {link}")
            try:
                link_afiliado = converter_link(link)
                print(f"  link de afiliado: {link_afiliado}")
            except Exception as exc:
                print(f"  falha ao converter link: {exc}")
            else:
                destino = DATA_DIR / f"{chat_id}_{mensagem.id}.mp4"
                caminho = await client.download_media(mensagem, file=str(destino))
                print(f"  video salvo em: {caminho}")

                enviar_video_bot(str(caminho), link_afiliado)
                print(f"  video enviado para o grupo {GROUP_ID}")

                Path(caminho).unlink(missing_ok=True)
                print("  video local removido")

                enviados += 1

        estado[str(chat_id)] = maior_id
        salvar_estado(estado)

    return enviados


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Varre grupos e reposta vídeos com link Shopee convertido.")
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="numero maximo de videos a enviar nesta execucao (util para testes)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    apagar_divulgacao_anterior()
    estado = carregar_estado()

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        # popula o cache de entidades (necessário para o send_file achar o grupo de destino)
        await client.get_dialogs()
        enviados = 0
        for chat_id in CHAT_IDS:
            enviados = await processar_chat(client, chat_id, estado, args.limite, enviados)
            if args.limite is not None and enviados >= args.limite:
                break

    enviar_divulgacao()


if __name__ == "__main__":
    asyncio.run(main())
