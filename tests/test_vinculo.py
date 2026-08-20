"""Testes da extração do ID do anúncio a partir do link do Mercado Livre."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ml.catalog import extrair_id_anuncio       # noqa: E402


def test_link_de_anuncio_completo():
    url = "https://produto.mercadolivre.com.br/MLB-1234567890-bomba-de-arla-32-emitec-_JM"
    assert extrair_id_anuncio(url) == "MLB1234567890"


def test_link_de_produto_de_catalogo():
    url = "https://www.mercadolivre.com.br/bomba-arla/p/MLB19342323"
    assert extrair_id_anuncio(url) == "MLB19342323"


def test_link_com_parametros_de_rastreio():
    url = ("https://www.mercadolivre.com.br/p/MLB19342323"
           "?pdp_filters=category:MLB1747&tracking_id=abc-123")
    assert extrair_id_anuncio(url) == "MLB19342323"


def test_codigo_solto_sem_hifen():
    assert extrair_id_anuncio("MLB1234567890") == "MLB1234567890"


def test_codigo_em_minusculas():
    assert extrair_id_anuncio("mlb1234567890") == "MLB1234567890"


def test_outros_paises():
    assert extrair_id_anuncio("https://articulo.mercadolibre.com.ar/MLA-987654321-x") \
        == "MLA987654321"


def test_texto_sem_id_devolve_vazio():
    assert extrair_id_anuncio("bomba de arla 32") == ""
    assert extrair_id_anuncio("") == ""
    assert extrair_id_anuncio(None) == ""


def test_numero_curto_nao_e_confundido_com_anuncio():
    """'MLB1747' é categoria, não anúncio — precisa de 6+ dígitos."""
    assert extrair_id_anuncio("MLB1747") == ""


# --- páginas /up/ e parâmetro wid (caso real do item 5273337) --------------

URL_REAL = ("https://www.mercadolivre.com.br/bomba-arla-32-ford-cargo-816-1119-12v-"
            "cc455h298ba-5273337/up/MLBU4286980046#polycard_client=search-desktop"
            "&be_origin=backend&overlay_label=not_apply&search_layout=grid"
            "&position=9&type=product&tracking_id=6179c1ea-c33c-4a2c-b6a8"
            "&wid=MLB4876653919&sid=search")


def test_prefere_o_wid_por_ser_anuncio_ativo():
    """O wid aponta o anúncio que está ganhando a vitrine — é o mais confiável."""
    assert extrair_id_anuncio(URL_REAL) == "MLB4876653919"


def test_pagina_up_sem_wid_devolve_o_user_product():
    """Regressão: URL de página /up/ copiada direto não era reconhecida."""
    url = ("https://www.mercadolivre.com.br/bomba-arla-32-ford-cargo/"
           "up/MLBU4286980046")
    assert extrair_id_anuncio(url) == "MLBU4286980046"


def test_nao_confunde_mlbu_com_mlb():
    """'MLBU4286980046' não pode virar 'MLB4286980046' — é outro produto."""
    assert extrair_id_anuncio("MLBU4286980046") == "MLBU4286980046"


def test_wid_tem_prioridade_sobre_o_produto_da_url():
    url = "https://www.mercadolivre.com.br/x/up/MLBU1111111?wid=MLB2222222222"
    assert extrair_id_anuncio(url) == "MLB2222222222"
