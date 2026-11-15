# Usar a Converge no celular e computador

Após a implantação, abra `https://bot.getconverge.me/painel/instalar` no
celular ou computador. A própria página mostra o passo a passo do iPhone,
Android, Chrome e Microsoft Edge. Abra o app pelo novo ícone.

No computador, use **Instalar app** ou o ícone de instalação na barra de
endereço do Chrome/Edge e confirme. A Converge abre em uma janela própria,
com lista e conversa lado a lado quando há espaço. Em uma janela estreita,
ou no celular, a conversa ocupa a tela; a seta volta para a lista. É o mesmo
app e a mesma conta em todos os aparelhos.

As conversas têm busca por nome, prévia da última mensagem, canal ou
responsável, entre os resultados do filtro atual. Os detalhes e a origem
ficam no menu **⋮**. No cabeçalho, **Assumir** aparece enquanto não há
atendente; **Devolver** aparece quando a conversa está com você. Se outra
pessoa estiver atendendo, nenhuma dessas ações aparece, e o servidor também
recusa a transferência. A seta à esquerda fecha a conversa; a seta à direita
envia a mensagem.

A tela e o campo de mensagem têm altura fixa; textos longos rolam dentro do
campo. No celular, o teclado desloca apenas a barra de resposta para mantê-la
visível. O restante da conversa conserva o tamanho.

Quando a leitura estiver habilitada, **Extrair texto** em uma imagem ou PDF
abre o resultado para conferência e cópia. Veja os limites e a opção de
desativar em [OCR_INBOX.md](OCR_INBOX.md).

Entre com a mesma conta da equipe, senha e autenticador. No app instalado,
“Manter conectado neste aparelho” começa marcado; use essa opção em aparelho
pessoal. No navegador ela começa desmarcada. O app abre na Inbox.

Um aparelho mantido conectado perde o acesso após 90 dias sem uso ou quando
alguém o desconectar. No menu **Aparelhos conectados**, a equipe vê o nome,
o tipo de aparelho e o último uso. **Desconectar** pede confirmação e exige
novo login naquele aparelho. **Sair** encerra só a sessão atual.

As conversas e os envios precisam de internet. Sem conexão, o app avisa;
“Tentar novamente” busca a página de novo. As respostas não ficam numa fila
offline. Quando uma versão nova estiver pronta, use **Atualizar**; se houver
resposta escrita no campo, o app pede confirmação antes de descartá-la.

Este incremento entrega instalação e sessão por aparelho. Notificações push
e respostas prontas vêm nos próximos incrementos. Os avisos da equipe
continuam pelo Telegram enquanto isso.

## Conferência após implantação

1. Instalar no Android, no iPhone e no computador pelo roteiro da página.
2. Abrir pelo ícone, conferir a opção de manter conectado e completar os dois
   fatores. A Inbox deve aparecer sem barra de endereço.
3. Fechar o app, reabrir e conferir que continua conectado.
4. Abrir **Aparelhos conectados** em outro aparelho e desconectar o primeiro.
   No próximo acesso, o primeiro deve pedir o login novamente.
5. Em uma conversa de teste, ativar modo avião e conferir o aviso; reconectar
   e confirmar que o app volta a funcionar sem reenviar uma resposta.
6. No computador, redimensionar a janela e conferir lista, conversa e campo
   de resposta; no celular, conferir o campo com o teclado aberto. No
   computador, Enter envia e Shift+Enter cria uma linha; no celular, Enter
   cria uma linha e o botão envia.

## Operação

- `PANEL_DEVICE_IDLE_DAYS` controla os dias sem uso, com padrão 90. Os limites
  de navegador `PANEL_SESSION_HOURS` e `PANEL_IDLE_MINUTES` continuam valendo
  para sessões sem a opção de manter conectado.
- `python -m cli.panel_users disconnect <email>` revoga todos os aparelhos
  mantidos conectados daquela pessoa. A desativação de usuário continua
  revogando todas as suas sessões.
- A migração aditiva `0014_device_sessions.sql` roda pelo deploy normal. Em
  rollback, as colunas podem ficar; o código anterior continua aplicando seus
  limites de sessão curta, e a instalação não dispensa a autenticação da API.

Instruções de instalação verificadas na documentação da
[Apple](https://support.apple.com/pt-br/guide/iphone/iphea86e5236/ios) e do
[Chrome para Android](https://support.google.com/chrome/answer/9658361?co=GENIE.Platform%3DAndroid&hl=pt-BR),
[Chrome para computador](https://support.google.com/chrome/answer/9658361?co=GENIE.Platform%3DDesktop&hl=pt-BR) e
[Microsoft Edge](https://learn.microsoft.com/pt-br/microsoft-edge/progressive-web-apps/ux).
