"""Extração de dados técnicos de páginas de produto de qualquer site.

Por que existe: a peça pode não estar na loja da Águia, e o Mercado Livre
fechou os dados de terceiros para aplicações. Sobrava só fotografar. Mas a
ficha técnica da peça — em quais veículos aplica, códigos equivalentes,
marca, medidas — está publicada em site de fabricante, de fornecedor e de
concorrente. Ler isso encurta muito o trabalho de identificar a peça.

Como lê: prioriza **dados estruturados** que os próprios sites publicam para
o Google — JSON-LD `schema.org/Product` e as meta tags OpenGraph. É bem mais
robusto do que adivinhar o HTML de cada site, que muda a cada troca de tema.
Quando não há dado estruturado, cai para meta tags e título da página.

SOBRE FOTO — leia antes de mexer:

Este módulo NÃO baixa imagem de site de terceiro por padrão. Foto de produto
é obra protegida do site que a produziu. Reutilizar sem direito é violação de
direito autoral e, na prática, é o tipo de coisa que derruba anúncio por
denúncia e leva à suspensão da conta de vendedor — justamente a conta em que
a Águia está construindo tudo isto.

A exceção é a fonte que a própria Águia declara ter direito de usar: material
de fabricante ou fornecedor de quem ela é distribuidora, por exemplo. Esses
domínios são listados em ``FONTES_IMAGEM_AUTORIZADAS`` (vazio por padrão) e
só deles a foto é aproveitada. A declaração é do dono do negócio, que é quem
sabe o que contratou; o sistema apenas respeita a lista.

Para todo o resto, o extrator traz FATO TÉCNICO — que não tem dono — e a
descrição alheia entra apenas como **referência na tela**, para a pessoa
conferir se é a mesma peça. O texto do anúncio é montado por nós.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import get_settings


@dataclass
class DadosExtraidos:
    url: str
    dominio: str
    nome: str = ""
    marca: str = ""
    codigos: list[str] = field(default_factory=list)      # sku, mpn, gtin
    aplicacao: str = ""
    atributos: dict = field(default_factory=dict)
    preco: float | None = None
    descricao_referencia: str = ""     # SÓ para a pessoa ler na tela
    fotos: list[str] = field(default_factory=list)        # só de fonte autorizada
    fotos_bloqueadas: int = 0          # quantas existiam mas não podem ser usadas
    fonte_autorizada: bool = False
    avisos: list[str] = field(default_factory=list)

    @property
    def util(self) -> bool:
        return bool(self.nome or self.codigos or self.atributos)


class ExtratorError(RuntimeError):
    pass


_CABECALHOS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Aguiahub/1.0"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

#: Chaves de atributo que interessam numa autopeça, e o rótulo em português.
CHAVES_INTERESSANTES = {
    "mpn": "código do fabricante",
    "sku": "código",
    "gtin": "GTIN",
    "gtin13": "EAN",
    "brand": "marca",
    "model": "modelo",
    "material": "material",
    "color": "cor",
    "weight": "peso",
    "width": "largura",
    "height": "altura",
    "depth": "profundidade",
}


def dominio_de(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def fontes_permitidas() -> set[str]:
    """Domínios autorizados: os do banco mais os da variável de ambiente.

    O banco é o caminho normal — a Águia acrescenta marca representada o tempo
    todo, e cada uma dessas não pode exigir mexer no EasyPanel e refazer o
    deploy. A variável continua valendo para quem já a configurou.
    """
    from app import storage

    do_ambiente = {d.strip().lower().removeprefix("www.")
                   for d in (get_settings().fontes_imagem_autorizadas or "").split(",")
                   if d.strip()}
    return do_ambiente | storage.dominios_autorizados()


def fonte_autorizada_para_imagem(url: str) -> bool:
    """A Águia declarou ter direito de usar imagem deste domínio?"""
    permitidos = fontes_permitidas()
    if not permitidos:
        return False
    if "*" in permitidos:
        # Curinga: a Águia declarou ter direito sobre a imagem de qualquer
        # fonte. Existe porque a decisão é do dono do negócio, mas continua
        # sendo uma escolha explícita — nunca o padrão.
        return True
    alvo = dominio_de(url)
    # aceita subdomínio de um domínio autorizado (cdn.bosch.com para bosch.com)
    return any(alvo == d or alvo.endswith("." + d) for d in permitidos)


async def robots_permite(url: str) -> bool:
    """Respeita o robots.txt do site.

    Não é formalidade: é o sinal que o site publica dizendo o que aceita que
    robô leia. Ignorar isso é o começo do caminho que termina em IP bloqueado
    e, dependendo do site, em problema jurídico.

    Público desde a v0.24 (era ``_robots_permite``) porque ``app/busca.py``
    lê a página de busca dos sites e precisa da mesma checagem: a regra vale
    para tudo que a aplicação busca sozinha, não só para a ficha do produto.
    """
    partes = urlparse(url)
    robots = f"{partes.scheme}://{partes.netloc}/robots.txt"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers=_CABECALHOS) as cli:
            resp = await cli.get(robots)
    except httpx.RequestError:
        return True          # site sem robots acessível: seguimos
    if resp.status_code >= 400:
        return True

    leitor = RobotFileParser()
    leitor.parse(resp.text.splitlines())
    return leitor.can_fetch(_CABECALHOS["User-Agent"], url) or \
        leitor.can_fetch("*", url)


async def extrair(url: str) -> DadosExtraidos:
    """Lê a página e devolve o que dá para aproveitar."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ExtratorError(
            "Endereço inválido. Cole o link completo da página do produto, "
            "começando com https://")

    if not await robots_permite(url):
        raise ExtratorError(
            f"O site {dominio_de(url)} não autoriza leitura automática desta "
            "página (robots.txt). Dá para abrir no navegador e digitar os "
            "dados à mão, mas o sistema não vai buscar sozinho.")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers=_CABECALHOS) as cli:
            resp = await cli.get(url)
    except httpx.RequestError as exc:
        raise ExtratorError(
            f"Não consegui acessar {dominio_de(url)}: {exc}") from exc

    if resp.status_code >= 400:
        raise ExtratorError(
            f"{dominio_de(url)} respondeu HTTP {resp.status_code}. "
            + ("O site está bloqueando leitura automática."
               if resp.status_code in (401, 403, 429) else
               "A página pode não existir mais."))

    return montar(resp.text, url)


def montar(html: str, url: str) -> DadosExtraidos:
    """Monta os dados a partir do HTML. Separado de `extrair` para testar."""
    d = DadosExtraidos(url=url, dominio=dominio_de(url))
    d.fonte_autorizada = fonte_autorizada_para_imagem(url)

    produto = _achar_produto_jsonld(html)
    if produto:
        _do_jsonld(produto, d, url)
    else:
        d.avisos.append("O site não publica ficha estruturada; li o que deu "
                        "das meta tags da página.")

    _do_opengraph(html, d, url)

    # Última tentativa: as <img> da própria página. É onde a foto está nos
    # sites que não publicam ficha estruturada — a maioria dos fornecedores.
    if not d.fotos and not d.fotos_bloqueadas:
        _guardar_fotos(_imagens_da_pagina(html, url), d, url)

    if not d.nome:
        titulo = re.search(r"<title[^>]*>(.*?)</title>", html,
                           re.I | re.S)
        if titulo:
            d.nome = _texto(titulo.group(1))[:200]

    d.aplicacao = _achar_aplicacao(d)
    return d


def _achar_produto_jsonld(html: str) -> dict | None:
    """Procura o bloco JSON-LD do tipo Product.

    É o mesmo dado que o site publica para aparecer no Google Shopping, então
    costuma estar correto e atualizado.
    """
    for bruto in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S):
        try:
            dados = json.loads(bruto.strip())
        except ValueError:
            continue
        for candidato in _achatar(dados):
            tipos = candidato.get("@type") or ""
            tipos = tipos if isinstance(tipos, list) else [tipos]
            if any(str(t).lower() == "product" for t in tipos):
                return candidato
    return None


def _achatar(dados) -> list[dict]:
    """JSON-LD vem como objeto, lista, ou embrulhado em @graph."""
    saida: list[dict] = []
    pilha = [dados]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, list):
            pilha.extend(atual)
        elif isinstance(atual, dict):
            saida.append(atual)
            if "@graph" in atual:
                pilha.append(atual["@graph"])
    return saida


def _do_jsonld(produto: dict, d: DadosExtraidos, url: str) -> None:
    d.nome = _texto(produto.get("name") or "")[:200]

    marca = produto.get("brand")
    if isinstance(marca, dict):
        marca = marca.get("name")
    if marca:
        d.marca = _texto(str(marca))[:60]

    for chave in ("sku", "mpn", "gtin", "gtin13", "gtin14", "productID"):
        valor = produto.get(chave)
        if valor and str(valor).strip() not in d.codigos:
            d.codigos.append(str(valor).strip())

    for chave, rotulo in CHAVES_INTERESSANTES.items():
        valor = produto.get(chave)
        if isinstance(valor, dict):
            valor = valor.get("value") or valor.get("name")
        if valor and rotulo not in d.atributos:
            d.atributos[rotulo] = _texto(str(valor))[:120]

    # atributos livres que muitos sites publicam
    props = produto.get("additionalProperty") or []
    for prop in props if isinstance(props, list) else [props]:
        if not isinstance(prop, dict):
            continue
        nome = _texto(str(prop.get("name") or ""))[:60]
        valor = prop.get("value")
        if isinstance(valor, dict):
            valor = valor.get("value") or valor.get("name")
        if nome and valor:
            d.atributos.setdefault(nome, _texto(str(valor))[:200])

    oferta = produto.get("offers")
    if isinstance(oferta, list):
        oferta = oferta[0] if oferta else None
    if isinstance(oferta, dict):
        try:
            d.preco = float(str(oferta.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            pass

    # Descrição entra como REFERÊNCIA de tela. Não vai para o anúncio: texto
    # descritivo é obra do site de origem.
    if produto.get("description"):
        d.descricao_referencia = _texto(str(produto["description"]))[:4000]

    _guardar_fotos(_lista_de_imagens(produto.get("image")), d, url)


def _lista_de_imagens(bruto) -> list[str]:
    if not bruto:
        return []
    if isinstance(bruto, str):
        return [bruto]
    if isinstance(bruto, dict):
        return [bruto.get("url") or bruto.get("contentUrl") or ""]
    if isinstance(bruto, list):
        saida = []
        for i in bruto:
            saida.extend(_lista_de_imagens(i))
        return saida
    return []


def _guardar_fotos(urls: list[str], d: DadosExtraidos, base: str) -> None:
    """Só guarda foto se o domínio estiver na lista autorizada pela Águia."""
    limpas = [urljoin(base, u) for u in urls if u]
    if not limpas:
        return
    if d.fonte_autorizada:
        for u in limpas:
            if u not in d.fotos:
                d.fotos.append(u)
    else:
        d.fotos_bloqueadas = len(limpas)


#: Lê os atributos de uma tag qualquer sem depender da ordem em que vêm.
_RE_ATRIBUTO = re.compile(
    r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s">]+))""")


def _atributos(tag: str) -> dict:
    saida = {}
    for nome, aspas, apostrofo, solto in _RE_ATRIBUTO.findall(tag):
        saida[nome.lower()] = aspas or apostrofo or solto or ""
    return saida


_RE_META = re.compile(r"<meta\b[^>]*>", re.I)


def _metas(html: str) -> dict:
    """Todas as meta tags da página, por property/name.

    A versão anterior usava uma expressão que exigia ``property`` ANTES de
    ``content``. Metade dos sites escreve ao contrário
    (``<meta content="..." property="og:image">``) e a foto simplesmente não
    era encontrada — foi o que aconteceu com a apolloonibus.com.br.
    """
    saida: dict = {}
    for tag in _RE_META.findall(html or ""):
        attrs = _atributos(tag)
        chave = (attrs.get("property") or attrs.get("name") or "").lower()
        if chave and "content" in attrs and chave not in saida:
            saida[chave] = attrs["content"]
    return saida


def _do_opengraph(html: str, d: DadosExtraidos, url: str) -> None:
    metas = _metas(html)
    if not d.nome and metas.get("og:title"):
        d.nome = _texto(metas["og:title"])[:200]
    if not d.descricao_referencia and metas.get("og:description"):
        d.descricao_referencia = _texto(metas["og:description"])[:4000]

    imagem = (metas.get("og:image") or metas.get("og:image:secure_url")
              or metas.get("twitter:image") or metas.get("twitter:image:src"))
    if imagem and not d.fotos and not d.fotos_bloqueadas:
        _guardar_fotos([imagem], d, url)


_RE_IMG = re.compile(r"<img\b[^>]*>", re.I)

#: Onde a foto pode estar escondida: plataforma de loja quase sempre carrega
#: imagem preguiçosa, e aí o ``src`` verdadeiro está num data-attribute.
_FONTES_DE_IMG = ("src", "data-src", "data-original", "data-lazy-src",
                  "data-lazy", "data-zoom-image", "data-large_image",
                  "data-image", "data-echo")

#: Não é foto de produto — é a mecânica da loja.
_LIXO_NA_IMAGEM = (
    "logo", "icone", "icon", "favicon", "banner", "sprite", "placeholder",
    "avatar", "selo", "bandeira", "pagamento", "payment", "cartao", "visa",
    "master", "boleto", "pix", "whatsapp", "instagram", "facebook", "cart",
    "carrinho", "pixel", "loader", "spinner", "blank", "seta", "arrow",
    "bullet", "divider", "separador", "rodape", "footer", "header", "topo",
)

#: Caminho com cara de foto de produto — vale mais que o resto.
_PISTA_DE_PRODUTO = ("/img/p/", "/produto", "/produtos", "/product", "/media/",
                     "/uploads/", "/fotos/", "/images/produto")


def _imagens_da_pagina(html: str, base: str, limite: int = 8) -> list[str]:
    """As fotos que a pessoa vê na página, quando não há dado estruturado.

    Site pequeno de autopeça quase nunca publica JSON-LD, e muitos nem og:image
    têm — mas a foto do produto está lá, numa ``<img>``. Sem olhar para elas, o
    extrator dizia "achei zero fotos" numa página cheia de foto.

    Pontua em vez de aceitar tudo: caminho com cara de produto vale mais,
    imagem declarada pequena e nome de arquivo com cara de enfeite saem fora.
    """
    achadas: dict[str, tuple[int, str]] = {}

    for tag in _RE_IMG.findall(html or ""):
        attrs = _atributos(tag)

        bruto = ""
        for chave in _FONTES_DE_IMG:
            if attrs.get(chave, "").strip():
                bruto = attrs[chave].strip()
                break
        if not bruto and attrs.get("srcset"):
            # srcset: "a.jpg 1x, b.jpg 2x" — a primeira serve
            bruto = attrs["srcset"].split(",")[0].strip().split(" ")[0]
        if not bruto or bruto.startswith("data:"):
            continue

        endereco = urljoin(base, bruto)
        if not endereco.startswith(("http://", "https://")):
            continue

        caminho = urlparse(endereco).path.lower()
        if caminho.endswith(".svg"):
            continue
        alvo = (caminho + " " + attrs.get("alt", "").lower() +
                " " + attrs.get("class", "").lower())
        if any(lixo in alvo for lixo in _LIXO_NA_IMAGEM):
            continue

        # imagem declarada pequena é ícone, não foto de peça
        try:
            if (attrs.get("width") and int(attrs["width"]) < 120) or \
                    (attrs.get("height") and int(attrs["height"]) < 120):
                continue
        except ValueError:
            pass

        pontos = 1
        if any(p in caminho for p in _PISTA_DE_PRODUTO):
            pontos += 3
        if attrs.get("alt"):
            pontos += 1

        # A mesma foto costuma aparecer em vários tamanhos, mudando só a query
        # (?w=530&h=530). Guardar por caminho evita cinco cópias da mesma peça.
        chave = urlparse(endereco).netloc + caminho
        anterior = achadas.get(chave)
        if anterior is None or pontos > anterior[0]:
            achadas[chave] = (pontos, endereco)

    ordenadas = sorted(achadas.values(), key=lambda x: -x[0])
    return [endereco for _, endereco in ordenadas[:limite]]


# Modelos de veículo aparecem soltos no nome e na descrição; achar isso é o que
# mais economiza tempo de quem está identificando a peça.
_RE_APLICACAO = re.compile(
    r"\b(?:para|aplica[çc][ãa]o|compat[íi]vel(?:\s+com)?)\s*:?\s*([^.;\n]{6,180})",
    re.I)


def _achar_aplicacao(d: DadosExtraidos) -> str:
    for rotulo in ("aplicação", "aplicacao", "veículo", "veiculo",
                   "compatibilidade", "montadora"):
        for chave, valor in d.atributos.items():
            if rotulo in chave.lower():
                return valor
    achado = _RE_APLICACAO.search(d.descricao_referencia or "")
    return _texto(achado.group(1)) if achado else ""


def _texto(bruto: str) -> str:
    import html as _html
    texto = re.sub(r"<[^>]+>", " ", bruto or "")
    texto = _html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()
