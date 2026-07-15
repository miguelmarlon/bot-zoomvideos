"""Cliente para expandir links curtos da Shopee e gerar links de afiliado."""

import hashlib
import json
import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.getenv("SHOPEE_APP_ID")
API_SECRET = os.getenv("SHOPEE_API_SECRET")

GRAPHQL_ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"

# URLs de produto da Shopee aparecem em mais de um formato, ex:
# .../produto-nome-i.{shopId}.{itemId}
# .../product/{shopId}/{itemId}
# .../{shop-username}/{shopId}/{itemId}
PADROES_IDS_PRODUTO = [
    re.compile(r"-i\.(\d+)\.(\d+)"),
    re.compile(r"/(\d{5,})/(\d{5,})(?:[/?]|$)"),
]


def _extrair_ids_produto(url: str) -> tuple[str, str] | None:
    for padrao in PADROES_IDS_PRODUTO:
        match = padrao.search(url)
        if match:
            return match.groups()
    return None


def expandir_link(url_curta: str) -> str:
    """Segue os redirecionamentos de um link curto da Shopee e retorna a URL final."""
    resposta = requests.head(url_curta, allow_redirects=True, timeout=10)
    if resposta.status_code >= 400 or resposta.url == url_curta:
        resposta = requests.get(url_curta, allow_redirects=True, timeout=10)
    return resposta.url


def _assinar(payload: str, timestamp: int) -> str:
    base = f"{APP_ID}{timestamp}{payload}{API_SECRET}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def gerar_link_afiliado(url_origem: str, sub_ids: list[str] | None = None) -> str:
    """Chama a Shopee Affiliate Open API (generateShortLink) e retorna o link de afiliado."""
    query = (
        "mutation{generateShortLink(input:{originUrl:\"%s\"%s}){shortLink}}"
        % (
            url_origem,
            f",subIds:{json.dumps(sub_ids)}" if sub_ids else "",
        )
    )
    payload = json.dumps({"query": query})

    timestamp = int(time.time())
    assinatura = _assinar(payload, timestamp)
    headers = {
        "Content-Type": "application/json",
        "Authorization": (
            f"SHA256 Credential={APP_ID}, Timestamp={timestamp}, Signature={assinatura}"
        ),
    }

    resposta = requests.post(GRAPHQL_ENDPOINT, data=payload, headers=headers, timeout=10)
    resposta.raise_for_status()
    corpo = resposta.json()

    if "errors" in corpo:
        raise RuntimeError(f"Erro da API Shopee: {corpo['errors']}")

    return corpo["data"]["generateShortLink"]["shortLink"]


def converter_link(url_curta: str, sub_ids: list[str] | None = None) -> str:
    """Expande um link curto da Shopee e converte para um link de afiliado.

    Rejeita links cujo destino expandido não é uma página de produto (shopId/itemId) —
    alguns links curtos expiram e caem em página inicial, busca, carteira de cupons etc.
    """
    url_expandida = expandir_link(url_curta)
    if _extrair_ids_produto(url_expandida) is None:
        raise RuntimeError(f"link nao aponta para um produto valido: {url_expandida}")
    return gerar_link_afiliado(url_expandida, sub_ids)


if __name__ == "__main__":
    link_teste = "https://s.shopee.com.br/8AUUqdEUvn"
    url_expandida = expandir_link(link_teste)
    print(f"URL expandida: {url_expandida}")

    link_afiliado = gerar_link_afiliado(url_expandida)
    print(f"Link de afiliado: {link_afiliado}")
