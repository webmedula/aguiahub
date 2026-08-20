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


# O 'wid' aponta o anúncio que está ganhando a vitrine — é a informação mais
# confiável da URL, porque é um anúncio comprovadamente ativo.
_RE_WID = re.compile(r"[?&#]wid=(ML[ABCMU]\d{6,})", re.IGNORECASE)
# Página /up/ = "user product" do ML (identificador com U: MLBU...)
_RE_USER_PRODUCT = re.compile(r"(ML[ABCMU]U)(\d{6,})", re.IGNORECASE)
# Anúncio ou produto de catálogo comuns
_RE_ANUNCIO = re.compile(r"(ML[ABCMU])-?(\d{6,})", re.IGNORECASE)


def extrair_referencias(texto: str) -> list[tuple[str, str]]:
    """Devolve [(tipo, id)] achados na URL, na ordem em que devemos TENTAR.

    Produto de catálogo vem primeiro porque o Mercado Livre bloqueou a leitura
    de anúncios de terceiros pela API (403 Forbidden). O anúncio (`wid`) fica
    como último recurso: se a peça for do próprio vendedor, ele ainda funciona.
    """
    texto = texto or ""
    achados: list[tuple[str, str]] = []

    # 1. produto de catálogo do formato novo: /up/MLBU...
    for m in _RE_USER_PRODUCT.finditer(texto):
        achados.append(("produto", f"{m.group(1).upper()}{m.group(2)}"))

    # 2. produto de catálogo do formato antigo: /p/MLB...
    if "/p/" in texto:
        m = re.search(r"/p/(ML[ABCMU]-?\d{6,})", texto, re.IGNORECASE)
        if m:
            achados.append(("produto", m.group(1).upper().replace("-", "")))

    # 3. o anúncio apontado pelo wid
    m = _RE_WID.search(texto)
    if m:
        achados.append(("anuncio", m.group(1).upper()))

    # 4. qualquer outro ML... solto no texto
    for m in _RE_ANUNCIO.finditer(texto):
        ident = f"{m.group(1).upper()}{m.group(2)}"
        # não confundir o miolo de 'MLBU123' com 'MLB123'
        if any(ident in j for _, j in achados):
            continue
        achados.append(("anuncio", ident))

    vistos, unicos = set(), []
    for tipo, ident in achados:
        if ident not in vistos:
            vistos.add(ident)
            unicos.append((tipo, ident))
    return unicos


def extrair_id_anuncio(texto: str) -> str:
    """Tira o identificador do Mercado Livre de uma URL ou de um texto solto.

    Aceita tudo que o operador consegue copiar do navegador:
        https://produto.mercadolivre.com.br/MLB-1234567890-bomba-arla-_JM
        https://www.mercadolivre.com.br/p/MLB12345678
        https://www.mercadolivre.com.br/nome-da-peca/up/MLBU4286980046
        https://...#...&wid=MLB4876653919&sid=search
        MLB1234567890

    A ordem de preferência importa. O `wid` vem primeiro porque identifica um
    anúncio que está de fato ativo — enquanto a mesma peça pode ter também uma
    ficha de catálogo abandonada, que é justamente o que atrapalhou o
    diagnóstico do item 5273337.
    """
    texto = texto or ""

    achado = _RE_WID.search(texto)
    if achado:
        return achado.group(1).upper()

    achado = _RE_ANUNCIO.search(texto)
    if achado:
        # cuidado: 'MLBU4286980046' não deve virar 'MLB4286980046'
        casamento_u = _RE_USER_PRODUCT.search(texto)
        if casamento_u and casamento_u.start() <= achado.start():
            return f"{casamento_u.group(1).upper()}{casamento_u.group(2)}"
        return f"{achado.group(1).upper()}{achado.group(2)}"

    achado = _RE_USER_PRODUCT.search(texto)
    return f"{achado.group(1).upper()}{achado.group(2)}" if achado else ""


async def produto_do_anuncio(client: MLClient, referencia: str) -> dict:
    """Descobre o produto de catálogo a partir de um link do Mercado Livre.

    Tenta as referências da URL em cascata, produto de catálogo primeiro. Isso
    importa porque o ML passou a responder 403 na leitura de anúncios de outros
    vendedores — o mesmo bloqueio que derrubou a busca pública. A página do
    produto (`/p/` ou `/up/`) continua acessível e é o que realmente
    precisamos, já que é dela que vêm foto, ficha e categoria.
    """
    referencias = extrair_referencias(referencia)
    if not referencias:
        raise ValueError(
            "Não consegui identificar o anúncio nesse endereço. Copie o link "
            "direto da página da peça no Mercado Livre."
        )

    bloqueios: list[str] = []

    for tipo, ident in referencias:
        try:
            if tipo == "produto":
                produto = await client.get(f"/products/{ident}")
                return {
                    "tipo": "produto_de_catalogo",
                    "catalog_product_id": ident,
                    "category_id": produto.get("category_id") or "",
                    "titulo": produto.get("name") or "",
                    "status": produto.get("status") or "",
                    "foto": _extrair_foto(produto),
                    "permalink": produto.get("permalink") or "",
                }

            # O ML bloqueia a leitura completa de anúncios de terceiros, mas
            # às vezes libera campos específicos. Tentamos o mínimo necessário
            # antes de desistir e mandar o operador navegar até o produto.
            try:
                anuncio = await client.get(
                    f"/items/{ident}",
                    params={"attributes": "id,catalog_product_id,category_id,"
                                          "title,thumbnail,permalink,status,price"},
                )
            except MLApiError as parcial:
                if parcial.status not in (401, 403):
                    raise
                anuncio = await client.get(f"/items/{ident}")
            return {
                "tipo": "anuncio",
                "catalog_product_id": anuncio.get("catalog_product_id") or "",
                "category_id": anuncio.get("category_id") or "",
                "titulo": anuncio.get("title") or "",
                "status": anuncio.get("status") or "",
                "foto": (anuncio.get("thumbnail")
                         or ((anuncio.get("pictures") or [{}])[0].get("url", ""))),
                "permalink": anuncio.get("permalink") or "",
                "preco": anuncio.get("price"),
            }
        except MLApiError as exc:
            if exc.status in (401, 403):
                bloqueios.append(ident)
                continue
            if exc.status == 404:
                continue
            raise

    if bloqueios:
        raise ValueError(
            "O Mercado Livre não deixa a aplicação ler anúncios de outros "
            "vendedores (erro 403). Em vez do link do anúncio, copie o link da "
            "PÁGINA DO PRODUTO — o endereço que tem /p/ ou /up/ no meio. "
            "Para chegar nela, clique no nome do produto dentro do anúncio."
        )

    raise ValueError(
        "Não encontrei esse produto no Mercado Livre. Confira se o link está "
        "completo e se a página ainda existe."
    )


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


_ORDEM_CONFIANCA = {"alta": 3, "media": 2, "baixa": 1}


async def categoria_e_de_autopecas(client: MLClient, category_id: str) -> tuple[bool, str]:
    """Confere se a categoria pertence à árvore de acessórios para veículos.

    Esta é a trava contra o pior tipo de erro que o sistema já cometeu: casar
    'CORPO DISTRIBUIDOR' (Bosch, R$ 10.011) com o livro "Corpo a Corpo" de Alex
    Varenne, ou 'REPARO UNIDADE HEUI' com um kit de alfinetes de costura.

    Sem raízes configuradas, a checagem é desligada e tudo passa.
    """
    s = get_settings()
    raizes = s.categorias_raiz
    if not raizes or not category_id:
        return True, ""

    try:
        cat = await client.get(f"/categories/{category_id}")
    except MLApiError:
        return True, ""        # na dúvida não bloqueia; o operador ainda confere

    caminho = cat.get("path_from_root") or []
    if not caminho:
        return True, ""

    raiz = (caminho[0].get("id") or "").upper()
    if raiz in raizes:
        return True, ""

    nomes = " > ".join(c.get("name", "") for c in caminho[:3])
    return False, (f"categoria do ML é '{nomes}', fora de acessórios para "
                   f"veículos — provável casamento errado")


async def prever_categoria(client: MLClient, titulo: str) -> str:
    """Usa o preditor do ML para achar a categoria a partir do título.

    Necessário no anúncio próprio: não há produto de catálogo de onde herdar.
    """
    if not titulo.strip():
        return ""
    s = get_settings()
    try:
        sugestoes = await client.get(
            f"/sites/{s.ml_site_id}/domain_discovery/search",
            params={"limit": 1, "q": titulo[:120]},
        )
    except MLApiError:
        return ""
    if isinstance(sugestoes, list) and sugestoes:
        return sugestoes[0].get("category_id") or ""
    return ""


def publicavel(cand: CandidatoCatalogo) -> tuple[bool, str]:
    """Checagens que não dependem de chamada à API."""
    s = get_settings()

    if (cand.status or "").lower() != "active":
        return False, f"produto de catálogo está '{cand.status or 'sem status'}' no ML"
    if not cand.category_id:
        return False, "categoria do Mercado Livre não identificada"

    minimo = _ORDEM_CONFIANCA.get(s.confianca_minima, 3)
    if _ORDEM_CONFIANCA.get(cand.confianca, 0) < minimo:
        return False, (f"confiança '{cand.confianca}' abaixo do mínimo "
                       f"'{s.confianca_minima}' — casou só pela descrição, "
                       f"sem bater o part number")
    return True, ""


async def anuncios_ativos(client: MLClient, termo: str) -> dict:
    """Procura anúncios ATIVOS no ML para o termo dado.

    Serve para quando não há produto de catálogo utilizável: se existem
    concorrentes vendendo a peça, ela é anunciável — só que como anúncio
    próprio, o que exige foto. De quebra descobrimos a categoria e a faixa de
    preço praticada.
    """
    s = get_settings()
    vazio = {"total": 0, "category_id": "", "exemplos": [], "precos": [],
             "erro": ""}
    if not termo.strip():
        return vazio
    try:
        resp = await client.get(f"/sites/{s.ml_site_id}/search",
                                params={"q": termo[:120], "limit": 5})
    except MLApiError as exc:
        # O ML bloqueou a busca pública de anúncios (403) para aplicações.
        # Devolver lista vazia aqui faria "bloqueado" parecer "não encontrado",
        # que foi exatamente o que confundiu o diagnóstico do item 5273337.
        vazio["erro"] = (
            "a busca pública de anúncios do ML está bloqueada para aplicações "
            f"(HTTP {exc.status}) — não dá para verificar automaticamente"
            if exc.status in (401, 403)
            else f"falha na busca de anúncios: {exc.mensagem_amigavel()}"
        )
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
        if not ok:
            motivos.append(f"{enriquecido.catalog_product_id}: {motivo}")
            continue

        # A checagem de categoria custa uma chamada, então só roda depois que
        # o candidato passou pelo resto.
        compativel, motivo_cat = await categoria_e_de_autopecas(
            client, enriquecido.category_id)
        if not compativel:
            motivos.append(f"{enriquecido.catalog_product_id}: {motivo_cat}")
            continue

        return enriquecido, motivos
    return None, motivos
