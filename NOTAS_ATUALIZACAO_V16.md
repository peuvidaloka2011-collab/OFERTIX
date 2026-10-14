# OFERTIX V16 — Atualização profissional

**Objetivo:** aumentar a clareza, confiança e facilidade de uso da vitrine OFERTIX sem alterar o funcionamento do backend de afiliados.

## Melhorias visuais e de navegação
- Identidade OFERTIX laranja/verde, banner redesenhado em SVG local (sem imagem externa fictícia).
- Cards de produtos, valores, descontos, botões e seções refinados para desktop, tablet e celular.
- Explicação "Como funciona" com etapas reais: pesquisar, conferir o preço na loja e comprar diretamente nela.
- Tratamento de navegação por teclado, campo de pesquisa, filtros, categorias e textos de acessibilidade.
- Estado vazio mais útil: explica a ausência de ofertas reais e oferece limpeza de filtros.
- Metadados básicos para compartilhamento do site em redes sociais.
- Botão **Compartilhar oferta** nos produtos com ID público; ele envia ou copia um link para a vitrine OFERTIX com o filtro da oferta. Nenhuma venda é processada no OFERTIX.

## Correções preventivas
- Não exibir ofertas com valores inválidos ou desconto inconsistente com os preços informados (tolerância de arredondamento de até 3 pontos percentuais).
- Links de compra continuam usando URLs HTTPS de afiliado recebidas pela API e são abertos com rel=sponsored, noopener, noreferrer.
- Página pública ainda lê os dados reais do endpoint `/api/products`. **Não foram incluídos preços, estrelas, reviews, lojas ou produtos falsos.**
- Novos testes: HTML com IDs únicos, destinos de navegação acessíveis, componentes presentes e regras de preço/URL executadas em JavaScript sem conectar à internet.
- Arquivo alterado para a vitrine: `index.html`. O restante da aplicação V13 mantém o comportamento atual.

## Pendências reais identificadas

**1. Render não publicado.** A captura do painel Render mostra `Deploy Error — Internal Server Error` no deploy manual. Os testes do GitHub aprovados não são prova de que o Render está mostrando o último commit. Confirmar branch main, último commit, logs de deploy, filtros de build e ligação com o GitHub.

**2. Risco de perda de dados.** O aplicativo usa `OFERTIX_DB_PATH` ou, na ausência dessa variável, `ofertix.db` na pasta do aplicativo. No Render Free o armazenamento local não é persistente. Um deploy/reinício pode apagar ofertas manuais e tokens OAuth. **Antes de publicar/operar comercialmente, migrar para Postgres persistente ou outro armazenamento durável, com backup e procedimento de migração.**

**3. Fonte de ofertas automáticas não ativa.** A integração Awin da PR #13 é experimental e permanece fora de `main`. Para publicação automática de vários lojistas é necessário aprovar programas de afiliados, conseguir feeds válidos e configurar a URL de feed privada no servidor. O Mercado Livre tem retornado HTTP 403 para consultas de anúncios/preços em testes anteriores.

**4. Comissões não garantidas.** Apenas links de afiliado oficiais de produtos e campanhas aprovados devem ser divulgados. Comissão depende das regras da loja/rede e da atribuição da compra.

## Publicação

Esta atualização é só de interface e pode ser revisada na PR antes do merge. Como o Render está em `On Commit`, integrar à `main` pode iniciar novo deploy e afetar dados locais. Não publicar antes de decidir como proteger os dados ou confirmar que existe armazenamento persistente.

## Checklist para aprovação
- [ ] CI e testes V16 aprovados
- [ ] Captura da vitrine verificada em computador e celular
- [ ] Dados salvos ou migrados para armazenamento persistente
- [ ] GitHub branch main e Render conectados
- [ ] Novo deploy Live confirmado pelo Render
- [ ] Pesquisas, categorias, URLs de compra e compartilhamento validados no site publicado
