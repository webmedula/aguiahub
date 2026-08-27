"""Testes da busca da peça na internet (app/busca.py).

O que mais tem chance de errar aqui é a leitura da página de resultados: cada
loja monta o HTML do seu jeito e a página vem cheia de link que não é produto
(carrinho, categoria, rede social). Por isso o teste usa HTML parecido com o
real, não um caso de laboratório.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                              # noqa: E402
import app.config as cfg                                   # noqa: E402
from app.busca import (BuscaError, Candidato, buscar_por_api,  # noqa: E402
                       caminhos_a_tentar, links_candidatos,
                       montar_url_busca)

# Página de resultado de uma busca WooCommerce, com o ruído que existe de
# verdade: menu, categoria, carrinho, rede social, imagem do produto linkada.
RESULTADO_WOO = """<html><body>
<nav>
  <a href="/">Início</a>
  <a href="/categoria-produto/injecao/">Injeção Eletrônica</a>
  <a href="/carrinho/">Carrinho</a>
  <a href="/minha-conta/">Minha conta</a>
</nav>
<ul class="products">
  <li class="product">
    <a href="/produto/modulo-injecao-0281036486/" class="woocommerce-LoopProduct-link">
      <img src="/wp-content/uploads/modulo.jpg" alt="">
      <h2 class="woocommerce-loop-product__title">Módulo de Injeção Bosch 0.281.036.486</h2>
    </a>
    <a href="/produto/modulo-injecao-0281036486/?add-to-cart=812" class="button">
      Comprar</a>
  </li>
  <li class="product">
    <a href="/produto/sensor-rotacao-0261210170/">
      <h2>Sensor de Rotação Bosch 0261210170</h2></a>
  </li>
</ul>
<footer>
  <a href="https://facebook.com/lojax">Facebook</a>
  <a href="/politica-de-privacidade/">Privacidade</a>
  <a href="/wp-content/uploads/catalogo.pdf">Catálogo em PDF</a>
</footer>
</body></html>"""

BASE = "https://loja-x.com.br/?s=0281036486&post_type=product"


def test_acha_a_pagina_do_produto_pelo_codigo():
    achados = links_candidatos(RESULTADO_WOO, BASE, "0281036486")
    urls = [c.url for c in achados]
    assert "https://loja-x.com.br/produto/modulo-injecao-0281036486/" in urls


def test_o_produto_com_o_codigo_vem_na_frente():
    achados = links_candidatos(RESULTADO_WOO, BASE, "0281036486")
    assert achados[0].url.endswith("/produto/modulo-injecao-0281036486/")
    assert achados[0].confere_codigo is True


def test_codigo_pontuado_do_site_casa_com_o_do_erp():
    """O ERP grava 0281036486 e o site escreve 0.281.036.486.

    É o mesmo problema que já tinha derrubado o cruzamento com a loja (ver
    loja.variantes_de_codigo, CHANGELOG v0.14). A busca precisa aguentar as
    duas formas ou o item mais caro do lote continua aparecendo como
    'não achei'.
    """
    achados = links_candidatos(RESULTADO_WOO, BASE, "0.281.036.486")
    assert any(c.confere_codigo and "0281036486" in c.url for c in achados)


def test_nao_traz_carrinho_conta_categoria_nem_rede_social():
    urls = " ".join(c.url for c in links_candidatos(RESULTADO_WOO, BASE,
                                                    "0281036486"))
    for lixo in ("/carrinho", "/minha-conta", "/categoria-produto",
                 "facebook.com", "/politica-de-privacidade"):
        assert lixo not in urls


def test_nao_traz_link_de_arquivo_nem_de_adicionar_ao_carrinho():
    urls = [c.url for c in links_candidatos(RESULTADO_WOO, BASE, "0281036486")]
    assert not any(u.endswith(".pdf") for u in urls)
    assert not any("add-to-cart" in u for u in urls)


def test_pagina_sem_nada_a_ver_nao_inventa_candidato():
    html = """<html><body><a href="/">Início</a>
    <a href="/carrinho/">Carrinho</a>
    <p>Nenhum produto encontrado.</p></body></html>"""
    assert links_candidatos(html, BASE, "0281036486") == []


def test_outro_produto_do_mesmo_site_entra_mas_sem_conferir_codigo():
    """A busca de site costuma trazer parecidos. Eles podem ser úteis (peça
    equivalente), mas não podem se passar por casamento de código."""
    achados = links_candidatos(RESULTADO_WOO, BASE, "0281036486")
    outro = [c for c in achados if "sensor-rotacao" in c.url]
    assert outro and outro[0].confere_codigo is False


def test_link_para_fora_do_site_e_ignorado():
    html = ('<a href="https://concorrente.com.br/produto/0281036486/">'
            'Módulo 0281036486</a>')
    assert links_candidatos(html, BASE, "0281036486") == []


# --- montagem do endereço de busca ----------------------------------------

def test_monta_endereco_com_o_codigo_no_lugar_certo():
    url = montar_url_busca("https://site.com.br/busca?q={q}", "0281036486")
    assert url == "https://site.com.br/busca?q=0281036486"


def test_codigo_com_pontuacao_vai_escapado():
    url = montar_url_busca("https://site.com.br/busca?q={q}", "0.281.036/486")
    assert "0.281.036%2F486" in url


def test_endereco_sem_q_e_recusado_com_explicacao():
    with pytest.raises(BuscaError) as erro:
        montar_url_busca("https://site.com.br/busca", "0281036486")
    assert "{q}" in str(erro.value)


def test_dominio_puro_vira_varias_tentativas_e_endereco_proprio_vira_uma():
    assert len(caminhos_a_tentar("bosch.com.br")) > 1
    assert caminhos_a_tentar("https://site.com.br/b?q={q}") == \
        ["https://site.com.br/b?q={q}"]


def test_dominio_puro_ganha_https():
    assert all(c.startswith("https://bosch.com.br")
               for c in caminhos_a_tentar("bosch.com.br"))


# --- API de busca ---------------------------------------------------------

@pytest.mark.asyncio
async def test_sem_chave_a_api_fica_quieta_em_vez_de_quebrar(monkeypatch):
    """Faixa gratuita não contratada não pode virar erro na cara do operador:
    o caminho dos sites cadastrados continua funcionando sozinho."""
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("BUSCA_API_KEY", "")
    try:
        assert await buscar_por_api("0281036486") == []
    finally:
        cfg.get_settings.cache_clear()


# --- candidato ------------------------------------------------------------

def test_candidato_descobre_o_dominio_sozinho():
    c = Candidato(url="https://cdn.bosch.com.br/produto/x/")
    assert c.dominio == "cdn.bosch.com.br"


# --- cadastro dos sites ---------------------------------------------------

@pytest.fixture
def banco(tmp_path):
    """Banco limpo por teste, como o resto da suíte faz."""
    from app import storage

    s = cfg.get_settings()
    antes = (s.database_url, s.data_dir)
    s.database_url = f"sqlite:///{tmp_path}/t.db"
    s.data_dir = str(tmp_path)
    storage.init_db()
    yield storage
    s.database_url, s.data_dir = antes


def test_cadastra_dominio_puro_e_endereco_proprio(banco):
    assert banco.acrescentar_site_busca("https://Bosch.com.br/pagina") == "bosch.com.br"
    assert banco.acrescentar_site_busca("site.com.br/busca?q={q}") == \
        "https://site.com.br/busca?q={q}"
    assert len(banco.listar_sites_busca()) == 2


def test_recusa_o_que_nao_e_site(banco):
    with pytest.raises(ValueError):
        banco.acrescentar_site_busca("parafuso sextavado")


def test_cadastrar_duas_vezes_nao_duplica(banco):
    banco.acrescentar_site_busca("bosch.com.br", "Bosch")
    banco.acrescentar_site_busca("www.bosch.com.br")
    assert len(banco.listar_sites_busca()) == 1
    assert banco.listar_sites_busca()[0]["nome"] == "Bosch"   # não perde o nome


def test_site_desligado_sai_da_busca_sem_perder_o_cadastro(banco):
    banco.acrescentar_site_busca("bosch.com.br")
    site_id = banco.listar_sites_busca()[0]["id"]

    banco.alternar_site_busca(site_id)
    assert banco.listar_sites_busca(so_ativos=True) == []
    assert len(banco.listar_sites_busca()) == 1

    banco.alternar_site_busca(site_id)
    assert len(banco.listar_sites_busca(so_ativos=True)) == 1


def test_endereco_descoberto_e_reaproveitado_na_proxima_peca(banco):
    """Sem este cache, cada peça faria o sistema tentar de novo os cinco
    caminhos de PADROES_BUSCA no mesmo site — cinco requisições ao site
    alheio para redescobrir o que já se sabia."""
    from app.busca import sites_para_consultar

    banco.acrescentar_site_busca("bosch.com.br", "Bosch")
    site_id = banco.listar_sites_busca()[0]["id"]
    banco.gravar_padrao_descoberto(site_id, "https://bosch.com.br/?s={q}")

    site = sites_para_consultar()[0]
    assert site["padrao"] == "https://bosch.com.br/?s={q}"
    assert site["cadastrado_como"] == "bosch.com.br"


def test_fonte_autorizada_entra_na_busca_de_graca(banco):
    """Autorizar um domínio em /fontes é a Águia dizendo que representa a
    marca — é onde a peça mais tem chance de estar."""
    from app.busca import sites_para_consultar

    banco.autorizar_fonte("delphi.com.br")
    sites = sites_para_consultar()
    assert [s["padrao"] for s in sites] == ["delphi.com.br"]
    assert sites[0]["origem"] == "fonte autorizada"


def test_curinga_de_imagem_nao_vira_site_de_busca(banco):
    """'*' autoriza imagem de qualquer fonte; não é um lugar onde procurar."""
    from app.busca import sites_para_consultar

    s = cfg.get_settings()
    antes = s.fontes_imagem_autorizadas
    s.fontes_imagem_autorizadas = "*"
    try:
        assert sites_para_consultar() == []
    finally:
        s.fontes_imagem_autorizadas = antes


def test_dominio_autorizado_nao_aparece_duas_vezes(banco):
    from app.busca import sites_para_consultar

    banco.autorizar_fonte("bosch.com.br")
    banco.acrescentar_site_busca("bosch.com.br", "Bosch")
    assert len(sites_para_consultar()) == 1


# --- telas ----------------------------------------------------------------

def test_tela_de_sites_abre_e_avisa_que_a_busca_ampla_esta_desligada(banco,
                                                                     monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    cfg.get_settings().busca_api_key = ""
    cli = TestClient(app=main.app, base_url="https://testserver")
    pagina = cli.get("/sites").text
    assert "Sites onde procurar" in pagina
    assert "Desligada" in pagina
    # a distinção entre "onde procurar" e "de quem posso usar foto" precisa
    # estar na tela: confundir as duas é o que arrisca a conta da Águia
    assert "não autoriza foto" in pagina


def test_busca_da_peca_mostra_os_candidatos_sem_publicar_nada(banco,
                                                              monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    from app.busca import ResultadoBusca

    lote_id = banco.criar_lote("t.xls", 1, True, {})
    item_id = banco.registrar_item(
        lote_id, 6, "0281036486", "MODULO", {}, status="na_fila", valor=1.0,
        preco=3190.94, quantidade=15, descricao_erp="MODULO ELETRONICO",
        marca="BOSCH")

    async def falsa(codigo, contexto=""):
        assert codigo == "0281036486"
        return ResultadoBusca(candidatos=[
            Candidato(url="https://bosch.com.br/produto/modulo-0281036486/",
                      titulo="Módulo Bosch", confere_codigo=True)],
            sites_consultados=["bosch.com.br"])

    monkeypatch.setattr(main.mod_busca, "buscar", falsa)
    cli = TestClient(app=main.app, base_url="https://testserver")
    pagina = cli.post(f"/itens/{item_id}/buscar-na-internet",
                      data={"lote_id": lote_id}).text

    assert "Módulo Bosch" in pagina
    assert "o código aparece nesta página" in pagina
    # o item continua intocado: procurar não decide nem aproveita nada
    assert banco.item(item_id)["status"] == "na_fila"
    assert not banco.item(item_id)["loja_fotos"]
