# Canal WhatsApp — especificação

Data: 05/10/2026 · Status: aprovada pelo Paulo em 06/10/2026

## Objetivo

O agente passa a atender pelo **WhatsApp**, pela Cloud API da Meta, sem
intermediário, no **mesmo número que hoje está no Converge CRM**. O objetivo é
ter algo funcional para mostrar a clientes. O Telegram continua funcionando
igual e segue como o lugar de teste. A equipe responde às conversas do WhatsApp
pela **Inbox do painel**, como já faz com o Telegram. O CRM deixa de ser usado
para esse número.

Pronto quando: uma conversa no WhatsApp faz o mesmo que no Telegram (saudação,
FAQ, agendar com botões, remarcar, cancelar, áudio, uma pessoa assumir pela
Inbox), e o lembrete do compromisso sai pelo template aprovado.

## Decisões já tomadas com o Paulo (05/10/2026)

1. **Número:** o mesmo do Converge CRM. O agente assume o número na Meta, e o
   painel substitui o CRM para ele. Até a virada, o CRM segue atendendo.
2. **Escopo da v1:** tudo o que acontece dentro da conversa, mais o lembrete por
   template. As automações ficam só no Telegram até cada uma ter template
   aprovado.
3. **Abordagem:** um adaptador nas bordas. A lógica de conversa não é reescrita.
   O que chega do WhatsApp é convertido para o formato interno que os handlers já
   processam, e todo envio a cliente passa por um portão único que escolhe o
   canal.
4. **Telegram continua o lugar de teste.** A decisão D10 (testar tudo no Telegram
   primeiro) é revista: o roteiro de testes continua no Telegram, mas o WhatsApp
   entra agora, testado primeiro com o número de teste da Meta.

## Arquitetura

### `core/integrations/whatsapp.py` (novo)

Cliente da Cloud API, no mesmo molde do `telegram.py`. Um `httpx.AsyncClient`
compartilhado, fechado no shutdown como os outros.

- `send_text(to, text)`
- `send_buttons(to, body, buttons)`: até 3 botões de resposta
  (`[(título, id)]`)
- `send_list(to, body, button_label, rows)`: até 10 linhas (`[(título, id)]`)
- `send_template(to, name, language, body_params, button_payloads)`
- `mark_read_typing(message_id)`: marca a mensagem recebida como lida e mostra
  "digitando…"
- `download_media(media_id)`: pega a URL da mídia e baixa os bytes, com o token
  no cabeçalho
- `verify_signature(raw_body, header)`: HMAC-SHA256 do corpo com o App Secret,
  comparado ao `X-Hub-Signature-256` em tempo constante

Toda chamada devolve `bool` (ou `None` para mídia), como o `telegram.py`. Um erro
de transporte ou uma resposta não-2xx viram `False` e uma linha de log. O código
de erro da Meta vai no log. O erro de janela fechada (`131047`) é distinguido:
o envio devolve um resultado que diz "janela fechada", usado pela Inbox
(ver Erros).

### `core/channels.py` (novo): o portão único de envio ao cliente

- `is_whatsapp(chat_id)`: `True` quando o `chat_id` começa com `wa:`
- `send(chat_id, text, keyboard=None)`: com `wa:`, vai pelo WhatsApp, convertendo
  o teclado (ver Saída). Sem prefixo, chama `telegram.send_message` exatamente
  como hoje.
- `typing(chat_id, message_ref=None)`: no Telegram, `send_typing`. No WhatsApp,
  `mark_read_typing` da mensagem que acabou de chegar.
- `download_voice(file_id)`: `wa-media:<id>` baixa da Meta. Qualquer outro
  `file_id` usa o `telegram.download_voice`.

Passam por este portão todos os envios **ao cliente**: as respostas do handler,
a pergunta de período, os horários, a confirmação, o cancelamento, o aviso de
fora do horário, o aviso de limite de mensagens, a resposta e a devolução da
Inbox, o encerramento por inatividade e o lembrete. Continuam chamando o
Telegram diretamente o que é **da equipe**: o grupo, o aviso privado a quem
assumiu, `/resumo`, `/painel` e os convites do painel. A equipe continua no
Telegram. Exceção (08/10/2026): o `/painel` pedido pelo WhatsApp por alguém da
equipe responde pelo próprio WhatsApp, porque é o único caminho de volta.

### Rota `POST /whatsapp/webhook` e `GET /whatsapp/webhook` (`core/web/app.py`)

- `GET`: verificação da Meta. Se `hub.mode == "subscribe"` e `hub.verify_token`
  bate com `WHATSAPP_WEBHOOK_VERIFY_TOKEN`, devolve `hub.challenge` como texto.
  Senão, 403.
- `POST`: confere a assinatura antes de ler o JSON. Assinatura ausente ou errada
  devolve 401. Sem WhatsApp configurado, devolve 503. Converte cada mensagem do
  corpo (ver Entrada) e enfileira na **mesma fila** do Telegram, com a mesma regra
  de fora do horário. Devolve 200 assim que tudo foi gravado na fila. Se a fila
  falhar, devolve 503, e a Meta tenta de novo.

### Identidade

- Contato do WhatsApp: `chat_id = "wa:<wa_id>"` (o número com DDI, só dígitos;
  cabe no `VARCHAR(20)`) e `source = "whatsapp"`. O `CHECK` da migração 0005 já
  aceita `whatsapp`, então **não há migração nova**.
- O nome vem de `contacts[0].profile.name` no webhook, como o `first_name` do
  Telegram.
- A mesma pessoa no Telegram e no WhatsApp continua sendo dois contatos. Juntar
  fica fora desta etapa.

## Entrada: conversão para o formato interno

Cada item de `entry[].changes[].value.messages[]` vira um update no formato que
`process_message` e `process_callback` já leem:

| Do WhatsApp | Vira |
|---|---|
| `text.body` | `message.text` |
| `audio` (inclui nota de voz) | `message.voice = {file_id: "wa-media:<id>", mime_type}`. O limite passa a ser `max_voice_bytes`, porque a Meta não informa a duração. |
| `interactive.button_reply` / `list_reply` | `callback_query` com `data = id` e `_acked = true`, e um `reply_markup` com o botão tocado (título + id), para que `_tapped_label` mostre o texto na Inbox |
| `button` (resposta ao botão do template) | `callback_query` com `data = payload` |
| imagem, documento, figurinha, localização, contato, vídeo | `message` sem texto: o mesmo caminho do "não consigo ler isso" do Telegram |

Sempre: `update_id = "wa:<message.id>"` (é a chave contra duplicados na fila),
`chat = {id: "wa:<from>", type: "private"}`, e `from.first_name` com o nome do
perfil. O id da mensagem da Meta vai junto (`_wa_message_id`), para o
"digitando…".

`statuses[]` (enviado, entregue, lido) é ignorado. Só o `failed` vai para o log,
com o código de erro.

## Saída: conversão no portão

- **Texto:** formatação do Telegram para a do WhatsApp. `*negrito*` e `_itálico_`
  ficam iguais, `` `código` `` vira texto simples e `[texto](url)` vira
  `texto: url`. Corta em 4096 caracteres.
- **Teclado com até 3 botões no total:** botões de resposta. O título é cortado
  em 20 caracteres e o id é o `callback_data` de hoje.
- **De 4 a 10 botões:** mensagem de lista, com o botão de abrir
  "`whatsapp.list_button`" ("Escolher"). Títulos cortados em 24 caracteres.
- **Mais de 10:** lista com os 10 primeiros e uma linha no log. Não acontece nos
  fluxos atuais.
- **Corpo acima de 1024 caracteres com teclado** (o limite das mensagens
  interativas): envia o texto e, em seguida, os botões com
  "`whatsapp.choose_prompt`" ("Escolha uma opção:").
- **Botão de link** (`link_keyboard`): só é usado em mensagens à equipe, que
  ficam no Telegram. No portão, um botão de link vira texto com a URL.

## Janela de 24 horas

A Meta só aceita mensagem livre até 24 horas depois da última mensagem do
cliente.

- **Cálculo:** `inbox_repo.window_open(conn, contact_id)` é verdadeiro quando a
  última linha `role = 'user'` em `sessions` tem menos de 24 horas. Só vale para
  contatos do WhatsApp; no Telegram é sempre verdadeiro.
- **Bot:** responde dentro da janela, porque está respondendo ao que chegou.
- **Inbox:** a API da conversa passa a devolver `window_open`. Com a janela
  fechada, a tela troca a caixa de resposta pelo aviso
  "`inbox.window_closed`" e o `POST /reply` recusa com 409, mesmo se alguém
  forçar.
- **Devolver ao bot com a janela fechada:** fecha a sessão sem enviar a mensagem
  ao cliente e avisa o grupo normalmente. Com a janela aberta, é igual a hoje.
- **Encerramento por inatividade (60 min):** acontece sempre dentro da janela.
  Se a Meta recusar, a sessão fecha mesmo assim, como hoje.

## Lembrete por template

- Template `WHATSAPP_REMINDER_TEMPLATE` (sugestão: `lembrete_conversa`),
  categoria **utilidade**, idioma `pt_BR`.
- Corpo com 4 variáveis: nome, dia, hora e link do Meet. Três botões de resposta
  rápida: Confirmo, Remarcar e Cancelar. No envio, cada botão recebe o payload
  `appt:ok:<id>`, `appt:move:<id>` e `appt:cancel:<id>`, os mesmos de hoje.
- O texto exato do template é entregue ao Paulo junto com o plano, para cadastrar
  no WhatsApp Manager.
- **Sempre por template** para contatos do WhatsApp, com a janela aberta ou não.
  Utilidade dentro da janela não é cobrada, e um caminho só é mais simples.
- **Enquanto `WHATSAPP_REMINDER_TEMPLATE` estiver vazio:** o lembrete de um
  contato do WhatsApp não é enviado e fica um aviso no log. O compromisso segue
  normal para a equipe.

## Automações

Os candidatos das automações excluem contatos do WhatsApp na v1, filtrando por
`source <> 'whatsapp'` nas consultas de candidatos. Assim nenhuma mensagem
iniciada pela empresa sai pelo WhatsApp sem template.

## Erros

| Situação | Comportamento |
|---|---|
| A Meta recusa um envio | `False`, como no Telegram. Quem chama já trata, e a Inbox mostra "não foi possível enviar". |
| Janela fechada na Meta (`131047`) | Na Inbox, vira o aviso de janela fechada, mesmo se a nossa conta dizia aberta. |
| Token inválido ou expirado (`190`) | Linha de erro no log, com a palavra `WHATSAPP_ACCESS_TOKEN`. |
| Assinatura inválida no webhook | 401, nada é enfileirado. |
| WhatsApp não configurado | O webhook devolve 503 e o resto não muda. |
| Falha ao baixar o áudio | O mesmo caminho do áudio que não deu para transcrever. |

## Configuração (`core/config.py` e `.env.example`)

`WHATSAPP_APP_SECRET`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN` e
`WHATSAPP_WEBHOOK_VERIFY_TOKEN` são os obrigatórios para ligar. Também entram
`WHATSAPP_APP_ID` e `WHATSAPP_BUSINESS_ACCOUNT_ID` (informativos),
`WHATSAPP_REMINDER_TEMPLATE` e `WHATSAPP_GRAPH_VERSION` (com um padrão).
`settings.whatsapp_configured()` só é verdadeiro com os quatro obrigatórios.
Sem eles, o webhook devolve 503 e nenhum envio tenta o WhatsApp. Por isso o
webhook nunca aceita um corpo sem assinatura conferida.

## Textos (`content/messages.toml`)

`[whatsapp]`: `list_button` e `choose_prompt`. `[inbox]`: `window_closed`. O
resto das mensagens é o mesmo do Telegram.

## Testes

- **Webhook:** assinatura válida e inválida, handshake `GET`, conversão de texto,
  áudio, botão, item de lista, resposta de template, tipo não suportado e status.
  Duas entregas da mesma mensagem enfileiram uma vez. Sem configuração, 503.
- **Portão:** `wa:` vai para o WhatsApp e o resto para o Telegram, com os mesmos
  argumentos de hoje. Conversão de teclado (3 viram botões; 4 a 10, lista; mais
  de 10, corta e loga), de formatação e de corpo longo com teclado.
- **Fluxos reais com contato `wa:`:** saudação, FAQ, agendar com período e
  horário, cancelar com confirmação, áudio e pessoa assumindo pela Inbox,
  reaproveitando o fixture de `tests/test_desk_flow.py`.
- **Janela de 24h:** Inbox bloqueada com a janela fechada, `POST /reply` recusado,
  devolver sem mensagem ao cliente e `131047` virando janela fechada.
- **Lembrete:** sai por template com os payloads certos e é pulado sem template
  configurado.
- **Automações:** não escolhem contatos do WhatsApp.
- **Telegram inalterado:** os 501 testes atuais passam.

## Teste real e virada

1. Construir e testar com o **número de teste da Meta** (vem com o app, manda para
   até 5 telefones cadastrados), com o token temporário. Nada toca no número do
   CRM.
2. Revisão do Codex.
3. O Paulo cadastra o template do lembrete e a Meta aprova.
4. Token permanente (usuário do sistema) e Verify Token na VPS.
5. **Situação confirmada pelo Paulo em 06/10/2026:** o número está na Cloud API
   desde sempre (nunca foi usado no aplicativo do celular), a conta do WhatsApp
   Business e o app do agente ficam no **mesmo Business Suite**, e o CRM é
   **outro app**. Por isso a virada é uma troca de qual app recebe, sem migrar o
   número de conta.
6. **Virada do número** (passo a passo entregue junto com o plano): inscrever o
   app do agente na conta do WhatsApp Business, apontar o webhook para
   `https://bot.getconverge.me/whatsapp/webhook` e, **no mesmo momento**,
   desligar o robô e a inscrição do CRM nesse número. Enquanto os dois apps
   estiverem inscritos, cada mensagem chega aos dois e o cliente recebe duas
   respostas.
7. **Volta atrás:** reinscrever o app do CRM e religar o robô dele, e
   desinscrever o app do agente. O número, o nome de exibição e os modelos
   aprovados nunca saem da conta.
8. O histórico de conversas do CRM **não migra** para o painel. Ele continua no
   CRM, enquanto o CRM for mantido. O Paulo confirmou que isso não é problema.

## Fora desta etapa

- Automações pelo WhatsApp (precisam de um template de marketing aprovado por
  automação)
- Reabrir uma conversa com a janela fechada usando template, pela Inbox
- Juntar o mesmo cliente do Telegram e do WhatsApp em um contato só
- Importar o histórico de conversas do CRM
- Instagram Direct e Facebook Messenger (entram pelo mesmo portão depois)
