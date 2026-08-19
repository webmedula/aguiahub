# Histórico de versões — Águiahub

A versão aparece no cabeçalho da interface e em `GET /health`. Depois de cada
deploy, confira ali se o número bate com o da versão que você subiu.

---

## v0.3.3 — 19/08/2026

**Diagnóstico da conta antes de publicar, e erros do ML traduzidos.**

Erro observado no lote #1: `address_pending` — um código seco, sem explicação.
Não era bug do sistema: a conta do Mercado Livre estava sem endereço cadastrado,
e o ML não libera publicação nessa condição.

- A tela inicial agora mostra se a conta está **apta a publicar**, listando os
  impedimentos encontrados (endereço faltando, restrições de anúncio, conta
  inativa). O operador vê isso ao entrar, não na hora de publicar.
- Antes de publicar um lote, o sistema checa a conta uma única vez. Se ela
  estiver bloqueada, aborta com mensagem única em vez de falhar item a item.
- Erros conhecidos do ML passam a ser traduzidos para instruções acionáveis.
  `address_pending`, por exemplo, vira: *"a conta está sem endereço cadastrado.
  Entre no ML → Meu perfil → Endereços e cadastre o endereço de origem das
  vendas."*
- Erros desconhecidos continuam aparecendo na íntegra, sem mascarar nada.
- Quatro testes cobrindo a tradução.

## v0.3.2 — 19/08/2026

**Correção: produto de catálogo inativo derrubava a publicação.**

Erro observado no lote #1:

```
Product MLB43115763 is not active
```

- O parâmetro `status=active` na busca `/products/search` **não é confiável** —
  ele devolveu um produto inativo. Agora o status é lido do detalhe do produto
  (`/products/{id}`), que é a fonte correta.
- Se o melhor candidato não estiver publicável, o sistema **tenta os próximos**
  (até 3) antes de desistir. Um mesmo part number costuma bater com vários
  produtos de catálogo, e o ML mantém descontinuados no acervo.
- Novo status de item: `catalogo_inativo` — achou correspondência, mas nenhuma
  utilizável. A mensagem lista o motivo de cada recusa, por produto.
- Item inelegível não chega à tela de aprovação, então não há como tentar
  publicar algo que o ML vai recusar.
- Quatro testes de regressão sobre elegibilidade.

## v0.3.1 — 19/08/2026

**Correção: publicação falhava por falta de `category_id`.**

Erro observado no lote #3, na primeira publicação real:

```
The body does not contains some or none of the following properties [category_id]
```

- O Mercado Livre exige `category_id` no corpo do `POST /items` **mesmo quando a
  publicação é por catálogo**. O campo estava sendo omitido nesse caminho.
- A categoria agora é descoberta na etapa de análise, tentando quatro fontes em
  ordem: o `category_id` do próprio produto de catálogo, o do anúncio que ganha o
  buy box, o de uma variação do produto, e por último o preditor de categoria do
  ML a partir do nome.
- A categoria encontrada aparece na tela de conferência, junto do ID do produto.
- Se mesmo assim não for possível determinar a categoria, o item é barrado
  **antes** da chamada à API, com mensagem clara — em vez de gastar a requisição
  e receber erro genérico do ML.
- Cinco testes de regressão cobrindo o formato do payload.

Nota: o casamento com o catálogo já estava funcionando. O item `A2C59517051` foi
corretamente identificado como "4 injetores de diesel Continental para 2.2
Transit Ford 5ws40745" — a falha era só o campo faltando.

## v0.3.0 — 19/08/2026

**Tela de conferência com aprovação item a item.**

- Nova etapa entre a prévia e a publicação: o sistema consulta o catálogo do ML
  e monta uma tela mostrando a **foto real do produto de catálogo** ao lado do
  código e da descrição do ERP.
- Publicação agora exige aprovação explícita: marcar o item **e** digitar a
  palavra `PUBLICAR`. Nenhum clique acidental cria anúncio.
- Item sem correspondência no catálogo não pode ser aprovado, nem forçando.
- Atalho "marcar só os de confiança alta" para agilizar lotes grandes.
- Migração automática do banco: as colunas novas são adicionadas ao banco que já
  está no volume, sem perder os dados nem a conexão com o ML.
- Versão passa a aparecer no cabeçalho e em `/health`.

Motivo: a conta conectada é a real. O pior erro possível não é falhar, é casar a
peça com o produto de catálogo errado e anunciar algo que você não vende.

## v0.2.0 — 17/08/2026

**Leitura de qualquer formato de planilha.**

- Passa a ler `.xlsx`, `.xlsm`, `.ods`, `.csv` e `.tsv`, além do `.xls` do ERP.
  Google Sheets, LibreOffice, WPS e Numbers exportam para algum desses.
- CSV com detecção automática de separador (`;` `,` tab) e de codificação
  (UTF-8, UTF-8 com BOM, cp1252, latin-1) — a exportação brasileira típica entra
  sem configuração.
- **Correção:** a tela oferecia `.csv` mas o código mandava o arquivo para o
  leitor de Excel e quebrava.
- **Correção de segurança:** o nome do arquivo enviado era usado direto no
  caminho de gravação, o que permitiria escrever fora da pasta de uploads.
- Arquivo em formato não suportado agora dá mensagem explicativa em vez de erro
  genérico.

## v0.1.0 — 17/08/2026

**Primeira versão.**

- OAuth completo com o Mercado Livre, com renovação automática do token e lock
  para não queimar o refresh token (que é de uso único).
- Leitura da planilha de estoque parado do ERP (3 abas, 1.687 itens).
- Geração de títulos a partir de `Descrição` + `Marca` + `Aplicação` + part
  number, com o part number protegido de truncamento.
- Quatro regras de preço: preço público, preço + margem, desconto do ERP, e
  cálculo sobre o custo médio.
- Busca no catálogo do ML por part number, com nível de confiança.
- Publicação por catálogo (`catalog_listing`), que herda foto, título e ficha
  técnica do ML — é o caminho legítimo para publicar sem ter foto própria.
- Script `scripts/testar_catalogo.py` para medir a cobertura do catálogo.
