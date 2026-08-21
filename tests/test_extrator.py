"""Testes da leitura de ficha técnica em sites externos.

O payload abaixo imita o JSON-LD schema.org/Product que a maioria dos
e-commerce publica para aparecer no Google Shopping.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                            # noqa: E402
import app.config as cfg                                 # noqa: E402
from app.extrator import (DadosExtraidos, dominio_de,    # noqa: E402
                          fonte_autorizada_para_imagem, montar)

PAGINA = """<html><head>
<title>Módulo de Injeção Bosch 0281036486 | Loja X</title>
<meta property="og:image" content="/img/og.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"BreadcrumbList","itemListElement":[]},
 {"@type":"Product","name":"Módulo de Injeção Fiat Toro 2.0 Diesel",
  "brand":{"@type":"Brand","name":"Bosch"},
  "sku":"0281036486","mpn":"0.281.036.486","gtin13":"7891234567890",
  "description":"Módulo de injeção eletrônica para Fiat Toro 2.0 Diesel.",
  "image":["https://loja-x.com.br/img/1.jpg","https://loja-x.com.br/img/2.jpg"],
  "additionalProperty":[
    {"@type":"PropertyValue","name":"Aplicação","value":"FIAT TORO / JEEP RENEGADE"}],
  "offers":{"@type":"Offer","price":"3450.00","priceCurrency":"BRL"}}]}
</script></head><body></body></html>"""

URL = "https://loja-x.com.br/produto/modulo"


@pytest.fixture(autouse=True)
def sem_fontes_autorizadas():
    """Estado padrão: nenhum domínio autorizado a ceder imagem."""
    s = cfg.get_settings()
    anterior = s.fontes_imagem_autorizadas
    s.fontes_imagem_autorizadas = ""
    yield s
    s.fontes_imagem_autorizadas = anterior


# --- dados técnicos --------------------------------------------------------

def test_le_a_ficha_do_jsonld():
    d = montar(PAGINA, URL)
    assert d.nome == "Módulo de Injeção Fiat Toro 2.0 Diesel"
    assert d.marca == "Bosch"
    assert d.preco == 3450.0


def test_junta_todos_os_codigos():
    """sku, mpn e EAN — é o que permite casar com o código do ERP."""
    d = montar(PAGINA, URL)
    assert "0281036486" in d.codigos
    assert "0.281.036.486" in d.codigos
    assert "7891234567890" in d.codigos


def test_acha_a_aplicacao_nos_atributos():
    d = montar(PAGINA, URL)
    assert "FIAT TORO" in d.aplicacao


def test_jsonld_dentro_de_graph_e_encontrado():
    """Muitos sites embrulham tudo em @graph junto com BreadcrumbList."""
    assert montar(PAGINA, URL).util is True


def test_pagina_sem_ficha_estruturada_avisa():
    html = "<html><head><title>Peça qualquer</title></head><body></body></html>"
    d = montar(html, URL)
    assert d.nome == "Peça qualquer"
    assert any("não publica ficha estruturada" in a for a in d.avisos)


# --- fotos: a linha que não se cruza ---------------------------------------

def test_foto_de_site_nao_autorizado_nao_e_aproveitada():
    """Foto de terceiro é obra protegida.

    Usar sem direito derruba anúncio por denúncia e arrisca a suspensão da
    conta de vendedor da Águia Parts — a conta em que todo este projeto se
    apoia. O sistema conta quantas existem, mas não as usa.
    """
    d = montar(PAGINA, URL)
    assert d.fotos == []
    assert d.fotos_bloqueadas == 2


def test_foto_e_aproveitada_quando_a_aguia_declara_ter_direito(sem_fontes_autorizadas):
    """A exceção é a fonte que o dono do negócio declarou ter licença."""
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "loja-x.com.br"
    d = montar(PAGINA, URL)
    assert len(d.fotos) == 2
    assert d.fotos_bloqueadas == 0


def test_subdominio_de_fonte_autorizada_vale(sem_fontes_autorizadas):
    """A imagem costuma vir de cdn.dominio.com."""
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "bosch.com.br"
    assert fonte_autorizada_para_imagem("https://cdn.bosch.com.br/a.jpg") is True


def test_dominio_parecido_nao_engana(sem_fontes_autorizadas):
    """'bosch.com.br.site-falso.com' não é a Bosch."""
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "bosch.com.br"
    assert fonte_autorizada_para_imagem(
        "https://bosch.com.br.site-falso.com/a.jpg") is False


def test_lista_vazia_bloqueia_todo_mundo():
    assert fonte_autorizada_para_imagem("https://qualquer-site.com/a.jpg") is False


# --- descrição: referência, nunca conteúdo do anúncio ----------------------

def test_descricao_do_site_fica_marcada_como_referencia():
    """O campo se chama descricao_referencia de propósito.

    Texto descritivo tem direito autoral do site de origem. Ele aparece na
    tela para a pessoa conferir a peça e não é usado no anúncio.
    """
    d = montar(PAGINA, URL)
    assert "Módulo de injeção eletrônica" in d.descricao_referencia
    assert not hasattr(d, "descricao_do_anuncio")


def test_descricao_da_pesquisa_so_entra_com_fonte_autorizada(tmp_path,
                                                              sem_fontes_autorizadas):
    """A descrição de site NÃO autorizado nunca chega ao item — nem como
    rascunho editável. Só domínio autorizado (v0.23.0) tem esse privilégio.
    """
    from app.publisher import aplicar_dados_da_pesquisa

    sem_fontes_autorizadas.fontes_imagem_autorizadas = ""
    storage, _, item_id = _item_de_teste(tmp_path, sem_fontes_autorizadas)
    dados = montar(PAGINA, URL)
    assert dados.fonte_autorizada is False        # confere a premissa do teste

    aplicar_dados_da_pesquisa(item_id, dados)
    assert not storage.item(item_id)["descricao_editada"]


def test_descricao_da_pesquisa_entra_como_rascunho_quando_autorizado(tmp_path,
                                                                     sem_fontes_autorizadas):
    from app.publisher import aplicar_dados_da_pesquisa

    sem_fontes_autorizadas.fontes_imagem_autorizadas = "loja-x.com.br"
    storage, _, item_id = _item_de_teste(tmp_path, sem_fontes_autorizadas)
    dados = montar(PAGINA, URL)
    assert dados.fonte_autorizada is True

    aplicar_dados_da_pesquisa(item_id, dados)
    item = storage.item(item_id)
    assert "Módulo de injeção eletrônica" in item["descricao_editada"]
    assert item["fonte_dados"] == "loja-x.com.br"


# --- casamento com o código do ERP -----------------------------------------

def test_codigo_do_erp_confere_apesar_da_pontuacao():
    from app.publisher import _codigo_confere
    d = montar(PAGINA, URL)
    assert _codigo_confere("0281036486", d) is True


def test_codigo_diferente_nao_confere():
    from app.publisher import _codigo_confere
    d = montar(PAGINA, URL)
    assert _codigo_confere("XYZ99887766", d) is False


def test_codigo_curto_nunca_confere():
    """'1504' casaria com qualquer coisa — o erro dos quatro casos ruins."""
    from app.publisher import _codigo_confere
    d = montar(PAGINA, URL)
    assert _codigo_confere("1504", d) is False


def test_dominio_de_url():
    assert dominio_de("https://www.Loja-X.com.br/p/1") == "loja-x.com.br"


def test_tela_inicial_mostra_as_fontes_autorizadas(sem_fontes_autorizadas):
    """Sem isso não há como confirmar que a variável do EasyPanel pegou."""
    import tempfile
    from fastapi.testclient import TestClient
    from app import storage
    import app.main as main

    pasta = tempfile.mkdtemp()
    sem_fontes_autorizadas.database_url = f"sqlite:///{pasta}/t.db"
    sem_fontes_autorizadas.data_dir = pasta
    storage.init_db()

    cli = TestClient(app=main.app, base_url="https://testserver")
    assert "Nenhum site autorizado" in cli.get("/").text

    sem_fontes_autorizadas.fontes_imagem_autorizadas = "bosch.com.br"
    assert "bosch.com.br" in cli.get("/").text


# --- uso da ficha completa (v0.21.0) ---------------------------------------

def test_curinga_libera_qualquer_fonte(sem_fontes_autorizadas):
    """'*' existe porque a decisão é do dono do negócio.

    Continua sendo escolha explícita: nunca é o padrão.
    """
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "*"
    assert fonte_autorizada_para_imagem("https://qualquer-site.com/a.jpg") is True


def test_sem_curinga_e_sem_lista_nada_passa(sem_fontes_autorizadas):
    sem_fontes_autorizadas.fontes_imagem_autorizadas = ""
    assert fonte_autorizada_para_imagem("https://qualquer-site.com/a.jpg") is False


def _item_de_teste(tmp_path, s):
    from app import storage
    s.database_url = f"sqlite:///{tmp_path}/t.db"
    s.data_dir = str(tmp_path)
    storage.init_db()
    lote = storage.criar_lote("x.xls", 1, True, {})
    item_id = storage.registrar_item(
        lote, 6, "0281036486", "MODULO", {}, status="na_fila", valor=1.0,
        preco=3190.94, quantidade=15, descricao_erp="MODULO ELETRONICO",
        marca="OUTRAS MARCAS")
    return storage, lote, item_id


def test_usar_ficha_deixa_a_peca_pronta_e_grava_a_origem(tmp_path,
                                                         sem_fontes_autorizadas):
    from app.publisher import usar_ficha_da_pesquisa
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "loja-x.com.br"
    storage, _, item_id = _item_de_teste(tmp_path, sem_fontes_autorizadas)

    r = usar_ficha_da_pesquisa(item_id, montar(PAGINA, URL))
    assert r["ok"] is True and r["fotos"] == 2

    item = storage.item(item_id)
    assert item["status"] == "pronto_com_fotos"
    assert "loja-x.com.br/img/1.jpg" in item["loja_fotos"]
    # a origem fica gravada para dar para rastrear o anúncio depois
    assert item["fonte_dados"] == "loja-x.com.br"


def test_usar_ficha_leva_a_descricao_quando_o_site_e_autorizado(tmp_path,
                                                                 sem_fontes_autorizadas):
    """Pedido do João (v0.23.0): domínio autorizado cobre imagem E descrição.

    Antes disso a descrição nunca ia — ficava só de referência na tela. Mudou
    porque a autorização da Águia, quando existe, é declarada para "revenda e
    uso das imagens dos produtos" daquela marca — e o João pediu
    explicitamente "preciso de todas informações". Continua sendo só o ponto
    de partida: a tela de pré-anúncio deixa editar antes de publicar.
    """
    from app.publisher import usar_ficha_da_pesquisa
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "loja-x.com.br"
    storage, _, item_id = _item_de_teste(tmp_path, sem_fontes_autorizadas)

    usar_ficha_da_pesquisa(item_id, montar(PAGINA, URL))
    assert "Módulo de injeção eletrônica" in storage.item(item_id)["loja_descricao"]


def test_usar_ficha_recusa_fonte_nao_autorizada(tmp_path,
                                                sem_fontes_autorizadas):
    from app.publisher import usar_ficha_da_pesquisa
    sem_fontes_autorizadas.fontes_imagem_autorizadas = ""
    storage, _, item_id = _item_de_teste(tmp_path, sem_fontes_autorizadas)

    r = usar_ficha_da_pesquisa(item_id, montar(PAGINA, URL))
    assert r["ok"] is False
    assert "fontes autorizadas" in r["mensagem"]
    assert storage.item(item_id)["status"] == "na_fila"


# --- fontes geridas pela tela, não pelo deploy (v0.22.0) -------------------

def test_fonte_autorizada_pelo_banco_vale(tmp_path, sem_fontes_autorizadas):
    """A Águia acrescenta marca representada o tempo todo.

    Se cada marca nova exigisse mexer em variável de ambiente e refazer o
    deploy, na prática ninguém acrescentaria — e a pessoa acabaria pulando
    peça que dava para anunciar.
    """
    from app import storage
    storage_, _, _ = _item_de_teste(tmp_path, sem_fontes_autorizadas)
    assert fonte_autorizada_para_imagem("https://bosch.com.br/x") is False

    storage.autorizar_fonte("bosch.com.br", "Bosch — oficial")
    assert fonte_autorizada_para_imagem("https://cdn.bosch.com.br/a.jpg") is True


def test_dominio_e_extraido_de_url_colada(tmp_path, sem_fontes_autorizadas):
    """A pessoa cola o link do produto, não o domínio limpo."""
    from app import storage
    _item_de_teste(tmp_path, sem_fontes_autorizadas)
    assert storage.autorizar_fonte(
        "https://www.bosch.com.br/produtos/modulo?ref=1") == "bosch.com.br"


def test_texto_que_nao_e_site_e_recusado(tmp_path, sem_fontes_autorizadas):
    from app import storage
    _item_de_teste(tmp_path, sem_fontes_autorizadas)
    with pytest.raises(ValueError):
        storage.autorizar_fonte("bosch")


def test_remover_fonte_volta_a_bloquear(tmp_path, sem_fontes_autorizadas):
    from app import storage
    _item_de_teste(tmp_path, sem_fontes_autorizadas)
    storage.autorizar_fonte("bosch.com.br")
    storage.remover_fonte("bosch.com.br")
    assert fonte_autorizada_para_imagem("https://bosch.com.br/x") is False


def test_ambiente_e_banco_somam(tmp_path, sem_fontes_autorizadas):
    """Quem já tinha a variável configurada não perde nada."""
    from app import storage
    from app.extrator import fontes_permitidas
    _item_de_teste(tmp_path, sem_fontes_autorizadas)
    sem_fontes_autorizadas.fontes_imagem_autorizadas = "delphi.com"
    storage.autorizar_fonte("bosch.com.br")
    assert {"delphi.com", "bosch.com.br"} <= fontes_permitidas()


def test_falha_de_banco_nao_libera_geral(monkeypatch):
    """Erro ao ler a lista não pode virar 'pode tudo'."""
    import sqlite3
    from app import storage

    def explode():
        raise sqlite3.Error("banco indisponível")

    monkeypatch.setattr(storage, "listar_fontes", explode)
    assert storage.dominios_autorizados() == set()
