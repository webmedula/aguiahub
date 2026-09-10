"""A lista: todas as peças da planilha, com a situação de cada uma.

Substitui a fila que mostrava **uma peça por vez** e escolhia a próxima pelo
critério dela (maior valor parado). Isso tirava do operador exatamente o
controle que ele precisa ter: ver a lista inteira, filtrar, e decidir o que
fazer com cada peça — ou com cinquenta de uma vez.

A ideia central deste módulo é uma só: **classificar cada peça por situação**,
e fazer isso num lugar só. A situação é o que decide o que aparece na linha,
o que dá para fazer em bloco, e o que exige abrir a peça.

    no ar            já publicada no Mercado Livre
    pronta           tem foto e dados — pode publicar em bloco, sem abrir
    precisa de você  o sistema achou alguma coisa, mas não deu para concluir
    sem foto         nenhuma origem conhecida tem essa peça
    descartada       alguém decidiu não anunciar (não achei, em dúvida, erro)
    fora da fila     a triagem barrou antes (sem código utilizável)

A classificação é feita em SQL, não em Python, de propósito: são 1.687 linhas
e a tela precisa filtrar, contar e paginar sem carregar tudo na memória. O
`CASE` abaixo é a única definição de situação no sistema inteiro — se um dia
a regra mudar, muda aqui e a tela toda acompanha.
"""
from __future__ import annotations

import json
import sqlite3

from app import storage

NO_AR = "no_ar"
PRONTA = "pronta"
PRECISA = "precisa_de_voce"
SEM_FOTO = "sem_foto"
DESCARTADA = "descartada"
FORA = "fora"

#: Ordem em que as situações aparecem na tela — do que rende agora para o que
#: pode esperar. Quem abre a lista quer ver primeiro o que dá para publicar.
SITUACOES = {
    PRONTA: {
        "rotulo": "prontas para publicar",
        "curto": "pronta",
        "cor": "ok",
        "ajuda": "Tem foto e dados. Dá para marcar várias e publicar de uma vez.",
    },
    PRECISA: {
        "rotulo": "precisam de você",
        "curto": "precisa de você",
        "cor": "alerta",
        "ajuda": ("O sistema achou páginas da peça mas não teve certeza. "
                  "Abra para conferir os links que ele guardou."),
    },
    SEM_FOTO: {
        "rotulo": "sem foto",
        "curto": "sem foto",
        "cor": "neutro",
        "ajuda": ("Nenhuma origem conhecida tem essa peça. Precisa de foto "
                  "tirada no estoque, ou de um link colado à mão."),
    },
    NO_AR: {
        "rotulo": "no ar",
        "curto": "no ar",
        "cor": "ok",
        "ajuda": "Anúncio publicado. O código, o link e a data ficam guardados.",
    },
    DESCARTADA: {
        "rotulo": "descartadas",
        "curto": "descartada",
        "cor": "neutro",
        "ajuda": "Alguém decidiu não anunciar por enquanto. Dá para reabrir.",
    },
    FORA: {
        "rotulo": "fora da fila",
        "curto": "fora da fila",
        "cor": "neutro",
        "ajuda": ("A triagem barrou antes de começar: sem código utilizável, "
                  "sem preço, ou não marcada na planilha."),
    },
}

#: A ÚNICA definição de situação do sistema. Fica em SQL porque a tela precisa
#: filtrar e contar 1.687 linhas sem trazer tudo para a memória.
CASE_SITUACAO = """
CASE
  WHEN status = 'publicado' THEN 'no_ar'
  WHEN status IN ('pronto_com_fotos','foto_do_operador','aguardando_aprovacao')
       THEN 'pronta'
  WHEN status IN ('sem_dado','nao_selecionado') THEN 'fora'
  WHEN status = 'na_fila' AND esteira_rotulo IN ('candidatos','enriquecida')
       THEN 'precisa_de_voce'
  WHEN status = 'na_fila' THEN 'sem_foto'
  ELSE 'descartada'
END
"""

#: Faixas de valor da peça. A decisão de anunciar é diferente em cada uma: a
#: peça de R$ 35 pode não pagar a comissão do ML e o frete.
FAIXAS = {
    "alto":  ("acima de R$ 1.000", "preco >= 1000"),
    "medio": ("de R$ 100 a R$ 1.000", "preco >= 100 AND preco < 1000"),
    "baixo": ("abaixo de R$ 100", "preco < 100"),
}

POR_PAGINA = 40


def contagem(lote_id: int) -> dict:
    """Quantas peças e quanto valor parado em cada situação."""
    with storage.conexao() as conn:
        linhas = conn.execute(
            f"""SELECT {CASE_SITUACAO} AS situacao, COUNT(*) n,
                       COALESCE(SUM(valor), 0) v
                FROM itens WHERE lote_id = ? GROUP BY situacao""",
            (lote_id,)).fetchall()

    fora = {r["situacao"]: {"itens": r["n"], "valor": r["v"]} for r in linhas}
    # devolve na ordem de SITUACOES, com zero para as que não têm nenhuma peça
    return {chave: fora.get(chave, {"itens": 0, "valor": 0.0})
            for chave in SITUACOES}


def listar(lote_id: int, situacao: str = "", busca: str = "", faixa: str = "",
           pagina: int = 1) -> dict:
    """Uma página da lista, já filtrada. Devolve linhas, total e paginação."""
    where = ["lote_id = ?"]
    params: list = [lote_id]

    if situacao in SITUACOES:
        where.append(f"{CASE_SITUACAO} = ?")
        params.append(situacao)
    if faixa in FAIXAS:
        where.append(f"({FAIXAS[faixa][1]})")
    if busca.strip():
        where.append("(UPPER(sku) LIKE ? OR UPPER(descricao_erp) LIKE ? "
                     "OR UPPER(loja_nome) LIKE ? OR UPPER(aplicacao) LIKE ?)")
        alvo = f"%{busca.strip().upper()}%"
        params += [alvo, alvo, alvo, alvo]

    condicao = " AND ".join(where)
    pagina = max(1, int(pagina or 1))

    with storage.conexao() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) n FROM itens WHERE {condicao}", params
        ).fetchone()["n"]

        linhas = conn.execute(
            f"""SELECT *, {CASE_SITUACAO} AS situacao FROM itens
                WHERE {condicao}
                ORDER BY valor DESC, linha ASC
                LIMIT ? OFFSET ?""",
            (*params, POR_PAGINA, (pagina - 1) * POR_PAGINA)).fetchall()

    paginas = max(1, -(-total // POR_PAGINA))     # divisão para cima
    return {
        "linhas": [enfeitar(l) for l in linhas],
        "total": total,
        "pagina": min(pagina, paginas),
        "paginas": paginas,
    }


def peca(item_id: int) -> dict | None:
    """Uma peça, já com a situação calculada pelo mesmo CASE da lista.

    Existe porque `storage.item()` devolve as colunas cruas — sem a situação,
    que é derivada. Buscar aqui, com o mesmo `CASE`, é o que garante que a
    tela da peça e a linha da lista nunca discordem sobre o estado dela.
    """
    with storage.conexao() as conn:
        linha = conn.execute(
            f"SELECT *, {CASE_SITUACAO} AS situacao FROM itens WHERE id = ?",
            (item_id,)).fetchone()
    return enfeitar(linha) if linha is not None else None


def enfeitar(linha: sqlite3.Row) -> dict:
    """Acrescenta à linha o que a tela precisa e o banco não guarda.

    A foto do operador mora em disco, não no banco — então a origem da imagem
    só dá para saber olhando os dois. Fazer isso aqui evita a tela ter lógica.
    """
    from app import fotos as mod_fotos

    dados = dict(linha)
    fotos_site = json.loads(linha["loja_fotos"] or "[]")
    proprias = mod_fotos.listar(linha["id"])

    if proprias:
        dados["capa"] = f"/fotos/{linha['id']}/{proprias[0]}"
        # Foto baixada de endereço colado não é foto do estoque: dizer que é
        # esconderia justamente a informação que interessa se alguém
        # reclamar da imagem.
        origens = storage.origens_de_foto(linha["id"])
        baixada = origens.get(proprias[0])
        dados["origem_foto"] = (f"foto de {baixada['dominio']}" if baixada
                                else "foto tirada no estoque")
    elif fotos_site:
        dados["capa"] = fotos_site[0]
        dados["origem_foto"] = (f"foto de {linha['fonte_dados']}"
                                if linha["fonte_dados"] else "foto da loja da Águia")
    elif linha["catalog_foto"]:
        dados["capa"] = linha["catalog_foto"]
        dados["origem_foto"] = "foto do catálogo do Mercado Livre"
    else:
        dados["capa"] = ""
        dados["origem_foto"] = ""

    dados["quantas_fotos"] = len(proprias) or len(fotos_site)
    dados["titulo_anuncio"] = (linha["titulo_editado"] or linha["loja_nome"]
                               or linha["catalog_nome"] or linha["titulo"]
                               or linha["descricao_erp"] or "")
    dados["candidatos_lista"] = json.loads(linha["candidatos"] or "[]")
    return dados


def resumo_do_anuncio(item_id: int) -> dict | None:
    """O cartão: a peça exatamente como ela vai virar anúncio.

    É o que a pessoa confere antes de publicar. Monta o mesmo título,
    descrição e fotos que a publicação usaria — se este resumo mostrar uma
    coisa e o anúncio sair outra, o resumo não serve para nada.
    """
    from app import fotos as mod_fotos
    from app.publisher import _descricao_efetiva, _titulo_efetivo
    from app.sheets import LinhaProduto, montar_titulo

    linha = storage.item(item_id)
    if linha is None:
        return None

    p = LinhaProduto(
        aba="", linha=linha["linha"], codigo=linha["sku"] or "",
        descricao=linha["descricao_erp"] or "",
        quantidade=int(linha["quantidade"] or 1),
        marca=linha["marca"] or "",
        aplicacao=linha["aplicacao"] or "",
    )

    fotos_site = json.loads(linha["loja_fotos"] or "[]")
    proprias = mod_fotos.listar(item_id)
    if proprias:
        imagens = [f"/fotos/{item_id}/{n}" for n in proprias]
        origens = storage.origens_de_foto(item_id)
        dominios = sorted({o["dominio"] for n, o in origens.items()
                           if n in proprias})
        origem = (f"fotos de {', '.join(dominios)}" if dominios
                  else "fotos tiradas no estoque")
    else:
        imagens = fotos_site[:10]
        origem = (f"fotos de {linha['fonte_dados']}" if linha["fonte_dados"]
                  else "fotos da loja da Águia")

    por_catalogo = linha["status"] == "aguardando_aprovacao"
    if por_catalogo:
        imagens = [linha["catalog_foto"]] if linha["catalog_foto"] else []
        origem = "foto e ficha do catálogo do Mercado Livre"

    return {
        "item": linha,
        "titulo": _titulo_efetivo(linha, linha["loja_nome"] or montar_titulo(p)),
        "descricao": _descricao_efetiva(linha, p),
        "imagens": imagens,
        "origem_fotos": origem,
        "por_catalogo": por_catalogo,
        "preco": float(linha["preco"] or 0),
        "quantidade": int(linha["quantidade"] or 1),
        "marca": linha["marca"] or "",
        "codigo": linha["sku"] or "",
        "aplicacao": linha["aplicacao"] or "",
        "publicavel": bool(imagens),
    }
