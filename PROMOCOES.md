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

O servidor consulta as categorias configuradas e revalida até 100 produtos com links cadastrados por ciclo. O admin permite importação manual por categoria. Só itens do tipo ITEM são importados; PRODUCT e USER_PRODUCT ainda não são resolvidos nesta versão.

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

## Limitações importantes antes de divulgar para clientes

1. A renovação OAuth foi implementada, mas só será confiável após mover o banco de tokens para armazenamento persistente; no Render Free o SQLite é temporário.
2. No Render Free, o servidor hiberna após 15 minutos sem tráfego: tarefas em segundo plano param.
3. SQLite é temporário no Render Free; após suspensão/redeploy os links e importações podem desaparecer.
4. Para funcionamento público contínuo, será necessário banco externo persistente, renovação segura de tokens e execução agendada confiável.
5. Nunca coloque Client Secret, senhas, access tokens ou refresh tokens no GitHub.

Testes sem API real:

    python -m unittest discover -s tests -v

Esta versão é uma primeira implementação testável, não uma automação ininterrupta pronta para produção.
