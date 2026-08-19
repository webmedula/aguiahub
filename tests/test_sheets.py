"""Testes do parser e do gerador de títulos."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sheets import (MAX_TITULO, LinhaProduto, calcular_preco,  # noqa: E402
                        montar_titulo, _num)


def linha(**kw) -> LinhaProduto:
    base = dict(aba="t", linha=1, codigo="ABC12345", descricao="BICO INJETOR",
                quantidade=3)
    base.update(kw)
    return LinhaProduto(**base)


# --- título ---------------------------------------------------------------

def test_titulo_respeita_limite_do_ml():
    p = linha(descricao="BICO INJETOR DIESEL RANGER 2.2 / 3.2 PUMA TRANSIT",
              marca="CONTINENTAL",
              aplicacao="BK2Q9K546AG RANGER 2.2/3.2 PUMA 13> TRANSIT 2.2")
    assert len(montar_titulo(p)) <= MAX_TITULO


def test_part_number_nunca_e_descartado():
    """O comprador de autopeça busca pelo código — ele tem que sobreviver."""
    p = linha(descricao="X" * 80, codigo="A2C59517051")
    assert "A2C59517051" in montar_titulo(p)


def test_nao_corta_palavra_no_meio():
    p = linha(descricao="CORPO DISTRIBUIDOR", marca="BOSCH",
              aplicacao="ANTIGA 9443612846 MITSUBISHI PAJERO FULL",
              codigo="CD110087")
    titulo = montar_titulo(p)
    # nenhum pedaço truncado tipo "Mitsubis"
    assert "Mitsubis " not in titulo + " "
    assert len(titulo) <= MAX_TITULO


def test_remove_palavra_repetida_entre_descricao_e_aplicacao():
    p = linha(descricao="INJETOR LAND ROVER", marca="CONTINENTAL",
              aplicacao="INJETOR LAND ROVER DISCOVERY", codigo="4H2Q9K546AF")
    titulo = montar_titulo(p)
    assert titulo.upper().count("LAND") == 1


def test_marca_generica_e_ignorada():
    p = linha(marca="OUTRAS MARCAS")
    assert "Outras Marcas" not in montar_titulo(p)


def test_marca_ja_na_descricao_nao_repete():
    p = linha(descricao="BICO INJETOR BOSCH", marca="BOSCH")
    assert montar_titulo(p).upper().count("BOSCH") == 1


# --- preço ----------------------------------------------------------------

def test_preco_publico():
    p = linha(preco_publico=1000.0)
    assert calcular_preco(p, "preco_publico") == 1000.0


def test_acrescimo():
    p = linha(preco_publico=1000.0)
    assert calcular_preco(p, "acrescimo", 20) == 1200.0


def test_desconto_do_erp():
    p = linha(preco_publico=1000.0, pct_desconto=21.0)
    assert calcular_preco(p, "desconto_erp") == 790.0


def test_cai_para_custo_quando_falta_preco_publico():
    p = linha(preco_publico=0.0, custo_medio=100.0)
    assert calcular_preco(p, "preco_publico", 50) == 150.0


# --- números --------------------------------------------------------------

def test_num_formato_brasileiro():
    assert _num("1.234,56") == 1234.56
    assert _num("57676.65") == 57676.65
    assert _num("R$ 99,90") == 99.90
    assert _num("") == 0.0
    assert _num("texto") == 0.0


# --- formatos de arquivo --------------------------------------------------

import csv as _csv          # noqa: E402
import io as _io            # noqa: E402
import pytest               # noqa: E402

from app.sheets import FormatoNaoSuportado, ler_planilha, _ler_csv  # noqa: E402

CABECALHO = ["Qtd", "Custo", "Cód", "Descrição", "NOME_GRUPO",
             "UTILIZACAO_ITEM", "PRECO_PUBLICO_ATUAL", "CUSTO_MEDIO"]
LINHA = ["2", "100", "CD110087", "CORPO DISTRIBUIDOR", "BOSCH",
         "PAJERO FULL", "10011,60", "6006,96"]


def _escrever_csv(tmp_path, delim, encoding, nome="t.csv"):
    buf = _io.StringIO()
    w = _csv.writer(buf, delimiter=delim)
    w.writerow(CABECALHO)
    w.writerow(LINHA)
    caminho = tmp_path / nome
    caminho.write_bytes(buf.getvalue().encode(encoding, errors="replace"))
    return caminho


def test_csv_ponto_e_virgula_cp1252(tmp_path):
    """Exportação típica de ERP brasileiro: ';' e cp1252."""
    itens = ler_planilha(_escrever_csv(tmp_path, ";", "cp1252"))
    assert len(itens) == 1
    assert itens[0].codigo == "CD110087"
    assert itens[0].descricao == "CORPO DISTRIBUIDOR"     # acento preservado
    assert itens[0].preco_publico == 10011.60             # vírgula decimal


def test_csv_virgula_utf8(tmp_path):
    itens = ler_planilha(_escrever_csv(tmp_path, ",", "utf-8"))
    assert len(itens) == 1
    assert itens[0].marca == "BOSCH"


def test_csv_utf8_com_bom(tmp_path):
    """Excel salva 'CSV UTF-8' com BOM — não pode virar lixo no cabeçalho."""
    itens = ler_planilha(_escrever_csv(tmp_path, ";", "utf-8-sig"))
    assert len(itens) == 1
    assert itens[0].codigo == "CD110087"


def test_tsv(tmp_path):
    caminho = _escrever_csv(tmp_path, "\t", "utf-8", nome="t.tsv")
    assert len(ler_planilha(caminho)) == 1


def test_formato_nao_suportado_da_mensagem_util(tmp_path):
    ruim = tmp_path / "planilha.pdf"
    ruim.write_bytes(b"%PDF-1.4")
    with pytest.raises(FormatoNaoSuportado) as exc:
        ler_planilha(ruim)
    assert ".xlsx" in str(exc.value)


def test_aba_com_layout_diferente_e_ignorada(tmp_path):
    """Aba sem as colunas obrigatórias não pode derrubar a leitura."""
    caminho = tmp_path / "x.csv"
    caminho.write_text("coluna_a;coluna_b\n1;2\n", encoding="utf-8")
    assert ler_planilha(caminho) == []
