"""Análise de catálogo e publicação dos anúncios no Mercado Livre.

O fluxo tem DOIS passos separados de propósito, porque a conta é real:

  1. analisar_lote()      — consulta o catálogo, monta o preview, NÃO publica.
  2. publicar_aprovados() — publica somente o que o operador aprovou na tela.

Nenhum caminho publica sem aprovação explícita. Isso existe porque o pior erro
possível aqui não é falhar, é casar a peça com o produto de catálogo errado e
anunciar algo que você não vende.

Sobre fotos: na publicação por catálogo o ML herda imagens, título e ficha
técnica do produto de catálogo (`pictures: []`). Reaproveitar imagem de anúncio
de terceiro seria violação de direito autoral e não é feito em lugar nenhum
deste código.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

from app import storage
from app.config import get_settings
from app.abreviacoes import expandir
from app.ml.catalog import (CandidatoCatalogo, anuncios_ativos,
                            buscar_no_catalogo, categoria_e_de_autopecas,
                            enriquecer_candidato, escolher_publicavel,
                            prever_categoria, produto_do_anuncio, publicavel)
from app.ml.client import MLClient, MLApiError
from app.sheets import LinhaProduto, calcular_preco, montar_descricao, montar_titulo

log = logging.getLogger("aguiahub.publisher")

# Preço mínimo aceito pelo ML no Brasil. Abaixo disso o anúncio é recusado.
PRECO_MINIMO = 1.0

# Status possíveis de um item
AGUARDANDO = "aguardando_aprovacao"   # casou com o catálogo, esperando o operador
SEM_CATALOGO = "sem_catalogo"         # não achou match -> precisa de foto própria
CATALOGO_INATIVO = "catalogo_inativo"  # achou, mas o produto não está ativo no ML
SEM_CATEGORIA = "sem_categoria"       # produto ativo, mas não achei a categoria
DUVIDOSO = "sugestao_duvidosa"        # achou algo, mas não é confiável
IGNORADO = "ignorado"                 # dado ruim, preço inválido ou já publicado
ERRO = "erro"
PUBLICADO = "publicado"


@dataclass
class ResultadoItem:
    linha: int
    codigo: str
    titulo: str
    status: str
    preco: float = 0.0
    quantidade: int = 0
    descricao_erp: str = ""
    marca: str = ""
    candidato: CandidatoCatalogo | None = None
    outros_candidatos: list[CandidatoCatalogo] = field(default_factory=list)
    ml_item_id: str | None = None
    permalink: str | None = None
    mensagem: str = ""


def montar_payload(
    p: LinhaProduto,
    preco: float,
    listing_type_id: str = "gold_special",
    catalog_product_id: str | None = None,
    category_id: str | None = None,
) -> dict:
    s = get_settings()
    payload: dict = {
        "site_id": s.ml_site_id,
        "price": preco,
        "currency_id": "BRL",
        "available_quantity": max(1, p.quantidade),
        "buying_mode": "buy_it_now",
        "condition": "new",          # catálogo só aceita 'new'
        "listing_type_id": listing_type_id,
    }

    # O ML exige category_id SEMPRE, inclusive na publicação por catálogo.
    if category_id:
        payload["category_id"] = category_id

    if catalog_product_id:
        # Título, fotos e atributos vêm do catálogo — enviar os nossos causaria
        # conflito de validação com o produto já cadastrado.
        payload["catalog_product_id"] = catalog_product_id
        payload["catalog_listing"] = True
        payload["pictures"] = []
    else:
        titulo = montar_titulo(p)
        payload["title"] = titulo
        payload["pictures"] = []     # preenchido quando houver foto própria
        payload["attributes"] = _atributos(p)
        aplicar_user_product(payload, titulo)

    return payload


# Comprimento máximo do family_name: a doc do ML manda respeitar o
# `max_title_length` do domínio, que no MLB é 60 — o mesmo do título.
FAMILY_NAME_MAX = 60


def aplicar_user_product(payload: dict, titulo: str) -> dict:
    """Acrescenta `family_name` e a garantia exigidos pelo modelo User Product.

    Erro real na primeira publicação de verdade (injetor A2C59517051):

        The body does not contains some or none of the following properties
        [family_name]

    O Mercado Livre migrou os anúncios sem catálogo para o modelo *User
    Product* (os MLBU). Agora, ao criar um anúncio próprio, é preciso mandar
    ou o `user_product_id` de um produto seu que já exista, ou o
    `family_name` — o nome da família de produtos que o ML vai criar em nome
    do vendedor. É campo de topo, não vai dentro de `attributes`.

    A garantia entra junto porque é a exigência seguinte da mesma validação e
    não faz sentido descobrir isso num segundo deploy. Peça nova de reposição
    tem garantia do vendedor; o padrão pode ser mudado por variável de
    ambiente sem mexer no código.
    """
    if payload.get("catalog_product_id") or payload.get("user_product_id"):
        # Publicação por catálogo: a identidade do produto já vem de lá.
        return payload

    s = get_settings()
    payload.setdefault("family_name", (titulo or "").strip()[:FAMILY_NAME_MAX])

    termos = {t.get("id") for t in payload.get("sale_terms", [])}
    sale_terms = list(payload.get("sale_terms", []))
    if "WARRANTY_TYPE" not in termos:
        sale_terms.append({"id": "WARRANTY_TYPE",
                           "value_name": s.ml_garantia_tipo})
    if "WARRANTY_TIME" not in termos:
        sale_terms.append({"id": "WARRANTY_TIME",
                           "value_name": s.ml_garantia_prazo})
    payload["sale_terms"] = sale_terms
    return payload


def _atributos(p: LinhaProduto) -> list[dict]:
    attrs: list[dict] = []
    if p.marca:
        attrs.append({"id": "BRAND", "value_name": p.marca.strip().title()})
    if p.codigo:
        attrs.append({"id": "PART_NUMBER", "value_name": p.codigo.strip().upper()})
        attrs.append({"id": "SELLER_SKU", "value_name": p.codigo.strip().upper()})
    return attrs


async def _pista_de_mercado(client: MLClient, p: LinhaProduto) -> str:
    """Diz se a peça é vendida no ML mesmo sem produto de catálogo utilizável.

    Sem isso, o operador só recebe "sem catálogo" e não sabe se o problema é a
    peça não ter mercado ou apenas faltar foto.
    """
    termo = f"{p.codigo} {expandir(p.descricao)}".strip()
    dados = await anuncios_ativos(client, termo)
    if not dados["total"]:
        dados = await anuncios_ativos(client, expandir(p.descricao))

    if dados.get("erro"):
        return (f"Não deu para consultar anúncios: {dados['erro']}. "
                "Se você achar a peça no ML pelo navegador, cole o link aqui "
                "para vincular manualmente.")

    if not dados["total"]:
        return ("Também não encontrei anúncios ativos com esse termo. "
                "Se você achar a peça no ML pelo navegador, cole o link aqui "
                "para vincular manualmente.")

    precos = [x for x in dados["precos"] if x]
    faixa = ""
    if precos:
        faixa = f" Preços praticados: R$ {min(precos):.2f} a R$ {max(precos):.2f}."
    exemplo = dados["exemplos"][0] if dados["exemplos"] else ""
    return (f"Há {dados['total']} anúncio(s) ativo(s) no ML "
            f"(ex.: \"{exemplo}\").{faixa} "
            f"Dá para anunciar como anúncio próprio, mas precisa de foto.")


# ---------------------------------------------------------------------------
# Passo 1 — análise (nunca publica)
# ---------------------------------------------------------------------------

async def analisar_lote(
    itens: list[LinhaProduto],
    *,
    regra_preco: str = "preco_publico",
    percentual: float = 0.0,
    lote_id: int,
    pular_ja_publicados: bool = True,
) -> list[ResultadoItem]:
    """Casa cada item com o catálogo e grava tudo como AGUARDANDO aprovação."""
    s = get_settings()
    client = MLClient()
    semaforo = asyncio.Semaphore(s.publish_concurrency)

    async def tratar(p: LinhaProduto) -> ResultadoItem:
        async with semaforo:
            preco = calcular_preco(p, regra_preco, percentual)
            res = ResultadoItem(
                linha=p.linha, codigo=p.codigo, titulo=montar_titulo(p),
                status=ERRO, preco=preco, quantidade=p.quantidade,
                descricao_erp=p.descricao, marca=p.marca,
            )

            if not p.publicavel:
                res.status = IGNORADO
                res.mensagem = "; ".join(p.problemas)
                return res

            if preco < PRECO_MINIMO:
                res.status = IGNORADO
                res.mensagem = f"preço calculado (R$ {preco:.2f}) abaixo do mínimo do ML"
                return res

            if pular_ja_publicados:
                ja = storage.sku_ja_publicado(p.codigo)
                if ja:
                    res.status = IGNORADO
                    res.ml_item_id = ja
                    res.mensagem = f"SKU já publicado antes ({ja})"
                    return res

            try:
                candidatos = await buscar_no_catalogo(client, p.codigo, p.descricao)
            except MLApiError as exc:
                res.mensagem = f"falha ao consultar catálogo: {exc.mensagem_amigavel()}"
                return res

            if not candidatos:
                res.status = SEM_CATALOGO
                res.mensagem = ("sem correspondência no catálogo. "
                                + await _pista_de_mercado(client, p))
                return res

            # Percorre os candidatos até achar um que esteja ATIVO e com
            # categoria. Produto inativo faz o POST /items falhar com
            # "Product MLB... is not active".
            melhor, recusas = await escolher_publicavel(client, candidatos)

            if melhor is None:
                # Separar os dois motivos importa: 'inativo' é limitação do
                # catálogo do ML (só o vínculo manual resolve), 'sem categoria'
                # é falha nossa de detecção e tem conserto no código.
                juntos = " ".join(recusas)
                if "fora de acessórios" in juntos or "confiança" in juntos:
                    # Achou produto, mas era casamento ruim (livro, esteira,
                    # alfinete). Melhor não oferecer para aprovação.
                    res.status = DUVIDOSO
                elif recusas and all("ategoria" in m for m in recusas):
                    res.status = SEM_CATEGORIA
                else:
                    res.status = CATALOGO_INATIVO
                res.mensagem = ("nenhum produto de catálogo utilizável ("
                                + "; ".join(recusas) + "). "
                                + await _pista_de_mercado(client, p))
                return res

            res.candidato = melhor
            res.outros_candidatos = candidatos[1:4]
            res.status = AGUARDANDO
            res.mensagem = f"correspondência de confiança {melhor.confianca}"
            return res

    resultados = list(await asyncio.gather(*(tratar(p) for p in itens)))

    for r in resultados:
        cand = r.candidato
        storage.registrar_item(
            lote_id, r.linha, r.codigo, r.titulo,
            payload={"preco": r.preco,
                     "catalog_product_id": cand.catalog_product_id if cand else None},
            status=r.status,
            erro=r.mensagem if r.status in (ERRO, IGNORADO, SEM_CATALOGO,
                                            CATALOGO_INATIVO,
                                            SEM_CATEGORIA,
                                            DUVIDOSO) else None,
            descricao_erp=r.descricao_erp,
            marca=r.marca,
            quantidade=r.quantidade,
            preco=r.preco,
            confianca=cand.confianca if cand else None,
            catalog_product_id=cand.catalog_product_id if cand else None,
            catalog_nome=cand.nome if cand else None,
            catalog_foto=cand.foto_url if cand else None,
            catalog_permalink=cand.permalink if cand else None,
            catalog_atributos=cand.atributos if cand else None,
            catalog_category_id=cand.category_id if cand else None,
        )

    return resultados


# ---------------------------------------------------------------------------
# Vínculo manual, quando a busca automática não acha
# ---------------------------------------------------------------------------

async def vincular_por_link(item_id: int, referencia: str) -> dict:
    """Vincula um item do lote a um produto de catálogo achado manualmente.

    Necessário porque o ML bloqueou a busca pública de anúncios: o operador
    consegue achar a peça no navegador, mas a aplicação não. Colando o link,
    pegamos `catalog_product_id` e `category_id` direto da fonte — sem chute.
    """
    client = MLClient()
    dados = await produto_do_anuncio(client, referencia)

    if not dados["catalog_product_id"]:
        return {"ok": False, "mensagem": (
            f"O anúncio '{dados['titulo'][:60]}' não está vinculado ao catálogo "
            "do Mercado Livre, então não dá para herdar foto e ficha dele. "
            "Este item precisa de foto própria.")}

    cand = CandidatoCatalogo(
        catalog_product_id=dados["catalog_product_id"],
        nome=dados["titulo"], domain_id=None, status=dados.get("status"),
        confianca="alta", motivo="vinculado manualmente pelo operador",
        foto_url=dados.get("foto", ""), permalink=dados.get("permalink", ""),
        category_id=dados.get("category_id", ""),
    )
    cand = await enriquecer_candidato(client, cand)

    ok, motivo = publicavel(cand)
    if not ok:
        return {"ok": False, "mensagem": f"Não dá para publicar: {motivo}."}

    compativel, motivo_cat = await categoria_e_de_autopecas(client, cand.category_id)
    if not compativel:
        return {"ok": False, "mensagem": (
            f"O link aponta para um produto fora de autopeças — {motivo_cat}. "
            "Confira se copiou o anúncio certo.")}

    storage.atualizar_dados_catalogo(
        item_id,
        status=AGUARDANDO,
        decidido_em=datetime.now(timezone.utc).isoformat(),
        confianca="alta",
        catalog_product_id=cand.catalog_product_id,
        catalog_nome=cand.nome,
        catalog_foto=cand.foto_url,
        catalog_permalink=cand.permalink,
        catalog_category_id=cand.category_id,
        erro=None,
    )
    return {"ok": True, "mensagem": f"Vinculado a {cand.nome[:70]}.",
            "produto": cand.catalog_product_id}


# ---------------------------------------------------------------------------
# Passo 2 — publicação (somente aprovados)
# ---------------------------------------------------------------------------

async def publicar_aprovados(lote_id: int,
                             listing_type_id: str = "gold_special") -> list[dict]:
    """Publica APENAS os itens marcados como aprovados na tela de conferência."""
    s = get_settings()
    client = MLClient()
    semaforo = asyncio.Semaphore(s.publish_concurrency)
    aprovados = storage.itens_aprovados(lote_id)

    # Checa a conta ANTES de publicar. Se ela estiver bloqueada (sem endereço,
    # com restrição), todos os itens falhariam igual — melhor avisar uma vez.
    diagnostico = await client.diagnostico_conta()
    if not diagnostico["apta"]:
        motivo = " | ".join(diagnostico["impedimentos"])
        log.warning("Publicação abortada — conta inapta: %s", motivo)
        for row in aprovados:
            storage.atualizar_item(row["id"], status=ERRO, erro=motivo)
        return [{"linha": r["linha"], "codigo": r["sku"],
                 "titulo": r["catalog_nome"] or r["titulo"],
                 "preco": float(r["preco"] or 0),
                 "status": ERRO, "mensagem": motivo} for r in aprovados]

    async def publicar(row) -> dict:
        async with semaforo:
            item_id = row["id"]
            # Reconstrói o mínimo necessário — a publicação por catálogo não
            # depende de título nem de atributos nossos.
            p = LinhaProduto(
                aba="", linha=row["linha"], codigo=row["sku"] or "",
                descricao=row["descricao_erp"] or "",
                quantidade=int(row["quantidade"] or 1),
                marca=row["marca"] or "",
            )
            categoria = row["catalog_category_id"]
            if not categoria:
                # O ML recusa o POST sem category_id. Melhor barrar aqui, com
                # mensagem clara, do que deixar a API devolver erro genérico.
                msg = ("categoria do Mercado Livre não identificada para este "
                       "produto de catálogo — reanalise o lote")
                storage.atualizar_item(item_id, status=ERRO, erro=msg)
                return {"linha": row["linha"], "codigo": row["sku"],
                        "titulo": row["catalog_nome"] or row["titulo"],
                        "preco": float(row["preco"] or 0),
                        "status": ERRO, "mensagem": msg}

            payload = montar_payload(p, float(row["preco"] or 0), listing_type_id,
                                     row["catalog_product_id"], categoria)
            try:
                criado = await client.post("/items", payload)
            except MLApiError as exc:
                storage.atualizar_item(item_id, status=ERRO,
                                       erro=exc.mensagem_amigavel())
                return {"linha": row["linha"], "codigo": row["sku"],
                        "titulo": row["catalog_nome"] or row["titulo"],
                        "preco": float(row["preco"] or 0),
                        "status": ERRO, "mensagem": exc.mensagem_amigavel()}

            ml_id = criado.get("id")
            permalink = criado.get("permalink")
            storage.atualizar_item(item_id, status=PUBLICADO, ml_item_id=ml_id,
                                   permalink=permalink)

            try:
                await client.post(f"/items/{ml_id}/description",
                                  {"plain_text": montar_descricao(p)})
            except MLApiError as exc:
                log.warning("Anúncio %s criado, mas a descrição falhou: %s",
                            ml_id, exc.mensagem_amigavel())

            return {"linha": row["linha"], "codigo": row["sku"],
                    "titulo": row["catalog_nome"] or row["titulo"],
                    "preco": float(row["preco"] or 0),
                    "status": PUBLICADO, "ml_item_id": ml_id,
                    "permalink": permalink, "mensagem": ""}

    return list(await asyncio.gather(*(publicar(r) for r in aprovados)))


def resumir(resultados: list) -> dict:
    resumo: dict[str, int] = {}
    for r in resultados:
        status = r.status if hasattr(r, "status") else r.get("status")
        resumo[status] = resumo.get(status, 0) + 1
    return resumo


# ---------------------------------------------------------------------------
# Publicação com fotos da própria loja
# ---------------------------------------------------------------------------

DA_LOJA = "pronto_com_fotos"      # tem foto própria: pode virar anúncio direto


async def cruzar_lote_com_loja(lote_id: int) -> dict:
    """Cruza todos os itens pendentes do lote com o catálogo da loja.

    Baixa o catálogo uma vez e casa em memória: para 1.334 itens, sai de
    milhares de requisições ao site da Águia para algumas dezenas.
    """
    from app import loja

    produtos = await loja.listar_catalogo()
    indice = loja.indexar(produtos)

    pendentes = storage.itens_por_status(lote_id, "na_fila")
    casados = com_foto = 0
    valor_com_foto = 0.0
    sem_foto: list[str] = []

    for row in pendentes:
        p = loja.casar(row["sku"] or "", indice)
        if p is None:
            continue
        casados += 1

        if not p.fotos:
            sem_foto.append(row["sku"])
            continue

        storage.atualizar_dados_catalogo(
            row["id"],
            status=DA_LOJA,
            loja_url=p.url, loja_nome=p.nome, loja_sku=p.sku,
            loja_preco=p.preco,
            loja_fotos=json.dumps(p.fotos, ensure_ascii=False),
            loja_descricao=p.descricao,
            decidido_em=datetime.now(timezone.utc).isoformat(),
            erro=None if p.em_estoque else
                 "atenção: a loja marca este produto como fora de estoque",
        )
        com_foto += 1
        valor_com_foto += float(row["valor"] or 0)

    return {
        "produtos_na_loja": len(produtos),
        "pendentes": len(pendentes),
        "casados": casados,
        "com_foto": com_foto,
        "sem_foto": sem_foto,
        "valor_com_foto": valor_com_foto,
    }


async def vincular_qualquer(item_id: int, referencia: str) -> dict:
    """Aceita QUALQUER referência e decide sozinha o que fazer com ela.

    Existia um campo para link da loja e outro para link do Mercado Livre, e o
    operador colava no errado — erro previsível, culpa do desenho. Agora é um
    campo só:

      - link da loja da Águia  -> puxa foto, descrição e preço de lá
      - link do Mercado Livre  -> vincula ao produto de catálogo
      - código solto           -> procura primeiro na loja, depois no ML
    """
    from app import loja
    from urllib.parse import urlparse

    referencia = (referencia or "").strip()
    if not referencia:
        return {"ok": False, "mensagem": "Cole um link ou um código."}

    host_loja = urlparse(get_settings().loja_base_url).netloc.lower()
    host_dado = urlparse(referencia).netloc.lower() if "//" in referencia else ""

    # 1. é da loja da Águia?
    if host_dado and host_loja and host_loja in host_dado:
        return await vincular_produto_da_loja(item_id, referencia)

    # 2. é do Mercado Livre?
    if "mercadoliv" in referencia.lower() or re.search(r"ML[ABCMU]U?\d{6,}",
                                                       referencia, re.I):
        return await vincular_por_link(item_id, referencia)

    # 3. código solto: loja primeiro (tem foto), ML depois
    try:
        achados = await loja.buscar_por_codigo(referencia)
        if achados:
            return await vincular_produto_da_loja(item_id, achados[0].url)
    except loja.LojaError as exc:
        log.warning("Busca na loja falhou para %r: %s", referencia, exc)

    return {"ok": False, "mensagem": (
        f"Não achei '{referencia}' na loja da Águia, e isso não parece um link "
        "do Mercado Livre. Cole o endereço completo da página do produto — "
        "da loja da Águia ou do Mercado Livre.")}


async def vincular_produto_da_loja(item_id: int, referencia: str) -> dict:
    """Puxa nome, descrição e FOTOS do site da Águia para o item da fila.

    É o caminho que dispensa o catálogo do Mercado Livre: com foto própria,
    o anúncio pode ser criado do zero. Nada de imagem de terceiro.
    """
    from app import loja

    referencia = (referencia or "").strip()
    if referencia.startswith("http"):
        produto = await loja.buscar_por_url(referencia)
    else:
        achados = await loja.buscar_por_codigo(referencia)
        produto = achados[0] if achados else None

    if produto is None:
        return {"ok": False, "mensagem": "Não achei esse produto na loja da Águia."}

    if not produto.publicavel:
        return {"ok": False, "mensagem": (
            f"'{produto.nome[:60]}' está na loja mas sem foto cadastrada. "
            "O Mercado Livre exige pelo menos uma imagem.")}

    storage.atualizar_dados_catalogo(
        item_id,
        status=DA_LOJA,
        loja_url=produto.url,
        loja_nome=produto.nome,
        loja_sku=produto.sku,
        loja_preco=produto.preco,
        loja_fotos=json.dumps(produto.fotos, ensure_ascii=False),
        loja_descricao=produto.descricao,
        decidido_em=datetime.now(timezone.utc).isoformat(),
        erro=None if produto.em_estoque else
             "atenção: a loja marca este produto como fora de estoque",
    )
    return {"ok": True,
            "mensagem": f"{produto.nome[:70]} — {len(produto.fotos)} foto(s).",
            "produto": produto}


async def publicar_da_loja(item_id: int,
                           listing_type_id: str = "gold_special") -> dict:
    """Cria um anúncio PRÓPRIO no ML usando as fotos da loja da Águia."""
    linha = storage.item(item_id)
    if linha is None:
        return {"ok": False, "mensagem": "Item não encontrado."}

    fotos = json.loads(linha["loja_fotos"] or "[]")
    if not fotos:
        return {"ok": False, "mensagem": "Este item não tem fotos da loja."}

    client = MLClient()
    titulo = (linha["loja_nome"] or linha["titulo"] or "")[:60].strip()

    categoria = linha["catalog_category_id"]
    if not categoria:
        categoria = await prever_categoria(client, titulo)
    if not categoria:
        return {"ok": False, "mensagem": (
            "Não consegui determinar a categoria do Mercado Livre para "
            f"'{titulo}'. Sem categoria o ML recusa o anúncio.")}

    p = LinhaProduto(
        aba="", linha=linha["linha"], codigo=linha["sku"] or "",
        descricao=linha["descricao_erp"] or "",
        quantidade=int(linha["quantidade"] or 1),
        marca=linha["marca"] or "",
    )

    payload = {
        "site_id": get_settings().ml_site_id,
        "title": titulo,
        "category_id": categoria,
        "price": float(linha["preco"] or 0),
        "currency_id": "BRL",
        "available_quantity": max(1, int(linha["quantidade"] or 1)),
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": listing_type_id,
        # As imagens são da própria Águia; o ML baixa a partir da URL.
        "pictures": [{"source": url} for url in fotos[:10]],
        "attributes": _atributos(p),
    }
    aplicar_user_product(payload, titulo)

    try:
        criado = await client.post("/items", payload)
    except MLApiError as exc:
        storage.atualizar_item(item_id, status=ERRO, erro=exc.mensagem_amigavel())
        return {"ok": False, "mensagem": exc.mensagem_amigavel()}

    ml_id = criado.get("id")
    storage.atualizar_item(item_id, status=PUBLICADO, ml_item_id=ml_id,
                           permalink=criado.get("permalink"))

    descricao = linha["loja_descricao"] or montar_descricao(p)
    try:
        await client.post(f"/items/{ml_id}/description", {"plain_text": descricao})
    except MLApiError as exc:
        log.warning("Anúncio %s criado, descrição falhou: %s", ml_id,
                    exc.mensagem_amigavel())

    return {"ok": True, "ml_item_id": ml_id, "permalink": criado.get("permalink"),
            "mensagem": f"Anúncio {ml_id} publicado."}


COM_FOTO_PROPRIA = "foto_do_operador"


async def publicar_com_fotos_proprias(item_id: int,
                                      listing_type_id: str = "gold_special") -> dict:
    """Publica anúncio próprio com as fotos que o operador tirou.

    Caminho que não depende de nada de terceiros: nem catálogo do ML, nem
    anúncio de outro vendedor, nem a loja. Foto da prateleira, título montado a
    partir do ERP, categoria pelo preditor do Mercado Livre.
    """
    from app import fotos as mod_fotos

    linha = storage.item(item_id)
    if linha is None:
        return {"ok": False, "mensagem": "Item não encontrado."}

    arquivos = mod_fotos.listar(item_id)
    if not arquivos:
        return {"ok": False, "mensagem": (
            "Este item ainda não tem foto. Envie ao menos uma antes de publicar.")}

    client = MLClient()
    p = LinhaProduto(
        aba="", linha=linha["linha"], codigo=linha["sku"] or "",
        descricao=linha["descricao_erp"] or "",
        quantidade=int(linha["quantidade"] or 1),
        marca=linha["marca"] or "",
        aplicacao=linha["aplicacao"] or "",
    )
    titulo = montar_titulo(p)

    categoria = linha["catalog_category_id"] or await prever_categoria(client, titulo)
    if not categoria:
        return {"ok": False, "mensagem": (
            f"Não consegui determinar a categoria do Mercado Livre para "
            f"'{titulo}'. Sem categoria o ML recusa o anúncio.")}

    # Sobe as imagens primeiro: se alguma falhar, nada é publicado pela metade.
    ids: list[str] = []
    for nome in arquivos[:mod_fotos.MAX_FOTOS]:
        try:
            pid = await client.enviar_foto(mod_fotos.ler(item_id, nome), nome)
        except MLApiError as exc:
            return {"ok": False, "mensagem": (
                f"O Mercado Livre recusou a foto '{nome}': "
                f"{exc.mensagem_amigavel()}")}
        if pid:
            ids.append(pid)

    if not ids:
        return {"ok": False, "mensagem": "Nenhuma foto foi aceita pelo Mercado Livre."}

    payload = {
        "site_id": get_settings().ml_site_id,
        "title": titulo,
        "category_id": categoria,
        "price": float(linha["preco"] or 0),
        "currency_id": "BRL",
        "available_quantity": max(1, int(linha["quantidade"] or 1)),
        "buying_mode": "buy_it_now",
        "condition": "new",
        "listing_type_id": listing_type_id,
        "pictures": [{"id": i} for i in ids],
        "attributes": _atributos(p),
    }
    aplicar_user_product(payload, titulo)

    try:
        criado = await client.post("/items", payload)
    except MLApiError as exc:
        storage.atualizar_item(item_id, status=ERRO, erro=exc.mensagem_amigavel())
        return {"ok": False, "mensagem": exc.mensagem_amigavel()}

    ml_id = criado.get("id")
    storage.atualizar_item(item_id, status=PUBLICADO, ml_item_id=ml_id,
                           permalink=criado.get("permalink"))
    try:
        await client.post(f"/items/{ml_id}/description",
                          {"plain_text": montar_descricao(p)})
    except MLApiError as exc:
        log.warning("Anúncio %s criado, descrição falhou: %s", ml_id,
                    exc.mensagem_amigavel())

    return {"ok": True, "ml_item_id": ml_id, "permalink": criado.get("permalink"),
            "mensagem": f"Anúncio {ml_id} publicado com {len(ids)} foto(s)."}


async def publicar_selecionados(ids: list[int],
                                listing_type_id: str = "gold_special") -> dict:
    """Publica de uma vez as peças que o operador escolheu na tela de prontas.

    Cada peça sabe por qual caminho sair, pelo próprio status: foto da loja,
    foto tirada no estoque, ou catálogo do ML. A conta é conferida uma vez só,
    antes de tudo — se ela estiver bloqueada, as 40 peças falhariam igual e o
    operador só veria 40 mensagens iguais.
    """
    if not ids:
        return {"ok": False, "mensagem": "Nenhuma peça selecionada.",
                "resultados": []}

    client = MLClient()
    diagnostico = await client.diagnostico_conta()
    if not diagnostico["apta"]:
        motivo = " | ".join(diagnostico["impedimentos"])
        return {"ok": False, "mensagem": f"A conta não está apta: {motivo}",
                "resultados": []}

    semaforo = asyncio.Semaphore(get_settings().publish_concurrency)

    async def uma(item_id: int) -> dict:
        async with semaforo:
            linha = storage.item(item_id)
            if linha is None:
                return {"item_id": item_id, "ok": False,
                        "titulo": f"#{item_id}",
                        "mensagem": "Item não encontrado."}

            rotulo = (linha["loja_nome"] or linha["catalog_nome"]
                      or linha["titulo"] or linha["descricao_erp"] or "")[:70]
            status = linha["status"]

            if status == DA_LOJA:
                r = await publicar_da_loja(item_id, listing_type_id)
            elif status == COM_FOTO_PROPRIA:
                r = await publicar_com_fotos_proprias(item_id, listing_type_id)
            elif status == AGUARDANDO:
                r = await _publicar_por_catalogo(client, linha, listing_type_id)
            else:
                r = {"ok": False, "mensagem": (
                    f"Esta peça está como '{status}' — não está pronta para "
                    "publicar.")}

            return {"item_id": item_id, "titulo": rotulo,
                    "codigo": linha["sku"] or "", **r}

    resultados = await asyncio.gather(*(uma(i) for i in ids))
    ok = [r for r in resultados if r.get("ok")]

    return {
        "ok": bool(ok),
        "publicados": len(ok),
        "falharam": len(resultados) - len(ok),
        "mensagem": (f"{len(ok)} anúncio(s) publicado(s)."
                     if ok else "Nenhum anúncio foi publicado."),
        "resultados": resultados,
    }


async def _publicar_por_catalogo(client: MLClient, linha,
                                 listing_type_id: str) -> dict:
    """Publica pelo catálogo do ML — o anúncio herda foto e ficha do produto."""
    if not linha["catalog_product_id"]:
        return {"ok": False, "mensagem": "Sem produto de catálogo vinculado."}
    if not linha["catalog_category_id"]:
        return {"ok": False, "mensagem": (
            "Sem categoria — o Mercado Livre recusa o anúncio sem ela.")}

    p = LinhaProduto(
        aba="", linha=linha["linha"], codigo=linha["sku"] or "",
        descricao=linha["descricao_erp"] or "",
        quantidade=int(linha["quantidade"] or 1),
        marca=linha["marca"] or "",
    )
    payload = montar_payload(
        p, float(linha["preco"] or 0),
        catalog_product_id=linha["catalog_product_id"],
        category_id=linha["catalog_category_id"],
        listing_type_id=listing_type_id,
    )
    try:
        criado = await client.post("/items", payload)
    except MLApiError as exc:
        storage.atualizar_item(linha["id"], status=ERRO,
                               erro=exc.mensagem_amigavel())
        return {"ok": False, "mensagem": exc.mensagem_amigavel()}

    ml_id = criado.get("id")
    storage.atualizar_item(linha["id"], status=PUBLICADO, ml_item_id=ml_id,
                           permalink=criado.get("permalink"))
    return {"ok": True, "ml_item_id": ml_id,
            "permalink": criado.get("permalink"),
            "mensagem": f"Anúncio {ml_id} publicado."}
