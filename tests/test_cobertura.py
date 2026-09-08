"""Testes da medição de cobertura.

Ela existe para responder uma pergunta que decide o próximo mês de trabalho:
quantas peças viram anúncio sem ninguém tirar foto? Por isso o que mais
importa aqui é que ela NÃO mexa no que está medindo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio                                            # noqa: E402
import inspect                                            # noqa: E402

import app.config as cfg                                  # noqa: E402
from app import cobertura, storage                        # noqa: E402


def _fila(tmp_path, monkeypatch, quantas=30):
    s = cfg.get_settings()
    monkeypatch.setattr(s, "database_url", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(s, "data_dir", str(tmp_path))
    storage.init_db()
    lote_id = storage.criar_lote("Itens parados.xls", quantas, True, {})
    for i in range(quantas):
        preco = [5000.0, 500.0, 40.0][i % 3]      # uma peça de cada faixa
        storage.registrar_item(
            lote_id, i + 2, f"COD{i:05d}", f"peça {i}", {}, status="na_fila",
            preco=preco, quantidade=1, valor=preco,
            descricao_erp=f"PECA {i}", marca="Bosch")
    return lote_id


def _sem_loja(monkeypatch):
    async def explode():
        raise RuntimeError("loja fora do ar")
    import app.loja as loja
    monkeypatch.setattr(loja, "listar_catalogo", explode)


def _catalogo_responde(monkeypatch, com_catalogo: set[str]):
    """Finge o catálogo do ML: só os SKUs listados casam."""
    from app.ml.catalog import CandidatoCatalogo

    async def falsa_busca(client, codigo, descricao=""):
        if codigo in com_catalogo:
            return [CandidatoCatalogo(
                catalog_product_id="MLB123", nome=f"produto de {codigo}",
                domain_id=None, status="active", confianca="alta",
                motivo="teste", category_id="MLB1747")]
        return []

    async def falsa_escolha(client, candidatos):
        return (candidatos[0], []) if candidatos else (None, ["sem produto"])

    monkeypatch.setattr(cobertura, "buscar_no_catalogo", falsa_busca)
    monkeypatch.setattr(cobertura, "escolher_publicavel", falsa_escolha)
    monkeypatch.setattr(cobertura, "MLClient", lambda *a, **k: object())


# ---------------------------------------------------------------------------
# A regra que não pode ser quebrada
# ---------------------------------------------------------------------------

def test_a_medicao_nunca_escreve_no_item():
    """Diagnóstico que altera o diagnosticado não serve para decidir nada.

    Nenhum status muda, nenhuma peça fica pronta, nada é publicado.
    """
    fonte = inspect.getsource(cobertura)
    for proibido in ("atualizar_item", "atualizar_dados_catalogo",
                     "marcar_esteira", "usar_ficha_da_pesquisa",
                     "aplicar_dados_da_pesquisa", "cruzar_lote_com_loja",
                     "publicar"):
        assert proibido not in fonte, proibido


def test_medir_nao_muda_status_de_ninguem(tmp_path, monkeypatch):
    lote_id = _fila(tmp_path, monkeypatch, 12)
    _sem_loja(monkeypatch)
    _catalogo_responde(monkeypatch, {"COD00000", "COD00003"})

    antes = [(i["id"], i["status"]) for i in storage.itens_do_lote(lote_id)]
    asyncio.run(cobertura.medir(lote_id, amostra=12))
    depois = [(i["id"], i["status"]) for i in storage.itens_do_lote(lote_id)]

    assert antes == depois


# ---------------------------------------------------------------------------
# A conta que ela faz
# ---------------------------------------------------------------------------

def test_conta_quem_tem_catalogo_e_quem_precisa_de_foto(tmp_path, monkeypatch):
    lote_id = _fila(tmp_path, monkeypatch, 9)
    _sem_loja(monkeypatch)
    _catalogo_responde(monkeypatch, {"COD00000", "COD00001", "COD00002"})

    r = asyncio.run(cobertura.medir(lote_id, amostra=9))

    assert r["medidas"] == 9
    assert r["por_origem"][cobertura.CATALOGO] == 3
    assert r["por_origem"][cobertura.PRECISA_FOTO] == 6
    assert r["total_fila"] == 9


def test_projeta_por_faixa_de_preco(tmp_path, monkeypatch):
    """A taxa da peça de R$ 5.000 não vale para a de R$ 40 — projetar pela
    média geral mentiria justamente na decisão que importa."""
    lote_id = _fila(tmp_path, monkeypatch, 30)
    _sem_loja(monkeypatch)
    _catalogo_responde(monkeypatch, set())

    r = asyncio.run(cobertura.medir(lote_id, amostra=30))

    assert set(r["por_faixa"]) == {"alto", "medio", "baixo"}
    for faixa in r["por_faixa"].values():
        assert faixa["na_fila"] == 10
        assert faixa["valor_parado"] > 0


def test_amostra_pega_todas_as_faixas(tmp_path, monkeypatch):
    """Sorteio simples pegaria quase só peça barata (914 das 1.687) e
    responderia bem a pergunta que menos importa."""
    lote_id = _fila(tmp_path, monkeypatch, 120)
    fila = storage.itens_por_status(lote_id, "na_fila")

    amostra = cobertura.sortear(fila, 30)
    faixas = {cobertura._faixa_de(float(i["preco"] or 0)) for i in amostra}

    assert faixas == {"alto", "medio", "baixo"}


def test_amostra_menor_que_a_fila_devolve_a_fila_inteira(tmp_path, monkeypatch):
    lote_id = _fila(tmp_path, monkeypatch, 6)
    fila = storage.itens_por_status(lote_id, "na_fila")
    assert len(cobertura.sortear(fila, 150)) == 6


def test_loja_fora_do_ar_nao_derruba_a_medicao(tmp_path, monkeypatch):
    lote_id = _fila(tmp_path, monkeypatch, 6)
    _sem_loja(monkeypatch)
    _catalogo_responde(monkeypatch, set())

    r = asyncio.run(cobertura.medir(lote_id, amostra=6))
    assert r["erro_loja"]
    assert r["medidas"] == 6


def test_fila_vazia_avisa_em_vez_de_quebrar(tmp_path, monkeypatch):
    lote_id = _fila(tmp_path, monkeypatch, 0)
    r = asyncio.run(cobertura.medir(lote_id, amostra=10))
    assert "erro" in r


def test_tela_de_cobertura_abre(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    lote_id = _fila(tmp_path, monkeypatch, 3)
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.get(f"/lotes/{lote_id}/cobertura")
    assert r.status_code == 200
    assert "De onde viria a foto" in r.text


def test_medir_sem_conta_conectada_e_recusado(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main

    lote_id = _fila(tmp_path, monkeypatch, 3)
    storage.desconectar()
    cli = TestClient(main.app, base_url="https://testserver")
    r = cli.post(f"/lotes/{lote_id}/cobertura", data={"amostra": 60})
    assert r.status_code == 400
