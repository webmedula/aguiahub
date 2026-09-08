"""Testes da esteira — o processamento em massa da fila.

A esteira roda sozinha sobre 1.687 peças na conta real da Águia Parts. O que
estes testes trancam não é o caminho feliz: é o que ela NÃO pode fazer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio                                            # noqa: E402
import inspect                                            # noqa: E402

import app.config as cfg                                  # noqa: E402
from app import esteira, storage                          # noqa: E402
from app.extrator import montar                           # noqa: E402

PAGINA = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Bomba Alta Pressao Sprinter 311 415 515 2.2 CDI",
 "brand":{"@type":"Brand","name":"Delphi"},
 "sku":"28447439",
 "description":"Bomba de alta pressao para Mercedes-Benz Sprinter 2.2 CDI.",
 "image":["https://fornecedor-x.com.br/img/1.jpg"],
 "additionalProperty":[{"@type":"PropertyValue","name":"Aplicacao",
   "value":"MERCEDES-BENZ SPRINTER 311/415/515"}]}
</script></head><body></body></html>"""

PAGINA_OUTRA_PECA = PAGINA.replace('"sku":"28447439"', '"sku":"99999999"') \
                          .replace("28447439", "99999999")

URL = "https://fornecedor-x.com.br/produto/bomba-28447439"


def _lote(tmp_path, monkeypatch, codigo="28447439"):
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    monkeypatch.setattr(s, "fontes_imagem_autorizadas", "")
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", 1, True, {})
    item_id = storage.registrar_item(
        lote_id, 15, codigo, "BOMBA ALTA PRESSAO", {}, status="na_fila",
        valor=20637.44, preco=10318.72, quantidade=2,
        descricao_erp="BOMBA ALTA PRESSAO", marca="OUTRAS MARCAS")
    return lote_id, item_id


def _fingir_busca(monkeypatch, urls, avisos=None):
    """Substitui a busca na internet por um resultado fixo."""
    from app.busca import Candidato, ResultadoBusca

    async def falsa(codigo, contexto=""):
        return ResultadoBusca(
            candidatos=[Candidato(url=u, titulo="Bomba", confere_codigo=True)
                        for u in urls],
            avisos=list(avisos or []))

    monkeypatch.setattr(esteira.busca, "buscar", falsa)


def _fingir_extrator(monkeypatch, paginas):
    """paginas: {url: html}. Qualquer outra URL levanta ExtratorError."""
    from app.extrator import ExtratorError

    async def falso(url):
        if url not in paginas:
            raise ExtratorError(f"não consegui abrir {url}")
        return montar(paginas[url], url)

    monkeypatch.setattr(esteira.extrator, "extrair", falso)


def _sem_loja(monkeypatch):
    """A loja da Águia fora do ar não pode impedir a esteira de rodar."""
    async def explode(lote_id):
        raise RuntimeError("loja indisponível")

    import app.publisher as publisher
    monkeypatch.setattr(publisher, "cruzar_lote_com_loja", explode)


# ---------------------------------------------------------------------------
# O que ela NÃO pode fazer
# ---------------------------------------------------------------------------

def test_a_esteira_nunca_publica():
    """A trava mais importante do módulo: nada de POST /items rodando sozinho.

    Publicação automática em 1.687 peças transformaria um casamento errado num
    estrago irreparável na conta real. A esteira termina em "pronta para
    publicar" e a pessoa clica em publicar.
    """
    fonte = inspect.getsource(esteira)
    for proibido in ("publicar_da_loja", "publicar_selecionados",
                     "publicar_com_fotos_proprias", "publicar_aprovados",
                     '"/items"'):
        assert proibido not in fonte, proibido


def test_codigo_diferente_nao_e_aproveitado(tmp_path, monkeypatch):
    """Nome parecido não vale — só o código bater vale.

    É a regra que existe desde que um corpo distribuidor casou com um livro
    (v0.4.0). Rodando sozinha, sem ninguém conferindo, ela vale ainda mais.
    """
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.get_settings(), "fontes_imagem_autorizadas",
                        "fornecedor-x.com.br")
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [URL])
    _fingir_extrator(monkeypatch, {URL: PAGINA_OUTRA_PECA})

    asyncio.run(esteira.rodar(lote_id))

    item = storage.item(item_id)
    assert item["status"] == "na_fila"        # continua esperando decisão
    assert not item["loja_fotos"]             # nenhuma foto adotada
    assert item["esteira_rotulo"] == esteira.CANDIDATOS


def test_foto_de_site_nao_autorizado_nao_entra(tmp_path, monkeypatch):
    """Código bate, mas a Águia não declarou direito sobre a imagem: entra só
    o fato técnico, e a peça continua esperando foto do estoque."""
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [URL])
    _fingir_extrator(monkeypatch, {URL: PAGINA})

    resultado = asyncio.run(esteira.rodar(lote_id))

    item = storage.item(item_id)
    assert resultado[esteira.ENRIQUECIDA] == 1
    assert item["status"] == "na_fila"
    assert not item["loja_fotos"]
    # o fato técnico, esse sim, é da peça e foi aproveitado
    assert "SPRINTER" in (item["aplicacao"] or "").upper()
    assert (item["marca"] or "").lower() == "delphi"


# ---------------------------------------------------------------------------
# O que ela deve fazer
# ---------------------------------------------------------------------------

def test_fonte_autorizada_deixa_a_peca_pronta(tmp_path, monkeypatch):
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg.get_settings(), "fontes_imagem_autorizadas",
                        "fornecedor-x.com.br")
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [URL])
    _fingir_extrator(monkeypatch, {URL: PAGINA})

    resultado = asyncio.run(esteira.rodar(lote_id))

    item = storage.item(item_id)
    assert resultado[esteira.PRONTA] == 1
    assert item["status"] == "pronto_com_fotos"
    assert "fornecedor-x.com.br" in item["loja_fotos"]
    assert item["fonte_dados"] == "fornecedor-x.com.br"
    # a origem da descrição fica rastreável junto com a da foto
    assert "Bomba de alta pressao" in (item["loja_descricao"] or "")


def test_peca_sem_resultado_fica_registrada(tmp_path, monkeypatch):
    """Guardar o "não achei" é o que impede a esteira de bater no mesmo site
    pela mesma peça na próxima execução."""
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [], avisos=["nenhum site conhecido tem"])

    asyncio.run(esteira.rodar(lote_id))

    item = storage.item(item_id)
    assert item["esteira_rotulo"] == esteira.NADA
    assert item["esteira_em"]


def test_candidatos_ficam_guardados_para_a_pessoa(tmp_path, monkeypatch):
    """A peça que a esteira não resolveu volta para a pessoa COM os links —
    senão ela refaz à mão a busca que o sistema acabou de fazer."""
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [URL])
    _fingir_extrator(monkeypatch, {URL: PAGINA_OUTRA_PECA})

    asyncio.run(esteira.rodar(lote_id))

    import json
    guardados = json.loads(storage.item(item_id)["candidatos"])
    assert guardados and guardados[0]["url"] == URL


def test_segunda_passagem_pula_quem_ja_passou(tmp_path, monkeypatch):
    """É o que torna a execução retomável depois de um deploy no meio."""
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [], avisos=["nada"])
    asyncio.run(esteira.rodar(lote_id))

    assert storage.itens_para_esteira(lote_id) == []
    assert len(storage.itens_para_esteira(lote_id, reprocessar=True)) == 1


def test_loja_fora_do_ar_nao_derruba_a_esteira(tmp_path, monkeypatch):
    """O cruzamento com a loja é o passo 0; se ele explodir, o resto continua."""
    lote_id, item_id = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [], avisos=["nada"])

    resultado = asyncio.run(esteira.rodar(lote_id))
    assert resultado["total"] == 1          # rodou mesmo com a loja quebrada


def test_peca_de_maior_valor_vem_primeiro(tmp_path, monkeypatch):
    """R$ 2,4 milhões parados: a ordem de trabalho é por dinheiro parado."""
    lote_id, _ = _lote(tmp_path, monkeypatch)
    storage.registrar_item(lote_id, 20, "BARATA1", "peça barata", {},
                           status="na_fila", valor=80.0)
    storage.registrar_item(lote_id, 21, "CARA1", "peça cara", {},
                           status="na_fila", valor=47864.0)

    fila = storage.itens_para_esteira(lote_id)
    assert [r["sku"] for r in fila] == ["CARA1", "28447439", "BARATA1"]


def test_limite_pega_so_as_mais_caras(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    storage.registrar_item(lote_id, 21, "CARA1", "peça cara", {},
                           status="na_fila", valor=47864.0)

    fila = storage.itens_para_esteira(lote_id, limite=1)
    assert [r["sku"] for r in fila] == ["CARA1"]


# ---------------------------------------------------------------------------
# Controle da execução
# ---------------------------------------------------------------------------

def test_execucao_grava_o_andamento(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    _sem_loja(monkeypatch)
    _fingir_busca(monkeypatch, [], avisos=["nada"])

    resultado = asyncio.run(esteira.rodar(lote_id))
    execucao = storage.execucao(resultado["execucao_id"])

    assert execucao["status"] == "terminada"
    assert execucao["processados"] == 1
    assert execucao["terminado_em"]


def test_parada_a_pedido_interrompe_e_marca(tmp_path, monkeypatch):
    """Parar tem que ser possível: são horas de execução batendo em site alheio."""
    lote_id, _ = _lote(tmp_path, monkeypatch)
    for i in range(6):
        storage.registrar_item(lote_id, 30 + i, f"COD{i}0000", f"peça {i}", {},
                               status="na_fila", valor=10.0 * i)
    _sem_loja(monkeypatch)

    execucao_id = storage.criar_execucao(lote_id, 7)

    async def falsa(codigo, contexto=""):
        # pede a parada logo na primeira peça
        storage.pedir_parada(execucao_id)
        from app.busca import ResultadoBusca
        return ResultadoBusca(avisos=["nada"])

    monkeypatch.setattr(esteira.busca, "buscar", falsa)

    asyncio.run(esteira.rodar(lote_id, execucao_id=execucao_id))

    execucao = storage.execucao(execucao_id)
    assert execucao["status"] == "parada"
    assert execucao["processados"] < 7      # não processou a fila inteira


def test_nao_deixa_soltar_duas_esteiras_no_mesmo_lote(tmp_path, monkeypatch):
    lote_id, _ = _lote(tmp_path, monkeypatch)
    storage.criar_execucao(lote_id, 10)
    assert storage.esteira_rodando(lote_id) is True

    from fastapi.testclient import TestClient
    import app.main as main

    cli = TestClient(main.app, base_url="https://testserver",
                     follow_redirects=False)
    r = cli.post(f"/lotes/{lote_id}/esteira", data={"limite": 0})
    assert r.status_code == 303
    # continua existindo uma execução só
    with storage.conexao() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM esteira_execucoes").fetchone()["n"]
    assert n == 1


def test_tela_da_esteira_abre(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    lote_id, _ = _lote(tmp_path, monkeypatch)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get(f"/lotes/{lote_id}/esteira")
    assert r.status_code == 200
    assert "Esteira" in r.text
    # a tela precisa dizer que não publica — é a dúvida óbvia de quem clica
    assert "não publica" in r.text.lower() or "Não publica" in r.text


def test_execucao_orfa_de_container_anterior_e_encerrada(tmp_path, monkeypatch):
    """A esteira vive em memória: se o container cai no meio, a linha fica
    'rodando' para sempre e trava a tela e o botão de soltar outra.

    Este teste existe porque a primeira versão desta limpeza tinha SQL
    inválido (duas strings coladas) e derrubava a aplicação no start — o
    pytest passou e só o teste ponta a ponta pegou.
    """
    lote_id, _ = _lote(tmp_path, monkeypatch)
    execucao_id = storage.criar_execucao(lote_id, 100)
    assert storage.esteira_rodando(lote_id) is True

    assert storage.encerrar_execucoes_orfas() == 1

    execucao = storage.execucao(execucao_id)
    assert execucao["status"] == "parada"
    assert "reinício do servidor" in execucao["mensagem"]
    assert storage.esteira_rodando(lote_id) is False


def test_startup_da_aplicacao_roda_a_limpeza(tmp_path, monkeypatch):
    """Trava de regressão do bug acima: o start tem que sobreviver."""
    from fastapi.testclient import TestClient
    import app.main as main

    lote_id, _ = _lote(tmp_path, monkeypatch)
    storage.criar_execucao(lote_id, 100)

    with TestClient(main.app, base_url="https://testserver") as cli:
        assert cli.get("/health").status_code == 200

    assert storage.esteira_rodando(lote_id) is False
