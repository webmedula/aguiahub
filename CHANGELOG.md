# Histórico de versões — Águiahub

A versão aparece no cabeçalho da interface e em `GET /health`. Depois de cada
deploy, confira ali se o número bate com o da versão que você subiu.

---

## v0.28.0 — 10/09/2026

**A foto estava na página o tempo todo — o sistema é que não olhava.**

Relato do João, numa peça real (módulo PLD, `apolloonibus.com.br`): a tela da
ficha não falava nada sobre foto. Nem "achei", nem "achei mas não posso usar".
Silêncio — e ele teve que perguntar *"não está pegando a imagem? ou está?"*.

Não estava. E por dois motivos independentes, que valem registrar porque um
escondia o outro:

**1. O leitor de meta tag exigia os atributos numa ordem.** A expressão
antiga só casava `<meta property="og:image" content="...">`. Metade dos sites
escreve `<meta content="..." property="og:image">` — e aí a foto não existia
para o sistema. Agora cada `<meta>` é lida atributo por atributo, sem depender
de ordem, e `twitter:image` também vale.

**2. Ninguém olhava as `<img>` da página.** O extrator lia ficha estruturada
(JSON-LD) e meta tags, e parava aí. Site pequeno de autopeça quase nunca tem
JSON-LD — mas a foto está lá, numa `<img>`, que é justamente onde a pessoa a
vê. Agora, quando não há nada estruturado, o sistema varre as imagens da
página: aceita `data-src` e afins (é como plataforma de loja carrega imagem
preguiçosa), descarta logo, banner, ícone e selo de pagamento, ignora imagem
declarada pequena, e prefere caminho com cara de produto (`/img/p/`,
`/produto/`, `/uploads/`). A mesma foto em cinco tamanhos vira uma só.

### A tela nunca mais fica em silêncio sobre foto

Três estados, sempre um deles:

- **achei N, pode usar** — o site está autorizado;
- **achei N, mas o site não está autorizado** — com o botão de autorizar ali
  mesmo, que vale para todas as peças daquele fornecedor daqui em diante;
- **não achei nenhuma** — com a instrução do que fazer.

### Baixar a foto por endereço colado

Pedido do João: às vezes o sistema não acha e a pessoa que está olhando a
página acha em dois segundos. Agora dá para clicar na foto com o botão
direito, copiar o endereço da imagem e colar — na tela da ficha ou na tela da
peça. O sistema baixa, confere que é imagem de verdade, guarda como foto da
peça e ela passa a estar pronta para publicar.

**Decisão do João sobre a regra:** este caminho **baixa e registra a origem**,
em vez de exigir o site autorizado antes. Então o registro precisa ser bom: o
sistema guarda a **URL exata** de cada foto baixada, o domínio e a data — por
foto, não por peça, porque uma peça pode ter imagem de dois fornecedores
diferentes e a pergunta que importa é "de onde veio ESTA imagem". A tela da
peça mostra essa lista.

Efeito colateral bom: foto baixada de fora **deixou de ser chamada de "foto
tirada no estoque"** na lista e no cartão. Ela aparece como "foto de
apolloonibus.com.br" — chamar de foto do estoque esconderia exatamente a
informação que interessa se alguém reclamar.

### Um detalhe de domínio que evitou um erro sério

A foto daquela peça mora em `apolloonibus.fbitsstatic.net` — a CDN da
plataforma de e-commerce, não o site da loja. Se a autorização olhasse o
domínio da imagem, autorizar essa CDN significaria autorizar **todas as lojas
que usam a FBits**. O sistema decide pelo domínio da **página**, que é a loja
— e agora há teste trancando isso.

### Também nesta versão

O aviso *"já aconteceu neste projeto de um corpo distribuidor casar com um
livro"* saiu das telas, a pedido do João. Contar história interna do projeto
para o pessoal da Águia é estranho, e o cartão de "o código não bate" já diz o
que a pessoa precisa saber. A regra continua igual no código, com o mesmo
teste — mudou só o que o cliente lê.

Doze testes novos, 256 no total.

---

## v0.27.0 — 09/09/2026

**Três telas: a lista, a peça, o cartão.**

Esta versão é o redesenho combinado com o João, que está entregando o sistema
para a Águia Diesel. O pedido dele foi direto: *"quero um controle maior do
processo... ver cada item da planilha e decidir qual a melhor forma de incluir
o anúncio"*, e *"a publicação o mais intuitiva possível"*.

Isso é uma crítica correta a uma escolha minha. **A fila mostrava uma peça por
vez e escolhia a próxima pelo critério dela** (maior valor parado). Para quem
conhece o estoque, isso tira o controle: não dá para ver o conjunto, não dá
para escolher, não dá para pular. E, para quem vai operar na Águia, o sistema
tinha oito telas — ferramenta de desenvolvedor, não de estoquista.

### A lista (`/lotes/{id}/lista`)

Todas as peças da planilha, uma por linha, com foto, situação, origem do dado
e valor parado. Filtro por situação, por faixa de preço e busca por código,
descrição ou aplicação. **Seleção múltipla para publicar em bloco.**

A situação de cada peça é calculada em **um lugar só** (`app/painel.py`), em
SQL, para a tela filtrar e contar 1.687 linhas sem carregar tudo na memória:

    pronta           tem foto e dados — publica em bloco, sem abrir
    precisa de você  o sistema achou algo, mas não teve certeza
    sem foto         nenhuma origem conhecida tem essa peça
    no ar            publicada, com código, link e data guardados
    descartada       alguém decidiu não anunciar — dá para reabrir
    fora da fila     a triagem barrou antes (sem código utilizável)

É essa classificação que faz "rápido" e "seguro" conviverem: o óbvio sai em
bloco, o duvidoso exige um par de olhos. Sem ela, a segurança passaria a
depender da disciplina de quem está com pressa.

### A peça (`/itens/{id}`)

Tudo sobre uma peça num lugar só: o que o sistema encontrou e de onde, os
links candidatos que ele guardou, colar um link novo, subir foto da câmera,
editar título e descrição — e o cartão do anúncio embaixo. Antes isso estava
espalhado em três telas e era preciso saber qual botão levava onde.

### O cartão

A peça **como ela vai aparecer no anúncio**: foto, título, preço, quantidade,
ficha e descrição, com uma frase dizendo exatamente o que o botão faz. Sem
JSON, sem "payload", sem termo técnico. Vale para uma peça e para um bloco de
cinquenta — a tela de conferência mostra todos os cartões antes de qualquer
coisa subir, e depois vira o comprovante, com o link de cada anúncio no ar.

Há teste garantindo que o cartão e a publicação usam **as mesmas funções** de
título e descrição: um cartão que mostra uma coisa e publica outra seria pior
do que não ter cartão nenhum.

### Configuração (`/config`)

Fontes autorizadas, sites de busca, esteira, medição, conexão com o ML e
diagnóstico saíram do caminho e viraram uma tela só, ajustada na entrega. Quem
só quer anunciar as peças do dia nunca precisa abrir.

### Um bug antigo que esta versão corrigiu

O status `foto_do_operador` existia no código desde a v0.9 e **nenhuma rota o
gravava**: a peça ganhava foto tirada no estoque e continuava contada como
"sem foto", invisível na lista de prontas. Agora enviar foto marca a peça, e
apagar a última devolve — senão ela ficaria listada como pronta e falharia na
publicação.

Dezenove testes novos, 244 no total. O fluxo inteiro — lista, peça,
conferência, publicação, comprovante — foi verificado ponta a ponta com o app
rodando de verdade, não só no pytest.

---

## v0.26.0 — 08/09/2026

**Medir de onde viria a foto — a pendência mais antiga do projeto.**

Contexto: 1.682 peças paradas na fila, 5 anúncios publicados. Conversando
sobre o objetivo ("criar anúncios dos produtos que estão na lista"), ficou
claro que o gargalo não é mais software. Um anúncio precisa de cinco coisas;
quatro a planilha dá de graça e a quinta o ML adivinha. Sobra a **foto** — e é
só nela que as 1.682 estão presas.

A pergunta que decide o próximo mês de trabalho é: **quantas dessas peças
conseguiriam foto sem ninguém fotografar?** Ela estava anotada como pendência
desde a v0.1 ("medir a cobertura do catálogo") e nunca tinha sido respondida.

Nova tela `/lotes/{id}/cobertura`. Ela consulta o catálogo do Mercado Livre e
o catálogo da loja da Águia peça por peça, numa amostra, e responde:

- quantas **herdam foto do catálogo do ML** (custo de foto: zero);
- quantas **têm foto na loja da Águia** (foto nossa, sem licença de ninguém);
- quantas **dependem da câmera do estoque**.

E projeta para a fila inteira **faixa de preço por faixa de preço**, porque a
taxa da peça de R$ 10 mil não vale para a de R$ 35 — e é exatamente essa
diferença que decide o que vale a pena anunciar.

### O que ela não faz

**Não escreve nada.** Nenhum status muda, nenhuma peça fica "pronta", nada é
publicado. Um diagnóstico que altera o diagnosticado não serve para decidir
coisa nenhuma — e no meio de uma fila com R$ 2,4 milhões parados, decidir
errado custa caro. Um teste lê o código-fonte do módulo e falha se qualquer
função de escrita aparecer lá dentro.

**Não consulta as 1.682.** Seriam milhares de requisições ao ML para responder
o que uma amostra responde. São 150 peças por padrão (~7 pontos percentuais de
margem, suficiente para distinguir 3% de 30%), **sorteadas dentro de cada
faixa de preço** — sorteio simples pegaria quase só peça barata, já que 914
das 1.687 estão abaixo de R$ 100, e responderia bem a pergunta que menos
importa. O sorteio é reprodutível: a mesma fila dá a mesma amostra.

### Detalhes

- Roda em segundo plano, como a esteira: 150 consultas ao ML passam de
  qualquer timeout de requisição. A tela se atualiza sozinha.
- Loja fora do ar não derruba a medição — a coluna dela fica zerada e a tela
  avisa que o número está subestimado.
- A tela mostra exemplos concretos dos dois lados: peças que sairiam sem foto
  (para conferir se o casamento com o catálogo está correto — casar peça
  errada continua sendo o pior erro possível) e as mais caras que dependem de
  foto, que é por onde começar a fotografar.
- Vale repetir a medição depois de autorizar marcas novas em `/fontes`: a
  resposta muda, e o histórico fica guardado para comparar.

Dez testes novos, 225 no total.

---

## v0.25.0 — 08/09/2026

**A esteira: o sistema trabalha a lista sozinho.**

Pedido do João, depois de olhar um anúncio publicado: *"a questão maior é
pegar a lista e realmente automatizar de forma que fique mais fácil gerar os
anúncios a partir da lista da planilha"*.

Até aqui todas as peças do quebra-cabeça existiam — cruzamento com a loja
(v0.16), extrator de ficha (v0.20), fontes autorizadas (v0.22), pré-anúncio
(v0.23), busca na internet (v0.24) — mas **cada uma exigia um clique humano,
peça por peça**. Isso é razoável para dez peças e impossível para 1.687. O
gargalo nunca foi a capacidade do sistema; era a quantidade de cliques.

Agora tem `/lotes/{id}/esteira`: um botão que solta o processamento em massa
no servidor. Para cada peça da fila, da mais cara para a mais barata, ela faz
sozinha exatamente o que a pessoa fazia à mão:

1. cruza o lote com a loja da Águia (foto própria, sem questão de licença);
2. procura o código nos sites cadastrados em `/sites`;
3. lê a ficha dos candidatos mais promissores;
4. confere se o **código bate de verdade** dentro da ficha;
5. aproveita só o que a licença permite:
   - domínio autorizado em `/fontes` → ficha, fotos e descrição, e a peça sai
     **pronta para publicar**;
   - qualquer outro site → só o **fato técnico** (aplicação, marca), que é da
     peça e não tem dono; a foto continua esperando a câmera do estoque.

### As três travas que ela não contorna

- **Não publica nada.** Termina em "pronta para publicar"; quem manda para o
  Mercado Livre continua sendo a pessoa, na tela de prontas. Publicação
  automática em 1.687 peças transformaria um casamento errado num estrago
  irreparável na conta real da Águia Parts. Há teste lendo o código-fonte do
  módulo para garantir que nenhuma função de publicar entrou ali.
- **Não adivinha peça.** Só aproveita quando o código do ERP aparece na ficha
  do site. Nome parecido não vale — é a regra que existe desde que um corpo
  distribuidor casou com um livro na v0.4.0, e ela pesa mais ainda quando não
  há ninguém olhando.
- **Não usa foto de quem não autorizou.** A esteira chama as mesmas funções
  que a tela usa; a lista de `/fontes` continua sendo a única porta.

### O que sobra para a pessoa

Para cada peça que a esteira não resolveu, ela **guarda os links que
encontrou** e diz por que parou ("achei 4 páginas, mas o código não apareceu
na ficha de nenhuma"). A tela mostra essa lista ordenada por valor parado,
com o link do lado. É a diferença entre *"1.687 peças para pesquisar"* e
*"120 peças para fotografar, com a página já aberta"*.

### Detalhes que decidem se isso funciona no VPS

- **Retomável.** Peça que já passou é pulada. Se o EasyPanel reiniciar o
  container no meio (a execução leva horas), é só soltar de novo.
- **Execução órfã é encerrada no start.** A esteira vive num task em memória:
  sem essa limpeza, um container derrubado deixaria a tela mostrando uma
  esteira fantasma para sempre e travaria o botão de soltar outra.
- **Três peças por vez, teto de tempo por peça, e botão de parar.** São
  requisições a sites de terceiros: ser educado aqui é o que evita o IP do
  VPS ser bloqueado no meio das 1.687.
- **Falha isolada.** Loja fora do ar, site pendurado ou ficha ilegível
  derrubam aquela peça, não a esteira.
- **Começar pequeno.** A tela oferece rodar nas 50 ou 200 de maior valor
  antes de soltar na fila inteira.

### O gargalo verdadeiro, dito na cara

A tela mostra, lado a lado, **onde ela procura** e **de quem pode usar foto** —
porque é a segunda lista que decide quantas peças saem prontas sozinhas. Com
um domínio autorizado, a esteira traz ficha técnica de muita coisa e foto de
quase nada. Cada marca que a Águia representa e cadastra em `/fontes` vira
peça a menos para fotografar.

Vinte e dois testes novos, 215 no total. Um deles nasceu de um bug que só o
teste ponta a ponta pegou: a limpeza de execuções órfãs tinha SQL inválido e
derrubava a aplicação no start — o pytest passava, e o deploy não subiria.

---

## v0.24.0 — 27/08/2026

**O sistema procura a peça na internet sozinho.**

Pedido do João: *"acho que o sistema poderia ter a opção de procurar na
internet em geral se acha o produto da lista"*.

O que existia até aqui era só a metade de baixo desse fluxo. O extrator
(v0.20.0) lê a ficha de qualquer site — mas **só depois que alguém tinha
achado o link e colado na tela**. Achar o link era trabalho manual, peça por
peça, com um Google aberto do lado. Com 1.687 itens, essa era a parte cara.

Agora a fila tem o botão **"Procurar esta peça na internet"**: o sistema
consulta a busca interna dos sites que conhece pelo código do ERP e devolve
as páginas candidatas. Cada uma leva para a mesma tela de ficha de sempre —
**nada é aproveitado sem a confirmação da pessoa**.

### Onde ele procura

Duas fontes, e a segunda vem desligada:

1. **Sites conhecidos** (padrão, sem chave nenhuma). Nova tela `/sites`, no
   mesmo espírito de `/fontes`: acrescentar o site de um fabricante não pode
   exigir mexer no EasyPanel e refazer o deploy. Basta o domínio
   (`bosch.com.br`) — o sistema descobre sozinho o endereço de busca do site
   na primeira peça, tentando os padrões de WooCommerce, WordPress, VTEX e
   Magento, e **grava o que funcionou**. Da segunda peça em diante aquele
   site custa uma requisição em vez de cinco. Quem tiver endereço de busca
   fora do comum pode cadastrar com `{q}` no lugar do código.

   Os domínios já autorizados em `/fontes` entram de graça: se a Águia
   declarou representar aquela marca, é onde a peça mais tem chance de estar
   — e onde a foto poderá ser aproveitada depois.

2. **API de busca** (opcional). Com `BUSCA_API_KEY` configurada, consulta
   Brave Search ou Google Custom Search e alcança a internet inteira. Sem a
   chave, esse caminho fica simplesmente inativo e o resto funciona igual. As
   duas provedoras têm faixa gratuita que cobre o uso da Águia.

**O que não foi feito, de propósito:** raspar página de resultado do Google
ou do Bing. Quebra sem aviso, faz o IP do VPS ser bloqueado e vai contra os
termos dos dois — seria uma funcionalidade que funciona no teste e morre no
`31.97.87.68`. É por isso que a busca ampla passa por chave de API.

### O que a busca não muda

Nada nas regras de direito autoral. Este caminho descobre **endereços**;
quem lê foto e texto continua sendo o extrator, com as mesmas regras: foto e
descrição só de domínio autorizado, fato técnico de qualquer um. A tela
`/sites` avisa isso na cara: cadastrar um site ali diz *onde procurar*, não
*de quem posso usar foto*. Site de concorrente pode ser ótimo para conferir
ficha técnica e continua proibido de ceder imagem.

Anúncio do Mercado Livre é descartado dos resultados da API: o ML bloqueia a
leitura (403) e reaproveitar foto de lá é exatamente o que derruba conta.

O `robots.txt` de cada site é respeitado também na página de busca — a regra
vale para tudo que a aplicação lê sozinha, não só para a ficha do produto.
Por isso `extrator._robots_permite` virou `extrator.robots_permite`.

### Detalhes que já custaram caro antes

- **Código pontuado.** A busca casa `0281036486` do ERP com `0.281.036.486`
  do site, reusando `loja.variantes_de_codigo`. É o mesmo problema que fazia
  o item de R$ 47.864 aparecer como "não tenho na loja" até a v0.14.
- **Peça sem código utilizável** não dispara requisição nenhuma: avisa que
  sem código não há o que procurar e manda tirar foto da prateleira.
- **Ordem dos candidatos:** quem tem o código no endereço ou no texto do link
  vem primeiro; a tela diz, candidato a candidato, se o código bate ou não —
  em vez de deixar parecer que o sistema conferiu a peça. Ele não conferiu.
- **Teto de 8 sites por busca**, para a tela não ficar pendurada e para não
  bater na porta de dez sites em cada uma das 1.687 peças.

Novos arquivos: `app/busca.py`, `app/templates/candidatos.html`,
`app/templates/sites.html`, `tests/test_busca.py` (25 testes novos; 199 no
total).

---

## v0.23.0 — 21/08/2026

**Tela de pré-anúncio: editar texto e trocar foto antes de publicar.**

Quatro pedidos do João na mesma mensagem, todos ligados:

1. **Bug: "aproveitar os dados técnicos" não levava a lugar nenhum.** A rota
   `/aplicar-pesquisa` salvava os dados e voltava para `/fila/{lote}` — que
   mostra a peça de MAIOR valor pendente, não necessariamente a mesma que
   acabou de ser pesquisada. O efeito parecia "não aconteceu nada". Agora as
   duas rotas de pesquisa (`usar-ficha` e `aplicar-pesquisa`) levam direto
   para a tela nova de pré-anúncio **desta peça**.

2. **Descrição de site autorizado agora pode ser usada.** Até aqui, mesmo com
   o domínio autorizado, só a foto entrava — a descrição ficava só de
   referência na tela, por entender que direito sobre imagem não é direito
   sobre texto de vendas. O João confirmou explicitamente que a autorização
   cobre "revenda e uso das imagens dos produtos" e pediu "todas as
   informações". Mudou para: **domínio autorizado → foto e descrição podem
   ser usadas**, sempre como rascunho editável. Site **não** autorizado
   continua só com fato técnico (aplicação, marca) — nunca texto nem foto.

3. **Tela de pré-anúncio nova** (`/itens/{id}/preparar`): mostra o título e a
   descrição que vão para o anúncio (vindos da loja, do site autorizado, ou
   montados do ERP) com campo para editar os dois antes de publicar. Nada
   aqui publica nada.

4. **Foto própria substitui a do site, não soma.** A mesma tela deixa tirar
   foto própria; se houver alguma, ela sozinha vai para o anúncio no lugar
   das fotos da loja/site — dá para começar o pré-anúncio com a foto
   encontrada e trocar depois pela foto de verdade da peça, sem editar código
   nem mexer em duas telas diferentes. Também dá para remover uma foto do
   site individualmente sem precisar tirar foto própria.

Por baixo do capô, `publicar_da_loja` deixou de exigir foto da loja — agora
aceita publicar só com foto própria também, então virou o caminho único para
qualquer combinação de fonte de foto. `titulo_editado` e `descricao_editada`
são as duas colunas novas que guardam a edição do operador; elas têm
prioridade sobre o que seria montado automaticamente, mas nunca entram na
publicação por catálogo (lá o título e a ficha são do próprio Mercado Livre).

Onze testes novos, 174 no total.

---

## v0.22.0 — 21/08/2026

**"Sempre estamos acrescentando novas marcas."**

Então a lista de fontes autorizadas não pode viver numa variável de ambiente.
Do jeito que estava, cada marca nova exigia abrir o EasyPanel, editar a
variável e refazer o deploy. Na prática ninguém faria isso no meio do
trabalho — o operador ia acabar pulando peça que dava para anunciar.

Passou para dentro do sistema, gravado no banco:

- Tela nova em `/fontes`: acrescentar, ver e remover marcas. Sem deploy, sem
  reiniciar nada.
- Aceita o **link colado**, não só o domínio limpo: cole
  `https://www.bosch.com.br/produtos/modulo?ref=1` e o sistema guarda
  `bosch.com.br`. Subdomínio entra junto, que é onde as imagens costumam
  estar (`cdn.bosch.com.br`).
- Campo de observação para anotar a marca — daqui a um ano, `denso.com.br` na
  lista sem contexto não diz nada.
- **Botão "Autorizar este site" na própria tela de pesquisa.** Quando a foto
  aparece bloqueada, dá para liberar ali mesmo e seguir, em vez de abandonar
  a peça para ir configurar em outro lugar.
- A variável `FONTES_IMAGEM_AUTORIZADAS` continua valendo e soma com a lista
  do banco — quem já configurou não perde nada. A tela mostra as duas origens
  separadas.

Detalhe de segurança: se a leitura do banco falhar, a lista volta **vazia**,
não liberada. Erro de infraestrutura não pode virar permissão.

Seis testes novos, 164 no total.

---

## v0.21.0 — 21/08/2026

**A pesquisa passa a alimentar o anúncio.**

O João informou que a Águia tem autorização de revenda e de uso das imagens
dos produtos que vai anunciar. Faltava o sistema aproveitar isso: até aqui a
pesquisa lia a ficha mas não tinha caminho para virar anúncio.

- Botão **"Usar esta ficha e as N fotos"** na tela de pesquisa. Deixa a peça
  pronta para publicar, com as fotos do site, aplicação e marca preenchidas.
  Continua tendo o botão menor de só aproveitar os dados técnicos.
- **A origem fica gravada.** Cada item guarda em `fonte_dados` o domínio de
  onde vieram as imagens. Se um dia chegar reclamação sobre a imagem de algum
  anúncio, dá para responder de onde veio em vez de adivinhar — com 1.334
  peças, isso deixa de ser detalhe.
- `FONTES_IMAGEM_AUTORIZADAS` aceita `*` para liberar qualquer fonte. Existe
  porque a decisão é do dono do negócio; continua sendo escolha explícita e
  nunca o padrão.

*O que continua fora, e por quê.* A **descrição** do site não vai para o
anúncio, mesmo com a fonte autorizada. Licença de uso da imagem do produto e
direito sobre o texto de vendas que outra empresa escreveu são coisas
diferentes, e o segundo raramente está no contrato de revenda. O texto do
anúncio continua sendo montado a partir dos dados da Águia. Tem teste
garantindo.

A distinção que vale guardar: a licença de revenda cobre o material do
**fabricante**. A fotografia que outro revendedor produziu no estúdio dele é
obra dele — ser oficial Bosch não dá direito sobre a foto de outra loja. Por
isso a recomendação continua sendo listar os domínios dos fabricantes em vez
de usar o curinga.

Cinco testes novos, 158 no total.

---

## v0.20.1 — 21/08/2026

**Dá para ver quais fontes de imagem estão autorizadas.**

O João informou que a Águia é revendedora oficial. Isso muda o quadro para o
material dos fabricantes que ela representa — e a tela precisava mostrar se a
configuração pegou, em vez de obrigar a testar colando um link.

- A tela inicial lista os domínios de `FONTES_IMAGEM_AUTORIZADAS`. Vazio, diz
  que nenhum site cede imagem e explica como autorizar.
- Mesmo espírito do número da versão no cabeçalho e do aviso de volume: o
  estado do sistema fica visível, não adivinhado.

Um teste novo, 153 no total.

---

## v0.20.0 — 21/08/2026

**Ler a ficha da peça em qualquer site.**

Ideia do João, e boa: o Mercado Livre fechou os dados de terceiros, mas a
ficha técnica das peças está publicada em site de fabricante, de fornecedor e
de concorrente. Ler isso encurta o trabalho de identificar a peça.

Campo novo na fila: cole o link da página do produto em **qualquer site**. O
sistema lê e mostra uma comparação lado a lado — o que está na planilha do
ERP contra o que está no site.

- **Lê dado estruturado, não HTML adivinhado.** Prioriza o JSON-LD
  `schema.org/Product` e as meta tags OpenGraph, que os próprios sites
  publicam para aparecer no Google. Muito mais estável do que raspar o HTML,
  que muda a cada troca de tema.
- **Confere o código.** Diz na cara se o código da planilha aparece na ficha
  do site. Quando não aparece, avisa em amarelo e lembra que a confirmação é
  da pessoa. Código com menos de 5 caracteres nunca confere — foi o que
  produziu os quatro casamentos ruins da v0.4.0.
- **Aproveita aplicação e marca**, que são os campos que faltam na planilha e
  melhoram o título e a ficha do anúncio. Só preenche o que está vazio: não
  sobrescreve dado da Águia.
- **Respeita o robots.txt** do site. É o sinal que o site publica dizendo o
  que aceita que robô leia; ignorar é o caminho para IP bloqueado e problema
  jurídico.

*Sobre foto e texto — a parte que não fiz como pedido.*

O João pediu dados, descrição e fotos. Dados eu trago; foto e texto de site
alheio, não — e a razão não é jurídica no abstrato, é concreta: foto e texto
descritivo são obra de quem publicou, e o Mercado Livre derruba anúncio por
denúncia de cópia e suspende conta de vendedor reincidente. Seria construir
1.334 anúncios em cima da conta que pode cair.

O que fiz no lugar:

- **Descrição** aparece na tela como *referência* — para conferir se é a
  mesma peça — e tem teste garantindo que nenhum caminho de publicação a
  utiliza. O texto do anúncio continua sendo montado a partir dos dados da
  Águia.
- **Foto** tem uma exceção, e ela é do João decidir: os domínios listados em
  `FONTES_IMAGEM_AUTORIZADAS` (vazio por padrão). É onde entra o fabricante
  ou fornecedor de quem a Águia é distribuidora e cujo material tem direito
  de usar. Quem sabe o que foi contratado é o dono do negócio; o sistema
  respeita a lista. Fora dela, a tela diz quantas fotos existem, por que não
  são usadas, e como autorizar o domínio se for o caso.

Dezesseis testes novos, 152 no total.

---

## v0.19.0 — 21/08/2026

**"Não adianta essa mensagem, não sei como achar essa página do produto."**

Relato justo, e o problema é maior que a mensagem: **essa página não resolve
nada**. A mensagem mandava caçar um endereço com `/p/` ou `/up/` como se
fosse a solução. Em todo o projeto, nenhum link do Mercado Livre — de
anúncio, de produto, com `wid`, com `/p/`, com `/up/` — trouxe informação
alguma. O ML fechou os dados de terceiros para aplicações e não vai reabrir
porque o operador copiou outro endereço.

Mandar alguém procurar uma saída que não existe é pior do que dizer que não
tem saída. Isso gastou tempo do João mais de uma vez.

- A mensagem agora diz a verdade: **nenhum endereço do ML vai funcionar**,
  não adianta procurar outro.
- E não termina em beco: a tela mostra a peça e as **duas saídas que
  funcionam**, com botão. Procurar na loja da Águia (já com o código na
  busca) ou voltar para a fila e fotografar. Mais o lembrete de que existe o
  *deixar para o João* quando não for nenhuma das duas agora.
- O botão **"Procurar no Mercado Livre"** saiu da fila. Ele só levava a
  pessoa a copiar um link que ia falhar.
- O campo de link passou a se chamar *"Está na loja da Águia? Cole o link
  aqui"* — é o que ele de fato faz.

Três testes novos, incluindo um que varre os templates garantindo que a
instrução impossível não volte. 136 no total.

---

## v0.18.0 — 21/08/2026

**Primeiro anúncio no ar.**

    https://www.mercadolivre.com.br/modulo-de-injecao--fiat-toro--jeep-renegade
    --jeep-compass/up/MLBU4863355537

Módulo de injeção Fiat Toro / Jeep Renegade / Jeep Compass, R$ 3.190,94, 15
unidades. Saiu do `Itens parados.xls` e chegou ao Mercado Livre sem ninguém
digitar nada.

*O susto das fotos, e o erro que quase cometi.* O anúncio apareceu vazio e
pausado. Diagnostiquei firewall da loja bloqueando o robô do ML — eu mesmo
tinha levado 403 do site nos testes — e reescrevi a publicação para baixar
cada imagem aqui e subir pelo `/pictures/items/upload`.

Estava errado. O João avisou que a foto entrou sozinha minutos depois: o ML
**busca a imagem em segundo plano**, e o anúncio fica sem foto (e pausado) até
terminar. Revertido. A publicação continua mandando `pictures: [{"source":
url}]`, que é o caminho mais barato — não faz o WordPress da Águia servir
1.334 peças de imagens, nem duplica tráfego.

Fica um teste trancando esse caminho, para eu não "consertar" de novo o que
não está quebrado.

*O que sobrou de útil do engano:*

- **Botão "reenviar fotos"** nas peças publicadas, na tela de decididas. Baixa
  a imagem daqui e sobe pelo endpoint oficial, sem depender de o ML alcançar
  a loja — serve para o anúncio que continuar sem imagem depois da espera.
  Também reativa o anúncio, já que o ML pausa quem está sem foto.
- Funciona tanto com foto da loja quanto com foto tirada no estoque.
- `baixar_foto()` recusa resposta que não é imagem — a loja atrás de firewall
  devolve HTML de bloqueio com status 200, e subir isso como "foto" daria um
  erro bem mais confuso lá na frente.
- A tela de publicação agora avisa que a foto pode demorar alguns minutos, em
  vez de deixar a pessoa achar que deu errado.

Cinco testes novos, 133 no total.

---

## v0.17.0 — 21/08/2026

**A terceira camada da validação do ML: o título não vai mais.**

A resposta do validador foi clara onde as anteriores não eram:

    The fields [title] are invalid for requested call.

No modelo **User Product**, o Mercado Livre monta o título do anúncio a partir
do `family_name` e dos atributos. Mandar `title` junto é erro. A cascata foi:

1. `[family_name]` faltando → passei a mandar (v0.14.0).
2. `[title]` inválido → tenho que parar de mandar (agora).

O `title` some do payload de anúncio próprio; o texto que ele levava passa a
ir no `family_name`. Publicação por catálogo não muda — lá nunca houve
`title` nem `family_name`.

**Publicar virou um clique só.** A pedido do João, o campo onde era preciso
digitar `PUBLICAR` saiu de todas as telas. Fica o aviso do que vai acontecer
e a confirmação do navegador. Vale registrar o que isso significa: a aplicação
está numa URL pública **sem senha**, e agora quem tiver o endereço publica
anúncios reais na conta da Águia Parts em dois cliques. Se em algum momento
quiser fechar, basta definir `APP_PASSWORD` no EasyPanel — o mecanismo já
existe e está desligado.

**Um teste que passava pelo motivo errado.** O que verificava a exigência da
palavra `PUBLICAR` batia, na verdade, no erro de "conecte a conta" — ele
teria continuado verde mesmo com a trava toda aberta. Substituído por dois
que checam o que importa agora: sem conta conectada nada é publicado, e com
conta conectada o botão publica direto. Mais um que varre os templates
garantindo que nenhum campo de confirmação sobrou por engano.

Cinco testes novos, 128 no total.

---

## v0.16.0 — 21/08/2026

**"Não sei quais peças já foram decididas, e não tenho como revisar."**

Três problemas relatados de uma vez, e o terceiro muda o desenho do sistema.

*Ver o que já foi decidido.* Tela nova em `/lotes/{id}/decididas`: lista tudo
que saiu da fila, com a situação, a data e o valor parado. Filtra por
situação, procura por código ou descrição. As bolinhas de contagem no topo da
fila viraram links — clicar em "2 com erro" abre exatamente essas duas.

*Revisar.* Botão **rever** em cada peça, que devolve para a fila. Guarda o
que já tinha sido descoberto — dados da loja, fotos, catálogo — e apaga só o
veredito; refazer a busca à toa seria castigo, não correção. Peça já
publicada no ML **não** volta: desfazer aqui não apagaria o anúncio de lá, e
a tela estaria mentindo sobre o estado real.

*Escolher as peças na planilha.* Esta é a mudança de fundo, e a razão é
simples: **quem conhece o estoque é o João, não o sistema**. O Águiahub sabe
ordenar por dinheiro parado, mas não sabe que uma peça de R$ 20 mil é
encalhe insalvável e outra de R$ 300 sai toda semana.

Agora, se a planilha tiver uma coluna chamada `Anunciar` (ou `Publicar`,
`Selecionar`, `Marcar`, `Subir`, `X`, `OK`), **só as linhas marcadas entram
na fila**. Marca-se com `x`, `sim`, `1` ou `ok` — a grafia é solta de
propósito, é coluna digitada à mão. Sem a coluna, nada muda: a fila vem
inteira, como antes.

As não marcadas não somem — ficam listadas no fim da tela de decididas, com
botão **trazer para a fila**, para quando mudar de ideia sobre alguma sem
precisar reenviar a planilha. E elas não entram na barra de progresso: não
são trabalho feito nem trabalho pendente.

A tela de prévia diz qual dos dois casos é o seu antes de montar a fila, com
o número de linhas marcadas.

Quinze testes novos, 123 no total.

---

## v0.15.0 — 21/08/2026

**"body.invalid_fields" não é uma mensagem — é uma porta fechada.**

O módulo da Fiat Toro chegou ao `POST /items` e voltou com isso e nada mais.
O texto é um rótulo de validação do Mercado Livre, não uma explicação: não dá
para agir sobre ele. E cada rodada de adivinhação custava um deploy inteiro,
com risco de criar anúncio pela metade numa conta real.

*Parei de adivinhar e coloquei o ML para responder.*

- **Botão "testar" em cada peça pronta.** Manda o anúncio para o validador do
  ML (`POST /items/validate`) e mostra a resposta. **Nada é publicado** — tem
  teste automatizado garantindo que esse caminho nunca chama `POST /items`.
  Se a aplicação não tiver o validador liberado, a tela diz isso em vez de
  fingir que está tudo certo.
- **A tela mostra o payload exato** que seria enviado, campo por campo, e a
  resposta crua do ML. É o que dá para copiar e mandar para mim.
- **Categoria folha.** O ML só publica na última categoria da árvore. Uma
  categoria de meio — 'Acessórios para Veículos' — é recusada exatamente com
  `body.invalid_fields`, sem dizer qual campo. Agora isso é conferido antes e
  explicado em português.

*Por que a mensagem veio vazia.* Duas falhas minhas na leitura do erro:
`mensagem_amigavel()` lia apenas `cause[].message`, e o ML às vezes manda só
`code` e `references` — a lista voltava vazia e sobrava o rótulo pelado. E
quando o ML de fato não detalha, eu descartava o corpo em vez de mostrá-lo.
Agora lê `message`, `code` e `references`, e quando não há causa nenhuma
devolve a resposta inteira.

- Toda falha de publicação passa a trazer, num "detalhe" recolhível, a
  resposta crua do Mercado Livre, mais um atalho para testar aquela peça.

Sete testes novos, 108 no total.

---

## v0.14.0 — 21/08/2026

**Primeira publicação de verdade, e o Mercado Livre mudou o modelo.**

O injetor A2C59517051 foi a primeira peça a chegar até o `POST /items`. O ML
recusou com:

    The body does not contains some or none of the following properties
    [family_name]

O Mercado Livre migrou os anúncios sem catálogo para o modelo **User
Product** — os tais `MLBU` que já tinham aparecido antes, quando a busca do
catálogo só devolvia fichas antigas. Agora, para criar anúncio próprio, é
preciso mandar ou o `user_product_id` de um produto que já exista na conta,
ou o **`family_name`**: o nome da família que o ML cria em nome do vendedor.
É campo de topo, não vai dentro de `attributes`, e respeita o mesmo limite de
60 caracteres do título.

- `aplicar_user_product()` acrescenta `family_name` nos três caminhos de
  anúncio próprio (foto da loja, foto do operador, e o payload genérico).
  Num lugar só — não quero descobrir num quarto caminho que esqueci.
- **Garantia junto.** É a exigência seguinte da mesma validação, e não faz
  sentido descobrir isso num segundo deploy. Vai `WARRANTY_TYPE` = *Garantia
  do vendedor* e `WARRANTY_TIME` = *90 dias*, ajustáveis por
  `ML_GARANTIA_TIPO` e `ML_GARANTIA_PRAZO` no EasyPanel, sem mexer no código.
- Publicação **por catálogo** continua sem `family_name` nem garantia — lá a
  identidade do produto vem do catálogo e mandar os nossos causaria conflito.
- Se o erro voltar a aparecer, a mensagem na tela agora explica o que é e
  manda conferir a versão que está rodando, em vez de repetir o texto cru
  do ML.

Sete testes novos, 101 no total.

---

## v0.13.0 — 21/08/2026

**"Mostra 353 decididas. Mas como sei quais são e como publicar elas?"**

Duas respostas, e a primeira é que o número estava errado de novo — na
direção contrária.

*Os 353 não eram trabalho feito.* Eram os itens que a **triagem** descartou
antes da fila começar, por não ter código utilizável (status `sem_dado`).
Consertando o contador na v0.12.0 eu passei a somar tudo que não fosse
`na_fila`, e varri esses para dentro. A fila nasceu marcando 21% de
progresso sem ninguém ter clicado em nada. Agora eles saem da conta inteira
— não são feitos nem pendentes — e aparecem numa frase à parte: *"outras
353 ficaram fora da fila por não ter código utilizável"*.

*Faltava a tela.* Não existia lugar nenhum para ver as peças prontas nem
para publicá-las em lote — a única publicação possível era uma a uma, dentro
da fila. Agora tem:

- Faixa verde no topo da fila: **"N peças prontas para publicar"**, com botão.
- Tela nova em `/lotes/{id}/prontas`: lista com foto, título, código, preço,
  estoque e de onde veio a foto (loja da Águia, foto tirada no estoque, ou
  catálogo do ML). Ordenada pela peça de maior valor parado primeiro.
- Seleção por caixinha, *marcar todas*, e **publicar as selecionadas de uma
  vez** — cada peça sai pelo caminho certo conforme o próprio status.
- Continua exigindo digitar `PUBLICAR`, mais a confirmação do navegador. A
  conta é real e o preço vem da planilha.
- A conta do Mercado Livre é conferida **uma vez, antes de tudo**. Se
  estiver bloqueada, 40 peças falhariam com a mesma mensagem — melhor dizer
  uma vez e não publicar nada.
- O resultado volta peça por peça, com link do anúncio no ML quando deu
  certo e o motivo quando não deu.

Seis testes novos, 94 no total.

---

## v0.12.0 — 21/08/2026

**O contador não andava, e o trabalho corria risco de sumir.**

Duas coisas, a partir da tela que o João mandou com "0 de 1333" depois de já
ter decidido peças.

*O contador.* A tela somava uma lista fixa de status para saber o que já
tinha sido feito — e nessa lista estava `vinculado`, que não existe em lugar
nenhum do sistema. Os status que o operador realmente produz
(`pronto_com_fotos`, `foto_do_operador`, `aguardando_aprovacao`) ficaram de
fora. Ou seja: decidir peça nunca mexeu no número. Agora a conta é ao
contrário — pendente é só `na_fila`, todo o resto conta como decidido. Não
quebra de novo quando eu criar um status novo.

*A memória.* O andamento sempre esteve gravado no SQLite, mas com dois
furos:

- `decidido_em` só era carimbado na rota de decisão manual. Quem vinculava
  pela loja ou publicava com foto própria não deixava registro de quando.
  Passou a ser carimbado num lugar só, dentro do `storage`, na primeira vez
  que a peça sai da fila — e nunca é reescrito depois.
- Se a pasta `data/` não for um volume montado no EasyPanel, o deploy apaga
  o banco e as 1.333 decisões voltam ao zero. O sistema agora **verifica isso
  e avisa em faixa amarela no topo de todas as telas**. Enquanto a faixa
  estiver aparecendo, o trabalho não está seguro.

Também: peça pulada agora conta quantas vezes foi pulada (`adiado_vezes`) —
três vezes é sinal de que falta informação, não de enrolação —, e a tela
mostra um resumo do que já foi decidido, separado por tipo.

*Erro nos testes.* Descobri, corrigindo isso, que os testes escreviam no
banco de desenvolvimento em vez de numa pasta temporária. `Settings` lê os
`os.getenv` na definição da classe, uma vez só, no import — então mexer em
`os.environ` dentro do teste não mudava nada. Não afeta produção, onde as
variáveis já existem antes do processo subir, mas afetava a confiança nos
testes. Corrigido.

Seis testes novos, 88 no total.

---

## v0.11.0 — 20/08/2026

**O código estava na loja o tempo todo — só escrito de outro jeito.**

Investigando o módulo de injeção da Fiat Toro (item 6 da planilha, código
`0281036486`, R$ 47.864), descobri que a peça **já está na loja da Águia, com
duas fotos**:

`loja.aguiadiesel.com.br/produto/modulo-de-injecao-fiat-toro-jeep-renegade-jeep-compass-0-281-036-486/`

O sistema não achava porque o ERP grava `0281036486` e a loja grava
`0.281.036.486`. A busca do WooCommerce é literal: procura o texto exato. O
cruzamento em lote já normalizava a pontuação e funcionava, mas a busca por
código avulso — a que roda quando alguém cola só o número no campo — não.

- `variantes_de_codigo()` gera as formas de escrever o mesmo part number
  (cru, com ponto, com espaço, com hífen), agrupando os dígitos de três em
  três a partir da direita, que é a convenção Bosch/Delphi.
- `buscar_por_codigo()` tenta SKU e busca livre em cada variante.
- `indexar()` passou a exigir que a chave tenha ao menos um dígito. Sem isso,
  palavras do nome do produto (`RENEGADE`, `COMPASS`) viravam chave de índice
  e podiam casar por acidente com um código do ERP — o mesmo tipo de falso
  positivo que gerou os quatro casos ruins da v0.4.0.

Seis testes novos, 82 no total.

---

## v0.10.0 — 20/08/2026

**Foto tirada na hora, direto da fila.**

O Mercado Livre fechou, um a um, todos os caminhos que dependiam de dados de
terceiros: busca pública (403), leitura de anúncios de outros vendedores (403),
e o `/products/search` que só devolve fichas antigas e inativas. Cada remendo
durou até eles fecharem a porta seguinte.

O que sobra funcionando é o que **não depende de ninguém**: anúncio próprio com
foto própria. A loja da Águia cobre parte das peças; para o resto, agora a
pessoa fotografa.

- **Envio de fotos na tela da fila.** No celular o botão abre a câmera direto
  (`capture="environment"`). Até 10 fotos por peça, 10 MB cada.
- Prévia das fotos enviadas, com botão de apagar cada uma.
- **Publicação com foto própria**: as imagens vão para o endpoint oficial de
  upload do ML (`/pictures/items/upload`), e o anúncio é criado com os ids
  devolvidos. Título montado a partir do ERP, categoria pelo preditor do ML,
  preço e estoque da planilha.
- As fotos ficam no volume do VPS, não no Mercado Livre, até a publicação. Isso
  permite conferir antes, refazer se der erro, e não deixa imagem órfã na conta.
- **Validações**: recusa arquivo vazio, arquivo que não é imagem (testado com
  PDF renomeado), e acima de 10 MB. Nome de arquivo é sanitizado — testei com
  `../../etc/passwd` e variantes, todos caem dentro da pasta do item.
- Publicar continua exigindo a palavra `PUBLICAR` digitada.

O vínculo por link do Mercado Livre continua na tela, funcionando quando
funciona, conforme decidido — mas deixou de ser o caminho principal.

## v0.9.1 — 20/08/2026

**Um caminho só.**

Relato: o botão "Cruzar agora" não aparecia. A causa era estar na tela errada —
a *Conferência* do lote #1, criada pelo fluxo antigo "Analisar catálogo". O botão
vive na **fila de trabalho**, que é outro fluxo e outro lote.

Dois caminhos concorrentes na mesma tela, um deles comprovadamente sem retorno,
é convite ao erro — ainda mais para quem opera todo dia.

- A tela de prévia agora oferece **só** *Montar fila de trabalho*. O fluxo
  "Analisar catálogo" saiu da interface (a rota continua, para os lotes antigos).
- A tela de conferência antiga passa a avisar que é a versão antiga e aponta o
  caminho atual.
- A lista de lotes na tela inicial marca cada lote como *fila de trabalho* ou
  *tela antiga*, para não abrir o errado de novo.
- A prévia sugere começar por `Acima de R$1000,00` — 126 peças, R$ 1.035.011.

## v0.9.0 — 20/08/2026

**Cruzamento da fila inteira com a loja, de uma vez.**

Confirmado que a leitura da loja funciona a partir do VPS (HTTP 200, produto
lido com foto e estoque), o passo seguinte é responder de uma vez: *quantas das
peças da planilha já estão no site?*

- Botão **"Cruzar agora"** no topo da fila: baixa o catálogo inteiro da loja e
  casa com todos os itens pendentes. As peças que já têm foto no site ficam
  **prontas para anunciar** na hora.
- **Baixa o catálogo uma vez, cruza em memória.** Consultar a loja item a item
  para 1.334 peças custaria milhares de requisições ao WordPress da Águia; assim
  são algumas dezenas.
- **Casa por SKU e também pelo nome.** Vários produtos da loja estão com o campo
  SKU vazio e trazem o código só no nome — foi o caso do injetor `A2C59513553`.
  Ignorar isso deixaria essas peças de fora.
- **Normaliza pontuação:** o site grava `0.445.025.016` e o ERP grava
  `0445025016`. Sem normalizar, os dois nunca se encontrariam.
- **Código curto não casa.** `1504` ou `19P` casariam com qualquer coisa.
- O resultado mostra quantas ficaram prontas, **quanto valor isso destrava**,
  quantas foram achadas e quantos produtos a loja tem.
- Lista à parte as peças que **estão na loja mas sem foto** — cadastrando a
  imagem no site, elas entram no próximo cruzamento.

## v0.8.1 — 20/08/2026

**Campo único para o link, e diagnóstico da loja.**

Relato: colar o link da loja devolvia *"Não consegui identificar o anúncio nesse
endereço"*. O link tinha ido para o campo do Mercado Livre. Dois campos parecidos,
cada um aceitando um tipo de link — erro previsível, culpa do desenho.

- **Um campo só.** Aceita link da loja da Águia, link do Mercado Livre, ou
  código solto, e decide sozinho para onde ir. Código solto tenta a loja
  primeiro, porque é de lá que vêm as fotos.
- Dois botões de busca lado a lado: *Procurar na loja da Águia* e *Procurar no
  Mercado Livre*, ambos já preenchidos com o código da peça.
- **User-Agent de navegador** nas chamadas à loja. Sites WordPress atrás de
  firewall costumam recusar requisição identificada como `python-httpx`.
- **Erros da loja passam a dizer o que houve**: status HTTP, URL chamada, e uma
  interpretação — 404 é API desativada, 403 é firewall, HTML no lugar de JSON é
  página de bloqueio.
- **Nova tela `/diagnostico/loja`** (link na tela inicial): cole um link ou
  código e veja exatamente o que o servidor da loja respondeu, com o começo da
  resposta crua. Serve para descobrir a causa sem adivinhação.

Verificações feitas contra a loja real:

- `INJETOR COMMON RAIL LAND ROVER DISCOVERY 2.7 – A2C59513553` (linha 11 da
  planilha, R$ 27.718) está na loja, com foto e 5 em estoque — mas com o campo
  **SKU vazio**. A busca por SKU falha e a busca livre acha, porque o código está
  no nome. A cascata SKU → busca livre já cobria esse caso.
- `BOMBA DE ALTA PRESSÃO CB18 – 0.445.025.016` tem SKU preenchido e é achada
  pelas duas vias.

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
