# Águiahub

Publicação de anúncios no Mercado Livre a partir da planilha de estoque parado do ERP.

## O problema que ele resolve

A planilha do ERP tem 1.687 itens de estoque parado, mas **não tem fotos** — e o
Mercado Livre exige pelo menos uma imagem por anúncio.

A saída é a **publicação por catálogo**: quando o part number do item já existe no
catálogo do ML, o anúncio é criado com `catalog_product_id` + `catalog_listing: true`
e `pictures: []`. Fotos, título e ficha técnica são herdados do catálogo pelo próprio
Mercado Livre.

> Baixar imagens de anúncios de outros vendedores **não** é uma opção: é violação de
> direito autoral e o ML derruba o anúncio ou suspende a conta. Este código não faz
> isso em lugar nenhum. Itens sem correspondência no catálogo ficam marcados como
> `sem_foto` e aguardam imagem própria.

## Primeiro passo: medir a cobertura

Antes de publicar qualquer coisa, descubra **quantos** dos seus itens têm catálogo:

```bash
python scripts/testar_catalogo.py "Itens parados.xls" --amostra 100
```

Saída: quantos por faixa de confiança, e um CSV item a item. Esse número decide se
o projeto vale como está ou se precisa de sessão de fotos.

## Rodando local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # preencha ML_CLIENT_SECRET e SECRET_KEY
uvicorn app.main:app --reload
```

## Deploy no EasyPanel

1. **App** → *Create Service* → aponte para este repositório (Dockerfile).
2. **Environment**: cole o conteúdo do `.env` (com o secret real).
3. **Domains**: adicione `31-97-87-68.sslip.io`, porta interna `8000`, SSL ativado.
4. **Volumes**: monte `/app/data` — é onde ficam o SQLite e os tokens.
   Sem volume, cada deploy desconecta a conta do ML.
5. Abra a URL, clique em **Conectar conta do Mercado Livre**.

## Fluxo de uso

1. Conectar a conta do ML (OAuth — o Águiahub nunca vê sua senha)
2. Enviar a planilha → **prévia** com os títulos gerados e preços calculados
3. Rodar em **simulação** (nada é publicado; mostra o que casaria com o catálogo)
4. Conferir o resultado e só então rodar em modo real

## Como o título é montado

A coluna `Descrição` do ERP tem média de 15–20 caracteres ("PARAFUSO", "ANEL") — curta
demais para a busca do ML, que aceita 60. O título é montado como:

```
<Descrição> <Marca> <Aplicação> <Part Number>
```

Com duas garantias: o part number **nunca** é descartado (é por ele que se busca
autopeça) e nenhuma palavra é cortada ao meio. Palavras repetidas entre descrição e
aplicação são removidas.

## Formatos de planilha aceitos

O que importa é o **formato do arquivo**, não o programa que o gerou.

| Formato | Vem de |
|---|---|
| `.xls` | Excel antigo — é o que o ERP da Águia exporta hoje |
| `.xlsx` / `.xlsm` | Excel moderno, Google Sheets, WPS Office, Numbers |
| `.ods` | LibreOffice Calc / OpenOffice |
| `.csv` / `.tsv` | qualquer coisa, incluindo exportação direta de banco |

Google Sheets: *Arquivo → Fazer download → Microsoft Excel (.xlsx)* ou *CSV*.
LibreOffice e WPS abrem e salvam nos mesmos formatos.

O leitor de CSV detecta sozinho o separador (`;` `,` tab `|`) e a codificação
(UTF-8, UTF-8 com BOM, cp1252, latin-1) — ou seja, aguenta a exportação
brasileira típica com `;` e acento em cp1252, sem configuração.

**Única diferença prática:** CSV não tem abas. Um `.xls`/`.xlsx`/`.ods` traz as
três abas de uma vez; em CSV é um arquivo por aba.

## Regras de preço

| Regra | O que faz |
|---|---|
| `preco_publico` | usa `PRECO_PUBLICO_ATUAL` como está |
| `acrescimo` | preço público + % (para cobrir comissão e frete do ML) |
| `desconto_erp` | aplica o `PCT_DESCONTO` da própria planilha |
| `sobre_custo` | `CUSTO_MEDIO` × (1 + %) — usado também quando falta preço público |

## Segurança

- `ML_CLIENT_SECRET` só existe como variável de ambiente; nunca é logado nem commitado.
- O `state` do OAuth é validado no callback (proteção contra CSRF).
- O refresh token do ML é de **uso único**: a renovação é serializada por lock para
  duas requisições simultâneas não queimarem o token.
- SKU já publicado é bloqueado por índice único — rodar o mesmo lote duas vezes não
  duplica anúncio.

## Limitações conhecidas

- A taxa de cobertura do catálogo para autopeça **ainda não foi medida** — rode o
  script acima com a conta conectada.
- Anúncio de catálogo compete por preço (buy box). Preço fora da faixa = anúncio
  sem visibilidade.
- Itens sem catálogo continuam bloqueados até haver foto própria.
- A aba `Abaixo R$100,00` (914 itens, mediana R$35) provavelmente não fecha conta
  depois de comissão e frete — avaliar antes de publicar.
