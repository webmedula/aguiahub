"""Travas contra casamento errado com o catálogo do Mercado Livre.

Casos REAIS observados no lote #1, todos com 'confiança baixa' e todos
oferecidos para aprovação numa conta de produção:

  CORPO DISTRIBUIDOR (Bosch, R$ 10.011) -> livro "Corpo a Corpo", Martins Fontes
  VALVULA DOSADORA MBB (R$ 9.063)       -> cuba de banheiro marca Japi
  REPARO UNIDADE HEUI (R$ 569)          -> kit de alfinetes de costura
  MODULO ELETRONICO Bosch (R$ 9.745)    -> painel de esteira ergométrica
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings                    # noqa: E402
from app.ml.catalog import CandidatoCatalogo, publicavel  # noqa: E402


def cand(**kw):
    base = dict(catalog_product_id="MLB22153660", nome="Corpo A Corpo",
                domain_id=None, status="active", confianca="baixa",
                motivo="descrição por extenso", category_id="MLB457963")
    base.update(kw)
    return CandidatoCatalogo(**base)


def test_confianca_baixa_nao_chega_na_aprovacao():
    """O livro 'Corpo a Corpo' casou só pela descrição — não pode passar."""
    ok, motivo = publicavel(cand())
    assert not ok
    assert "confiança" in motivo
    assert "part number" in motivo


def test_confianca_media_tambem_barra_no_padrao():
    ok, _ = publicavel(cand(confianca="media"))
    assert not ok, "o padrão é exigir casamento por part number"


def test_confianca_alta_passa():
    ok, motivo = publicavel(cand(confianca="alta"))
    assert ok and motivo == ""


def test_inativo_continua_barrado_mesmo_com_confianca_alta():
    ok, motivo = publicavel(cand(confianca="alta", status="inactive"))
    assert not ok and "inactive" in motivo


def test_sem_categoria_continua_barrado():
    ok, motivo = publicavel(cand(confianca="alta", category_id=""))
    assert not ok and "ategoria" in motivo


def test_configuracao_padrao_exige_confianca_alta():
    assert get_settings().confianca_minima == "alta"


def test_raiz_de_autopecas_configurada():
    """MLB5672 = Acessórios para Veículos."""
    assert "MLB5672" in get_settings().categorias_raiz
