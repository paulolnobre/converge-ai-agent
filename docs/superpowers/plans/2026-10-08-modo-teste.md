# Modo teste da equipe (`/teste`) — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alguém da equipe manda `/teste` (ou `/teste antigo`) e passa a ser atendido como cliente, pelo caminho real, com tudo marcado como TESTE e apagado no `/sair` ou depois de 2h sem mensagem.

**Architecture:** O contato do próprio chat de quem testa ganha `contacts.test_mode`. Dois predicados SQL em `core/repositories/contacts.py` decidem onde ele aparece. A marca TESTE nos avisos à equipe e no título do evento sai de um módulo puro, `core/services/test_marks.py`. O fluxo (ligar, sair, expirar, limpar) fica em `core/services/test_mode.py`, sobre o SQL de `core/repositories/test_mode.py`, e entra no turno logo depois de `_note_inbound`, antes do limite de mensagens.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pytest + pytest-asyncio (modo auto), ruff, pyright 1.1.411. Painel em JS puro (`core/web/static/painel/`).

**Spec:** `docs/superpowers/specs/2026-10-08-modo-teste-design.md` (aprovada pelo Paulo em 08/10/2026).

## Global Constraints

- Branch `feat/modo-teste`. Commits **sem** linha `Co-Authored-By` (regra do Paulo).
- Testes: `.venv/bin/python -m pytest -p no:warnings`, sem `AWS_*`/`RESTIC_*` exportados no shell (quebram `test_backup.py`). Todos os testes atuais continuam passando.
- Migração aditiva `infra/migrations/0008_test_mode.sql`, exatamente o SQL da spec: `test_mode VARCHAR(10) CHECK (test_mode IN ('novo', 'antigo'))` e `test_started_at TIMESTAMPTZ`, com `ADD COLUMN IF NOT EXISTS`.
- Comandos com a barra, sem diferenciar maiúsculas: `/teste`, `/teste antigo`, `/sair`. `SAIR` sem barra continua sendo o opt-out.
- Prefixo da equipe: `teste.team_prefix` = `"🧪 TESTE · "`. Título do evento começa com `"[TESTE] "`.
- `TEST_IDLE_MINUTES` padrão `120` (`settings.test_idle_minutes`).
- Textos da seção `[teste]` de `content/messages.toml`, literais da spec:
  - `started_new` = "Modo teste ligado: agora te trato como cliente novo. Tudo fica marcado como TESTE e é apagado quando você mandar /sair, ou depois de 2h sem mensagem."
  - `started_returning` = o mesmo, com "cliente antigo" no lugar de "cliente novo".
  - `ended` = "Teste encerrado e apagado."
  - `expired` = "Teste encerrado por inatividade e apagado."
  - `not_in_test` = "Você não está em modo teste. Mande /teste para começar."
  - `cancel_failed` = "Não consegui cancelar na agenda o evento de {quando}. Apague à mão."
  - `team_prefix` = "🧪 TESTE · "
- Toda limpeza traz a condição de escopo no próprio SQL (`test_mode IS NOT NULL`, ou, só ao ligar, "é o contato do próprio chat de alguém da equipe"). Um erro no Python não pode apagar cliente real.
- Nunca imprimir credenciais. Nada de dado pessoal real em teste: nomes fictícios.

## Decisões que a spec deixou abertas

- **Telefone no perfil "antigo":** usa o número do próprio chat (`"+" + chat_id.removeprefix("wa:")`) em vez de `staff.whatsapp_ids[0]`. É o número que a pessoa está usando, e o `StaffMember` não carrega `whatsapp_ids`.
- **Varredura:** o worker de handoff roda a cada `_SWEEP_SECONDS = 300` (5 min), não a cada minuto como diz a spec. A expiração acontece entre 120 e 125 min, e o plano não muda esse intervalo.
- **`/teste <outra palavra>`:** só `/teste` e `/teste antigo` são comandos. `/teste xyz` é mensagem comum.

## Review Focus

1. **Janela do WhatsApp depois do `/teste`:** a limpeza apaga o contato e, com ele, `last_inbound_at`. O contato recriado precisa nascer com `last_inbound_at` = hora da mensagem do `/teste`. Senão a janela de 24h parece fechada e o bot não consegue responder. Teste na Task 4.
2. **Variações do comando:** `" /Teste "`, `"/TESTE ANTIGO"` e `"/teste@ConvergeBot"` (Telegram) contam como comando. `"/teste xyz"`, `"teste"` e áudio não contam. Teste na Task 4.
3. **`/sair` de quem saiu do `staff.json` no meio do teste:** quem está em teste sai, seja ou não da equipe agora, porque o que vale é `contacts.test_mode`. Teste na Task 4.
4. **Expiração contra uma mensagem em andamento:** a varredura pega o lock do chat e confere de novo, sob o lock, se o contato continua parado. Uma mensagem que chegou nesse meio-tempo cancela a limpeza. Teste na Task 4.
5. **`/teste` com reunião marcada no teste anterior:** os eventos futuros são cancelados no Google antes de o contato ser apagado e marcado de novo. Uma falha do Google não impede a limpeza e aparece no aviso. Teste na Task 4.

---

### Task 1: Coluna, modelo e onde o contato em teste aparece

**Files:**
- Create: `infra/migrations/0008_test_mode.sql`
- Modify: `core/models.py` (`Contact`), `core/repositories/contacts.py` (`_COLUMNS`, `_to_contact`, predicados), `core/repositories/inbox.py`, `core/repositories/funnel.py`, `core/repositories/briefing.py`, `core/repositories/automations.py`
- Test: `tests/test_test_mode.py` (novo), `tests/test_staff_whatsapp.py`

**Interfaces:**
- Produces:
  - `Contact.test_mode: str | None = None` ('novo' | 'antigo' | None), lido em `contacts._COLUMNS` como 11ª coluna.
  - `hidden_from_customer_lists_sql(column: str) -> str`: verdadeiro quando `column` (um chat_id) é da equipe **e** o contato desse chat não está em teste.
  - `left_out_of_metrics_sql(column: str) -> str`: verdadeiro quando o chat é da equipe **ou** o contato desse chat está em teste.
  - Coluna `is_test` (boolean) em `inbox_repo.list_conversations`, `inbox_repo.conversation`, `funnel_repo.list_leads` e `funnel_repo.lead` (via `_LEAD_COLUMNS`).

Os dois predicados recebem a coluna do chat, como `is_staff_sql`, e testam o teste com `EXISTS (SELECT 1 FROM contacts tm WHERE tm.chat_id = {column} AND tm.test_mode IS NOT NULL)`. `is_staff_sql` fica como está.

Troca de `NOT is_staff_sql(...)`:
- por `NOT hidden_from_customer_lists_sql(...)`: as três consultas de `inbox.py` (lista, `count_waiting`, `claim_conversation_notice`) e `funnel.list_leads`;
- por `NOT left_out_of_metrics_sql(...)`: as três de `briefing.py` e `automations._CLIENT_OK`.

Também ganham `NOT left_out_of_metrics_sql('c.chat_id')` as consultas do resumo que hoje não filtram: `appointments_between`, `open_handoffs`, `unrecorded_outcomes` e `stale_proposals`. Onde não houver alias `c`, use `LEFT JOIN contacts c ON c.id = <tabela>.contact_id`.

- [ ] **Step 1: Testes que falham** em `tests/test_test_mode.py`, com o `_Conn` que grava o SQL (copie o de `test_staff_whatsapp.py`):
  - `test_customer_lists_hide_staff_but_show_tests`: parametrizado com lista da Inbox, `count_waiting`, `claim_conversation_notice` e `list_leads`. O SQL contém `whatsapp_ids` e `test_mode IS NOT NULL`, e não usa mais o `is_staff_sql` puro (confira que o trecho de teste está dentro do mesmo `NOT (...)`).
  - `test_metrics_leave_out_staff_and_tests`: parametrizado com as sete consultas de `briefing.py` e `automations._CLIENT_OK`. Todas contêm `test_mode IS NOT NULL`.
  - `test_panel_rows_carry_is_test`: o SQL de `list_conversations`, `conversation` e `list_leads` contém `AS is_test`.
  - `test_contact_reads_test_mode`: `contacts_repo._to_contact` de uma linha com `"antigo"` na 11ª posição dá `test_mode == "antigo"`.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_test_mode.py -p no:warnings`. Esperado: FAIL.
- [ ] **Step 3:** Migração, campo no modelo, predicados e trocas acima. Em `test_staff_whatsapp.py`, a checagem `"whatsapp_ids" in conn.sql` continua valendo, então ele não muda (se mudar, é sinal de que um predicado perdeu o `is_staff_sql`).
- [ ] **Step 4:** a suíte inteira passa.
- [ ] **Step 5: Commit** `feat: coluna test_mode e onde o contato em teste aparece`

### Task 2: Textos, configuração e a marca TESTE

**Files:**
- Create: `core/services/test_marks.py`
- Modify: `content/messages.toml` (seção `[teste]`), `core/content.py` (`_PLACEHOLDERS`), `core/config.py`, `core/handlers/message.py` (`_title`), `core/services/inbox.py`, `core/services/handoff.py`, `core/services/attendance.py`
- Test: `tests/test_test_mode.py`, `tests/test_content.py` (já valida os placeholders)

**Interfaces:**
- Consumes: `Contact.test_mode` (Task 1).
- Produces:
  - `test_marks.team_text(text: str, contact: Contact | None) -> str`: `content.text("teste.team_prefix") + text` quando `contact` existe e tem `test_mode`, senão `text` sem mudança.
  - `test_marks.event_title(title: str, contact: Contact) -> str`: `"[TESTE] " + title` em teste, senão `title`.
  - `settings.test_idle_minutes: int = 120`.
  - `attendance.notify_team(appointment: Appointment, contact: Contact | None, key: str) -> None`: troca `contact_name` por `contact`, para poder marcar.

Pontos que passam por `team_text`, todos com o `Contact` na mão ou buscado por `contacts_repo.get_by_id`:
- `inbox.notify_new_conversation`, `inbox.claim`, `inbox.release` e `inbox.ping_owner`;
- `handoff.start`, o `closed_notice` de `handoff.handle_customer_message` e `handoff._close_stale` (busca o contato por `session.contact_id`);
- `attendance.notify_team` (marcou, remarcou, confirmou, cancelou), `attendance.ask_outcomes` e `attendance.handle_team_tap`.

`_title(contact)` em `message.py` passa a devolver `test_marks.event_title(...)`. As mensagens de automações e o resumo não mudam: o teste já está fora deles (Task 1).

- [ ] **Step 1: Testes que falham:**
  - `test_team_text_marks_only_tests`: `Contact(test_mode="novo")` dá `"🧪 TESTE · Nova conversa…"`; `Contact()` e `None` devolvem o texto igual.
  - `test_event_title_marks_only_tests`: `"[TESTE] Atendimento: Ana"` só com `test_mode`.
  - `test_team_notices_carry_the_prefix_for_a_test_contact`: com `telegram.post_message` e `inbox.notify_team` capturando o texto, `handoff.start`, `inbox.notify_new_conversation` (com `claim_conversation_notice` dando `True`) e `attendance.notify_team(..., Contact(test_mode="novo", name="Ana"), "booked_notice")` produzem texto começando com `"🧪 TESTE · "`. O mesmo com `test_mode=None` não tem o prefixo.
  - `test_idle_minutes_default`: `Settings().test_idle_minutes == 120`.
- [ ] **Step 2:** rode só esses. Esperado: FAIL.
- [ ] **Step 3:** seção `[teste]` com os sete textos e um comentário de placeholders por chave, no estilo das outras seções. Registre em `_PLACEHOLDERS`: `teste.cancel_failed: {"quando"}` e as outras com `set()`. Depois o módulo `test_marks.py`, a configuração e os pontos acima. Atualize os chamadores de `attendance.notify_team` (`message.py:_book_picked_slot` e `attendance.handle_tap`) para passar o `contact`.
- [ ] **Step 4:** a suíte inteira passa (`test_content.py` pega placeholder errado).
- [ ] **Step 5: Commit** `feat: marca TESTE nos avisos à equipe e no título do evento`

### Task 3: SQL do teste: criar, semear, apagar e achar quem está parado

**Files:**
- Create: `core/repositories/test_mode.py`
- Test: `tests/test_test_mode.py`, `tests/test_test_mode_pg.py` (novo, Postgres real)

**Interfaces:**
- Consumes: `is_staff_sql` (contacts).
- Produces:
  - `create(conn, chat_id: str, mode: str, *, name: str | None, email: str | None, phone: str | None, inbound_at: datetime) -> Contact`: insere o contato com `test_mode = mode`, `test_started_at = NOW()`, `last_inbound_at = inbound_at` e `source = channels.channel_of(chat_id)`, e devolve o `Contact` (`RETURNING` com as colunas de `contacts._COLUMNS`).
  - `seed_yesterday(conn, contact_id: int, chat_id: str, user_text: str, assistant_text: str) -> None`: duas linhas em `sessions` (`user`, depois `assistant`), com `created_at = NOW() - INTERVAL '1 day'` e `intent = 'greeting'`.
  - `delete(conn, contact_id: int, *, staff_own: bool) -> bool`: apaga o contato e o que é dele, numa instrução só. Devolve se apagou.
  - `idle(conn, minutes: int) -> list[tuple[int, str]]`: `(id, chat_id)` dos contatos em teste parados há mais de `minutes`.
  - `still_idle(conn, contact_id: int, minutes: int) -> bool`: a mesma condição para um contato, conferida de novo sob o lock.

O `delete` é a parte que não pode ter erro. Use exatamente esta forma: o escopo fica no `target`, e nada é apagado fora dele.

```sql
WITH target AS (
    SELECT c.id, c.chat_id FROM contacts c
     WHERE c.id = %(id)s
       AND (c.test_mode IS NOT NULL
            OR (%(staff_own)s AND {is_staff_sql('c.chat_id')}))
     FOR UPDATE
), s AS (DELETE FROM sessions          WHERE chat_id    IN (SELECT chat_id FROM target)),
   w AS (DELETE FROM workspace_actions WHERE contact_id IN (SELECT id FROM target)),
   a AS (DELETE FROM appointments      WHERE contact_id IN (SELECT id FROM target))
DELETE FROM contacts WHERE id IN (SELECT id FROM target) RETURNING id
```

A cascata de `contacts` leva `offers`, `handoff_sessions` (e com elas `handoff_messages`), `handoffs` e `automation_queue`. `tasks` e `mail_threads` ficam com `contact_id` nulo (`ON DELETE SET NULL`), como já é hoje.

Condição de parado, em `idle` e `still_idle`: `c.test_mode IS NOT NULL AND GREATEST(c.last_inbound_at, c.test_started_at) < NOW() - make_interval(mins => %s)`. O `GREATEST` do Postgres ignora nulos.

- [ ] **Step 1: Testes com SQL gravado:**
  - `test_delete_is_scoped_in_sql`: o SQL de `delete` contém `test_mode IS NOT NULL` dentro do `target`, e `staff_own` vai como parâmetro, nunca colado no texto.
  - `test_create_keeps_the_whatsapp_window`: os parâmetros de `create` incluem o `inbound_at` recebido, e o SQL contém `last_inbound_at`.
  - `test_idle_uses_the_minutes_given`: `idle(conn, 120)` passa `120` como parâmetro.
- [ ] **Step 2: Teste em Postgres real**, `tests/test_test_mode_pg.py`, pulado com `pytest.skip` quando `TEST_DATABASE_URL` não está definido. Cada teste abre uma conexão, faz o trabalho e dá `rollback` no fim. Casos:
  - `test_migration_applies_twice`: aplicar `0008_test_mode.sql` duas vezes não dá erro. O `CHECK` recusa `test_mode = 'outro'`.
  - `test_delete_cascades`: um contato em teste com sessão, oferta, reunião, `workspace_actions`, handoff com mensagem e item na fila de automação, todos ligados a ele. Depois do `delete(..., staff_own=False)`, nenhuma dessas linhas existe.
  - `test_delete_never_touches_a_real_customer`: um contato sem `test_mode` e fora da equipe, mesmo com `staff_own=True`: `delete` devolve `False`, e o contato e as sessões continuam lá.
  - `test_predicates_on_real_rows`: com um contato da equipe em teste, um da equipe fora de teste e um cliente, `hidden_from_customer_lists_sql` e `left_out_of_metrics_sql` dão os valores esperados para cada um, numa consulta `SELECT`.
- [ ] **Step 3:** rode os dois arquivos. Esperado: FAIL.
- [ ] **Step 4:** implemente `core/repositories/test_mode.py`.
- [ ] **Step 5: Verificação em Postgres real**, num container descartável que não encosta no banco do bot:
  ```bash
  docker run -d --rm --name pg-modo-teste -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1::5432 postgres:16-alpine
  PORT=$(docker port pg-modo-teste 5432/tcp | head -1 | cut -d: -f2)
  sleep 3
  docker exec -i pg-modo-teste psql -U postgres < infra/init.sql
  for f in infra/migrations/*.sql; do docker exec -i pg-modo-teste psql -U postgres -v ON_ERROR_STOP=1 < "$f"; done
  TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:$PORT/postgres .venv/bin/python -m pytest tests/test_test_mode_pg.py -p no:warnings
  docker stop pg-modo-teste
  ```
  Esperado: os quatro testes passam, nenhum pulado.
- [ ] **Step 6:** a suíte inteira passa (sem `TEST_DATABASE_URL`, os testes de Postgres aparecem como pulados).
- [ ] **Step 7: Commit** `feat: SQL do modo teste (criar, semear, apagar, expirar)`

### Task 4: O fluxo: ligar, sair, expirar

**Files:**
- Create: `core/services/test_mode.py`
- Modify: `core/handlers/message.py` (`process_message`), `core/services/handoff.py` (`run_handoff_timeout_worker`)
- Test: `tests/test_test_mode.py`

**Interfaces:**
- Consumes: Task 3 (`create`, `seed_yesterday`, `delete`, `idle`, `still_idle`), `contacts_repo.find_staff_by_chat`, `contacts_repo.get_or_create`, `appointments_repo.upcoming_for_contact`, `scheduling.cancel`, `scheduling.when_parts`, `handoffs_repo.lock_chat`, `channels.send`.
- Produces:
  - `parse_command(text: str | None) -> Literal["novo", "antigo", "sair"] | None`: só olha o texto cru da mensagem. Tira espaços e `@bot`, ignora maiúsculas. Não transcreve áudio.
  - `handle_command(conn, chat_id, text: str | None, inbound_at: datetime) -> str | None`: devolve `"test_started"`, `"test_ended"` ou `"not_in_test"` quando tratou o comando (e já respondeu no chat). Devolve `None` quando a mensagem segue o caminho normal: não é comando, ou é `/teste` de quem não é da equipe, ou é `/sair` de quem não é da equipe nem está em teste.
  - `cleanup(conn, contact: Contact, *, staff_own: bool) -> list[str]`: cancela no Google os compromissos futuros (`upcoming_for_contact` e depois `scheduling.cancel` para cada um) e chama `repo.delete`. Devolve um texto `teste.cancel_failed` por evento que o Google recusou, com `quando = f"{p['data']} às {p['horario']}"`, onde `p = scheduling.when_parts(TimeSlot(start=a.starts_at, end=a.ends_at))`.
  - `sweep_in_pool(pool) -> int`: para cada `(id, chat_id)` de `repo.idle(conn, settings.test_idle_minutes)`, abre conexão própria, faz `lock_chat`, confere `still_idle` e, se ainda parado, faz `cleanup(staff_own=False)` e manda `teste.expired` (mais as falhas) ao chat. Uma falha no envio não desfaz a limpeza. Devolve quantos limpou.

Regras de `handle_command`:
- **`/teste` ou `/teste antigo`:** só com `find_staff_by_chat` encontrando alguém.
  1. Pega o contato do chat (`get_or_create`) e faz `cleanup(staff_own=True)`.
  2. Faz `repo.create(...)` com o modo. No "novo", sem nome, e-mail nem telefone. No "antigo", com o nome e o e-mail do `StaffMember`, telefone só no WhatsApp (decisão no topo) e `seed_yesterday` com `"oi"` e `content.text("reply.welcome")`.
  3. Responde `teste.started_new` ou `teste.started_returning`, mais as falhas da limpeza, se houver.
- **`/sair`:** com o contato do chat em teste (`test_mode` não nulo), faz `cleanup(staff_own=False)` e responde `teste.ended`, mais as falhas. Sem teste, se for da equipe, responde `teste.not_in_test`. Sem teste e fora da equipe, devolve `None`.
- **Nada é gravado em `sessions` para os comandos.** O histórico do teste começa limpo.

No `process_message`, a chamada fica logo depois de `await _note_inbound(...)` e antes de `_allowed(...)`:
`intent = await test_mode.handle_command(conn, chat_id, message.get("text"), channels.sent_at(message))`; se `intent`, devolve `intent`.

Em `run_handoff_timeout_worker`, no mesmo laço e com `try/except` próprio, chama `test_mode.sweep_in_pool(pool)` e loga quantos expirou.

- [ ] **Step 1: Testes que falham**, com `contacts_repo`, `test_mode` repo, `appointments_repo.upcoming_for_contact`, `scheduling.cancel` e `channels.send` trocados por fakes via `monkeypatch`:
  - `test_parse_command`: parametrizado. `"/teste"`, `" /Teste "`, `"/teste@ConvergeBot"` dão `"novo"`. `"/TESTE ANTIGO"` dá `"antigo"`. `"/sair"` e `"/SAIR"` dão `"sair"`. `"/teste xyz"`, `"teste"`, `"SAIR"`, `"sair"`, `""` e `None` dão `None`.
  - `test_staff_starts_a_new_test`: staff encontrado. Limpa o contato antigo com `staff_own=True`, cria com `mode="novo"`, `name=None` e `inbound_at` igual ao recebido, não semeia e envia `teste.started_new`. Devolve `"test_started"`.
  - `test_staff_starts_a_returning_test`: `"antigo"` cria com o nome e o e-mail do staff e, no chat `wa:5591980000002`, telefone `"+5591980000002"`. Semeia `("oi", reply.welcome)` e envia `teste.started_returning`.
  - `test_customer_teste_is_an_ordinary_message`: sem staff, `handle_command(..., "/teste", ...)` devolve `None`, sem limpar nem enviar.
  - `test_sair_ends_a_test_even_after_leaving_staff`: contato com `test_mode="novo"`, `find_staff_by_chat` dá `None`. Limpa com `staff_own=False`, envia `teste.ended` e devolve `"test_ended"`.
  - `test_sair_outside_a_test`: staff sem teste recebe `teste.not_in_test`. Cliente sem teste: `None`.
  - `test_cleanup_cancels_future_events_and_reports_failures`: dois compromissos, com o `scheduling.cancel` dando `True` no primeiro e `False` no segundo. `delete` é chamado mesmo assim, e o retorno tem um único texto com a data e a hora do segundo.
  - `test_restarting_a_test_cancels_the_old_booking`: `/teste` com o contato em teste e uma reunião futura chama `scheduling.cancel` para ela antes de `create`.
  - `test_sweep_skips_a_contact_that_wrote_meanwhile`: `idle` devolve um contato e `still_idle` dá `False`. Nada é apagado nem enviado.
  - `test_sweep_expires_and_tells_the_chat`: `still_idle` dá `True`. `cleanup` acontece e `teste.expired` é enviado ao `chat_id`. Com `channels.send` lançando exceção, a limpeza continua valendo e conta 1.
  - `test_process_message_reads_the_command_before_the_rate_limit`: com `_allowed` falso, um `/teste` de staff ainda é tratado (`handle_command` é chamado e devolve `"test_started"`).
  - `test_sair_without_slash_is_still_opt_out`: uma mensagem `"SAIR"` de um contato em teste segue até `automations.opt_out` (o `handle_command` devolve `None`).
- [ ] **Step 2:** rode só esses. Esperado: FAIL.
- [ ] **Step 3:** implemente `core/services/test_mode.py` e as duas ligações.
- [ ] **Step 4:** a suíte inteira passa.
- [ ] **Step 5: Commit** `feat: /teste, /sair e expiração do modo teste`

### Task 5: Painel: etiqueta TESTE e números sem teste

**Files:**
- Modify: `core/web/static/painel/painel.js`, `core/web/static/painel/inbox.js`, `core/web/static/painel/painel.css`
- Test: verificação no navegador (o painel não tem testes de JS)

**Interfaces:**
- Consumes: `is_test` nas respostas de `/painel/api/leads`, `/painel/api/leads/{id}`, `/painel/api/inbox` e `/painel/api/inbox/{id}` (Task 1).
- Produces:
  - classe CSS `.test-tag`: etiqueta pequena "TESTE", em maiúsculas, com cor de aviso, legível nos temas claro e escuro, usando os tokens de `:root`;
  - em `painel.js`: `normalize` ganha `isTest: !!row.is_test`, e uma função `customers()` devolve `state.leads` sem os `isTest`.

Onde a etiqueta aparece, escrita com `textContent` ou `esc()`, como o resto:
- na linha da Inbox (`renderList`), ao lado do nome;
- no cabeçalho da conversa (`renderHead`), ao lado do nome;
- no card do funil (`makeCard`);
- na tabela de contatos (`renderTable`), na célula do nome;
- no painel lateral (`fillPanel`), ao lado do nome.

Os números usam `customers()`:
- `renderTiles`, `renderMiniFunnel`, `renderRecent`, `renderOrigins`, `renderStageFilter` (contagens);
- `updateCounts`.

O quadro do funil e a tabela continuam mostrando o contato em teste, com a etiqueta.

- [ ] **Step 1:** implemente as mudanças.
- [ ] **Step 2: Verificação no navegador**, com o painel real em `localhost` e a API trocada por dados fictícios (o mesmo método de `brag-output/work/painel-capture.js`, com rotas do Playwright), com um lead e uma conversa `is_test: true` e outros sem a marca. Confira, em capturas de tela nos temas claro e escuro:
  - **Visível:** a etiqueta aparece na Inbox, no cabeçalho, no card, na tabela e no painel lateral.
  - **Fora dos números:** os blocos do Painel, a contagem por etapa e os números do menu não contam o contato em teste.
  - **Contraste:** a etiqueta é legível nos dois temas.
- [ ] **Step 3: Commit** `feat: etiqueta TESTE no painel e números sem os testes`

### Task 6: Fechamento

**Files:**
- Modify: `docs/WHATSAPP.md` ou o README da operação, com uma linha sobre `/teste`, `/teste antigo`, `/sair` e `TEST_IDLE_MINUTES`.

- [ ] **Step 1:** rode `ruff check .`, `.venv/bin/pyright` e a suíte inteira. Tudo limpo.
- [ ] **Step 2:** repita a verificação em Postgres real da Task 3, Step 5.
- [ ] **Step 3: Revisão do Codex**, modelo `gpt-6.1-sol` ou mais forte, só leitura, com `< /dev/null`. Peça foco na limpeza (escopo do SQL, Google antes do banco), no lugar do comando no turno e na janela do WhatsApp. Corrija o que ele confirmar e repita até não sobrar achado P1/P2.
- [ ] **Step 4:** commit da doc. Push da branch e PR para `main`, com o resumo do que muda e o roteiro de teste manual:
  1. `/teste` pelo WhatsApp de alguém da equipe;
  2. marcar uma reunião e pedir uma pessoa;
  3. assumir pela Inbox;
  4. `/sair`;
  5. conferir que a Inbox e a agenda ficaram limpas.
