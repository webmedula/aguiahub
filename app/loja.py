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


# Muitos sites WordPress ficam atrás de firewall/CDN que recusa requisição sem
# cara de navegador. O httpx se identifica como "python-httpx" e leva 403.
_CABECALHOS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Aguiahub/1.0"),
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


async def _consultar(params: dict) -> list[dict]:
    dados, _ = await consultar_bruto(params)
    return dados


async def consultar_bruto(params: dict) -> tuple[list[dict], dict]:
    """Consulta a loja e devolve (dados, diagnóstico).

    O diagnóstico serve para a tela de teste: quando algo falha no VPS, dá para
    ver o status HTTP, a URL exata e o começo da resposta — em vez de adivinhar.
    """
    s = get_settings()
    base = s.loja_base_url.rstrip("/")
    if not base:
        raise LojaError("Endereço da loja não configurado (LOJA_BASE_URL).")

    url = f"{base}/wp-json/wc/store/v1/products"
    diag: dict = {"url": url, "params": params}

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers=_CABECALHOS) as cli:
            resp = await cli.get(url, params=params)
    except httpx.RequestError as exc:
        diag["erro"] = f"{type(exc).__name__}: {exc}"
        raise LojaError(
            f"Não consegui acessar {url} — {type(exc).__name__}: {exc}. "
            "Verifique se o VPS alcança o site da loja."
        ) from exc

    diag["status"] = resp.status_code
    diag["content_type"] = resp.headers.get("content-type", "")
    diag["inicio_da_resposta"] = resp.text[:400]

    if resp.status_code >= 400:
        raise LojaError(
            f"A loja respondeu HTTP {resp.status_code} em {url}. "
            + ("A API de produtos do WooCommerce parece desativada nesse site."
               if resp.status_code == 404 else
               "Pode ser firewall do site bloqueando a aplicação."
               if resp.status_code in (401, 403) else
               f"Resposta: {resp.text[:200]}")
        )

    try:
        dados = resp.json()
    except ValueError as exc:
        raise LojaError(
            f"{url} não devolveu JSON (veio {diag['content_type']}). "
            "Confirme que a Store API do WooCommerce está ativa."
        ) from exc

    lista = dados if isinstance(dados, list) else [dados]
    diag["quantidade"] = len(lista)
    return lista, diag


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


def variantes_de_codigo(codigo: str) -> list[str]:
    """Gera as formas de escrever o mesmo part number.

    Descoberto em produção com o módulo da Fiat Toro: a planilha do ERP grava
    ``0281036486`` e a loja grava ``0.281.036.486``. A busca do WooCommerce é
    literal — procura o texto exato — então buscar pelo código do ERP não
    achava um produto que estava lá, com foto, o tempo todo. Foi por isso que
    o item 6 (R$ 47.864) aparecia como "não tenho na loja".

    Convenção Bosch/Delphi: dígitos agrupados de três em três a partir da
    direita ('0.281.036.486', '0.445.025.016').
    """
    codigo = (codigo or "").strip()
    if not codigo:
        return []

    formas = [codigo]
    nu = re.sub(r"[^A-Za-z0-9]", "", codigo)

    def _add(valor: str) -> None:
        if valor and valor not in formas:
            formas.append(valor)

    _add(nu)

    if nu.isdigit() and 7 <= len(nu) <= 13:
        grupos: list[str] = []
        resto = nu
        while len(resto) > 3:
            grupos.insert(0, resto[-3:])
            resto = resto[:-3]
        if resto:
            grupos.insert(0, resto)
        for separador in (".", " ", "-"):
            _add(separador.join(grupos))

    return formas


async def buscar_por_codigo(codigo: str, limite: int = 5) -> list[ProdutoDaLoja]:
    """Procura na loja pelo código da peça.

    Tenta o SKU exato primeiro e depois a busca livre, em cada forma de
    escrever o código (ver ``variantes_de_codigo``). Para na primeira que
    devolver resultado — não adianta gastar requisição no WordPress da Águia
    depois de já ter achado.
    """
    codigo = (codigo or "").strip()
    if not codigo:
        return []

    s = get_settings()
    achados: dict[int, ProdutoDaLoja] = {}
    formas = variantes_de_codigo(codigo)

    tentativas = [{"sku": f} for f in formas] + [{"search": f} for f in formas]

    for params in tentativas:
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


# ---------------------------------------------------------------------------
# Catálogo inteiro, para cruzar com a planilha
# ---------------------------------------------------------------------------

def normalizar_codigo(texto: str) -> str:
    """Tira pontuação e acento para comparar códigos.

    O site grava '0.445.025.016' e o ERP grava '0445025016' — sem normalizar,
    os dois nunca se encontram.
    """
    if not texto:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(texto)).upper()


async def listar_catalogo(paginas_max: int = 60,
                          por_pagina: int = 100) -> list[ProdutoDaLoja]:
    """Baixa o catálogo inteiro da loja, paginado.

    Consultar a loja item a item para 1.334 peças custaria milhares de
    requisições ao WordPress da Águia. Baixando o catálogo uma vez e cruzando
    em memória, são algumas dezenas de chamadas.
    """
    s = get_settings()
    produtos: list[ProdutoDaLoja] = []

    for pagina in range(1, paginas_max + 1):
        lote, _ = await consultar_bruto({"page": pagina, "per_page": por_pagina})
        if not lote:
            break
        produtos.extend(_montar(b, s.loja_base_url) for b in lote)
        if len(lote) < por_pagina:
            break

    return produtos


def indexar(produtos: list[ProdutoDaLoja]) -> dict[str, ProdutoDaLoja]:
    """Monta um índice código -> produto, a partir do SKU e do nome.

    Vários produtos da loja estão com o SKU vazio e trazem o código só no nome
    (foi o caso do injetor A2C59513553). Por isso indexamos os dois.
    """
    indice: dict[str, ProdutoDaLoja] = {}

    for p in produtos:
        chaves = set()
        if p.sku:
            chaves.add(normalizar_codigo(p.sku))
        # pega do nome os pedaços que parecem part number
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-/]{4,}", p.nome):
            chaves.add(normalizar_codigo(token))

        for chave in chaves:
            # Exigir dígito evita que palavras do nome ('RENEGADE', 'COMPASS')
            # virem chave e casem por acidente com um código do ERP.
            if len(chave) >= 5 and any(c.isdigit() for c in chave) \
                    and chave not in indice:
                indice[chave] = p

    return indice


def casar(codigo: str, indice: dict[str, ProdutoDaLoja]) -> ProdutoDaLoja | None:
    chave = normalizar_codigo(codigo)
    return indice.get(chave) if len(chave) >= 5 else None
