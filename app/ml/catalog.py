"""Busca de produtos no catálogo do Mercado Livre.

Estratégia para autopeça: o part number (coluna `Cód` da planilha) é o
identificador que o catálogo do ML usa de fato. Buscamos por ele primeiro;
se não achar, tentamos código + descrição; por último a descrição sozinha
(que é fraca e por isso entra com confiança baixa).

IMPORTANTE: publicar por catálogo é o caminho legítimo para não precisar de
fotos próprias — o ML herda imagens, título e ficha técnica do produto de
catálogo. Baixar imagens de anúncios de terceiros seria violação de direito
autoral e não é feito em lugar nenhum deste código.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.abreviacoes import expandir
from app.config import get_settings
from app.ml.client import MLClient, MLApiError


@dataclass
class CandidatoCatalogo:
    catalog_product_id: str
    nome: str
    domain_id: str | None
    status: str | None
    confianca: str          # "alta" | "media" | "baixa"
    motivo: str
    atributos: dict = field(default_factory=dict)
    fotos: int = 0
    foto_url: str = ""
    permalink: str = ""
    category_id: str = ""


def _normalizar(texto: str) -> str:
    """Remove acentos, pontuação e espaços — para comparar part numbers."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]", "", t).upper()


def _part_number_bate(codigo: str, produto: dict) -> bool:
    """Confere se o código da planilha aparece nos atributos do produto."""
    alvo = _normalizar(codigo)
    if len(alvo) < 5:          # códigos curtos dão falso positivo demais
        return False
    for attr in produto.get("attributes", []) or []:
        aid = (attr.get("id") or "").upper()
        if aid in {"PART_NUMBER", "ALPHANUMERIC_MODEL", "MODEL", "GTIN", "SELLER_SKU",
                   "OEM", "MPN"}:
            if _normalizar(attr.get("value_name") or "") == alvo:
                return True
    return _normalizar(produto.get("name") or "").find(alvo) >= 0


async def buscar_no_catalogo(
    client: MLClient,
    codigo: str,
    descricao: str = "",
    limite: int = 5,
) -> list[CandidatoCatalogo]:
    """Retorna candidatos de catálogo ordenados por confiança."""
    s = get_settings()
    # O ERP abrevia ("BBA ARLA EMITEC 12V"); o catálogo do ML escreve por extenso
    # ("Bomba De Arla 32 Emitec 12v"). Sem expandir, a busca por descrição não
    # encontra nada.
    desc_expandida = expandir(descricao)

    tentativas = [
        (codigo, "part number exato"),
        (f"{codigo} {desc_expandida}".strip(), "código + descrição"),
        (desc_expandida, "descrição por extenso"),
    ]
    if desc_expandida.upper() != (descricao or "").upper():
        # ainda tenta a descrição crua, caso o catálogo use a abreviação
        tentativas.append((descricao, "descrição do ERP"))

    vistos: dict[str, CandidatoCatalogo] = {}

    for termo, motivo in tentativas:
        if not termo.strip():
            continue
        try:
            resp = await client.get(
                "/products/search",
                params={"status": "active", "site_id": s.ml_site_id, "q": termo},
            )
        except MLApiError as exc:
            if exc.status == 404:
                continue
            raise

        for produto in (resp.get("results") or [])[:limite]:
            pid = produto.get("id")
            if not pid or pid in vistos:
                continue

            bate = _part_number_bate(codigo, produto)
            if motivo == "part number exato" and bate:
                confianca = "alta"
            elif bate:
                confianca = "media"
            else:
                confianca = "baixa"

            vistos[pid] = CandidatoCatalogo(
                catalog_product_id=pid,
                nome=produto.get("name") or "",
                domain_id=produto.get("domain_id"),
                status=produto.get("status"),
                confianca=confianca,
                motivo=motivo,
                atributos={a.get("id"): a.get("value_name")
                           for a in (produto.get("attributes") or [])},
                fotos=len(produto.get("pictures") or []),
            )

        # achou match forte? não precisa degradar a busca
        if any(c.confianca == "alta" for c in vistos.values()):
            break

    ordem = {"alta": 0, "media": 1, "baixa": 2}
    return sorted(vistos.values(), key=lambda c: ordem[c.confianca])


async def detalhe_produto(client: MLClient, catalog_product_id: str) -> dict:
    return await client.get(f"/products/{catalog_product_id}")


def _extrair_foto(produto: dict) -> str:
    """Pega a URL da primeira imagem do produto de catálogo.

    O ML varia o formato entre 'pictures[].url', 'pictures[].secure_url' e
    'pictures[].id' — por isso a busca é defensiva.
    """
    for pic in (produto.get("pictures") or []):
        if not isinstance(pic, dict):
            continue
        for chave in ("secure_url", "url"):
            if pic.get(chave):
                return pic[chave]
        if pic.get("id"):
            return f"https://http2.mlstatic.com/D_{pic['id']}-O.jpg"
    return ""


async def _categoria_do_produto(client: MLClient, produto: dict,
                                nome: str) -> str:
    """Descobre o category_id do produto de catálogo.

    O ML EXIGE `category_id` no POST /items mesmo quando a publicação é por
    catálogo — omitir devolve:
        "The body does not contains some or none of the following properties
         [category_id]"

    O campo nem sempre vem na raiz de /products/{id}, então tentamos três
    fontes, da mais confiável para a menos:
      1. `category_id` na raiz do produto
      2. `category_id` do anúncio que está ganhando o buy box
      3. o preditor de categoria do ML a partir do nome do produto
    """
    if produto.get("category_id"):
        return produto["category_id"]

    vencedor = produto.get("buy_box_winner") or {}
    if isinstance(vencedor, dict) and vencedor.get("category_id"):
        return vencedor["category_id"]

    for filho in (produto.get("children_ids") or [])[:1]:
        try:
            detalhe = await client.get(f"/products/{filho}")
            if detalhe.get("category_id"):
                return detalhe["category_id"]
        except MLApiError:
            pass

    if nome:
        s = get_settings()
        try:
            sugestoes = await client.get(
                f"/sites/{s.ml_site_id}/domain_discovery/search",
                params={"limit": 1, "q": nome[:120]},
            )
            if isinstance(sugestoes, list) and sugestoes:
                return sugestoes[0].get("category_id") or ""
        except MLApiError:
            pass

    return ""


async def enriquecer_candidato(client: MLClient,
                               cand: CandidatoCatalogo) -> CandidatoCatalogo:
    """Busca foto, permalink e ficha técnica do produto de catálogo.

    É o que alimenta a tela de conferência: sem a foto, o operador não tem como
    confirmar que o produto do ML é mesmo a peça dele.
    """
    try:
        produto = await detalhe_produto(client, cand.catalog_product_id)
    except MLApiError:
        return cand      # sem detalhe a tela ainda funciona, só fica sem imagem

    cand.foto_url = _extrair_foto(produto)
    cand.permalink = produto.get("permalink") or ""
    cand.fotos = len(produto.get("pictures") or [])
    if produto.get("attributes"):
        cand.atributos = {
            a.get("name") or a.get("id"): a.get("value_name")
            for a in produto["attributes"]
            if a.get("value_name")
        }
    if produto.get("name"):
        cand.nome = produto["name"]
    # O status do detalhe é a verdade. O filtro status=active da busca não é
    # confiável: já voltou produto inativo, e o POST /items recusa com
    # "Product MLB... is not active".
    cand.status = produto.get("status") or cand.status
    cand.category_id = await _categoria_do_produto(client, produto, cand.nome)
    return cand


def publicavel(cand: CandidatoCatalogo) -> tuple[bool, str]:
    """Diz se dá para publicar contra este produto de catálogo, e por que não."""
    if (cand.status or "").lower() != "active":
        return False, f"produto de catálogo está '{cand.status or 'sem status'}' no ML"
    if not cand.category_id:
        return False, "categoria do Mercado Livre não identificada"
    return True, ""


async def anuncios_ativos(client: MLClient, termo: str) -> dict:
    """Procura anúncios ATIVOS no ML para o termo dado.

    Serve para quando não há produto de catálogo utilizável: se existem
    concorrentes vendendo a peça, ela é anunciável — só que como anúncio
    próprio, o que exige foto. De quebra descobrimos a categoria e a faixa de
    preço praticada.
    """
    s = get_settings()
    vazio = {"total": 0, "category_id": "", "exemplos": [], "precos": []}
    if not termo.strip():
        return vazio
    try:
        resp = await client.get(f"/sites/{s.ml_site_id}/search",
                                params={"q": termo[:120], "limit": 5})
    except MLApiError:
        return vazio

    resultados = resp.get("results") or []
    if not resultados:
        return vazio

    categorias = [r.get("category_id") for r in resultados if r.get("category_id")]
    mais_comum = max(set(categorias), key=categorias.count) if categorias else ""
    return {
        "total": (resp.get("paging") or {}).get("total", len(resultados)),
        "category_id": mais_comum,
        "exemplos": [r.get("title", "")[:70] for r in resultados[:3]],
        "precos": [r.get("price") for r in resultados if r.get("price")],
    }


async def escolher_publicavel(
    client: MLClient,
    candidatos: list[CandidatoCatalogo],
    maximo: int = 8,
) -> tuple[CandidatoCatalogo | None, list[str]]:
    """Percorre os candidatos e devolve o primeiro que dá para publicar.

    Um part number pode bater com vários produtos de catálogo, e nem todos estão
    ativos — o ML mantém produtos descontinuados no acervo. Em vez de desistir no
    primeiro, tentamos os próximos antes de marcar o item como sem catálogo.

    Retorna (candidato_ok, motivos_das_recusas).
    """
    motivos: list[str] = []
    for cand in candidatos[:maximo]:
        enriquecido = await enriquecer_candidato(client, cand)
        ok, motivo = publicavel(enriquecido)
        if ok:
            return enriquecido, motivos
        motivos.append(f"{enriquecido.catalog_product_id}: {motivo}")
    return None, motivos
