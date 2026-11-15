# Canal WhatsApp — guia para o revisor

## Rodada 4 — confira as correções da rodada 3

A rodada 3 (Codex `gpt-6.1-sol`, commit `2132509`) deu **3/5**, com dois P2.
Conferi os dois no código e corrigi ambos:

| # | Achado | Correção | Testes |
|---|---|---|---|
| P2 | O horário de entrada era desfeito quando o turno falhava (rollback em `core/agent.py`) | O worker grava o horário numa transação própria, já confirmada, antes do turno: `handler.note_inbound(conn, update)`, com o mesmo `GREATEST`. O turno que falha volta atrás sozinho | `tests/test_agent.py::test_when_the_customer_wrote_survives_a_failed_update`, `test_note_inbound_reads_messages_and_taps` |
| P2 | Uma primeira mensagem ilegível impedia o aviso "Nova conversa" (regressão também no Telegram) | O aviso sai antes de a entrada ilegível ser gravada | `test_a_first_unreadable_message_still_announces_the_conversation` |

**Mensagens que esperam na fila adiada:** o horário só é gravado quando o
update é processado. Em produção nada é adiado (`DESK_ALWAYS_OPEN=true`). Com
o adiamento ligado, a mensagem nem aparece na Inbox antes de ser processada, e
a regra de mensagem vencida cobre o que passar de 24h. Por isso mantive o
webhook sem escrita no Postgres. Ele continua dependendo só da fila.

**Verificação:** 583 testes, Ruff e Pyright limpos.

---


## Rodada 3 — confira as correções da rodada 2

A rodada 2 (Codex `gpt-6.1-sol`, no commit `4cdcd3f`) deu **3/5**, com três P2.
Conferi cada um no código e corrigi todos no commit seguinte a `4cdcd3f`:

| # | Achado | Correção | Testes |
|---|---|---|---|
| P2 | Uma mensagem vencida contava no limite de mensagens, e uma atual vinda depois era bloqueada | Nos dois handlers, a ordem passou a ser: lock, mensagem vencida (grava e sai), horário de entrada, e só então o limite | `test_expired_messages_do_not_use_up_the_message_limit` |
| P2 | Os retornos antecipados (limite atingido, `/id`, toque bloqueado) não moviam a janela | Todo update válido do WhatsApp registra o horário de entrada logo depois do lock, antes de qualquer retorno (`_note_inbound`) | `test_a_rate_limited_whatsapp_message_still_moves_the_window`, `test_id_on_whatsapp_still_moves_the_window` |
| P2 | O aviso de fora do horário, no webhook, ignorava a mensagem vencida | `_accept` só manda o aviso se a mensagem ou o toque ainda estiver dentro da janela. O enfileiramento e a deduplicação não mudam | `test_an_old_whatsapp_message_gets_no_out_of_hours_notice` (texto e toque) |

**Mudança de estrutura:** a regra de tempo (`sent_at` e `expired`) saiu do
handler e foi para `core/channels.py`, para o webhook e o handler usarem a
mesma.

**Ajuste nos testes:** o helper `_text` do teste do webhook usava
`timestamp: "1"` e passou a usar o horário atual.

**Verificação:** 580 testes, Ruff e Pyright limpos.

---


## Rodada 2 — confira as correções da rodada 1

A rodada 1 (no commit `46d389f`) apontou um P1, três P2 e um ponto estrutural.
Conferi cada um no código e corrigi todos no commit `2743d93`, com testes que
falharam antes:

| # | Achado | Correção | Testes |
|---|---|---|---|
| P1 | A janela de 24h contava do processamento, não de quando o cliente escreveu, e uma mensagem processada com atraso ganhava resposta impossível | Nova coluna `contacts.last_inbound_at` (migração `0006`, aditiva, com preenchimento a partir de `sessions`). O conversor leva o `timestamp` da Meta como `date`, e o `get_or_create(..., inbound_at=)` guarda o mais recente (`GREATEST`). `window_open` passa a ler essa coluna. Uma mensagem ou toque do WhatsApp processado mais de 24h depois de escrito é gravado como `expired`, e o bot não tenta responder | `test_every_inbound_update_says_when_the_customer_wrote`, `test_a_whatsapp_message_older_than_a_day_gets_no_bot_reply`, `test_an_old_whatsapp_tap_gets_no_bot_reply`, `test_an_old_telegram_message_is_still_answered`, e no conversor `date` em texto e toque |
| P2 | Uma imagem sem ninguém com a conversa não reabria a janela | O contato é tocado com o horário original. A entrada fica gravada como `user` (`inbox.unreadable`), e a resposta do bot também, se foi entregue | `test_an_unreadable_whatsapp_message_still_counts_as_the_customer_writing` |
| P2 | O timeout mandava texto livre com a janela fechada e gravava mesmo sem entrega | Com a janela do WhatsApp fechada, avisa só a equipe. O aviso ao cliente só é gravado se foi entregue, nos dois canais | `test_a_timeout_past_the_whatsapp_window_only_tells_the_team`, `test_a_timeout_notice_that_did_not_go_out_is_not_recorded` |
| P2 | O 131047 na devolução virava 502 e desfazia a devolução | É tratado como devolução com a janela fechada: fecha, avisa o grupo, não grava nada ao cliente | `test_giving_back_when_meta_says_the_window_closed` |
| — | O `/painel` da equipe passava pelo portão de clientes | Vai direto para o `telegram.send_message` | `test_the_panel_address_goes_to_telegram_directly` |

**Contexto do P1:** em produção, `DESK_ALWAYS_OPEN=true`, então nada é adiado
hoje. A correção cobre o adiamento, se um dia ele for ligado, e qualquer atraso
por queda. Com o adiamento ligado, uma mensagem de WhatsApp de fim de semana
ficaria sem resposta do bot. Isso fica anotado para o Paulo decidir.

**Verificação:**
- 575 testes, Ruff e Pyright limpos.
- No Postgres real, a migração `0006` foi aplicada. Uma mensagem de 30h atrás
  deixa a janela fechada e uma de 2h a abre. Uma mensagem mais antiga
  processada depois não volta o horário. O preenchimento cobriu todos os
  contatos com mensagem. Tudo numa transação desfeita.
- **Ajuste nos testes antigos:** o helper `_meta` usava `timestamp: "1"` (1970),
  que a nova regra lê corretamente como atrasado. Ele passou a usar o horário
  atual.

**Foco desta rodada:**
- `contacts.get_or_create` com `inbound_at` em todos os caminhos de entrada (os
  cinco chamadores em `core/handlers/message.py`);
- o caminho `expired` antes do limite de mensagens;
- a migração `0006` sobre um banco que já tem a `0005`.

---


Branch `feat/whatsapp`. A especificação aprovada está em
`docs/superpowers/specs/2026-10-05-whatsapp-design.md`, e o plano executado em
`docs/superpowers/plans/2026-10-06-whatsapp.md`. O guia de operação é
`docs/WHATSAPP.md`.

## O que entra

O agente passa a atender pelo WhatsApp (Cloud API da Meta, sem intermediário),
com a mesma conversa do Telegram. O desenho é um **adaptador nas bordas**:

| Arquivo | Papel |
|---|---|
| `core/integrations/whatsapp.py` | Cliente da Meta: texto, botões, lista, template, "lido + digitando", mídia e assinatura. Cada envio devolve `"ok"`, `"failed"` ou `"window_closed"` (erro 131047). |
| `core/integrations/whatsapp_updates.py` | Funções puras: o webhook da Meta vira o update no formato do Telegram que os handlers já leem (texto, voz, toque em botão). |
| `core/channels.py` | O portão único de envio **ao cliente**. `wa:` vai para o WhatsApp, convertendo o teclado do Telegram (até 3 botões de resposta, de 4 a 10 numa lista); o resto vai para o Telegram exatamente como antes. |
| `core/web/app.py` | `GET`/`POST /whatsapp/webhook`. A fila e a regra de fora do horário são as mesmas do Telegram, via `_accept`, extraído da rota do Telegram. |
| `core/handlers/message.py`, `core/services/handoff.py`, `core/services/inbox.py` | Os envios ao cliente passam pelo portão. Os envios à equipe (`post_message`) continuam no Telegram. |
| `core/repositories/inbox.py`, `core/web/panel.py`, `core/web/static/painel/inbox.js` | Janela de 24h: a Inbox bloqueia a resposta, `reply` recusa com 409 e "Devolver ao bot" fecha sem mensagem ao cliente. |
| `core/services/reminders.py` | O lembrete de um contato do WhatsApp sai pelo template, com os mesmos payloads de botão do Telegram. |
| `core/repositories/automations.py`, `core/services/automations.py` | Automações não escolhem nem enviam para contatos do WhatsApp. |
| `core/repositories/contacts.py` | Um contato novo `wa:` nasce com `source = 'whatsapp'`. |

Não há migração: o `CHECK` de `contacts.source` (0005) já aceitava `whatsapp`.

## Onde focar

1. **Assinatura:** o HMAC é calculado sobre os bytes crus, antes de ler o JSON.
   Confira que nenhum caminho enfileira algo sem assinatura válida e que o
   `GET` de verificação não responde o desafio sem o token certo.
2. **Duplicados:** a Meta reentrega. O `update_id` é `wa:<message.id>`, e a fila
   deduplica por ele. Confira que a reentrega não repete o aviso de fora do
   horário.
3. **Nada da equipe pelo portão, nada da empresa sem template:** procure
   qualquer `channels.send` com destino de equipe e qualquer envio que a empresa
   inicia para `wa:` fora do lembrete (automações, `attendance`, `secretarial`,
   avisos de inatividade).
4. **Janela de 24h:** a contagem usa a última mensagem `role = 'user'` em
   `sessions`. Confira o caminho em que a nossa conta diz "aberta" e a Meta
   responde 131047 (vira 409, não 502), e o "Devolver ao bot" com a janela
   fechada (fecha, avisa o grupo, não envia nem grava ao cliente).
5. **Telegram inalterado:** o portão chama
   `telegram.send_message(chat_id, text, parse_mode=..., reply_markup=...)`.
   Confira se algum caminho mudou de `parse_mode` ou de teclado sem querer.
6. **Itens que nenhum teste exercita** (do plano): lote com várias mensagens,
   outro número na mesma conta, só status, número de 15 dígitos e áudio acima
   do limite. Todos têm teste, mas vale ler o código com eles em mente.

## Decisões tomadas na execução

As decisões estão no ledger
`.superpowers/sdd/2026-10-06-whatsapp/progress.md` (git-ignored) e são
repetidas aqui:

- **Teste do cliente:** o token falso passou a ser `TOK-S3CRET-9`. Com "tok", a
  verificação "o token nunca vai para o log" casava com a palavra "token".
- **`test_a_channel_without_integration_says_so`:** passou a usar `instagram`,
  porque o WhatsApp agora é integrado. O caminho do 501 continua coberto.
- **Lembrete:** também não sai (e fica no log) quando o próprio WhatsApp não
  está configurado, não só quando o template está vazio.

## Revisão própria antes desta

Eu revisei o branch inteiro antes de passar para você. Achei um problema e o
corrigi com teste que falhou antes:

- Um token de verificação ou um cabeçalho de assinatura com acento fazia o
  `hmac.compare_digest` levantar `TypeError`, e a rota respondia 500 em vez de
  403/401. Agora a comparação é em bytes. Testes:
  `test_odd_characters_are_refused_not_a_crash` e
  `test_a_signature_with_odd_characters_is_false`.

Ficaram anotados, sem correção:

- Mensagens do WhatsApp que chegam enquanto uma pessoa está com a conversa não
  são marcadas como lidas (sem os tiques azuis) até o bot responder de novo.
- Um corpo assinado pela Meta, mas com estrutura inesperada (`entry` que não é
  lista), daria 500. Só a Meta assina.

## Limitações aceitas

- Sem template aprovado, o lembrete de contato do WhatsApp não sai. O
  compromisso fica marcado como lembrado e o aviso vai para o log.
- A mesma pessoa no Telegram e no WhatsApp são dois contatos.
- Reabrir uma conversa com a janela fechada por template fica para depois.
- A Meta não informa a duração do áudio, então o limite é de bytes
  (`MAX_VOICE_BYTES`).

## Como rodar

```bash
DB_HOST=<postgres local> .venv/bin/python -m pytest -p no:warnings
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

Não exporte `AWS_*`/`RESTIC_*` no shell antes do pytest (quebra
`test_backup.py`).

## Verificação feita

- Suíte completa, Ruff e Pyright limpos.
- Postgres real: `window_open` verdadeiro com 23h e falso com 25h; o contato
  `wa:` nasce `whatsapp`; o filtro das automações recusa `wa:` e aceita Telegram.
- Navegador (mock da API): com a janela fechada, a caixa e o Enviar ficam
  bloqueados e o aviso aparece, no desktop e no celular.
- **Falta:** o teste real com o número de teste da Meta. Ele depende do token
  temporário e do Verify Token na VPS.
