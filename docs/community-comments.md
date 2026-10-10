# Respostas aos avisos de comunidades

Implementação local em `feat/community-comments`. O suporte upstream ainda não
foi publicado. Há mudanças separadas nos repositórios irmãos `wa-js`,
`wppconnect` e `wppconnect-server`.

## Testar no WinZapp

Depois de compilar a API com `uv run python build_api.py`, feche o WinZapp e
abra novamente a partir deste checkout (`uv run winzapp`). O processo antigo
não incorpora mudanças de Python nem novas rotas de Node.

1. Abra o grupo de avisos de uma comunidade e selecione um aviso, inclusive
   uma mensagem de voz.
2. Abra seu menu de contexto e escolha **Respostas ao aviso da comunidade**.
3. Navegue pela lista com as setas. Cada resposta mostra autor, horário e texto. Nomes salvos têm prioridade;
   nomes de perfil são usados quando necessário. Respostas próprias respeitam
   a configuração "Como se referir a mim?".
   **Atualizar respostas** consulta novamente o histórico sincronizado.
4. Escreva no campo de resposta e use **Enviar resposta** ou **Ctrl+Enter**.
   **Enter** sozinho continua inserindo uma linha no campo.
5. Ao navegar pelos avisos, a contagem conhecida de respostas é anunciada no
   depois do conteúdo da mensagem. Abrir ou atualizar as respostas também atualiza essa contagem.
6. **Escape** fecha a janela. Atualizações preservam a resposta selecionada.

Chats antigos podem não ter a identificação de comunidade no cache. Nesse
caso, a opção também pode aparecer em um grupo comum: a API verifica o grupo
real e informa que não é um aviso de comunidade antes de habilitar o envio.

## Limites e contrato

- A leitura usa o histórico sincronizado no dispositivo vinculado. Respostas
  existentes apenas no telefone podem não aparecer. Não marca respostas lidas.
- Respostas apagadas e ainda criptografadas têm rótulos próprios, sem texto
  antigo. Identificadores internos e material de criptografia não são exibidos.
- Um comentário é enviado pelo remetente nativo de comentários. Não entra na
  fila de mensagens normais. Apenas `messageSendResult: "OK"` confirma o envio.
- Falha ou timeout mantém o texto e bloqueia outra tentativa até uma atualização
  explícita. Confira a lista antes de tentar outra vez: o envio pode ter ocorrido.
- A janela usa atualização manual; ainda não consome os eventos de comentários
  adicionados ao WPPConnect upstream.

## Integração provisória da API

`client/api_patches/src/util/communityCommentsRuntime.ts` usa primeiro a API
pública nova do WA-JS, quando disponível. Para o par homologado atual, usa os
módulos nativos observados no WhatsApp Web. O componente de comentários é
carregado sem abrir uma janela do navegador. As versões npm homologadas
permanecem iguais. O adaptador pode ser removido após a adoção da versão upstream.

As rotas autenticadas usam GET e POST em
`/api/:session/message-comments/:messageId`; o POST recebe `{text: "..."}`.
O adaptador e o controlador são restaurados pelos mesmos caminhos de setup e
build usados nos demais patches. Não edite `client/api/` manualmente.

## Validação realizada

- Compilação dos três projetos upstream e da API do WinZapp.
- Testes de contratos, segurança dos dados, falhas de envio, eventos upstream,
  callbacks de janela encerrada e seleção preservada, usando widgets simulados.
- Traduções completas nos sete idiomas cadastrados; MO e mapa de chaves compilados.
- Módulos novos resolvidos em uma página nova sem login, em modo headless,
  no WhatsApp Web `2.3000.1049878396`.
- Controlador compilado do WinZapp executado contra a sessão real no WhatsApp
  Web `2.3000.1049861608`: sete respostas com nomes, incluindo uma resposta própria identificada.
  A leitura anterior de seis respostas confirmou a preservação do estado de leitura.

As verificações automatizadas não enviaram respostas reais. O usuário testou
manualmente o envio pelo WinZapp: sua resposta foi a sétima, posteriormente
encontrada na sessão real e identificada como própria. Esse teste usou o adaptador
nativo provisório do WinZapp; a cadeia completa dos pacotes upstream ainda
precisa de validação. A experiência com NVDA também precisa de confirmação manual.
