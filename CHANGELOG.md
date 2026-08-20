# Histórico de versões — Águiahub

A versão aparece no cabeçalho da interface e em `GET /health`. Depois de cada
deploy, confira ali se o número bate com o da versão que você subiu.

---

## v0.8.0 — 20/08/2026

**A loja da Águia como origem das fotos. Resolve o problema que travava o projeto.**

O Mercado Livre exige pelo menos uma imagem por anúncio, e essa era a barreira
desde o primeiro dia. O catálogo do ML se mostrou um caminho estreito: a busca
da API alcança só as fichas antigas (formato MLB), o ML migrou os produtos ativos
para MLBU, e bloqueou a leitura de anúncios de terceiros.

A saída veio de fora do Mercado Livre: **a Águia já tem foto, descrição e ficha
das peças na própria loja**. São imagens próprias — nenhuma questão de direito
autoral, nenhuma dependência de catálogo, nenhuma sessão de fotos.

- Novo módulo `app/loja.py`, lendo a **Store API do WooCommerce**
  (`/wp-json/wc/store/v1/products`). JSON estruturado, e não HTML — não quebra
  quando o tema do site muda.
- Busca por **código** (SKU exato, depois busca livre) e por **link da página**.
- A fila procura na loja **antes** do catálogo do ML. Achando, mostra as fotos
  grandes para o operador conferir e um botão para usar aqueles dados.
- Se a peça está na loja mas não apareceu na busca automática, há um campo para
  colar o link direto.
- **Publicação com foto própria** (`publicar_da_loja`): anúncio próprio no ML
  com as imagens da loja, título e descrição do site, preço e estoque do ERP, e
  categoria obtida pelo preditor do Mercado Livre.
- Item sem foto na loja é barrado com mensagem clara — o ML recusaria de todo
  jeito.
- Avisa quando a loja marca o produto como fora de estoque, já que o ERP pode
  discordar.
- Publicar exige a palavra `PUBLICAR` digitada, como no resto do sistema.
- Doze testes sobre o payload real da loja: preço em centavos (`"360000"` →
  R$ 3.600,00), entidades HTML no nome (`&#8211;`), descrição HTML virando texto
  simples, e todas as fotos capturadas.

Nova variável: `LOJA_BASE_URL` (padrão `https://loja.aguiadiesel.com.br`).

## v0.7.3 — 20/08/2026

**Instruções visuais de qual link copiar, e mais uma tentativa antes de desistir.**

- A tela da fila mostra agora uma tabelinha com os três padrões de endereço —
  `/up/...` ✓, `/p/...` ✓, `MLB-...-_JM` ✗ — em vez de descrever em texto corrido.
  Quem opera confere na barra de endereço antes de copiar.
- Explica como sair de um anúncio e chegar na página do produto: clicar no nome
  do produto, ou em "outros vendedores".
- **Nova tentativa antes de desistir:** quando a leitura completa do anúncio é
  bloqueada (403), o sistema tenta ler só os campos necessários
  (`catalog_product_id`, `category_id`, título, foto). Alguns bloqueios do ML são
  por recurso inteiro, outros não — não custa nada tentar antes de mandar a
  pessoa navegar.

## v0.7.2 — 20/08/2026

**Vínculo passa a tentar o produto de catálogo antes do anúncio.**

Erro relatado ao vincular:
`{"detail":"Mercado Livre: Access to the requested resource is forbidden"}`

O Mercado Livre **bloqueia a leitura de anúncios de outros vendedores** pela API
(403) — a mesma política que já havia derrubado a busca pública de anúncios.

Pior: a mudança da v0.6.1, que passou a priorizar o `wid` da URL, apontava
justamente para o endpoint bloqueado. O `wid` identifica um **anúncio**; o
`/up/MLBU...` identifica o **produto de catálogo**, que continua acessível — e é
dele que vêm foto, ficha técnica e categoria, que é o que realmente precisamos.

- Nova função `extrair_referencias`, que devolve **todas** as referências da URL
  na ordem de tentativa: produto de catálogo primeiro (`/up/`, depois `/p/`),
  anúncio por último.
- O vínculo tenta em cascata. Bloqueio em uma referência não interrompe: passa
  para a próxima.
- Se tudo estiver bloqueado, a mensagem diz **o que fazer**: copiar o link da
  página do produto, clicando no nome do produto dentro do anúncio.
- A tela da fila explica isso antes do erro acontecer, no próprio campo de colar.
- Quatro testes cobrindo a ordem de tentativa.

## v0.7.1 — 20/08/2026

**Erro deixou de ser beco sem saída.**

Relato real: ao clicar em "Vincular" na fila, a tela virava
`{"detail":"Conecte a conta do Mercado Livre primeiro."}` — JSON cru, sem botão
de voltar e sem como conectar. O botão de conectar existia, mas só na tela
inicial, e de dentro do erro não havia caminho até ela.

- Erros passam a ser **página HTML** com mensagem em português e três saídas:
  *Conectar conta*, *Voltar* e *Ir para o início*.
- Quando o problema é falta de conexão com o Mercado Livre, a própria página do
  erro oferece o botão de conectar.
- **Aviso no topo de todas as telas** quando não há conta conectada, com botão
  de conectar. Antes, só a tela inicial mostrava isso — quem trabalhava na fila
  não tinha como saber.

Motivo de fundo: quem opera é a pessoa do estoque. Tela de JSON é aceitável para
quem programa, não para quem está conferindo peça na prateleira.

## v0.7.0 — 20/08/2026

**Triagem instantânea e fila de trabalho por valor.**

Mudança de estratégia. Os testes mostraram que o casamento automático com o
catálogo rende perto de zero — o Mercado Livre migrou os produtos ativos para
o formato MLBU, que a busca da API não alcança, e bloqueou a busca pública de
anúncios. Quem encontra a peça é a pessoa, no navegador. O sistema passa a ser
o assistente dessa pessoa, não o motor.

**Triagem** (`/planilha/triagem`) — classifica a planilha inteira **sem
consultar o Mercado Livre**. Consultar o ML para 1.687 itens gastaria milhares
de requisições só para descobrir que a maioria nem tem código utilizável.

Resultado na planilha real: **1.334 itens (79%) com código utilizável,
R$ 1.567.815 em estoque**. Na aba `Acima de R$1000,00`: 126 itens, R$ 1.035.011.

Classificação do código: *part number forte* (letras + números), *numérico
plausível* (6 a 12 dígitos), *codificado*, *numérico longo*, *curto demais*,
*duvidoso*, *sem código*.

**Fila de trabalho** (`/fila/{lote}`) — uma peça por tela, **sempre a de maior
valor parado ainda pendente**. Com 21 horas de trabalho humano para percorrer
tudo e 72% do valor concentrado em 197 itens, a ordem é a decisão de negócio
mais importante do sistema.

A tela foi desenhada para quem conhece a peça, não o sistema: descrição e
código em letra grande, valor parado visível, botão que abre a busca do ML já
preenchida, campo para colar o link, e três saídas — *é esta peça*, *não achei*,
*não tenho certeza, deixar para o João*.

O terceiro botão é o mais importante: sem ele, quem está em dúvida chuta — e
chute foi o que gerou o livro no lugar do injetor na v0.5.1.

A consulta ao catálogo passou a ser **sob demanda**, só para o item que está na
tela. Se falhar, a tela continua útil.

**Senha da aplicação** — implementei o `APP_PASSWORD` que estava declarado na
configuração desde a v0.1.0 e nunca havia sido escrito. Fica **desligado por
padrão**, conforme escolha do cliente; basta definir a variável no EasyPanel
para ativar. Vale lembrar que a URL é pública e a aplicação publica anúncios
reais na conta da Águia Parts.

## v0.6.1 — 20/08/2026

**Reconhecimento das páginas `/up/` e do parâmetro `wid`.**

Descoberta que reinterpreta todo o diagnóstico anterior: a Bomba de Arla
(código 5273337) tem **duas fichas** no Mercado Livre —

- `MLB64325646`, sem ofertas ("indisponível no momento"), que é a que a busca
  automática encontrava, e
- `MLBU4286980046`, com anúncio ativo (`MLB4876653919`), vendendo agora.

Ou seja: `status: inactive` na API significa *"nenhum vendedor com estoque"*, não
"produto descontinuado". E o catálogo do ML para essas peças **não está morto** —
a busca é que estava trazendo a ficha abandonada.

- `extrair_id_anuncio` passa a reconhecer páginas `/up/MLBU...` (user product) e
  o parâmetro `wid=` das URLs de busca.
- **Prioridade nova:** quando a URL traz `wid`, ele vence. O `wid` aponta o
  anúncio que está ganhando a vitrine — comprovadamente ativo — enquanto a mesma
  peça pode ter também uma ficha abandonada.
- Corrigido: `MLBU4286980046` não é mais truncado para `MLB4286980046`, que
  seria outro produto.
- Página `/up/` copiada direto do navegador, sem `wid`, agora funciona.
- Quatro testes com a URL real do item 5273337.

## v0.6.0 — 20/08/2026

**Travas contra casamento errado. Correção de um problema introduzido na v0.4.0.**

No lote #1, os 4 itens marcados como "prontos para aprovar" estavam **todos
errados**, numa conta de produção:

| Peça no ERP | O que o sistema casou |
|---|---|
| Corpo Distribuidor Bosch — R$ 10.011 | livro "Corpo a Corpo", de Alex Varenne |
| Válvula Dosadora MBB — R$ 9.063 | cuba de banheiro marca Japi |
| Reparo Unidade HEUI — R$ 569 | kit de alfinetes de costura |
| Módulo Eletrônico Bosch — R$ 9.745 | painel de esteira ergométrica |

Causa: a busca **só por descrição**, acrescentada na v0.4.0 para "melhorar a
cobertura", converteu "não encontrei" em "encontrei com confiança e está errado".
Todos vieram marcados como confiança baixa, mas mesmo assim chegaram à tela de
aprovação — bastava marcar "todos" para publicar um injetor diesel como livro.

Três travas:

- **Confiança mínima.** Só casamento por **part number** chega à aprovação.
  Casamento por descrição vira sugestão, não candidato. Configurável em
  `CONFIANCA_MINIMA` (padrão `alta`).
- **Categoria compatível.** O candidato precisa estar na árvore de *Acessórios
  para Veículos* (`MLB5672`). Livro, banheiro, costura e fitness são barrados
  automaticamente, com o caminho da categoria na mensagem. Configurável em
  `ML_CATEGORIAS_RAIZ`.
- **O vínculo manual também passa pelas travas.** Link colado para produto fora
  de autopeças é recusado com explicação — protege contra copiar o link errado.

- Novo status `sugestao_duvidosa`, para o que foi encontrado mas reprovado nas
  travas. Continua visível e aceita vínculo manual, mas não é oferecido para
  aprovação.
- Sete testes novos, escritos a partir dos quatro casos reais.

Consequência esperada: **a contagem de "prontos para aprovar" vai cair**, e isso
é o comportamento correto. Quatro casamentos errados valem menos que zero.

## v0.5.1 — 20/08/2026

**Separação de motivos e exportação do lote.**

Primeiro lote real analisado (20 itens da aba `Acima de R$1000,00`):
4 prontos (20%), 13 `catalogo_inativo` (65%), 3 erro (15%).

Os 65% estavam juntando dois problemas diferentes sob um rótulo só:

- `catalogo_inativo` — o produto existe no catálogo do ML mas está
  descontinuado. **Limitação do catálogo**; só o vínculo manual resolve.
- `sem_categoria` — o produto está ativo, mas não conseguimos determinar a
  categoria. **Falha nossa de detecção**, com conserto possível no código.

Sem separar, não dá para saber quanto do problema é do ML e quanto é nosso —
e portanto não dá para decidir onde investir esforço.

- Novo status `sem_categoria`, distinto de `catalogo_inativo`.
- Vínculo manual pelo link passa a valer também para `sem_categoria`.
- **Exportação em CSV** do lote inteiro (link na tela de conferência), com
  status, confiança, produto de catálogo e mensagem de erro por item. Abre no
  Excel com acento correto.

## v0.5.0 — 19/08/2026

**Vínculo manual pelo link do anúncio, e correção de erro mascarado.**

Diagnóstico do item `5273337`: a mensagem dizia *"não encontrei anúncios ativos
com esse termo"*, mas o anúncio existe no ML. A causa: **o Mercado Livre bloqueou
o endpoint `/sites/MLB/search` para aplicações (HTTP 403)**. O código capturava o
erro e devolvia lista vazia — fazendo "bloqueado" parecer "não encontrado".

- **Correção:** falha na busca de anúncios agora é reportada como falha, com o
  código HTTP. Erro mascarado como resultado vazio é pior do que erro visível,
  porque manda o diagnóstico para o lado errado.
- **Vínculo manual:** na tela de conferência, itens sem catálogo utilizável
  ganham um campo para colar o link do anúncio do ML. O sistema extrai o ID,
  consulta `/items/{id}` ou `/products/{id}` e lê o `catalog_product_id` e a
  `category_id` **direto da fonte** — sem adivinhação. O item passa a
  `aguardando_aprovacao` com confiança alta.
- Aceita as formas que dá para copiar do navegador: link de anúncio
  (`/MLB-123...-nome-_JM`), link de produto de catálogo (`/p/MLB123`), link com
  parâmetros de rastreio, ou o código solto.
- Se o anúncio colado não estiver vinculado ao catálogo, o sistema avisa que o
  item vai precisar de foto própria, em vez de falhar na publicação.
- Oito testes cobrindo a extração do ID, inclusive o caso de `MLB1747`
  (categoria) não ser confundido com anúncio.

Contexto: o bloqueio da busca pública é decisão do Mercado Livre, não limitação
do Águiahub. Vários desenvolvedores relatam o mesmo 403. O vínculo manual é a
forma de contornar sem depender desse endpoint.

## v0.4.0 — 19/08/2026

**Expansão das abreviações do ERP e pista de mercado.**

Caso que originou: o item `5273337`, descrito no ERP como `BBA ARLA EMITEC 12V`,
existe no Mercado Livre como **"Bomba De Arla 32 Emitec 12v"** — mas o sistema
não achava, porque procurava literalmente por "BBA", que ninguém escreve no ML.

- Novo arquivo `app/abreviacoes.py` com o dicionário de abreviações do ERP
  (`BBA`→Bomba, `CIL`→Cilindro, `VLV`→Válvula, `MBB`→Mercedes-Benz, e outras).
  **Para acrescentar uma abreviação basta editar esse arquivo** — nenhuma outra
  parte do código muda.
- A expansão é aplicada em dois lugares: no termo de busca do catálogo e no
  título gerado. `BBA ARLA EMITEC 12V` agora vira `Bomba Arla 32 Emitec 12V`.
- A busca no catálogo passou de 2 para até 4 tentativas, incluindo busca só pela
  descrição por extenso — antes, item cujo código não estivesse cadastrado como
  part number no ML nunca era encontrado.
- Ao percorrer candidatos em busca de um produto ativo, o limite subiu de 3 para
  8. Peça com muitos produtos de catálogo descontinuados tinha chance real de
  ficar de fora.
- **Pista de mercado:** quando não há produto de catálogo utilizável, o sistema
  agora busca anúncios ativos no ML e informa quantos existem, um exemplo de
  título e a faixa de preço praticada. Assim dá para distinguir "essa peça não
  vende no ML" de "vende, só falta foto".
- Abreviações de significado ainda não confirmado (`SMD`, `CI`, `CAT`, `TO`,
  `BTS`) são deixadas intactas de propósito — expandir no chute produziria busca
  errada, que é pior do que não expandir.
- Seis testes cobrindo a expansão, inclusive o caso real do item 5273337.

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
