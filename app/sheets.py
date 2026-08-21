"""Leitura da planilha de estoque parado do ERP (Águia Parts).

O arquivo real tem 3 abas com layouts diferentes:
  - "Acima de R$1000,00"   -> 12 colunas
  - "Abaixo de R$1000,00"  -> 27 colunas
  - "Abaixo R$100,00"      -> 27 colunas

As colunas que nos interessam existem nas três, com os mesmos nomes. Por isso
o parser trabalha por NOME de coluna, e não por posição — assim continua
funcionando se o ERP mudar a ordem ou adicionar campos.
"""
from __future__ import annotations

import csv
import io
import unicodedata
import re
from dataclasses import dataclass, field
from pathlib import Path

import xlrd
from openpyxl import load_workbook

from app.abreviacoes import expandir

# Nome na planilha -> nome interno
COLUNAS = {
    "Cód": "codigo",
    "Descrição": "descricao",
    "Qtd": "quantidade",
    "NOME_GRUPO": "marca",
    "NOME_CATEGORIA": "categoria_erp",
    "UTILIZACAO_ITEM": "aplicacao",
    "PRECO_PUBLICO_ATUAL": "preco_publico",
    "CUSTO_MEDIO": "custo_medio",
    "PCT_DESCONTO": "pct_desconto",
}

OBRIGATORIAS = ["Cód", "Descrição", "Qtd"]

# Coluna opcional pela qual o João escolhe, na própria planilha, quais peças
# entram na fila. Quem conhece o estoque é ele; o sistema não tem como saber
# que uma peça de R$ 20 mil é encalhe insalvável e outra de R$ 300 vende toda
# semana. Se a coluna não existir, a fila continua vindo inteira.
COLUNAS_SELECAO = {"ANUNCIAR", "PUBLICAR", "SELECIONAR", "SELECIONADO",
                   "MARCAR", "SUBIR", "FILA", "X", "OK"}

#: Valores que contam como "sim" na coluna de seleção.
MARCAS_SIM = {"X", "S", "SIM", "1", "OK", "V", "TRUE", "VERDADEIRO", "Y", "YES"}

# Marcas genéricas do ERP que não agregam nada ao título do anúncio
MARCAS_IGNORADAS = {"OUTRAS MARCAS", "DIVERSOS", "SEM MARCA", "", "GERAL"}

MAX_TITULO = 60


@dataclass
class LinhaProduto:
    aba: str
    linha: int
    codigo: str
    descricao: str
    quantidade: int
    marca: str = ""
    categoria_erp: str = ""
    aplicacao: str = ""
    preco_publico: float = 0.0
    custo_medio: float = 0.0
    pct_desconto: float = 0.0
    #: None = a planilha não tem coluna de seleção. True/False = marcada ou não.
    selecionada: bool | None = None
    problemas: list[str] = field(default_factory=list)

    @property
    def publicavel(self) -> bool:
        return not self.problemas


def _num(valor) -> float:
    if valor in (None, ""):
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    # planilha brasileira: 1.234,56
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _txt(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


FORMATOS_ACEITOS = (".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".tsv", ".txt")

# ERP e Windows brasileiros quase nunca exportam em UTF-8; tentamos na ordem.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


class FormatoNaoSuportado(ValueError):
    pass


def _ler_texto(caminho: str) -> str:
    """Lê CSV/TSV tentando as codificações comuns de exportação brasileira."""
    bruto = Path(caminho).read_bytes()
    for enc in _ENCODINGS:
        try:
            return bruto.decode(enc)
        except UnicodeDecodeError:
            continue
    # latin-1 aceita qualquer byte; só chega aqui se o arquivo estiver corrompido
    return bruto.decode("latin-1", errors="replace")


def _ler_csv(caminho: str) -> list[tuple[str, list[list]]]:
    texto = _ler_texto(caminho)
    amostra = texto[:8192]
    try:
        # pt-BR costuma exportar com ';' porque a vírgula é separador decimal
        dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t|")
        delim = dialeto.delimiter
    except csv.Error:
        delim = ";" if amostra.count(";") > amostra.count(",") else ","

    linhas = [list(l) for l in csv.reader(io.StringIO(texto), delimiter=delim)]
    return [(Path(caminho).stem, linhas)]


def _ler_ods(caminho: str) -> list[tuple[str, list[list]]]:
    try:
        from odf.opendocument import load as carregar_ods
        from odf.table import Table, TableRow, TableCell
        from odf.text import P
    except ImportError as exc:                       # pragma: no cover
        raise FormatoNaoSuportado(
            "Para ler .ods (LibreOffice / Calc) instale: pip install odfpy. "
            "Alternativa: salve a planilha como .xlsx ou .csv."
        ) from exc

    doc = carregar_ods(caminho)
    abas: list[tuple[str, list[list]]] = []

    for tabela in doc.spreadsheet.getElementsByType(Table):
        linhas: list[list] = []
        for tr in tabela.getElementsByType(TableRow):
            linha: list = []
            for td in tr.getElementsByType(TableCell):
                # ODS comprime células repetidas em 'number-columns-repeated'
                repete = int(td.getAttribute("numbercolumnsrepeated") or 1)
                valor = td.getAttribute("value")
                if valor is None:
                    valor = "".join(str(p) for p in td.getElementsByType(P))
                linha.extend([valor] * min(repete, 1024))
            linhas.append(linha)
        abas.append((tabela.getAttribute("name") or "Planilha", linhas))

    return abas


def _ler_abas(caminho: str | Path) -> list[tuple[str, list[list]]]:
    """Devolve [(nome_aba, linhas)] a partir de qualquer formato suportado.

    O que importa é o FORMATO do arquivo, não o programa que o gerou. Excel,
    Google Sheets, LibreOffice Calc, WPS, Numbers e qualquer ERP exportam para
    algum destes:

        .xls            Excel antigo (é o que o ERP da Águia gera hoje)
        .xlsx / .xlsm   Excel moderno, Google Sheets, WPS, Numbers
        .ods            LibreOffice / OpenOffice Calc
        .csv / .tsv     qualquer coisa, inclusive exportação de banco
    """
    caminho = str(caminho)
    ext = Path(caminho).suffix.lower()

    if ext == ".xls":
        wb = xlrd.open_workbook(caminho)
        return [
            (nome, [wb.sheet_by_name(nome).row_values(r)
                    for r in range(wb.sheet_by_name(nome).nrows)])
            for nome in wb.sheet_names()
        ]

    if ext in (".csv", ".tsv", ".txt"):
        return _ler_csv(caminho)

    if ext == ".ods":
        return _ler_ods(caminho)

    if ext in (".xlsx", ".xlsm"):
        wb = load_workbook(caminho, read_only=True, data_only=True)
        return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)])
                for ws in wb.worksheets]

    raise FormatoNaoSuportado(
        f"Formato '{ext or 'sem extensão'}' não suportado. "
        f"Aceitos: {', '.join(FORMATOS_ACEITOS)}."
    )


def _achar_coluna_selecao(cabecalho: list[str]) -> int | None:
    """Acha a coluna pela qual o operador escolhe as peças, se existir.

    Compara sem acento e sem maiúscula para aceitar 'Anunciar', 'ANUNCIAR',
    'Selecionar' — é uma coluna que a pessoa digita à mão, não vale ser
    exigente com a grafia.
    """
    for i, nome in enumerate(cabecalho):
        limpo = _sem_acento(nome).strip().upper()
        if limpo in COLUNAS_SELECAO:
            return i
    return None


def _sem_acento(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c))


def _ler_selecao(linha: list, coluna: int | None) -> bool | None:
    """None se a planilha não tem coluna de seleção; senão True/False."""
    if coluna is None:
        return None
    valor = linha[coluna] if coluna < len(linha) else None
    if valor in (None, ""):
        return False
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return float(valor) != 0
    return _sem_acento(str(valor)).strip().upper() in MARCAS_SIM


def resumo_da_selecao(itens: list["LinhaProduto"]) -> dict:
    """Diz se a planilha traz coluna de seleção e quantas linhas foram marcadas."""
    com_coluna = [i for i in itens if i.selecionada is not None]
    if not com_coluna:
        return {"tem_coluna": False, "marcadas": 0, "total": len(itens)}
    marcadas = [i for i in com_coluna if i.selecionada]
    return {"tem_coluna": True, "marcadas": len(marcadas), "total": len(itens)}


def ler_planilha(caminho: str | Path, abas: list[str] | None = None) -> list[LinhaProduto]:
    """Lê a planilha e devolve as linhas já validadas.

    `abas` filtra por nome; None lê todas.
    """
    produtos: list[LinhaProduto] = []

    for nome_aba, linhas in _ler_abas(caminho):
        if abas and nome_aba not in abas:
            continue
        if not linhas:
            continue

        cabecalho = [_txt(c) for c in linhas[0]]
        indice = {nome: i for i, nome in enumerate(cabecalho)}

        faltando = [c for c in OBRIGATORIAS if c not in indice]
        if faltando:
            # aba com layout diferente do esperado: ignora em vez de quebrar
            continue

        col_selecao = _achar_coluna_selecao(cabecalho)

        def pegar(linha: list, nome_col: str):
            i = indice.get(nome_col)
            return linha[i] if i is not None and i < len(linha) else None

        for n, linha in enumerate(linhas[1:], start=2):
            codigo = _txt(pegar(linha, "Cód"))
            descricao = _txt(pegar(linha, "Descrição"))
            if not codigo and not descricao:
                continue

            p = LinhaProduto(
                aba=nome_aba,
                linha=n,
                codigo=codigo,
                descricao=descricao,
                quantidade=int(_num(pegar(linha, "Qtd"))),
                marca=_txt(pegar(linha, "NOME_GRUPO")),
                categoria_erp=_txt(pegar(linha, "NOME_CATEGORIA")),
                aplicacao=_txt(pegar(linha, "UTILIZACAO_ITEM")),
                preco_publico=_num(pegar(linha, "PRECO_PUBLICO_ATUAL")),
                custo_medio=_num(pegar(linha, "CUSTO_MEDIO")),
                pct_desconto=_num(pegar(linha, "PCT_DESCONTO")),
                selecionada=_ler_selecao(linha, col_selecao),
            )

            if not p.codigo:
                p.problemas.append("sem código (Cód)")
            if not p.descricao:
                p.problemas.append("sem descrição")
            if p.quantidade <= 0:
                p.problemas.append("quantidade zerada")
            if p.preco_publico <= 0 and p.custo_medio <= 0:
                p.problemas.append("sem preço público nem custo")

            produtos.append(p)

    return produtos


# ---------------------------------------------------------------------------
# Composição do título
# ---------------------------------------------------------------------------

def _cortar_em_palavra(texto: str, limite: int) -> str:
    """Corta sem deixar palavra pela metade ('Mitsubui', 'Transit 2.2 Co')."""
    if len(texto) <= limite:
        return texto
    if limite <= 0:
        return ""
    recorte = texto[:limite]
    if " " in recorte:
        recorte = recorte[: recorte.rfind(" ")]
    return recorte.rstrip(" -/,.>")


def montar_titulo(p: LinhaProduto) -> str:
    """Monta o título do anúncio a partir das colunas do ERP.

    A `Descrição` sozinha é curta demais (média de 15-20 caracteres, coisas
    como "PARAFUSO" ou "ANEL"). Comprador de autopeça busca por part number e
    por aplicação, então a ordem é:

        <Descrição> <Marca> <Aplicação> <Código>

    Duas garantias:
      1. O part number NUNCA é sacrificado — se faltar espaço, encolhemos a
         aplicação e depois a descrição, mas o código sempre entra. É por ele
         que o comprador de autopeça pesquisa.
      2. Nada de palavra cortada ao meio.
    """
    def limpar(t: str) -> str:
        return re.sub(r"\s+", " ", (t or "").strip()).title()

    # "BBA ARLA EMITEC 12V" -> "Bomba Arla 32 Emitec 12V"
    descricao = limpar(expandir(p.descricao))
    marca = limpar(p.marca) if p.marca.upper() not in MARCAS_IGNORADAS else ""
    codigo = p.codigo.strip().upper()
    aplicacao = limpar(p.aplicacao)

    # não repete a marca se ela já está na descrição
    if marca and marca.upper() in descricao.upper():
        marca = ""

    # remove da aplicação as palavras que já apareceram antes, senão sai
    # "Injetor Land Rover Continental Injetor Land Rove"
    if aplicacao:
        ja_ditas = {w for w in f"{descricao} {marca}".upper().split() if len(w) > 2}
        aplicacao = " ".join(
            w for w in aplicacao.split() if w.upper() not in ja_ditas
        ).strip(" -/,")

    sufixo = f" {codigo}" if codigo else ""
    orcamento = MAX_TITULO - len(sufixo)

    # a descrição tem prioridade sobre a marca, e a marca sobre a aplicação
    cabeca = descricao
    if marca and len(cabeca) + 1 + len(marca) <= orcamento:
        cabeca = f"{cabeca} {marca}"
    cabeca = _cortar_em_palavra(cabeca, orcamento)

    sobra = orcamento - len(cabeca) - 1
    if aplicacao and sobra >= 4:
        cabeca = f"{cabeca} {_cortar_em_palavra(aplicacao, sobra)}"

    return re.sub(r"\s+", " ", f"{cabeca}{sufixo}").strip()[:MAX_TITULO]


def montar_descricao(p: LinhaProduto) -> str:
    partes = [p.descricao.strip()]
    if p.marca and p.marca.upper() not in MARCAS_IGNORADAS:
        partes.append(f"Marca: {p.marca.strip()}")
    if p.codigo:
        partes.append(f"Código / Part Number: {p.codigo.strip()}")
    if p.aplicacao:
        partes.append(f"Aplicação: {p.aplicacao.strip()}")
    partes.append("Produto novo, original de estoque.")
    return "\n\n".join(partes)


# ---------------------------------------------------------------------------
# Preço
# ---------------------------------------------------------------------------

def calcular_preco(p: LinhaProduto, regra: str = "preco_publico",
                   percentual: float = 0.0) -> float:
    """Aplica a regra de preço escolhida.

    regra:
      preco_publico -> usa PRECO_PUBLICO_ATUAL como está
      acrescimo     -> preço público + percentual (cobre comissão/frete do ML)
      desconto_erp  -> preço público - PCT_DESCONTO da própria planilha
      sobre_custo   -> CUSTO_MEDIO * (1 + percentual/100)
    """
    base = p.preco_publico or 0.0

    if regra == "sobre_custo" or base <= 0:
        base = (p.custo_medio or 0.0) * (1 + percentual / 100)
        return round(base, 2)
    if regra == "acrescimo":
        return round(base * (1 + percentual / 100), 2)
    if regra == "desconto_erp":
        return round(base * (1 - (p.pct_desconto or 0) / 100), 2)
    return round(base, 2)
