# Guia de uso do WinZapp — como manter atualizado

Regra do projeto: **a cada alpha lançada, as funcionalidades novas entram no guia.**

## O que é cada arquivo

| Arquivo | Função |
|---|---|
| `guia-de-uso.html` | O guia pronto (um arquivo só, acessível, com busca e modo escuro). É gerado; não edite à mão. |
| `secoes/NN-nome.html` | O conteúdo, uma seção por arquivo (a ordem dos números define a ordem no guia). **É aqui que se edita.** |
| `template.html` | Visual e script do guia (CSS, busca, menu). Raramente muda. |
| `cobertura.json` | Até que alpha o guia está completo (`alpha`, `tag`, `commit`, `data`). |
| `gerar_guia.py` | Junta tudo e escreve `guia-de-uso.html`. |
| `gerar_atalhos.py` | Gera as tabelas de atalhos direto do catálogo do WinZapp (`client/core/shortcut_catalog.py` + PO pt-BR). Chamado pelo `gerar_guia.py`. |
| `novidades_desde_a_ultima.py` | Mostra o que mudou desde a alpha coberta: commits/PRs, diff do changelog e atalhos novos/removidos/alterados. |

## Roteiro a cada alpha (ou a cada lote de alphas)

1. Atualize o clone do WinZapp (`git fetch --tags origin` e `git checkout origin/main`).
2. Rode `python novidades_desde_a_ultima.py <clone>`. A saída traz:
   - os commits novos com o número do PR;
   - o diff de `client/changelog_pt-BR.txt` (a melhor lista de funcionalidades novas, porque o projeto exige uma entrada por PR em todos os idiomas);
   - atalhos novos, removidos ou alterados.
3. Para cada funcionalidade nova do changelog:
   - ache a seção certa em `secoes/` e escreva como se usa (onde fica, qual atalho, o que o leitor de tela diz);
   - acrescente `<span class="desde">alpha 4412</span>` no título ou no começo do item, com o número da alpha em que ela chegou;
   - se não houver seção que sirva, crie `secoes/NN-nome.html` (começa com `<!--nav: 🔔 Título-->`).
4. Atalhos: **nada a fazer à mão**; o `gerar_guia.py` regenera as tabelas. Só atualize as tabelas escritas à mão nas seções (conversas, mensagens, voz, ligações) se um atalho citado nelas mudou.
5. Mudou ou foi removida uma funcionalidade? Ajuste o texto da seção para descrever **só como é agora** (sem "antes era assim").
6. Registro: acrescente uma linha em `secoes/99-registro.html` (alpha + o que entrou) e atualize `cobertura.json`.
7. Rode `python gerar_guia.py <clone>` e abra o HTML: confira a busca, o menu lateral e, com leitor de tela, as seções alteradas.

## Regras de conteúdo

- Só funcionalidades que funcionam. Nada de "não funciona ainda" nem de bug conhecido.
- Nunca descreva como era antes de uma mudança.
- Nomes de botões, menus e opções exatamente como aparecem no programa em português (conferir no PO: `translations/pt-BR/LC_MESSAGES/winzapp.po`).
- Atalhos sempre os de fábrica; o texto lembra que dá para trocar nas Configurações.
- O changelog entra só com mudanças estável-para-estável; para o guia vale o contrário: tudo o que existe na alpha entra, inclusive o que só existe em alpha.

## Verificação antes de publicar

- `python gerar_guia.py <clone>` roda sem erro e informa a alpha coberta.
- Nenhum `{{...}}` sobrou no HTML; todos os links `#...` do menu levam a uma seção.
- Os atalhos citados no texto conferem com a tabela gerada.
