"""Leitura da loja da Águia Diesel (WooCommerce).

Este módulo resolve o problema que travou o projeto desde o início: **fotos**.

O Mercado Livre exige pelo menos uma imagem por anúncio. O catálogo do ML se
mostrou um caminho estreito — a busca da API alcança só as fichas antigas, e o
ML bloqueou a leitura de anúncios de terceiros. Mas a Águia já tem foto,
descrição e ficha das peças na própria loja. São imagens próprias, então não há
questão de direito autoral: é material da empresa sendo reaproveitado por ela
mesma.

Usa a Store API pública do WooCommerce (`/wp-json/wc/store/v1/products`), que
devolve JSON estruturado — bem mais robusto do que ler o HTML da página, que
quebraria a cada mudança de tema do site.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.config import get_settings


@dataclass
class ProdutoDaLoja:
    id: int
    nome: str
    sku: str
    url: str
    preco: float                     # em reais
    descricao: str                   # texto limpo, sem HTML
    fotos: list[str] = field(default_factory=list)
    categorias: list[str] = field(default_factory=list)
    marca: str = ""
    em_estoque: bool = True

    @property
    def publicavel(self) -> bool:
        """Só serve para anunciar se tiver ao menos uma foto."""
        return bool(self.fotos)


class LojaError(RuntimeError):
    pass


def _limpar_html(bruto: str) -> str:
    """Converte a descrição HTML do WooCommerce em texto corrido.

    O Mercado Livre aceita apenas texto simples na descrição (`plain_text`).
    """
    if not bruto:
        return ""
    texto = re.sub(r"<br\s*/?>|</p>|</li>", "\n", bruto, flags=re.I)
    texto = re.sub(r"<li>", "• ", texto, flags=re.I)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = html.unescape(texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n\s*\n\s*\n+", "\n\n", texto)
    return texto.strip()


def _slug_da_url(url: str) -> str:
    """Extrai o slug de https://loja.aguiadiesel.com.br/produto/<slug>/."""
    caminho = urlparse(url).path.strip("/")
    partes = [p for p in caminho.split("/") if p]
    if not partes:
        return ""
    # o slug é o último trecho não vazio; 'produto' é só o prefixo da rota
    return partes[-1] if partes[-1] != "produto" else ""


def _montar(dados: dict, base: str) -> ProdutoDaLoja:
    # A Store API devolve preço em centavos, como string ("360000" = R$ 3.600).
    precos = dados.get("prices") or {}
    try:
        centavos = int(precos.get("price") or 0)
        casas = int(precos.get("currency_minor_unit", 2))
        preco = centavos / (10 ** casas)
    except (TypeError, ValueError):
        preco = 0.0

    fotos = []
    for img in dados.get("images") or []:
        src = img.get("src") or img.get("thumbnail")
        if src and src not in fotos:
            fotos.append(src)

    categorias = [c.get("name", "") for c in (dados.get("categories") or [])]
    marca = ""
    for attr in dados.get("attributes") or []:
        if (attr.get("name") or "").lower() in ("marca", "brand"):
            termos = attr.get("terms") or []
            if termos:
                marca = termos[0].get("name", "")

    descricao = _limpar_html(dados.get("description") or
                             dados.get("short_description") or "")

    estoque = (dados.get("stock_availability") or {}).get("text", "")
    em_estoque = "fora de estoque" not in estoque.lower() and \
                 dados.get("is_in_stock", True) is not False

    return ProdutoDaLoja(
        id=int(dados.get("id") or 0),
        nome=html.unescape(dados.get("name") or "").strip(),
        sku=(dados.get("sku") or "").strip(),
        url=dados.get("permalink") or base,
        preco=preco,
        descricao=descricao,
        fotos=fotos,
        categorias=[c for c in categorias if c],
        marca=marca,
        em_estoque=em_estoque,
    )


async def _consultar(params: dict) -> list[dict]:
    s = get_settings()
    base = s.loja_base_url.rstrip("/")
    if not base:
        raise LojaError("Endereço da loja não configurado (LOJA_BASE_URL).")

    url = f"{base}/wp-json/wc/store/v1/products"
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as cli:
            resp = await cli.get(url, params=params)
    except httpx.RequestError as exc:
        raise LojaError(f"Não consegui falar com a loja: {exc}") from exc

    if resp.status_code >= 400:
        raise LojaError(f"A loja respondeu {resp.status_code} para {url}")
    try:
        dados = resp.json()
    except ValueError as exc:
        raise LojaError("A loja não devolveu JSON — verifique o endereço.") from exc

    return dados if isinstance(dados, list) else [dados]


async def buscar_por_url(url: str) -> ProdutoDaLoja | None:
    """Lê o produto a partir do link da página na loja."""
    slug = _slug_da_url(url)
    if not slug:
        raise LojaError(
            "Não reconheci esse endereço da loja. Ele deve ser parecido com "
            "https://loja.aguiadiesel.com.br/produto/nome-da-peca/"
        )
    resultados = await _consultar({"slug": slug})
    s = get_settings()
    return _montar(resultados[0], s.loja_base_url) if resultados else None


async def buscar_por_codigo(codigo: str, limite: int = 5) -> list[ProdutoDaLoja]:
    """Procura na loja pelo código da peça.

    Tenta o SKU exato primeiro. Se não achar, busca livre — o site às vezes
    grava o código com pontos ('0.445.025.016') enquanto o ERP grava sem.
    """
    codigo = (codigo or "").strip()
    if not codigo:
        return []

    s = get_settings()
    achados: dict[int, ProdutoDaLoja] = {}

    for params in ({"sku": codigo}, {"search": codigo}):
        try:
            for bruto in await _consultar(params):
                p = _montar(bruto, s.loja_base_url)
                if p.id and p.id not in achados:
                    achados[p.id] = p
        except LojaError:
            continue
        if achados:
            break

    return list(achados.values())[:limite]
