"""Testes da leitura da loja da Águia Diesel (WooCommerce Store API).

O payload abaixo reproduz a resposta real de
/wp-json/wc/store/v1/products?slug=bomba-de-alta-pressao-cb18-massey-ferguson-0-445-025-016
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                          # noqa: E402
from app.loja import (LojaError, _limpar_html, _montar,   # noqa: E402
                      _slug_da_url, buscar_por_url)

BASE = "https://loja.aguiadiesel.com.br"

PRODUTO_REAL = {
    "id": 3115,
    "name": "BOMBA DE ALTA PRESSÃO CB18 &#8211; MASSEY FERGUSON &#8211; 0.445.025.016",
    "slug": "bomba-de-alta-pressao-cb18-massey-ferguson-0-445-025-016",
    "permalink": f"{BASE}/produto/bomba-de-alta-pressao-cb18-massey-ferguson-0-445-025-016/",
    "sku": "0445025016",
    "prices": {"price": "360000", "regular_price": "360000",
               "currency_code": "BRL", "currency_minor_unit": 2},
    "short_description": "<p>Bomba de alta press&atilde;o original Bosch.</p>",
    "description": ("<p>Bomba de alta press&atilde;o <strong>original Bosch</strong>, "
                    "nunca aberta para reparos.</p><ul><li>Massey Ferguson</li>"
                    "<li>Combust&iacute;vel diesel</li></ul>"
                    "<p>Produto novo com 6 meses de garantia.</p>"),
    "images": [
        {"id": 1, "src": f"{BASE}/wp-content/uploads/2022/09/2-7.png"},
        {"id": 2, "src": f"{BASE}/wp-content/uploads/2022/09/1-8.png"},
        {"id": 3, "src": f"{BASE}/wp-content/uploads/2022/09/3-5.png"},
    ],
    "categories": [{"id": 40, "name": "Bomba de alta pressão"}],
    "attributes": [{"name": "Marca", "terms": [{"name": "BOSCH"}]}],
    "stock_availability": {"text": "Fora de estoque"},
    "is_in_stock": False,
}


def test_extrai_os_campos_do_produto_real():
    p = _montar(PRODUTO_REAL, BASE)
    assert p.id == 3115
    assert p.sku == "0445025016"
    assert p.marca == "BOSCH"
    assert p.categorias == ["Bomba de alta pressão"]


def test_preco_vem_em_centavos_e_vira_reais():
    """A Store API devolve '360000' — são R$ 3.600,00, não R$ 360.000."""
    assert _montar(PRODUTO_REAL, BASE).preco == 3600.00


def test_entidades_html_do_nome_sao_decodificadas():
    """O WooCommerce grava travessão como &#8211;."""
    nome = _montar(PRODUTO_REAL, BASE).nome
    assert "&#8211;" not in nome
    assert "–" in nome


def test_todas_as_fotos_sao_capturadas():
    """A foto é o que destrava a publicação — não pode perder nenhuma."""
    fotos = _montar(PRODUTO_REAL, BASE).fotos
    assert len(fotos) == 3
    assert all(f.startswith("https://") for f in fotos)


def test_produto_com_foto_e_publicavel():
    assert _montar(PRODUTO_REAL, BASE).publicavel


def test_produto_sem_foto_nao_e_publicavel():
    """O ML exige ao menos uma imagem no anúncio próprio."""
    sem_foto = {**PRODUTO_REAL, "images": []}
    assert not _montar(sem_foto, BASE).publicavel


def test_fora_de_estoque_e_detectado():
    """A loja pode estar sem estoque mesmo o ERP tendo a peça — vale avisar."""
    assert _montar(PRODUTO_REAL, BASE).em_estoque is False


def test_descricao_vira_texto_limpo():
    """O ML aceita só texto simples na descrição."""
    d = _montar(PRODUTO_REAL, BASE).descricao
    assert "<p>" not in d and "<strong>" not in d
    assert "à" in d or "ã" in d          # entidades decodificadas
    assert "• Massey Ferguson" in d      # lista virou marcador
    assert "6 meses de garantia" in d


def test_limpar_html_lida_com_vazio():
    assert _limpar_html("") == ""
    assert _limpar_html(None) == ""


# --- extração do slug -----------------------------------------------------

def test_slug_da_url_real():
    assert _slug_da_url(
        f"{BASE}/produto/bomba-de-alta-pressao-cb18-massey-ferguson-0-445-025-016/"
    ) == "bomba-de-alta-pressao-cb18-massey-ferguson-0-445-025-016"


def test_slug_sem_barra_final():
    assert _slug_da_url(f"{BASE}/produto/bomba-teste") == "bomba-teste"


@pytest.mark.asyncio
async def test_url_sem_slug_da_mensagem_util():
    with pytest.raises(LojaError) as exc:
        await buscar_por_url(f"{BASE}/produto/")
    assert "loja.aguiadiesel" in str(exc.value)
