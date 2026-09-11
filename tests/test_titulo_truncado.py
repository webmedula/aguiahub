"""O nome que o ERP cortou — v0.31.3.

A peça `MC00522` da lista da Águia chegou com a descrição truncada e o nome
inteiro no campo de aplicação:

    Descrição:  VALVULA RETORNO JOHN
    Aplicação:  VALVULA RETORNO JOHN DEERE
    Marca:      FIRAD

O título saía **"Valvula Retorno John Firad Deere MC00522"** — a marca no meio
do nome próprio, partindo "John Deere". Ninguém procura por isso, e o anúncio
parece quebrado na vitrine do Mercado Livre.

Estes testes trancam a correção e, principalmente, o quanto ela **não** pode
crescer: a regra só vale quando a aplicação é a continuação literal da
descrição. Se ela passar a adivinhar, vai deformar título de peça boa.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sheets import (LinhaProduto, completar_nome_truncado,   # noqa: E402
                        montar_titulo)


def _titulo(descricao, marca, aplicacao, codigo):
    return montar_titulo(LinhaProduto(
        aba="", linha=1, codigo=codigo, descricao=descricao, quantidade=1,
        marca=marca, aplicacao=aplicacao))


# --- completar_nome_truncado ----------------------------------------------

def test_aplicacao_que_continua_a_descricao_devolve_o_nome_inteiro():
    assert completar_nome_truncado(
        "VALVULA RETORNO JOHN", "VALVULA RETORNO JOHN DEERE") == \
        "VALVULA RETORNO JOHN DEERE"


def test_aplicacao_diferente_nao_mexe_na_descricao():
    """"BOMBA ALTA PRESSAO" + "MERCEDES-BENZ SPRINTER" são coisas distintas."""
    assert completar_nome_truncado(
        "BOMBA ALTA PRESSAO", "MERCEDES-BENZ SPRINTER 311/415/515") == \
        "BOMBA ALTA PRESSAO"


def test_palavra_maior_nao_conta_como_continuacao():
    """Sem exigir espaço, "BOMBA" casaria com "BOMBAS HIDRAULICAS"."""
    assert completar_nome_truncado("BOMBA", "BOMBAS HIDRAULICAS") == "BOMBA"


def test_aplicacao_igual_ou_menor_nao_muda_nada():
    assert completar_nome_truncado("VALVULA RETORNO", "VALVULA RETORNO") == \
        "VALVULA RETORNO"
    assert completar_nome_truncado("VALVULA RETORNO", "VALVULA") == \
        "VALVULA RETORNO"


def test_campos_vazios_nao_quebram():
    assert completar_nome_truncado("", "QUALQUER COISA") == ""
    assert completar_nome_truncado("VALVULA", "") == "VALVULA"


# --- o título ---------------------------------------------------------------

def test_a_marca_nao_parte_mais_o_nome_proprio():
    titulo = _titulo("VALVULA RETORNO JOHN", "FIRAD",
                     "VALVULA RETORNO JOHN DEERE", "MC00522")

    assert titulo == "Valvula Retorno John Deere Firad MC00522"
    assert "John Firad Deere" not in titulo


def test_o_codigo_continua_sendo_a_ultima_coisa_a_sair():
    """A garantia antiga do montador não pode ter sido perdida no caminho:
    é pelo part number que o comprador de autopeça pesquisa."""
    titulo = _titulo("VALVULA RETORNO JOHN", "FIRAD",
                     "VALVULA RETORNO JOHN DEERE " + "X" * 120, "MC00522")

    assert titulo.endswith("MC00522")
    assert len(titulo) <= 60


def test_titulos_que_ja_estavam_certos_nao_mudaram():
    assert _titulo("BOMBA ALTA PRESSAO", "OUTRAS MARCAS",
                   "MERCEDES-BENZ SPRINTER 311/415/515", "28447439") == \
        "Bomba Alta Pressao Mercedes-Benz Sprinter 28447439"
    assert _titulo("INJETOR", "CONTINENTAL", "INJETOR LAND ROVER",
                   "28232248") == "Injetor Land Rover Continental 28232248"


# --- o termo de busca -------------------------------------------------------

def test_a_busca_da_tela_tambem_usa_o_nome_inteiro(tmp_path, monkeypatch):
    """A consulta saía "VALVULA RETORNO JOHN FIRAD" — ruim justamente porque o
    nome da peça é "John Deere" e estava partido."""
    import app.config as cfg
    from app import storage
    from app.main import _termo_pelo_nome

    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", 1, True, {})
    item_id = storage.registrar_item(
        lote_id, 7, "MC00522", "VALVULA RETORNO JOHN", {}, status="na_fila",
        valor=506.76, preco=506.76, quantidade=24,
        descricao_erp="VALVULA RETORNO JOHN", marca="FIRAD",
        aplicacao="VALVULA RETORNO JOHN DEERE")

    termo = _termo_pelo_nome(storage.item(item_id))

    assert "JOHN DEERE" in termo.upper()
    assert "JOHN FIRAD" not in termo.upper()
