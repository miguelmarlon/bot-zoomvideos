"""Varre grupos do Telegram em busca de mensagens com link de afiliado da Shopee + vídeo.

Para cada mensagem nova encontrada (desde a última execução): converte o link para o
link de afiliado, baixa o vídeo para a pasta data/, reposta o vídeo (com a legenda já
usando o link convertido) no grupo de destino (TELEGRAM_GROUP_ID) e remove o arquivo local.

O progresso é salvo em state.json (último ID de mensagem processado por chat/tópico), para
que execuções seguintes não reprocessem as mesmas mensagens. Cada item de
TELEGRAM_MONITOR_CHAT_IDS pode ser "chat_id" (grupo inteiro) ou "chat_id_topicoId"
(só um tópico de um grupo com Tópicos/fórum ativado).

No início da execução, apaga a imagem/texto de divulgação enviados na execução
anterior (IDs salvos em divulgacao_ids.json). Ao final, envia novamente a imagem
data/img_divulgacao.jpg e o texto de data/texto_divulgacao.txt para o grupo de destino.
"""
"SEMPRE DAR GIT PULL ANTES DE ALTERAR ESSE SCRIPT"

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from shopee_client import LinkInvalidoError, converter_link

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
# Telefone do admin que recebe o resumo/erros da execução (opcional; sem ele não notifica).
ADMIN_PHONE = os.getenv("TELEGRAM_ADMIN_PHONE")


def _parse_chat_ids(valor: str) -> list[tuple[int, int | None]]:
    """Cada item pode ser "chat_id" (grupo inteiro) ou "chat_id_topicoId" (só um tópico
    de um grupo com Tópicos/fórum ativado), ex: -1002698134896_6."""
    resultado = []
    for item in valor.split(","):
        item = item.strip()
        if not item:
            continue
        if "_" in item:
            chat_id_str, topico_str = item.rsplit("_", 1)
            resultado.append((int(chat_id_str), int(topico_str)))
        else:
            resultado.append((int(item), None))
    return resultado


CHAT_IDS = _parse_chat_ids(os.getenv("TELEGRAM_MONITOR_CHAT_IDS", ""))

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

TENTATIVAS = 3
PAUSA_ENTRE_TENTATIVAS = 5  # segundos

LINK_SHOPEE_RE =re.compile(
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


@dataclass
class Relatorio:
    enviados: int = 0
    ignorados: int = 0  # links inválidos/expirados, descartados de vez
    por_grupo: dict[str, int] = field(default_factory=dict)  # nome do grupo -> vídeos enviados
    erros: list[str] = field(default_factory=list)

    def formatar(self, duracao: float) -> str:
        detalhe = ", ".join(f"{qtd} do grupo {nome}" for nome, qtd in self.por_grupo.items())
        resumo = f"{self.enviados} vídeos enviados."
        if detalhe:
            resumo += f" {detalhe}."
        linhas = [resumo, f"Duração: {duracao:.0f}s"]
        if self.ignorados:
            linhas.append(f"Links inválidos ignorados: {self.ignorados}")
        if self.erros:
            linhas.append(f"Erros ({len(self.erros)}):")
            linhas.extend(f"- {erro}" for erro in self.erros)
        return "\n".join(linhas)[:4000]  # limite do Telegram é 4096


async def notificar_admin(client: TelegramClient, texto: str) -> None:
    """Envia texto ao admin pela conta do Telethon. Nunca levanta: falha só vira aviso."""
    if not ADMIN_PHONE:
        print("notificacao nao enviada: TELEGRAM_ADMIN_PHONE nao configurado")
        return
    try:
        try:
            destino = await client.get_entity(ADMIN_PHONE)
        except ValueError:
            # telefone fora dos contatos: importa o contato para poder resolver o usuário
            resultado = await client(
                functions.contacts.ImportContactsRequest(
                    [types.InputPhoneContact(0, ADMIN_PHONE, "Admin", "Bot")]
                )
            )
            if not resultado.users:
                raise RuntimeError(f"{ADMIN_PHONE} nao tem conta no Telegram")
            destino = resultado.users[0]
        await client.send_message(destino, texto)
        print("admin notificado")
    except Exception as exc:
        print(f"  aviso: nao foi possivel notificar o admin: {exc!r}")


async def processar_chat(
    client: TelegramClient,
    chat_id: int,
    topico_id: int | None,
    estado: dict,
    limite: int | None,
    relatorio: Relatorio,
) -> None:
    """Processa um chat. Cada vídeo é tentado até TENTATIVAS vezes; se todas falharem,
    o erro vai para o relatório final e o bot segue para a próxima mensagem."""
    chave_estado = f"{chat_id}_{topico_id}" if topico_id is not None else str(chat_id)
    rotulo = f"{chat_id}/topico {topico_id}" if topico_id is not None else str(chat_id)

    try:
        nome = getattr(await client.get_entity(chat_id), "title", None) or rotulo
    except Exception:
        nome = rotulo
    if topico_id is not None:
        nome = f"{nome} (tópico {topico_id})"
    relatorio.por_grupo.setdefault(nome, 0)

    ultimo_id = estado.get(chave_estado, 0)

    kwargs = {"min_id": ultimo_id, "reverse": True}
    if topico_id is not None:
        kwargs["reply_to"] = topico_id

    async for mensagem in client.iter_messages(chat_id, **kwargs):
        if limite is not None and relatorio.enviados >= limite:
            break

        legenda = mensagem.raw_text or ""
        link = extrair_link_shopee(legenda)
        if link and tem_video(mensagem):
            print(f"[{rotulo}] mensagem {mensagem.id}: link encontrado -> {link}")
            ultimo_erro = None
            for tentativa in range(1, TENTATIVAS + 1):
                caminho = None
                try:
                    link_afiliado = converter_link(link)
                    print(f"  link de afiliado: {link_afiliado}")

                    destino = DATA_DIR / f"{chat_id}_{mensagem.id}.mp4"
                    caminho = await client.download_media(mensagem, file=str(destino))
                    if not caminho:
                        raise RuntimeError("download_media nao retornou arquivo")
                    print(f"  video salvo em: {caminho}")

                    enviar_video_bot(str(caminho), link_afiliado)
                    print(f"  video enviado para o grupo {GROUP_ID}")

                    relatorio.enviados += 1
                    relatorio.por_grupo[nome] += 1
                    ultimo_erro = None
                    break
                except LinkInvalidoError as exc:
                    # erro permanente: não adianta tentar de novo
                    print(f"  link ignorado: {exc}")
                    relatorio.ignorados += 1
                    ultimo_erro = None
                    break
                except Exception as exc:
                    ultimo_erro = exc
                    print(f"  tentativa {tentativa}/{TENTATIVAS} falhou: {exc!r}")
                    if tentativa < TENTATIVAS:
                        await asyncio.sleep(PAUSA_ENTRE_TENTATIVAS)
                finally:
                    if caminho:
                        Path(caminho).unlink(missing_ok=True)

            if ultimo_erro is not None:
                relatorio.erros.append(
                    f"[{nome}] msg {mensagem.id}: falhou após {TENTATIVAS} tentativas: {ultimo_erro!r}"
                )

        estado[chave_estado] = max(estado.get(chave_estado, 0), mensagem.id)
        salvar_estado(estado)


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
    relatorio = Relatorio()
    inicio = time.time()

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        try:
            # popula o cache de entidades (necessário para o send_file achar o grupo de destino)
            await client.get_dialogs()
            estado = carregar_estado()

            try:
                apagar_divulgacao_anterior()
            except Exception as exc:
                relatorio.erros.append(f"falha ao apagar divulgacao anterior: {exc!r}")

            for chat_id, topico_id in CHAT_IDS:
                await processar_chat(client, chat_id, topico_id, estado, args.limite, relatorio)
                if args.limite is not None and relatorio.enviados >= args.limite:
                    break

            try:
                enviar_divulgacao()
            except Exception as exc:
                relatorio.erros.append(f"falha ao enviar divulgacao: {exc!r}")
        except Exception as exc:
            relatorio.erros.append(f"erro fatal: {exc!r}")

        resumo = relatorio.formatar(time.time() - inicio)
        print(resumo)
        await notificar_admin(client, resumo)

    if relatorio.erros:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
