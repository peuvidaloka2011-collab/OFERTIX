# OFERTIX — somente promoções verificadas (V2)

O sistema consulta a API oficial de preços do Mercado Livre: GET /items/{ID}/sale_price?context=channel_marketplace.

## Quando uma oferta aparece?

- Item ativo e disponível;
- sale_price.amount é menor que sale_price.regular_amount, ambos em BRL;
- desconto mínimo de 10% (configurável);
- verificação do preço realizada nas últimas 12 horas;
- link oficial de afiliado cadastrado no painel administrativo.

Atenção: regular_amount é uma referência promocional informada pela plataforma, não garantia de menor preço histórico.

## Usar no VS Code

Instale Python 3.10+; não há dependências externas obrigatórias. No PowerShell, dentro da pasta OFERTIX:

    $env:OFERTIX_ADMIN_KEY="UMA-SENHA-FORTE-E-UNICA"
    python main.py

Catálogo: http://localhost:8080/
Administração: http://localhost:8080/admin

A aprovação como afiliado é diferente do acesso OAuth da API. A importação real depende de ML_ACCESS_TOKEN válido e autorizado.

## Configurar o Render

Variáveis de ambiente:

- OFERTIX_ADMIN_KEY: chave secreta e exclusiva para administração (obrigatória).
- ML_ACCESS_TOKEN: token OAuth autorizado (não gravar no GitHub).
- OFERTIX_CATEGORIES: IDs de categorias, separados por vírgulas. Exemplo: MLB1051
- OFERTIX_MIN_DISCOUNT: desconto mínimo, padrão 10 (%).
- OFERTIX_FRESH_HOURS: horas de validade da informação, padrão 12.
- OFERTIX_SYNC_HOURS: intervalo de verificação enquanto o servidor estiver ativo, padrão 6.
- PORT: fornecido automaticamente pela hospedagem.

O servidor consulta as categorias configuradas e revalida até 100 produtos com links cadastrados por ciclo enquanto estiver ativo. O admin permite importar categorias, mas o Mercado Livre pode bloquear a busca por anúncios com HTTP 403. A V8 também permite cadastrar um anúncio e seu link oficial de afiliado sem importação por categoria.

## Links de afiliado

Na Central de Afiliados, use o Gerador de Links oficial. No painel /admin, associe o URL gerado ao ID do produto. O OFERTIX verifica o domínio, mas não consegue comprovar se um link realmente atribuirá comissão à sua conta.

Sem link de afiliado, a promoção fica oculta. Produtos que deixaram de ter desconto também ficam ocultos.

## Autenticar Mercado Livre pelo celular (ambiente de testes)

O app Mercado Livre Developers deve usar Authorization Code, Refresh Token e PKCE com a URI exata:
https://ofertix-ankt.onrender.com/oauth/callback

No serviço OFERTIX do Render, configure em Environment (sem colocar segredos no GitHub):
- ML_CLIENT_ID: ID da aplicação
- ML_CLIENT_SECRET: chave secreta do aplicativo
- ML_REDIRECT_URI: URL de callback acima
- OFERTIX_ADMIN_KEY: senha administrativa já configurada
- OFERTIX_TOKEN_KEY: use o botão Generate do Render para criar um segredo aleatório com pelo menos 32 caracteres. **Mantenha este valor estável** para conseguir descriptografar tokens salvos.

Após publicar a versão OAuth V2, abra https://ofertix-ankt.onrender.com/admin pelo celular:
1. Entre com OFERTIX_ADMIN_KEY.
2. Toque em "Preparar autorização".
3. Toque em "Autorizar Mercado Livre" e confirme a autorização no site oficial.
4. Volte à aba /admin e toque em "Atualizar painel".
5. Confira o indicador "OAuth conectado". Se houver erro, copie somente a mensagem, nunca tokens.

O fluxo utiliza state de uso único com validade de 10 minutos, PKCE S256, POST ao endpoint OAuth e criptografia Fernet para os tokens armazenados em SQLite, com uma chave que só está nas variáveis privadas do Render.

**Importante:** enquanto o OFERTIX estiver no Render Free, os tokens e links salvos no SQLite local podem ser perdidos em novos deploys ou reinicializações. Esta configuração serve apenas para testes. A automação de produção depende de banco persistente, armazenamento seguro e execução agendada fora do servidor Free. O OAuth não gera links de afiliado por si só; os links devem ser obtidos por mecanismos oficiais autorizados.

## V4: ranking de mais vendidos com produtos de catalogo

A API de highlights pode entregar itens de tres tipos:

- ITEM: ID MLB de publicacao; verificar preco diretamente.
- PRODUCT: ID MLB de produto de catalogo; consultar /products/ID e extrair buy_box_winner.item_id quando produto ativo.
- USER_PRODUCT: ID MLBU; consultar /user-products/ID para identificar user_id e /users/{user_id}/items/search?user_product_id=ID para localizar publicacoes. Verificar no maximo 2 itens por User Product para limitar chamadas.

A versao V4 nao inventa anuncios ou links: se o Mercado Livre nao devolver uma publicacao MLB valida, o produto sera registrado como "sem anuncio acessivel" e nao sera divulgado. Em caso de bloqueio 403 ou falta de permissao, o painel informa "Falhas de acesso ao catalogo"; algumas consultas a anuncios de terceiros podem nao estar disponiveis a esta aplicacao.

Apenas itens ativos com reducao confirmada de >= 10% no endpoint sale_price sao considerados promocao. Ainda e necessario associar **manualmente** o link oficial de afiliado do anuncio MLB correspondente. A API OAuth comum nao autoriza presumir link de comissao.

A importacao continua limitada a ate 20 entradas do ranking, e pode demorar ou sofrer limitacao de requisicoes. Render Free pode desligar e perder banco SQLite e tokens; antes de uso publico e necessaria base persistente.

## V5: produtos-pai e IDs MLBU no ranking

A documentacao de /highlights inclui type=ITEM com id=MLBU. Nesta versao, IDs MLBU sao resolvidos pelo User Product e pela busca dos itens do vendedor.
Para type=PRODUCT, produtos-pai podem nao ter buy_box_winner, mas ter children_ids ou variantes em pickers.products. A V5 consulta no maximo 7 paginas de produto por entrada e 2 anuncios reais; sempre consulta o preco de um anuncio com ID MLB.

A importacao manual agora roda em segundo plano no servidor. O painel consulta /api/admin/import/status com autenticacao a cada 3 segundos (ate 6 minutos). O resultado mostra tipos de ranking, IDs MLBU, anuncios analisados e itens sem anuncios acessiveis.

Limites: o Mercado Livre pode nao fornecer um anuncio compravel ou negar acesso a alguns recursos. O OFERTIX nao deve publicar anuncios sem link oficial de afiliado nem descontos nao verificados.

Render Free pode apagar o banco SQLite e a autorizacao OAuth em novos deploys. Para uso publico: adicionar banco persistente e agendamento confiavel.

## V7: busca alternativa de anuncios por categoria

Quando um ranking /highlights mostra produtos do tipo PRODUCT sem item vencedor e nao encontra nenhum anuncio MLB, a importacao tenta o recurso oficial /sites/MLB/search?category=ID&limit=20. Os resultados dessa busca sao **anuncios de categoria e NAO necessariamente os mais vendidos**.

Cada anuncio encontrado e consultado por /items/ID e /items/ID/sale_price?context=channel_marketplace. Somente descontos confirmados iguais ou superiores ao percentual minimo sao salvos como promocao. Para aparecer na vitrine, o administrador ainda precisa associar link valido e oficial de afiliado gerado pelo programa Mercado Livre.

A API pode recusar o endpoint de busca por categoria em algumas contas/permissoes (HTTP 403/401). O painel exibe a falha e nao inventa ofertas. Se os metodos falharem, conferir documentacao e alternativas autorizadas. Nao usar scraping de Mercado Livre para contornar bloqueios.

Nota: V7 usa SQLite temporario do Render Free. Cada novo deploy pode apagar o token OAuth e links associados. Migrar para banco persistente antes de operacao real.
## V8: cadastrar anúncios manualmente com links oficiais

Objetivo: manter o Mercado Livre ativo enquanto a Shopee não libera sua Open API. A busca por anúncios por categoria retornou HTTP 403, portanto não depender dela para o cadastro inicial.

No painel /admin:
1. Gere o link para o anúncio específico no Gerador de Links da Central de Afiliados ou na Barra de Afiliados do Mercado Livre.
2. Copie o ID do anúncio de venda (MLB seguido de números). O ID do produto de catálogo não serve.
3. Preencha ID + link e toque em **Salvar e verificar oferta**. Não precisa executar a importação por categoria.
4. O sistema tentará GET /items/{ID} e GET /items/{ID}/sale_price?context=channel_marketplace.
5. Se encontrar produto ativo e desconto real de pelo menos 10%, o item poderá aparecer na vitrine; sem desconto confirmado não aparece.
6. Se não houver autorização OAuth ou a API retornar HTTP 403/indisponibilidade, salva apenas o link com estado pendente e **não publica** o item. Pode tentar revalidar depois.

Importante: um domínio como meli.la ou mercadolivre.com não comprova vínculo a uma conta de afiliado. Confirme que o link foi realmente gerado pela Central/Barra oficial e corresponde ao mesmo anúncio MLB. Links comuns de categorias, buscas ou página inicial não são elegíveis para comissão.

O Render Free utiliza SQLite efêmero; reinicializações/deploy podem apagar links e tokens. Migrar para banco persistente ANTES da operação comercial. A revalidação periódica não é contínua em instâncias que hibernam.
## V9: identificar automaticamente anuncio a partir do link

No celular, basta colar o link oficial de afiliado (ex.: https://meli.la/...). O ID MLB fica em um campo opcional em **Opcoes avancadas**.

A V9 extrai IDs somente de caminhos de anuncios individuais e segue ate 5 redirecionamentos dentro dos dominios oficiais permitidos. Nao visita domínios de terceiros apontados por redirecionamento, nao analisa HTML e nao considera URLs /p/MLB (produtos de catalogo) como anuncios de vendedor.

Se o encurtador levar apenas ao catalogo, esconder o ID ou bloquear o acesso (403), o sistema apresenta erro claro e oferece preenchimento manual do ID de anuncio. O ID manual deve corresponder ao destino do link. Para URL de anuncio direto, IDs diferentes sao recusados.

Detectar um ID não garante que o link gere comissao, nem garante que o item tenha oferta valida. A V9 ainda verifica o desconto mínimo por /items/ID e /sale_price; se falhar, fica pendente e fora da vitrine.

**Limite operacional:** Shortlinks `meli.la` variam em comportamento; a capacidade de extrair o ID deve ser validada com um link oficial real. Com Render Free, o SQLite e as autorizacoes podem se perder em novos deploys, portanto esta etapa e para testes, nao para producao.
## V10: localizar anuncios vinculados ao catalogo por link

O botão **Encontrar anúncios do catálogo** fica no painel mobile ao lado do cadastro por link. Ele aceita links HTTPS de dominios oficiais do Mercado Livre, incluindo meli.la, seguindo no maximo cinco redirecionamentos controlados para chegar a uma pagina /p/MLB... (o código do produto de catálogo). Não faz scraping do site.

O backend consulta o detalhe de /products/{catalog_id} e, quando permitido, lista IDs de anuncios via /products/{product_id}/items, consultando o produto original e até duas variantes conhecidas, no máximo cinco IDs. Os IDs candidatos são exibidos no painel para seleção; **a V10 não verifica o preço, desconto nem publica automaticamente nessa etapa**.

Depois de selecionar um ID, ele preenche o campo opcional do cadastro de produto. O usuário deve conferir que o link de afiliado oficial corresponde ao vendedor e anúncio selecionados, preferencialmente gerando novo link direto para este anúncio. Ter um link de catálogo ou um ID válido não garante comissão.

HTTP 401/403 interrompe a consulta ao recurso bloqueado e mostra uma mensagem, sem acessar outra fonte para burlar a restrição. O aplicativo continua aceitando ID individual manual e segue verificando descontos mínimos de 10% na API antes de exibir ofertas.

Render Free pode hibernar e descartar o banco temporário após deploy, exigindo reautorização OAuth. Evite utilizar a V10 em produção até migrar para armazenamento persistente.
## V11: diagnosticar separadamente detalhes do anuncio e preco

No painel /admin, a nova area **Diagnostico de anuncio e preco** aceita um ID de anuncio MLB, como MLB6499468038. Com a conta Mercado Livre autorizada, o backend executa duas chamadas **separadas** em modo somente leitura:

- GET /items/{ID} — confirma status active e estoque disponivel, quando a API permite.
- GET /items/{ID}/sale_price?context=channel_marketplace — obtem amount, regular_amount e moeda, e calcula desconto somente quando ambos os valores reais e a moeda BRL estao presentes.

Cada resultado mostra HTTP 200, HTTP 403/401/404, erro de conexao ou dados insuficientes. Em caso de erro, o diagnostico revela somente codigos normalizados conhecidos como escopos_insuficientes ou acesso_restrito. Nao retorna headers, cookies, tokens OAuth, mensagens brutas do Mercado Livre ou dados do vendedor.

Mesmo que /sale_price responda 200 e indique desconto >=10%, **nao publica** quando /items estiver bloqueado ou nao houver disponibilidade verificada. O diagnostico nunca altera a tabela `products` e nao substitui a validacao e a associacao de link de afiliado oficial na rotina de cadastro.

O Mercado Livre pode retornar HTTP 403 por diversas causas (escopos, usuario, IP, aplicativo, falta de validacoes). Testar a conta real e revisar o detalhe de erro normalizado antes de concluir a causa; nao contornar bloqueios com rotas alternativas.

Antes de uso publico: Render Free usa SQLite efemero e pode perder tokens e links apos deploy/reinicio. Migrar para banco persistente.
## V12: ofertas manuais com links de afiliado Mercado Livre

Foco: contas somente de afiliado não conseguem consultar preços de anúncios de terceiros pela API (HTTP 403). Esta versão cria um **fluxo independente da API** para divulgar produtos com preço informado pelo administrador, mantendo o modo verificado por API existente.

### Cadastro no painel `/admin`

1. Gere seu link para **um produto específico** na Central/Barra de Afiliados. Não use links de perfil social, página inicial, buscas ou painel de afiliados.
2. Informe nome, categoria, preço atual e preço anterior **observados no momento no Mercado Livre**. Não invente preço anterior. É obrigatório desconto informado >= `OFERTIX_MIN_DISCOUNT` (10% por padrão).
3. Adicione imagem própria/autorizada JPG/PNG/WebP do computador ou URL HTTPS do domínio `mlstatic.com` (e subdomínios). Fotos locais são comprimidas e incorporadas ao banco em texto base64; máximo 55 KB por imagem.
4. Informe início opcional (agora se vazio) e validade obrigatória: até 72 horas após o início, programável no máximo 30 dias à frente. Confirme que conferiu preço e link para habilitar o salvamento.
5. Gerencie ofertas por **Editar** e **Pausar/Ativar**. A vitrine consulta diretamente `starts_at`/`expires_at`, então as ofertas somem do catálogo após a validade sem depender de processos de sincronização em segundo plano.

### Transparência e segurança

- Cada oferta manual é identificada na vitrine: **Preço e desconto informados pelo afiliado; não verificados pela API**. O símbolo * no desconto indica preço informado manualmente.
- Ofertas automáticas verificadas pela API mantêm sua identificação própria e regras de verificação e expiração originais.
- O servidor valida preços positivos, preço anterior maior, desconto mínimo, links HTTPS de domínios oficiais, prazo, autenticação administrativa e presença de imagem.
- A verificação apenas de domínio **não garante comissão**. Confirme que o link foi efetivamente gerado para aquele produto por ferramenta oficial e que o seu canal de divulgação está permitido nos termos do programa.
- Não usar imagens nem declarações de desconto sem autorização ou comprovação. O consumidor deve confirmar preços e disponibilidade na página oficial antes de comprar.
- Os dados manuais estão na tabela isolada `manual_offers`. Eles não são sobrescritos pelo sincronizador de ofertas automático, que consulta `/items` e `/sale_price`.

### Antes da operação pública

**Render Free usa disco efêmero:** reinício/deploy pode apagar ofertas e imagens manuais, e os tokens OAuth. Migrar para armazenamento persistente antes de depender de vendas, relatórios ou acessos constantes. O servidor pode hibernar; a validade é aplicada na leitura, mas solicitações podem demorar a iniciar. Link de afiliado não é garantia de comissão nem de preço.
## Limitações importantes antes de divulgar para clientes

1. A renovação OAuth foi implementada, mas só será confiável após mover o banco de tokens para armazenamento persistente; no Render Free o SQLite é temporário.
2. No Render Free, o servidor hiberna após 15 minutos sem tráfego: tarefas em segundo plano param.
3. SQLite é temporário no Render Free; após suspensão/redeploy os links e importações podem desaparecer.
4. Para funcionamento público contínuo, será necessário banco externo persistente, renovação segura de tokens e execução agendada confiável.
5. Nunca coloque Client Secret, senhas, access tokens ou refresh tokens no GitHub.

Testes sem API real:

    python -m unittest discover -s tests -v

Esta versão é uma primeira implementação testável, não uma automação ininterrupta pronta para produção.
