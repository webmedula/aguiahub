"""Testes da expansão de abreviações do ERP."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.abreviacoes import NAO_CONFIRMADAS, expandir     # noqa: E402
from app.sheets import LinhaProduto, montar_titulo        # noqa: E402


def test_caso_real_bba_arla_emitec():
    """O item 5273337 não era encontrado porque o ERP escreve 'BBA'.

    ERP: 'BBA ARLA EMITEC 12V'
    ML : 'Bomba De Arla 32 Emitec 12v'
    """
    assert expandir("BBA ARLA EMITEC 12V") == "Bomba Arla 32 EMITEC 12V"


def test_expande_apenas_palavra_inteira():
    assert expandir("ABBA") == "ABBA"           # não vira 'ABomba'
    assert expandir("BBA") == "Bomba"


def test_abreviacoes_nao_confirmadas_ficam_intactas():
    """Expandir no chute geraria busca errada — pior que não expandir."""
    for sigla in NAO_CONFIRMADAS:
        assert expandir(sigla) == sigla


def test_texto_vazio():
    assert expandir("") == ""
    assert expandir(None) == ""


def test_titulo_usa_a_expansao():
    p = LinhaProduto(aba="t", linha=17, codigo="5273337",
                     descricao="BBA ARLA EMITEC 12V", quantidade=3,
                     marca="OUTRAS MARCAS")
    titulo = montar_titulo(p)
    assert "Bomba" in titulo
    assert "Bba" not in titulo
    assert "5273337" in titulo
    assert "Outras Marcas" not in titulo      # marca genérica é descartada
