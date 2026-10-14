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

## Limitações importantes antes de divulgar para clientes

1. Tokens de acesso do Mercado Livre expiram; a renovação OAuth automática ainda não foi implementada.
2. No Render Free, o servidor hiberna após 15 minutos sem tráfego: tarefas em segundo plano param.
3. SQLite é temporário no Render Free; após suspensão/redeploy os links e importações podem desaparecer.
4. Para funcionamento público contínuo, será necessário banco externo persistente, renovação segura de tokens e execução agendada confiável.
5. Nunca coloque Client Secret, senhas, access tokens ou refresh tokens no GitHub.

Testes sem API real:

    python -m unittest discover -s tests -v

Esta versão é uma primeira implementação testável, não uma automação ininterrupta pronta para produção.
