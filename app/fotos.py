"""Fotos tiradas pelo operador, para peças que não estão na loja da Águia.

O Mercado Livre fechou o acesso a dados de outros vendedores, e nem toda peça
do estoque está no site da Águia. Sobra o caminho que não depende de ninguém:
a pessoa fotografa a peça na prateleira e o anúncio sai com essa imagem.

As fotos ficam no volume do VPS (`data/fotos/<item>/`) e só são enviadas ao ML
no momento da publicação. Guardar localmente permite conferir antes, refazer a
publicação se der erro, e não deixa imagem órfã na conta do Mercado Livre.
"""
from __future__ import annotations

import imghdr
from pathlib import Path

from app.config import get_settings

# Limites do Mercado Livre para imagem de anúncio.
TAMANHO_MAXIMO = 10 * 1024 * 1024        # 10 MB por foto
MAX_FOTOS = 10
FORMATOS = {"jpeg", "png", "webp", "gif"}


class FotoInvalida(ValueError):
    pass


def pasta_do_item(item_id: int) -> Path:
    caminho = Path(get_settings().data_dir) / "fotos" / str(item_id)
    caminho.mkdir(parents=True, exist_ok=True)
    return caminho


def listar(item_id: int) -> list[str]:
    """Nomes dos arquivos de foto do item, em ordem."""
    pasta = Path(get_settings().data_dir) / "fotos" / str(item_id)
    if not pasta.is_dir():
        return []
    return sorted(p.name for p in pasta.iterdir() if p.is_file())


def caminho_da_foto(item_id: int, nome: str) -> Path:
    """Resolve o arquivo, barrando nome que tente sair da pasta."""
    pasta = Path(get_settings().data_dir) / "fotos" / str(item_id)
    alvo = (pasta / Path(nome).name).resolve()
    if not str(alvo).startswith(str(pasta.resolve())):
        raise FotoInvalida("Nome de arquivo inválido.")
    return alvo


def salvar(item_id: int, conteudo: bytes, nome_original: str) -> str:
    """Valida e grava uma foto. Devolve o nome do arquivo salvo."""
    if not conteudo:
        raise FotoInvalida("Arquivo vazio.")
    if len(conteudo) > TAMANHO_MAXIMO:
        raise FotoInvalida(
            f"'{nome_original}' tem {len(conteudo) / 1024 / 1024:.1f} MB. "
            f"O limite é {TAMANHO_MAXIMO // 1024 // 1024} MB por foto.")

    tipo = imghdr.what(None, h=conteudo)
    if tipo not in FORMATOS:
        raise FotoInvalida(
            f"'{nome_original}' não parece uma imagem "
            f"({tipo or 'formato desconhecido'}). Use JPG, PNG ou WEBP.")

    ja_tem = listar(item_id)
    if len(ja_tem) >= MAX_FOTOS:
        raise FotoInvalida(
            f"Este item já tem {MAX_FOTOS} fotos, que é o limite do "
            "Mercado Livre. Apague alguma antes de enviar outra.")

    extensao = "jpg" if tipo == "jpeg" else tipo
    nome = f"{len(ja_tem) + 1:02d}.{extensao}"
    (pasta_do_item(item_id) / nome).write_bytes(conteudo)
    return nome


def apagar(item_id: int, nome: str) -> None:
    alvo = caminho_da_foto(item_id, nome)
    if alvo.is_file():
        alvo.unlink()


def ler(item_id: int, nome: str) -> bytes:
    return caminho_da_foto(item_id, nome).read_bytes()
