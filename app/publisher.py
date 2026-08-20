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
import logging
from dataclasses import dataclass, field

from app import storage
from app.config import get_settings
from app.abreviacoes import expandir
from app.ml.catalog import (CandidatoCatalogo, anuncios_ativos,
                            buscar_no_catalogo, enriquecer_candidato,
                            escolher_publicavel, produto_do_anuncio,
                            publicavel)
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
        payload["title"] = montar_titulo(p)
        payload["pictures"] = []     # preenchido quando houver foto própria
        payload["attributes"] = _atributos(p)

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
                so_categoria = (recusas and
                                all("ategoria" in m for m in recusas))
                res.status = SEM_CATEGORIA if so_categoria else CATALOGO_INATIVO
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
                                            SEM_CATEGORIA) else None,
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

    storage.atualizar_dados_catalogo(
        item_id,
        status=AGUARDANDO,
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
