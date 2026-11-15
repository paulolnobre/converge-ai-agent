# Inbox no painel — especificação

Data: 29/09/2026 · Status: aguardando revisão do Paulo

## Objetivo

Uma aba **Inbox** no `/painel` onde toda a equipe vê as conversas em andamento,
assume qualquer uma e responde ao cliente dali mesmo. O grupo do Telegram deixa
de receber cópia das mensagens: passa a receber só avisos curtos com o botão
**Abrir no painel**. Cada contato passa a ter **canal** (por onde conversa) e
**origem** (como chegou à Converge).

**Sucesso significa:**
- O grupo da equipe só tem avisos de uma linha: conversa nova, pedido de
  pessoa, quem assumiu e quem devolveu. Nenhuma mensagem de cliente é copiada.
- Quem assume responde pelo painel, no computador ou no celular, e o bot fica
  em silêncio enquanto isso.
- Nenhuma resposta humana sai para o cliente de quem não assumiu a conversa.
- Lembretes de compromissos já marcados continuam sendo enviados normalmente,
  inclusive durante um atendimento assumido; são avisos transacionais.
- Todo contato tem canal, e a origem aparece quando é conhecida.

## Decisões já tomadas com o Paulo

1. A Inbox mostra **todas** as conversas em andamento, não só as que pediram
   pessoa.
2. O grupo recebe aviso em **conversa nova** e em **pedido de pessoa**. Conta
   como conversa nova a primeira mensagem de um contato, ou a primeira depois de
   **24h** sem mensagem dele.
3. A resposta ao cliente sai **só pelo painel**. O repasse pelo privado do
   Telegram (`/assumir`, `/encerrar`, responder ao card no grupo) é removido.
4. O painel se atualiza **consultando o servidor**: a cada 4s com uma conversa
   aberta e a cada 10s na lista, com pausa quando a aba não está visível.
5. Quem assumiu recebe no **privado do Telegram** um aviso sem conteúdo quando
   o cliente escreve ("Nova mensagem de Ana — Abrir no painel"). É no máximo um
   a cada 5 minutos por conversa.
6. Cada contato tem dois campos, **canal** e **origem**.

## Fluxo

### Mensagem do cliente chega

1. `contacts_repo.get_or_create` (como hoje).
2. **Aviso de conversa nova.** Uma única instrução SQL reserva o aviso se o
   contato não tem mensagem própria nas últimas 24h e não recebeu esse aviso
   nas últimas 24h. Isso roda **antes** de gravar a mensagem, e duas mensagens
   chegando juntas geram um aviso só. Se a reserva der certo, o grupo recebe
   `inbox.new_conversation` com o botão. Os chats da própria equipe
   (`staff.chat_id`) nunca geram aviso.
3. **Há atendimento ativo?** O `handoff.handle_customer_message` decide:
   - **Assumido:** a mensagem é gravada, o `last_activity_at` é atualizado, sai
     o aviso no privado de quem assumiu (respeitando os 5 minutos) e o bot
     **não** responde.
   - **Aguardando pessoa:** a mensagem é gravada e o bot responde normalmente.
     **Nada** é copiado no grupo (sai `echo_bot_reply`).
   - **Nenhum:** segue o fluxo normal do bot.
4. `/bot` do cliente continua encerrando o atendimento e devolvendo ao bot.

### Pedido de pessoa

`handoff.start` continua abrindo a sessão (uma por contato). Em vez do card com
as últimas 8 mensagens, o grupo recebe `inbox.needs_person` (nome, canal,
motivo) com o botão. Se já havia sessão ativa, nada novo é postado, como hoje.

### No painel

- **Assumir:** se não existe sessão ativa, cria uma com motivo `painel` e já a
  marca como assumida. Se existe e está aguardando, assume. O grupo recebe
  `inbox.claimed` ("Paulo assumiu a conversa com Ana").
- **Assumir no lugar:** a sessão é de outra pessoa. Passa para quem clicou, e o
  grupo recebe `inbox.reassigned`.
- **Responder:** só quem assumiu. O texto é formatado com o `handoff.relay` já
  existente (`Paulo: resposta`) e enviado pelo canal do contato. Só se o envio
  der certo a mensagem é gravada (`role='assistant'`, `intent='relay'`,
  `author_user_id` = quem escreveu) e o `last_activity_at` é atualizado.
- **Devolver ao bot:** fecha a sessão, envia ao cliente `handoff.back_to_bot`
  (gravado no histórico), e o grupo recebe `inbox.released`.
- **Inatividade:** `sweep_timeouts` continua igual, avisando o cliente e o
  grupo. Os textos deixam de citar `/encerrar`.

### Links do botão

`{PUBLIC_BASE_URL}/painel#inbox/{contact_id}`. Sem `PUBLIC_BASE_URL`, o aviso
sai sem botão. Se a sessão do painel expirou, o painel leva ao login levando
junto o `#inbox/<id>`. Depois do login, a página volta para esse endereço, e só
aceita o formato `#inbox/<número>`: nada de redirecionar para outro lugar.

## Dados — `infra/migrations/0005_inbox.sql`

```sql
-- Canal: contacts.source já existe (init.sql, "the channel the person first
-- arrived through", sempre 'telegram' hoje). Ganha a lista de canais válidos.
ALTER TABLE contacts ADD CONSTRAINT contacts_source_channel
  CHECK (source IN ('telegram', 'whatsapp', 'instagram', 'messenger', 'email'));  -- dentro de um DO, só se não existir

-- Origem, e o aviso de conversa nova
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS origin VARCHAR(30)
    CHECK (origin IN ('anuncio_instagram', 'anuncio_facebook', 'site', 'google',
                      'indicacao', 'outro')),
  ADD COLUMN IF NOT EXISTS origin_detail VARCHAR(160),
  ADD COLUMN IF NOT EXISTS origin_set_by INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS conversation_noticed_at TIMESTAMPTZ;

-- Quem assumiu pelo painel, e o limite do aviso no privado
ALTER TABLE handoff_sessions
  ADD COLUMN IF NOT EXISTS claimed_by_user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS last_ping_at TIMESTAMPTZ;

-- Quem da equipe escreveu cada mensagem enviada pelo painel
ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS author_user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL;
```

**Nomes:** o canal continua na coluna existente `contacts.source` (a API a
expõe como `channel`), e a origem usa `origin`, `origin_detail` e
`origin_set_by`. Assim não há duas colunas com o mesmo significado.

- `claimed_by_id` (id do Telegram) e `handoff_messages` ficam no banco, sem uso
  novo. Uma sessão assumida pelo fluxo antigo no momento do deploy aparece como
  "Com <nome>" e pode ser assumida no lugar.
- O canal é um rótulo nesta etapa. A troca da identidade `chat_id` por
  `(channel, external_id)` continua sendo a Fase 5.2, junto com o WhatsApp.

## Canal e origem

- **Canal:** é preenchido por quem recebe a mensagem. Hoje todo contato é
  `telegram`, e o WhatsApp, o Instagram e o Messenger preenchem o seu quando
  existirem. Na tela aparece como "Telegram", "WhatsApp", "Instagram" ou
  "Facebook" (`messenger`).
- **Origem:** vale a **primeira** que aparecer, e a equipe pode trocar.
  1. **Pelo link do Telegram:** `t.me/<bot>?start=<origem>` chega como
     `/start <origem>`. Valores aceitos: `site`, `google`, `indicacao`,
     `anuncio_instagram`, `anuncio_facebook`. Qualquer outro é ignorado. Serve
     para links do bot compartilhados em algum lugar (bio, e-mail, anúncio).
     Os botões do site hoje apontam para o WhatsApp (`wa.me`), então o site não
     muda nesta etapa.
  2. **Pelos anúncios da Meta** (quando o WhatsApp, o Instagram e o Facebook
     existirem): o `referral` do webhook preenche `anuncio_instagram` ou
     `anuncio_facebook`, e `origin_detail` recebe o nome do anúncio. Isso fica
     documentado aqui e é implementado junto com o canal, não agora.
  3. **Pelo painel:** um seletor no cabeçalho da conversa na Inbox e no
     detalhe do contato. `origin_detail` é texto livre ("indicado pelo João").
     O painel grava quem mudou em `origin_set_by`.
- **Onde aparece:**
  - **Inbox:** canal na linha e no cabeçalho; filtro por canal.
  - **Contatos:** colunas Canal e Origem, com filtro pelas duas.
  - **Painel:** um card "De onde vêm os contatos (30 dias)" com barras por
    origem, incluindo "Não informado".
  - **Aviso no grupo:** o canal aparece.

## API (`core/web/panel.py`, todas exigem login com MFA)

| Método | Caminho | O que faz |
|---|---|---|
| GET | `/painel/api/inbox?filter=waiting\|mine\|bot\|all&channel=` | Lista: id, nome, canal, origem, situação (`bot`, `waiting`, `claimed`), dono, `mine`, prévia da última mensagem (120 caracteres, com o papel), hora; mais `waiting_total` para o menu |
| GET | `/painel/api/inbox/{contact_id}?after=<id>` | Contato (nome, telefone, e-mail, etapa, canal, origem), sessão e mensagens (as últimas 200, ou só as depois de `after`) com autor |
| POST | `/painel/api/inbox/{contact_id}/claim` | Assumir ou assumir no lugar |
| POST | `/painel/api/inbox/{contact_id}/release` | Devolver ao bot |
| POST | `/painel/api/inbox/{contact_id}/reply` `{text}` | Enviar resposta |
| PATCH | `/painel/api/contacts/{contact_id}/origin` `{origin, detail}` | Mudar a origem |

**Entram na lista** as conversas com mensagem nos últimos 7 dias ou com sessão
ativa. Chats da equipe ficam fora. A ordem é pela última mensagem, a mais nova
primeiro.

**Erros:**

| Código | Quando | O que a tela diz |
|---|---|---|
| 409 | Responder sem ser o dono | "Victor assumiu esta conversa." |
| 409 | Devolver uma conversa que não está com ninguém | — |
| 422 | Texto vazio ou com mais de 4000 caracteres | — |
| 502 | O canal recusou o envio | "O Telegram não entregou a mensagem (o cliente pode ter bloqueado o bot)." Nada é gravado. |
| 501 | Canal sem integração ainda | "Canal ainda não conectado." |

## Serviço — `core/services/inbox.py` (novo)

Uma única porta de envio, `send(contact, text) -> bool`, escolhida pelo
`contact.channel`. Hoje só implementa `telegram`, e é aqui que o WhatsApp entra
depois. Também ficam aqui:
- `claim`, `release` e `reply`, com as regras de dono e o aviso no grupo;
- `notify_new_conversation`;
- `ping_owner`, o aviso no privado com o limite de 5 minutos, pelo
  `last_ping_at` atualizado atomicamente.

Para achar o Telegram de quem assumiu, liga `panel_users.email` a
`staff.email` e usa o `staff.chat_id`. Se a pessoa não estiver no `staff`, fica
sem aviso no privado, e isso vai para o log.

Repositório novo: `core/repositories/inbox.py` (lista, conversa, reservas
atômicas). `core/repositories/handoffs.py` ganha `claim_by_user`.

## Textos (`content/messages.toml`, seção `[inbox]`)

| Chave | Placeholders |
|---|---|
| `new_conversation` | `{nome}`, `{canal}` |
| `needs_person` | `{nome}`, `{canal}`, `{motivo}` |
| `claimed` | `{nome}`, `{atendente}` |
| `reassigned` | `{nome}`, `{atendente}`, `{anterior}` |
| `released` | `{nome}`, `{atendente}` |
| `owner_ping` | `{nome}` |
| `open_button` | — |

Todas são validadas em `_PLACEHOLDERS`. Saem do `messages.toml` os textos que
só o repasse antigo usava: `dm_confirmation`, `dm_switch_notice`,
`dm_unavailable`, `not_your_session`, `ambiguous_claims`, `relay_failed` e
`card`. Os textos de inatividade são revistos para não citar `/encerrar`.

## Tela (`core/web/static/painel/inbox.js`, novo; `index.html`, `painel.css`)

- **Menu:** item "Inbox" com o contador de "esperando pessoa".
- **Computador:** a lista fica à esquerda (cerca de 340px) e a conversa à
  direita.
  - **Lista:** chips de filtro (Esperando pessoa · Comigo · Com o bot · Todas)
    e seletor de canal. Cada linha mostra nome, canal, situação, prévia e hora.
  - **Conversa:**
    - **Cabeçalho:** nome, telefone, e-mail, etapa, canal, seletor de origem e
      o botão de ação (Assumir, Assumir no lugar ou Devolver ao bot).
    - **Mensagens:** cliente à esquerda; bot e equipe à direita, com o nome.
    - **Caixa de resposta:** Enter envia e Shift+Enter quebra a linha. Se a
      conversa não é sua, a caixa fica desativada com "Com Victor — assuma
      para responder".
- **Celular (≤860px):** a lista ocupa a tela, e tocar abre a conversa em tela
  cheia com "‹ Voltar". No celular, Enter quebra a linha e o botão envia.
- **Atualização:** a lista a cada 10s e a conversa aberta a cada 4s, pedindo só
  as mensagens depois da última recebida. Para com `document.hidden`. A rolagem
  só desce sozinha se a pessoa já estava no fim.
- **Segurança:** tudo com `textContent`, como no resto do painel. `inbox.js`
  entra na lista `_ASSETS`.

## O que sai do código

- `handoff.handle_team_reply`, `handle_private_reply`, `_handle_reply`,
  `_claim_and_switch`, `echo_bot_reply`, `_card` e as chamadas deles em
  `core/handlers/message.py`.
- Os testes do repasse pelo Telegram em `tests/test_desk_flow.py` e onde mais
  existirem, trocados pelos testes novos.

## Testes

- **`tests/test_inbox.py` (serviço, com repositórios em memória):**
  - assumir, assumir no lugar e devolver, cada um com um aviso no grupo;
  - só o dono responde, e quem não é recebe 409 sem nada enviado;
  - um envio que falha não é gravado;
  - o aviso no privado respeita os 5 minutos;
  - sem `PUBLIC_BASE_URL`, o aviso sai sem botão.
- **`tests/test_desk_flow.py`:**
  - uma conversa assumida não chama o bot;
  - uma conversa aguardando é respondida pelo bot sem cópia no grupo;
  - conversa nova (primeira mensagem, e depois de 24h) avisa uma vez, e o chat
    da equipe não avisa;
  - `/start site` grava a origem só se ela estiver vazia.
- **`tests/test_panel.py`:**
  - login exigido nas rotas novas;
  - filtros da lista;
  - `after`;
  - os códigos de erro 409, 422, 502 e 501;
  - PATCH de origem com valor inválido dá 422;
  - `inbox.js` entra em `_ASSETS`.
- **`tests/test_content.py`:** os placeholders novos.
- **Postgres de verdade:**
  - a reserva do aviso de conversa nova com duas mensagens simultâneas gera um
    aviso só;
  - a lista e a conversa trazem os contatos certos;
  - a migração roda duas vezes sem erro.
- **Navegador (Playwright), no computador e no celular:**
  - assumir, responder, ver a resposta aparecer e devolver;
  - trocar a origem;
  - o link `#inbox/<id>` depois do login.

## Entrega

1. Um commit, sem coautoria, com os testes passando. O deploy pela `main`
   aplica a `0005`.
2. Atualizar o `docs/ROADMAP.md` (1.3 passa a descrever a Inbox), o
   `.docs/ROADMAP.md` e o `.docs/DECISOES.md` (D6 e uma decisão nova: responder
   pelo painel).
3. Adicionar uma seção "Inbox" ao roteiro de testes.

## Fora desta etapa

- Anexos, áudio e imagem enviados pelo painel. No histórico eles aparecem como
  hoje, só como texto.
- "Não lida" por pessoa, e notificação do navegador.
- A janela de 24h do WhatsApp na caixa de resposta. Entra com o canal.
- A origem por `referral` da Meta. Entra com o canal.
- A origem "site" para quem vem pelos botões `wa.me` do site: com o WhatsApp,
  o link pode levar um texto pronto (`?text=`) que identifica o site. Entra com
  o canal.
