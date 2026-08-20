"""Testes do payload enviado ao Mercado Livre."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publisher import montar_payload            # noqa: E402
from app.sheets import LinhaProduto                 # noqa: E402


def item():
    return LinhaProduto(aba="t", linha=1, codigo="A2C59517051",
                        descricao="BICO INJETOR DIESEL", quantidade=19,
                        marca="CONTINENTAL")


def test_catalogo_envia_category_id():
    """Regressão: o ML recusa o POST sem category_id, mesmo por catálogo.

    Erro real observado em produção (lote #3):
    'The body does not contains some or none of the following properties
     [category_id]'
    """
    p = montar_payload(item(), 3188.83, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert p["category_id"] == "MLB1747"
    assert p["catalog_product_id"] == "MLB123"
    assert p["catalog_listing"] is True


def test_catalogo_nao_envia_titulo_nem_atributos():
    """São herdados do catálogo; mandar os nossos gera conflito de validação."""
    p = montar_payload(item(), 100.0, catalog_product_id="MLB123",
                       category_id="MLB1747")
    assert "title" not in p
    assert "attributes" not in p
    assert p["pictures"] == []


def test_anuncio_proprio_leva_titulo_e_atributos():
    p = montar_payload(item(), 100.0, category_id="MLB1747")
    assert p["title"]
    ids = {a["id"] for a in p["attributes"]}
    assert {"BRAND", "PART_NUMBER", "SELLER_SKU"} <= ids


def test_campos_obrigatorios_sempre_presentes():
    p = montar_payload(item(), 3188.83, catalog_product_id="MLB123",
                       category_id="MLB1747")
    for campo in ("site_id", "price", "currency_id", "available_quantity",
                  "buying_mode", "condition", "listing_type_id", "category_id"):
        assert campo in p, f"faltou {campo}"
    assert p["condition"] == "new"          # catálogo só aceita novo
    assert p["currency_id"] == "BRL"
    assert p["available_quantity"] == 19


def test_quantidade_zero_vira_um():
    p = LinhaProduto(aba="t", linha=1, codigo="X", descricao="Y", quantidade=0)
    assert montar_payload(p, 10.0)["available_quantity"] == 1


# --- elegibilidade do produto de catálogo ---------------------------------

from app.ml.catalog import CandidatoCatalogo, publicavel   # noqa: E402


def cand(**kw):
    base = dict(catalog_product_id="MLB43115763", nome="4 injetores Continental",
                domain_id="MLB-INJECTORS", status="active", confianca="alta",
                motivo="part number exato", category_id="MLB1747")
    base.update(kw)
    return CandidatoCatalogo(**base)


def test_produto_ativo_com_categoria_e_publicavel():
    ok, motivo = publicavel(cand())
    assert ok and motivo == ""


def test_produto_inativo_nao_e_publicavel():
    """Regressão: o POST devolvia 'Product MLB43115763 is not active'.

    O filtro status=active da busca não é confiável — o status do detalhe é.
    """
    ok, motivo = publicavel(cand(status="inactive"))
    assert not ok
    assert "inactive" in motivo


def test_produto_sem_status_nao_e_publicavel():
    ok, _ = publicavel(cand(status=None))
    assert not ok


def test_produto_sem_categoria_nao_e_publicavel():
    ok, motivo = publicavel(cand(category_id=""))
    assert not ok
    assert "ategoria" in motivo


# --- tradução de erros do Mercado Livre -----------------------------------

from app.ml.client import MLApiError                        # noqa: E402


def test_address_pending_vira_instrucao_acionavel():
    """Regressão: o ML devolve só 'address_pending', sem dizer o que fazer."""
    erro = MLApiError(400, {"message": "address_pending"}, "/items")
    msg = erro.mensagem_amigavel()
    assert "endereço" in msg.lower()
    assert "Meu perfil" in msg


def test_produto_inativo_traduzido():
    erro = MLApiError(400, {"message": "Product MLB43115763 is not active"}, "/items")
    assert "inativo" in erro.mensagem_amigavel()


def test_erro_desconhecido_mantem_texto_original():
    erro = MLApiError(400, {"message": "algo totalmente novo"}, "/items")
    assert "algo totalmente novo" in erro.mensagem_amigavel()


def test_causas_do_ml_sao_concatenadas():
    erro = MLApiError(400, {"cause": [{"message": "campo A inválido"},
                                      {"message": "campo B inválido"}]}, "/items")
    msg = erro.mensagem_amigavel()
    assert "campo A inválido" in msg and "campo B inválido" in msg
