"""Abreviações do ERP da Águia Parts.

O ERP guarda descrições curtas e abreviadas ("BBA ARLA EMITEC 12V"), mas o
catálogo do Mercado Livre usa o nome por extenso ("Bomba De Arla 32 Emitec
12v"). Buscar com a abreviação não acha nada — daí este dicionário.

É usado em dois lugares:
  1. ao montar o termo de busca no catálogo do ML
  2. ao gerar o título dos anúncios próprios

PARA ACRESCENTAR uma abreviação, basta editar o dicionário abaixo. Nenhuma outra
parte do código precisa mudar. Use sempre MAIÚSCULAS na chave.
"""

ABREVIACOES: dict[str, str] = {
    # peças
    "BBA": "Bomba",
    "BBAS": "Bombas",
    "CIL": "Cilindro",
    "VLV": "Válvula",
    "VALV": "Válvula",
    "PF": "Parafuso",
    "PARAF": "Parafuso",
    "JG": "Jogo",
    "CJ": "Conjunto",
    "CONJ": "Conjunto",
    "RET": "Retentor",
    "ROLAM": "Rolamento",
    "ROL": "Rolamento",
    "ENG": "Engrenagem",
    "MANG": "Mangueira",
    "SUP": "Suporte",
    "TP": "Tampa",
    "ARR": "Arruela",
    "ADAPT": "Adaptador",
    "CONECT": "Conector",
    "ELET": "Elétrico",
    "ELETR": "Eletrônico",
    "INJ": "Injetor",
    "REG": "Regulador",
    "SENS": "Sensor",
    "TERM": "Termostato",
    "TRANSM": "Transmissor",

    # marcas e fabricantes
    "MBB": "Mercedes-Benz",
    "MB": "Mercedes-Benz",
    "VW": "Volkswagen",
    "SCA": "Scania",

    # o produto é "Arla 32"; o ERP escreve só "Arla"
    "ARLA": "Arla 32",
}

# Abreviações cujo significado ainda não foi confirmado com a Águia. Enquanto
# estiverem aqui, são deixadas EXATAMENTE como estão — expandir no chute geraria
# busca errada no catálogo, que é pior do que não expandir.
#   SMD (26x) · CI (20x) · CAT (19x) · TO (13x) · BTS (10x)
NAO_CONFIRMADAS = {"SMD", "CI", "CAT", "TO", "BTS", "CP", "XPI", "RAIL"}


def expandir(texto: str) -> str:
    """Troca as abreviações conhecidas pela forma por extenso.

    Só substitui a PALAVRA inteira: 'BBA' vira 'Bomba', mas 'ABBA' fica intacto.
    """
    if not texto:
        return ""
    saida = []
    for palavra in texto.split():
        chave = palavra.upper().strip(".,;:/-")
        if chave in NAO_CONFIRMADAS:
            saida.append(palavra)
        else:
            saida.append(ABREVIACOES.get(chave, palavra))
    return " ".join(saida)
