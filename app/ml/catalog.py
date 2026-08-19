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
    tentativas = [
        (codigo, "part number exato"),
        (f"{codigo} {descricao}".strip(), "código + descrição"),
    ]

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
