# OFERTIX V17 — Catálogo de promoções automático

## Suporte a catálogos Awin formato Google — Carraro BR

A V17 também reconhece automaticamente feeds CSV/GZIP `Google (Novo)` da Awin, sem converter o arquivo manualmente. Exemplo real analisado: `81383-retail-pt_BR.csv.gz` da **Carraro BR** (catálogo não incluído no código).

- **1.628** produtos no arquivo; **1.542** em estoque e **86** fora de estoque.
- Todos têm nome, foto HTTPS, preço em **BRL** e URL de afiliado Awin.
- A coluna `sale_price` está vazia em todos os registros. **Não foi identificado nenhum desconto que possa ser anunciado como promoção.** Por isso, nenhum desses produtos será publicado.
- **A V17 publica somente produtos em promoção**, com referência de preço válida e pelo menos 10% de desconto no feed. Produtos sem `sale_price` válido não entram no catálogo público nem na vitrine.
- O catálogo automático é alimentado pela URL de feed da Awin, cadastrada como segredo em GitHub Actions. **O CSV enviado para diagnóstico não deve ser incluído no repositório.**
- Uma atualização gera até 300 ofertas com desconto a partir de feeds autorizados. Não há catálogo separado de produtos regulares.
- O filtro por nome, categoria e loja utiliza somente ofertas publicadas. A compra é feita na loja anunciante; comissão depende dos termos comerciais da Awin.
- Se futuramente o feed trouxer `sale_price` menor que `price` em BRL e desconto de pelo menos 10%, esses itens poderão entrar automaticamente na vitrine, sem desenvolvimento adicional.

**Segurança:** uma URL de feed com chave apareceu anteriormente em captura de tela. Recomenda-se renovar essa chave antes de configurá-la no GitHub. Nunca registrar essa URL em código, commits ou arquivos públicos.

## O que esta versão automatiza

Sem cadastrar cada produto: GitHub Actions executa um sincronizador a cada 6 horas, lê os **feeds de produtos autorizados da Awin**, verifica links de afiliado, preços, fotos, moeda BRL e descontos de pelo menos 10%, e publica até 300 ofertas relevantes. O site lê o arquivo gerado pelo GitHub e atualiza a vitrine automaticamente. O comprador finaliza a compra no anunciante.

A rotina publica dados **no branch separado `ofertix-catalog`**, não modifica a branch `main`, portanto **não faz deploy no Render a cada atualização do catálogo**. O Render lê a lista pública com cache de 5 minutos. As ofertas expiram automaticamente 24 horas depois da última geração. Sem feed autorizado, não são publicados produtos falsos.

Nos feeds Awin padrão, o campo `product_price_old` é usado como referência do desconto quando o anunciante o fornece (não confundir com `rrp_price`). Nos feeds Google da Awin, o desconto precisa ser demonstrado por `sale_price` menor que `price`, ambos em BRL. Descontos vindos do feed aparecem identificados como valores informados pela loja, não verificados independentemente. O sistema não garante disponibilidade, frete ou comissão.

## O que o usuário precisa fazer uma única vez

**1 — Aprovação comercial**

Na Awin, selecione `Anunciantes → Fazer Parte dos Programas` e solicite programas de lojas de varejo cujo **Feed de produto = Sim**. Estar inscrito em um programa sem feed, como o HelloSafe anteriormente exibido no painel, não basta para popular esta vitrine.

**2 — Gerar a URL oficial**

Na Awin, procure `Ferramentas (Toolbox) → Create-a-Feed`. Escolha anunciantes permitidos, idioma Português (Brasil) quando disponível, formato **CSV**, delimitador **vírgula**, codificação **UTF-8**, compressão gzip opcional. Selecione no mínimo:

`aw_deep_link`, `aw_product_id`, `merchant_product_id`, `merchant_id`, `merchant_name`, `product_name`, `search_price`, `product_price_old`, `currency`, `merchant_image_url` ou `aw_image_url`, `merchant_category`, `in_stock`. Para formato Google, é indispensável `sale_price` e `price` (com BRL), além do link e disponibilidade.

Configure apenas conteúdo adequado para o público geral, lojas com entregas ao Brasil e preços em BRL. O feed deve ser pequeno: **até 12.000 linhas por feed e 12 MB comprimidos**. A Awin pode não fornecer todos esses campos para determinado anunciante; sem preço antigo confirmado, o item não aparece como promoção.

**3 — Guardar a chave em segurança**

No GitHub do repositório OFERTIX: `Settings → Secrets and variables → Actions → New repository secret`.

- Nome exato: **`OFERTIX_AWIN_FEED_URLS`**.
- Valor: **a URL completa do Create-a-Feed**, não a URL do site da loja. Pode colocar mais de uma URL, uma por linha (máximo de 8).
- Não compartilhe a URL, token, API key ou credenciais por chat ou em commits.

**4 — Ativar e validar**

Depois que o código da PR for integrado à `main`:

- Vá em `Actions → OFERTIX - Catálogo automático de afiliados → Run workflow` e escolha `main`.
- Na primeira execução, confira os logs: quantidade de entradas examinadas e ofertas elegíveis (nenhuma chave exibida).
- Em `ofertix-catalog/catalog_awin.json`, confira se há ofertas verdadeiras; não edite manualmente preços ou links.
- Abra a vitrine OFERTIX e valide um produto. O botão **Ver oferta** deve direcionar pelo link oficial de afiliado à página correta da loja.

## Restrições importantes

- **Persistência do Render:** os produtos da Awin ficam no GitHub, protegidos contra a perda do SQLite durante deploys. **Produtos manuais e tokens OAuth ainda ficam no SQLite da versão atual, com risco de perda no Render Free.** Faça backup/migre para banco persistente antes de operar a solução comercialmente ou provocar deploy.
- **Render Free:** pode hibernar; a atualização do catálogo ocorre no GitHub de forma independente, mas o primeiro visitante pode aguardar o servidor acordar. GitHub Actions não garante início exato do agendamento.
- **Aprovação:** é necessário aderir aos termos de cada loja e às regras de publicidade; não é garantida atribuição de comissão.
- **Sem feeds aprovados:** o catálogo permanecerá vazio. Isso é esperado, não erro de importação.
- **O sistema não compra nem recebe pagamentos dos clientes.** A loja efetua a compra, contabiliza a conversão e paga eventuais comissões segundo seu programa.

## Verificações

- Execução automática: a cada seis horas + opção manual no GitHub.
- Segurança: somente URL HTTPS de feeds Awin conhecidos, até 8, limites de tamanho, lojas e preços em BRL, deep links rastreáveis `awin1.com`; segredos só na execução da Actions.
- Validade: 24 horas após geração; ofertas antigas não são publicadas se a atualização falhar por muito tempo.
- Testes locais e de integração da API pública.
- PR revisada antes de qualquer deploy.

**Referência oficial:** https://help.awin.com/developers/docs/downloading-feeds-using-create-a-feed
