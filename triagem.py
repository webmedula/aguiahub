"""Triagem da planilha: o que dá para trabalhar hoje, e em que ordem.

Roda inteiramente offline — nenhuma chamada à API do Mercado Livre. Isso
importa porque a planilha tem 1.687 itens e consultar o ML para todos custaria
milhares de requisições para descobrir, no fim, que a maioria nem tem código
utilizável.

A pergunta que este módulo responde é: *por onde começar*. A consulta ao
catálogo acontece depois, um item por vez, quando o operador chega nele.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.sheets import LinhaProduto

# --- qualidade do código ---------------------------------------------------

FORTE = "part number forte"
PLAUSIVEL = "numérico plausível"
CODIFICADO = "codificado"
LONGO = "numérico longo"
CURTO = "curto demais"
DUVIDOSO = "duvidoso"
AUSENTE = "sem código"

# Qualidades com as quais o operador consegue procurar no ML e achar a peça.
UTILIZAVEIS = {FORTE, PLAUSIVEL}


def _parece_codificado(codigo: str) -> bool:
    """Detecta o padrão de código embaralhado do ERP.

    Alguns códigos vêm como pares de dígitos que representam caracteres
    ('57505152575252'). Os dígitos são recuperáveis, mas a ordem dos blocos
    não foi confirmada — então tratamos como ilegível em vez de arriscar uma
    busca errada. Ver CHANGELOG v0.6.1 e a conversa sobre a válvula 2722701.
    """
    if not codigo.isdigit() or len(codigo) % 2 or len(codigo) < 12:
        return False
    return all(
        50 <= int(codigo[i:i + 2]) <= 59 or 10 <= int(codigo[i:i + 2]) <= 35
        for i in range(0, len(codigo), 2)
    )


def qualidade_do_codigo(codigo: str) -> str:
    c = (codigo or "").strip().upper()
    if not c:
        return AUSENTE
    if _parece_codificado(c):
        return CODIFICADO
    if len(c) < 5:
        # '19P', '1504', '373' — genéricos demais para buscar no ML
        return CURTO
    if c.isdigit() and len(c) > 12:
        return LONGO
    tem_letra = bool(re.search(r"[A-Z]", c))
    tem_numero = bool(re.search(r"\d", c))
    if tem_letra and tem_numero:
        return FORTE
    if c.isdigit() and 6 <= len(c) <= 12:
        return PLAUSIVEL
    return DUVIDOSO


# --- classificação da linha ------------------------------------------------

@dataclass
class Triado:
    item: LinhaProduto
    qualidade: str
    pronto: bool
    valor: float            # preço × quantidade — usado para priorizar
    impedimentos: list[str]

    @property
    def termos_de_busca(self) -> list[str]:
        """O que a pessoa do estoque vai colar na busca do Mercado Livre."""
        termos = []
        if self.qualidade in UTILIZAVEIS:
            termos.append(self.item.codigo.strip())
        from app.abreviacoes import expandir
        desc = expandir(self.item.descricao).strip()
        if desc:
            if self.qualidade in UTILIZAVEIS:
                termos.append(f"{self.item.codigo.strip()} {desc}")
            termos.append(desc)
        return termos


def triar(item: LinhaProduto) -> Triado:
    qualidade = qualidade_do_codigo(item.codigo)
    impedimentos: list[str] = []

    if qualidade not in UTILIZAVEIS:
        rotulos = {
            CODIFICADO: "código do ERP está embaralhado — não dá para buscar",
            LONGO: "código numérico longo demais para ser part number",
            CURTO: "código curto/genérico demais para achar a peça",
            DUVIDOSO: "código em formato não reconhecido",
            AUSENTE: "item sem código",
        }
        impedimentos.append(rotulos.get(qualidade, "código não utilizável"))

    if item.quantidade <= 0:
        impedimentos.append("sem estoque")
    if item.preco_publico <= 0 and item.custo_medio <= 0:
        impedimentos.append("sem preço nem custo")
    if not item.descricao.strip():
        impedimentos.append("sem descrição")

    preco = item.preco_publico or item.custo_medio
    return Triado(
        item=item,
        qualidade=qualidade,
        pronto=not impedimentos,
        valor=preco * max(item.quantidade, 0),
        impedimentos=impedimentos,
    )


def triar_planilha(itens: list[LinhaProduto]) -> list[Triado]:
    """Tria e ordena: prontos primeiro, e dentro deles o de maior valor.

    A ordem é a decisão de negócio mais importante do sistema. Com 21 horas de
    trabalho humano para percorrer tudo e 72% do valor concentrado em 197 itens,
    trabalhar na ordem errada custa caro.
    """
    triados = [triar(i) for i in itens]
    return sorted(triados, key=lambda t: (not t.pronto, -t.valor))


def resumo(triados: list[Triado]) -> dict:
    prontos = [t for t in triados if t.pronto]
    por_qualidade: dict[str, dict] = {}
    for t in triados:
        d = por_qualidade.setdefault(t.qualidade, {"itens": 0, "valor": 0.0})
        d["itens"] += 1
        d["valor"] += t.valor

    return {
        "total": len(triados),
        "prontos": len(prontos),
        "valor_pronto": sum(t.valor for t in prontos),
        "valor_total": sum(t.valor for t in triados),
        "por_qualidade": por_qualidade,
    }
