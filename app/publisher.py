"""Montagem do payload e publicação dos anúncios no Mercado Livre.

Dois caminhos:

1. CATÁLOGO (preferido) — quando o part number bate com um produto do catálogo
   do ML. Envia `catalog_product_id` + `catalog_listing: true` e `pictures: []`.
   As fotos, o título e a ficha técnica são herdados do catálogo pelo próprio
   Mercado Livre. É o caminho oficial para publicar sem ter foto própria.

2. ANÚNCIO PRÓPRIO — quando não há match no catálogo. Aqui o ML EXIGE pelo
   menos uma imagem, então o item fica bloqueado até alguém fornecer a foto.
   Não existe atalho legítimo: reaproveitar imagem de anúncio de terceiro é
   violação de direito autoral e derruba a conta.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app import storage
from app.config import get_settings
from app.ml.catalog import CandidatoCatalogo, buscar_no_catalogo
from app.ml.client import MLClient, MLApiError
from app.sheets import LinhaProduto, calcular_preco, montar_descricao, montar_titulo

log = logging.getLogger("aguiahub.publisher")

# Preço mínimo aceito pelo ML no Brasil. Abaixo disso o anúncio é recusado.
PRECO_MINIMO = 1.0


@dataclass
class ResultadoItem:
    linha: int
    codigo: str
    titulo: str
    status: str            # 'catalogo' | 'sem_foto' | 'erro' | 'publicado' | 'simulado' | 'ignorado'
    preco: float = 0.0
    catalog_product_id: str | None = None
    candidatos: list[CandidatoCatalogo] | None = None
    ml_item_id: str | None = None
    permalink: str | None = None
    mensagem: str = ""


def montar_payload(
    p: LinhaProduto,
    preco: float,
    listing_type_id: str = "gold_special",
    catalog_product_id: str | None = None,
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

    if catalog_product_id:
        # Título, fotos e atributos vêm do catálogo — não enviar os nossos
        # evita conflito de validação com o produto já cadastrado.
        payload["catalog_product_id"] = catalog_product_id
        payload["catalog_listing"] = True
        payload["pictures"] = []
    else:
        payload["title"] = montar_titulo(p)
        payload["pictures"] = []     # preenchido quando houver foto
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


async def processar_lote(
    itens: list[LinhaProduto],
    *,
    regra_preco: str = "preco_publico",
    percentual: float = 0.0,
    dry_run: bool = True,
    listing_type_id: str = "gold_special",
    lote_id: int | None = None,
    pular_ja_publicados: bool = True,
) -> list[ResultadoItem]:
    """Casa cada item com o catálogo e publica (ou simula)."""
    s = get_settings()
    client = MLClient()
    semaforo = asyncio.Semaphore(s.publish_concurrency)
    resultados: list[ResultadoItem] = []

    async def tratar(p: LinhaProduto) -> ResultadoItem:
        async with semaforo:
            titulo = montar_titulo(p)
            preco = calcular_preco(p, regra_preco, percentual)
            res = ResultadoItem(linha=p.linha, codigo=p.codigo, titulo=titulo,
                                status="erro", preco=preco)

            if not p.publicavel:
                res.status = "ignorado"
                res.mensagem = "; ".join(p.problemas)
                return res

            if preco < PRECO_MINIMO:
                res.status = "ignorado"
                res.mensagem = f"preço calculado (R$ {preco:.2f}) abaixo do mínimo do ML"
                return res

            if pular_ja_publicados:
                ja = storage.sku_ja_publicado(p.codigo)
                if ja:
                    res.status = "ignorado"
                    res.ml_item_id = ja
                    res.mensagem = f"SKU já publicado antes ({ja})"
                    return res

            # 1) procura no catálogo
            try:
                candidatos = await buscar_no_catalogo(client, p.codigo, p.descricao)
            except MLApiError as exc:
                res.mensagem = f"falha ao consultar catálogo: {exc.mensagem_amigavel()}"
                return res

            res.candidatos = candidatos
            bons = [c for c in candidatos if c.confianca == "alta"]

            if not bons:
                res.status = "sem_foto"
                res.mensagem = (
                    "sem correspondência confiável no catálogo — precisa de foto própria"
                    if not candidatos
                    else f"{len(candidatos)} candidato(s) de baixa confiança; revisar na tela"
                )
                return res

            escolhido = bons[0]
            res.catalog_product_id = escolhido.catalog_product_id
            res.status = "catalogo"

            payload = montar_payload(p, preco, listing_type_id,
                                     escolhido.catalog_product_id)

            if dry_run:
                res.status = "simulado"
                res.mensagem = f"casaria com {escolhido.catalog_product_id} — {escolhido.nome[:60]}"
                return res

            # 2) publica de verdade
            try:
                criado = await client.post("/items", payload)
            except MLApiError as exc:
                res.status = "erro"
                res.mensagem = exc.mensagem_amigavel()
                return res

            res.status = "publicado"
            res.ml_item_id = criado.get("id")
            res.permalink = criado.get("permalink")

            # descrição é um recurso separado no ML
            try:
                await client.post(f"/items/{res.ml_item_id}/description",
                                  {"plain_text": montar_descricao(p)})
            except MLApiError as exc:
                log.warning("Anúncio %s criado, mas a descrição falhou: %s",
                            res.ml_item_id, exc.mensagem_amigavel())

            return res

    resultados = await asyncio.gather(*(tratar(p) for p in itens))

    if lote_id is not None:
        for r in resultados:
            item_id = storage.registrar_item(
                lote_id, r.linha, r.codigo, r.titulo,
                {"preco": r.preco, "catalog_product_id": r.catalog_product_id},
            )
            storage.atualizar_item(item_id, status=r.status, ml_item_id=r.ml_item_id,
                                   permalink=r.permalink, erro=r.mensagem or None)

    return list(resultados)


def resumir(resultados: list[ResultadoItem]) -> dict:
    resumo: dict[str, int] = {}
    for r in resultados:
        resumo[r.status] = resumo.get(r.status, 0) + 1
    return resumo
