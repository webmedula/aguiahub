"""Achar a foto na página, e baixar foto de um endereço colado.

Nasceu de um caso real: a página da apolloonibus.com.br mostrava a foto do
módulo PLD na tela, e o extrator dizia ter achado zero fotos. Duas causas —
o leitor de meta tag exigia os atributos numa ordem, e ninguém olhava as
<img> da página.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg                                  # noqa: E402
from app import storage                                   # noqa: E402
from app.extrator import _imagens_da_pagina, montar        # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

# Como a página da Apollo é: sem JSON-LD, com a foto numa <img> servida pela
# CDN da plataforma (fbitsstatic.net), num domínio diferente do site.
PAGINA_APOLLO = """<html><head>
<title>MODULO DE INJECAO PLD MERCEDES MBB SW23 EURO 3 AP528 | APOLLO ONIBUS</title>
<meta content="MODULO DE INJECAO PLD MERCEDES" property="og:title">
</head><body>
<img src="/img/layout/logo-apollo.png" alt="Apollo Onibus" width="180" height="60">
<img src="https://apolloonibus.fbitsstatic.net/img/p/modulo-de-injecao-pld-mercedes-mbb-sw23-euro-3-ap528-97501/613922-1.jpg?w=530&h=530&v=202510280954"
     alt="Modulo de injecao PLD Mercedes">
<img data-src="https://apolloonibus.fbitsstatic.net/img/p/modulo-de-injecao-pld-mercedes-mbb-sw23-euro-3-ap528-97501/613922-2.jpg?w=800"
     alt="Modulo PLD verso">
<img src="/img/selo-pagamento-visa.png" width="40" height="25" alt="visa">
<img src="/img/banner-frete-gratis.jpg" alt="frete gratis">
</body></html>"""

URL_APOLLO = "https://www.apolloonibus.com.br/modulo-de-injecao-pld-ap528"


def _fila(tmp_path, monkeypatch):
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "")
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", 1, True, {})
    item_id = storage.registrar_item(
        lote_id, 2, "AP528", "MODULO PLD EURO V NOVO", {}, status="na_fila",
        preco=12483.60, quantidade=1, valor=12483.60,
        descricao_erp="MODULO PLD EURO V NOVO")
    return lote_id, item_id


# ---------------------------------------------------------------------------
# Achar a foto na página
# ---------------------------------------------------------------------------

def test_acha_a_foto_na_img_quando_nao_ha_ficha_estruturada():
    achadas = _imagens_da_pagina(PAGINA_APOLLO, URL_APOLLO)
    assert any("613922-1.jpg" in u for u in achadas)
    assert any("613922-2.jpg" in u for u in achadas)      # veio do data-src


def test_nao_pega_logo_selo_nem_banner():
    achadas = " ".join(_imagens_da_pagina(PAGINA_APOLLO, URL_APOLLO))
    assert "logo-apollo" not in achadas
    assert "selo-pagamento" not in achadas
    assert "banner-frete" not in achadas


def test_meta_tag_com_atributos_invertidos_e_lida():
    """<meta content="..." property="og:title"> — metade dos sites escreve
    assim, e a expressão antiga não achava."""
    d = montar(PAGINA_APOLLO, URL_APOLLO)
    assert "MODULO DE INJECAO PLD MERCEDES" in d.nome


def test_a_pagina_da_apollo_deixa_de_contar_zero_fotos(tmp_path, monkeypatch):
    """O caso real: a tela dizia nada sobre foto porque contou zero."""
    s = cfg.get_settings()
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "")
    d = montar(PAGINA_APOLLO, URL_APOLLO)
    # site não autorizado: as fotos aparecem como bloqueadas, não somem
    assert d.fotos == []
    assert d.fotos_bloqueadas >= 2


def test_site_autorizado_libera_as_fotos_da_img(tmp_path, monkeypatch):
    s = cfg.get_settings()
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "apolloonibus.com.br")
    d = montar(PAGINA_APOLLO, URL_APOLLO)
    assert len(d.fotos) >= 2
    assert d.fotos_bloqueadas == 0


def test_autorizacao_olha_o_site_e_nao_a_cdn(tmp_path, monkeypatch):
    """A foto mora em apolloonibus.fbitsstatic.net, que é a CDN da plataforma.

    Autorizar a CDN seria autorizar todas as lojas que usam a FBits — por
    isso quem manda é o domínio da PÁGINA.
    """
    s = cfg.get_settings()
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "apolloonibus.com.br")
    d = montar(PAGINA_APOLLO, URL_APOLLO)
    assert d.fotos and "fbitsstatic.net" in d.fotos[0]     # foto da CDN
    assert d.dominio == "apolloonibus.com.br"              # origem registrada


# ---------------------------------------------------------------------------
# Baixar de um endereço colado
# ---------------------------------------------------------------------------

def test_baixar_foto_por_url_guarda_e_registra_a_origem(tmp_path, monkeypatch):
    import app.main as main
    from app import fotos as mod_fotos, loja

    lote_id, item_id = _fila(tmp_path, monkeypatch)

    async def falso_baixar(url, **kw):
        return b"GIF89a" + b"\x00" * 20

    monkeypatch.setattr(loja, "baixar_foto", falso_baixar)
    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)

    endereco = ("https://apolloonibus.fbitsstatic.net/img/p/"
                "modulo-97501/613922-1.jpg?w=530")
    r = cli.post(f"/itens/{item_id}/foto-por-url",
                 data={"endereco": endereco, "lote_id": lote_id})

    assert r.status_code == 303
    assert mod_fotos.listar(item_id)                      # a foto foi guardada

    origens = storage.origens_de_foto(item_id)
    registro = list(origens.values())[0]
    assert registro["url"] == endereco                    # a URL exata
    assert registro["dominio"] == "apolloonibus.fbitsstatic.net"
    assert registro["quando"]

    # e a peça passa a estar pronta para publicar
    assert storage.item(item_id)["status"] == "foto_do_operador"


def test_endereco_que_nao_e_imagem_e_recusado(tmp_path, monkeypatch):
    import app.main as main
    from app import loja

    lote_id, item_id = _fila(tmp_path, monkeypatch)

    async def bloqueado(url, **kw):
        raise loja.LojaError("o site não devolveu imagem")

    monkeypatch.setattr(loja, "baixar_foto", bloqueado)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.post(f"/itens/{item_id}/foto-por-url",
                 data={"endereco": "https://x.com/pagina.html",
                       "lote_id": lote_id})
    assert r.status_code == 400
    assert "não devolveu imagem" in r.text


def test_endereco_sem_http_e_recusado_com_instrucao(tmp_path, monkeypatch):
    import app.main as main
    lote_id, item_id = _fila(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.post(f"/itens/{item_id}/foto-por-url",
                 data={"endereco": "foto.jpg", "lote_id": lote_id})
    assert r.status_code == 400
    assert "copiar endereço da imagem" in r.text


def test_apagar_a_foto_apaga_o_registro_de_origem(tmp_path, monkeypatch):
    import app.main as main
    from app import fotos as mod_fotos, loja

    lote_id, item_id = _fila(tmp_path, monkeypatch)

    async def falso_baixar(url, **kw):
        return b"GIF89a" + b"\x00" * 20

    monkeypatch.setattr(loja, "baixar_foto", falso_baixar)
    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)
    cli.post(f"/itens/{item_id}/foto-por-url",
             data={"endereco": "https://x.com/a.jpg", "lote_id": lote_id})

    nome = mod_fotos.listar(item_id)[0]
    cli.post(f"/itens/{item_id}/fotos/{nome}/apagar", data={"lote_id": lote_id})

    assert storage.origens_de_foto(item_id) == {}


def test_a_tela_da_peca_mostra_de_onde_veio_a_foto(tmp_path, monkeypatch):
    import app.main as main
    from app import loja, painel

    lote_id, item_id = _fila(tmp_path, monkeypatch)

    async def falso_baixar(url, **kw):
        return b"GIF89a" + b"\x00" * 20

    monkeypatch.setattr(loja, "baixar_foto", falso_baixar)
    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)
    cli.post(f"/itens/{item_id}/foto-por-url",
             data={"endereco": "https://apolloonibus.com.br/a.jpg",
                   "lote_id": lote_id})

    # não pode dizer "tirada no estoque" uma foto que veio de fora
    assert painel.peca(item_id)["origem_foto"] == "foto de apolloonibus.com.br"

    tela = cli.get(f"/itens/{item_id}", follow_redirects=True).text
    assert "De onde vieram as fotos" in tela
    assert "apolloonibus.com.br" in tela


def test_o_aviso_do_corpo_distribuidor_saiu_das_telas():
    """Contar história interna do projeto para o pessoal da Águia é estranho —
    e o cartão de código não confere já diz o que importa."""
    pasta = Path(__file__).resolve().parent.parent / "app" / "templates"
    for arquivo in pasta.glob("*.html"):
        assert "corpo distribuidor" not in arquivo.read_text(encoding="utf-8"), \
            arquivo.name
