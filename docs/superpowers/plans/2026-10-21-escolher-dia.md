# Escolher o dia, depois o horário — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** com `BOOKING_MODE=choose_day`, o cliente escolhe o dia e depois o horário em listas (WhatsApp) ou botões (Telegram), por toque ou por texto. Com `MAX_UPCOMING_APPOINTMENTS`, nenhum caminho passa do limite de conversas futuras. Desligados, nada muda.

**Architecture:** um módulo puro, `core/services/booking_lists.py`, monta as páginas de dias e de horários a partir de uma disponibilidade completa (`scheduling.available_starts`). Os handlers só escolhem o ponto de entrada, os toques (`day:`, `days:`, `more:`) e a espera `pickday:`. O caminho de agendamento existente (`Offer`, `slot:`, `take_offer`) é reaproveitado, com uma trava por contato que valida remarcação e limite. Os teclados ganham descrição e rótulo de lista, renderizados por canal em `core/channels.py`.

**Tech Stack:** Python 3.12, psycopg 3 async, pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-escolher-dia-design.md`

## Global Constraints

- `BOOKING_MODE`: `suggest` (padrão) ou `choose_day`. `MAX_UPCOMING_APPOINTMENTS`: `0` (padrão, sem limite) a `5`. Valor inválido impede a subida.
- Horizonte: de hoje a hoje + `booking_horizon_days`, inclusive, no fuso do escritório, com origem fixa (nunca anda com a página nem com o dia pedido). Grade: `slot_step_minutes`. Duração: `appointment_duration_minutes`. Antecedência: `minimum_notice_minutes`. Agendas: `calendars_in_scope`, união dos ocupados.
- Lista de dias: até 9 dias; título "Terça, 21/10"; descrição "6 horários", "1 horário", "só de manhã" ou "só à tarde"; 10ª linha "Próximos dias" só se houver mais dias livres no horizonte.
- Lista de horários: com mais do que cabem, 8 horários + "Mais horários neste dia" + "Outro dia"; senão, até 9 + "Outro dia". Título "9h", "9h30". Sem o espaçamento de 2 h.
- Toques: `day:<AAAA-MM-DD>:<p>:<r>`, `days:<AAAA-MM-DD>:<p>:<r>`, `more:<AAAA-MM-DD>:<HHMM>:<p>:<r>`, `limit:keep`; `<p>` ∈ `m`, `t`, `-`; `<r>` = id ou `-`. Todo `callback_data` ≤ 64 bytes.
- Espera do texto: `pending_action = pickday:<p>:<r>` via `set_waiting`.
- WhatsApp: lista de dias ou de horários sempre como lista; título ≤ 24, descrição ≤ 72, botão da lista ≤ 20. Telegram: `label · description`.
- O limite conta `status = 'confirmed'` e `starts_at > agora` do contato; remarcação não conta; vale nos dois modos; sob `pg_advisory_xact_lock(contact_id)`.
- `appointments.title` é cifrado: nenhuma consulta nova lê ou filtra o título no SQL (usar `core/sealed_fields`).
- Migração aditiva `0010_offer_period.sql`. A do app da equipe passa a `0011`.
- Textos em `content/messages.toml`, seção nova `[booking]`, com os placeholders registrados em `core/content.py`.
- Commits sem coautoria.

## Review Focus

1. **Fuso e virada do dia:** um cliente que pede os dias às 23h50 (Belém) não pode ver "hoje" com horários que já passaram, nem o horizonte contado em UTC. A Task 3 testa `available_starts` e as páginas às 23h50 e às 00h10 no fuso do escritório.
2. **Um dia que a agenda devolve vazio por erro** (agenda fora do ar) não pode virar "sem horários": é a mensagem de falha da agenda de hoje. A Task 5 testa o erro do provedor no toque `day:`.
3. **Dois toques quase simultâneos** em horários de listas diferentes, com limite 1: só um agenda. A Task 6 testa em Postgres real, com duas conexões.
4. **Um `callback_data` antigo, de uma versão anterior do bot** (`period:`, `slot:`), continua funcionando ou cai em mensagem clara, nunca numa exceção. A Task 5 testa os toques antigos com `choose_day` ligado.
5. **Com os dois recursos desligados**, nenhuma mensagem, teclado ou SQL muda. A Task 5 e a Task 6 rodam os testes de agendamento de hoje sem alteração.

---

### Task 1: Configuração, migração, origem do agendamento

**Files:**
- Create: `infra/migrations/0010_offer_period.sql`
- Modify: `core/config.py`, `core/models.py` (`Offer.period`, `Appointment.booked_via`), `core/repositories/offers.py`, `core/repositories/appointments.py`, `core/services/scheduling.py` (`book` grava `booked_via`), `.env.example`, `docs/superpowers/specs/2026-10-13-app-da-equipe-design.md` (`0010` → `0011`)
- Test: `tests/test_config.py`, `tests/test_sealed_pg.py` (ou um `tests/test_booking_pg.py` novo, mesmo padrão)

**Interfaces:**
- Produces: `settings.booking_mode: Literal["suggest", "choose_day"]`, `settings.max_upcoming_appointments: int`; `Offer.period: str | None` (`"morning"`, `"afternoon"` ou `None`); `Appointment.booked_via: str = "telegram"`.

- [ ] **Step 1: Testes (falham):**
  - `test_booking_mode_and_limit_are_validated`: `Settings(booking_mode="x")` e `Settings(max_upcoming_appointments=6)` e `=-1` levantam; os padrões são `suggest` e `0`;
  - Postgres: `test_an_offer_keeps_its_period` (`offers_repo.save` com `period="morning"` e `pending_for_chat` devolve o mesmo);
  - Postgres: `test_a_booking_records_its_channel` (um `Appointment` com `booked_via="whatsapp"` gravado e lido de volta).
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar.**
  - `0010_offer_period.sql`: `ALTER TABLE offers ADD COLUMN IF NOT EXISTS period VARCHAR(10)`.
  - `scheduling.book` grava `booked_via=channel_of(contact.chat_id)`.
  - Renumerar a migração do app da equipe na spec dele (`0010` → `0011`).
  - Documentar as duas variáveis no `.env.example`.
- [ ] **Step 4: Rodar** → PASS, e a suíte (com `TEST_DATABASE_URL`).
- [ ] **Step 5: Commit.** `feat: configuração do agendamento por dia e origem do agendamento`.

---

### Task 2: Teclados com descrição e rótulo de lista

**Files:**
- Modify: `core/integrations/telegram.py` (`menu`), `core/channels.py` (`_buttons`, `_whatsapp`), `core/integrations/whatsapp.py` (`send_list`), `core/content.py` (validação dos rótulos renderizados)
- Test: `tests/test_channels.py`, `tests/test_whatsapp_client.py`, `tests/test_content.py`

**Interfaces:**
- Produces:
  - `telegram.menu(rows: list[list[Choice]], *, list_button: str | None = None) -> dict` e `@dataclass(frozen=True) class Choice: label: str; data: str; description: str | None = None`, ambos em `core/integrations/telegram.py`.
  - O teclado é um dict do Telegram: cada botão leva o texto `label · description` (ou só `label`) e o dict ganha duas chaves que o Telegram ignora: `"_list_button"` e, por botão, `"_label"` e `"_description"`.
  - `channels._buttons` devolve `(title, data, description)`.
  - `whatsapp.send_list(to, body, button_label, rows: list[tuple[str, str, str | None]])`.
- **Ruling a registrar:** o Telegram rejeita chaves desconhecidas no `reply_markup`? Conferir na Step 1 com o envio real dos testes de cliente; se rejeitar, `telegram.post_message` tira as chaves `_*` antes de enviar.

- [ ] **Step 1: Testes (falham):**
  - `test_a_menu_with_a_list_button_is_always_a_whatsapp_list`: um menu de 2 dias com `list_button="Escolher dia"` vira `send_list` com o rótulo certo, título e descrição por linha;
  - `test_other_keyboards_with_three_options_stay_buttons` (o comportamento de hoje, sem mudança);
  - `test_telegram_shows_label_and_description`: o texto do botão é `"Terça, 21/10 · 6 horários"`, e o `post_message` não manda as chaves `_*`;
  - `test_whatsapp_list_rows_carry_descriptions` (o JSON de `send_list` com `description`);
  - `test_rendered_labels_fit_whatsapp`: para a data mais longa ("Quarta, 31/12") e cada texto da seção `[booking]`, título ≤ 24, descrição ≤ 72, botão ≤ 20 — em `core/content.validate`, com os textos reais.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar.** No WhatsApp, um teclado com `_list_button` sempre vai por `send_list`, mesmo com 1 a 3 opções; os outros seguem a regra de hoje (até 3 botões, mais vira lista com "Escolher").
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `feat: teclados com descrição e rótulo de lista`.

---

### Task 3: A disponibilidade completa e as páginas

**Files:**
- Create: `core/services/booking_lists.py`, `tests/test_booking_lists.py`
- Modify: `core/services/scheduling.py` (`available_starts`)

**Interfaces:**
- Produces:
  - `async def available_starts(*, period: str | None, calendars: list[str] | None = None, now: datetime | None = None) -> list[TimeSlot]` em `scheduling.py`: todos os inícios livres do horizonte (origem hoje), com as regras de `offer_times` (agendas, união dos ocupados, grade, duração, expediente, `is_workday`, antecedência). Uma chamada ao provedor por pedido (`busy_periods` do horizonte inteiro).
  - Em `booking_lists.py`, puro (sem I/O):
    - `@dataclass class DayEntry: day: date; count: int; only: str | None` (`"morning"`, `"afternoon"` ou `None`);
    - `days_page(starts: list[TimeSlot], *, first: date | None, period: str | None, limit: int = 9) -> tuple[list[DayEntry], date | None]`: os dias com horário livre a partir de `first`, e o primeiro dia da página seguinte (ou `None`);
    - `times_page(starts: list[TimeSlot], day: date, *, after: time | None) -> tuple[list[TimeSlot], time | None]`: 8 horários e o "depois de" da página seguinte, ou até 9 e `None`;
    - `day_title(day: date) -> str` ("Terça, 21/10"), `time_title(slot) -> str` ("9h", "9h30");
    - `callback(kind, day, period, replaces, after=None) -> str` e `parse(data: str) -> Nav | None` para `day:`/`days:`/`more:`, com `p` e `r` como na spec; `parse` devolve `None` para qualquer coisa malformada.

- [ ] **Step 1: Testes (falham)** em `tests/test_booking_lists.py`, com um calendário falso (`busy_periods`):
  - `available_starts`: respeita agendas (com `STAFF_CALENDAR_IDS`, um horário em que a recepção está livre mas alguém da equipe está ocupado não aparece), grade (com `slot_step_minutes=60`), duração, expediente, dias úteis, antecedência, e o horizonte (com `booking_horizon_days` 7 e 30), inclusive, no fuso do escritório;
  - **Review Focus 1:** às 23h50 e às 00h10 (fuso do escritório), hoje não tem horário passado, e o último dia do horizonte é hoje + N no fuso do escritório, não em UTC;
  - `days_page`: só dias com horário; até 9; o "próximo" só quando há mais, e para no horizonte; o período filtra; `only` diz "manhã" quando todos os horários do dia são de manhã;
  - `times_page`: 12 horários dão 8 + próximo; a segunda página começa depois do último mostrado; 9 horários dão 9 e `None`;
  - `day_title(date(2026, 12, 31)) == "Quinta, 31/12"`; `time_title` dá `"9h"` e `"9h30"`;
  - `callback`/`parse`: ida e volta; o pior caso (`more:` com o período mais longo e um id de 19 dígitos) cabe em 64 bytes; `parse("day:2026-13-40:m:-")`, `parse("lixo")` e `parse("more:2026-10-21:9h:m:-")` dão `None`.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `feat: disponibilidade completa e páginas de dias e horários`.

---

### Task 4: Ler o texto

**Files:**
- Modify: `core/services/slot_choice.py` (`read_day_reply`, e a hora sem minutos em `interpret`)
- Test: `tests/test_slot_choice.py`

**Interfaces:**
- Produces: `@dataclass class DayReply: kind: Literal["clear", "mixed", "period_only", "cancel_or_exit", "none"]; days: list[date]; period: str | None` (`"morning"`, `"afternoon"`, `"any"` ou `None`); `read_day_reply(text: str, *, now: datetime | None = None) -> DayReply`.

- [ ] **Step 1: Testes (falham):**
  - `test_read_day_reply` parametrizado com **cada linha da tabela da spec** (19 frases, `kind`, dias, período);
  - `test_an_hour_without_minutes_means_on_the_hour`: com 10h e 10h30 na oferta, "às 10h" escolhe 10h; com só 10h30, é ambíguo; com 10h numa página e 10h30 na atual, "às 10h" não casa com nada (fora da página);
  - os testes de `slot_choice` de hoje passam sem mudança.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar** na ordem da spec (passos 0 a 6), reaproveitando `normalize` e a leitura de dias de `requested_day`. Quando há dia, `days` traz as datas resolvidas como `requested_day` resolve hoje.
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `feat: leitura conservadora do dia escrito`.

---

### Task 5: O fluxo `choose_day` nos handlers

**Files:**
- Modify: `core/handlers/message.py` (pontos de entrada, toques, espera `pickday`, a escolha do dia depois da triagem), `content/messages.toml` (seção `[booking]`), `core/content.py` (placeholders)
- Test: `tests/test_booking.py`, `tests/test_desk_flow.py` (ou um `tests/test_choose_day.py` novo, no padrão de `test_booking.py`)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `_offer_days(conn, contact, chat_id, *, period, replaces_id, first=None, only_days=None) -> tuple[str, dict | None]` e `_offer_day_times(conn, contact, chat_id, *, day, period, replaces_id, after=None) -> tuple[str, dict | None]` em `message.py`; o despacho por `booking_mode` em `_offer`.

- [ ] **Step 1: Testes (falham)**, com `booking_mode="choose_day"`, calendário falso e repositórios falsos como em `tests/test_booking.py`:
  - cada linha da tabela "Pontos de entrada" da spec (8 linhas), incluindo o horário ocupado, recusado e não entendido, que voltam aos dias com o período e a remarcação;
  - `day:` grava uma `Offer` com os horários da página, `period` e `replaces_id`; `slot:` dessa oferta agenda pelo caminho de hoje;
  - "Outro dia" e "Próximos dias" encerram a `Offer` pendente; depois disso, o `slot:` antigo dá `scheduling.slot_taken`;
  - um `day:` de um dia já passado, ou fora do horizonte, dá a lista de dias de hoje;
  - o dia que lotou entre a lista e o toque: `booking.day_full` e a lista de dias;
  - **Review Focus 2:** a agenda falha no toque `day:` → a mensagem de falha da agenda de hoje, não "sem horários";
  - **Review Focus 4:** toques antigos (`period:`, `slot:` de uma oferta já substituída, `day:` malformado) com `choose_day` → resposta clara, nenhuma exceção;
  - a espera `pickday:<p>:<r>`, com cada `kind` de `read_day_reply`, faz o que a spec diz (inclusive `period_only` trocando o período e mantendo `<r>`, e `cancel_or_exit`/`none` limpando a espera e seguindo a triagem);
  - depois da triagem `booking`/`reschedule`, só um `clear` com um dia vira dia preferido; `mixed` não vira;
  - **Review Focus 5:** com `booking_mode="suggest"`, os testes de agendamento de hoje (`tests/test_booking.py`, `tests/test_scheduling.py`, `tests/test_desk_flow.py`) passam sem mudança.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar.** Textos da seção `[booking]` exatamente como na spec. `_book_picked_slot`, num horário ocupado, volta aos dias com o período e o `replaces_id` da `Offer`.
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `feat: escolher o dia, depois o horário`.

---

### Task 6: O limite e a remarcação válida

**Files:**
- Modify: `core/models.py` (`BookingResult.outcome` ganha `"limit"` e `"stale_reschedule"`), `core/services/scheduling.py` (`take_offer`), `core/repositories/appointments.py` (`count_upcoming(conn, contact_id, *, excluding: int | None) -> int`, `lock_contact(conn, contact_id) -> None`), `core/handlers/message.py` (a checagem antes de oferecer, `limit:keep`, as respostas aos dois resultados novos), `content/messages.toml`, `core/content.py`
- Test: `tests/test_booking.py`, `tests/test_booking_pg.py`

**Interfaces:**
- Consumes: Task 5 (pontos de entrada).
- Produces: `scheduling.limit_reached(conn, contact) -> list[Appointment] | None` (as conversas que esgotam o limite, ou `None`).

- [ ] **Step 1: Testes (falham):**
  - com limite 1 e uma conversa: a nova oferta vira `booking.limit_one` com "Remarcar" (`appt:move:<id>`) e "Manter" (`limit:keep` → `booking.limit_kept`);
  - com limite 2 e duas: `booking.limit_many` com o "qual delas?" de hoje mais "Manter";
  - `take_offer` com a conta no limite → `limit`, a `Offer` encerrada, nada marcado na agenda;
  - remarcar não conta: com limite 1 e uma conversa, remarcar ela agenda;
  - `stale_reschedule`: uma `Offer` que substitui uma conversa já cancelada, já passada ou de outro contato → recusa, `booking.stale_reschedule`, nada marcado (vale também com limite 0);
  - **Review Focus 3** (Postgres real, `tests/test_booking_pg.py`): duas conexões, duas ofertas do mesmo contato, limite 1; as duas chamam o trecho travado (`lock_contact` + `count_upcoming` + gravação) ao mesmo tempo; só uma grava;
  - com limite 0, nenhuma conta nem trava a mais, e os testes de hoje passam;
  - o limite vale no modo `suggest` também.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar** a ordem da spec em `take_offer`: trava, validação da remarcação, conta, e só então `claim`, agenda e gravação. A trava é `SELECT pg_advisory_xact_lock(%s)` com o id do contato, dentro da transação do turno.
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `feat: limite de agendamentos e remarcação válida`.

---

### Task 7: Quando o cancelamento da antiga falha

**Files:**
- Modify: `core/services/scheduling.py` (`_replace`), `content/messages.toml`, `core/content.py`
- Test: `tests/test_booking.py`

- [ ] **Step 1: Testes (falham):** numa remarcação em que a agenda recusa cancelar a antiga:
  - a antiga fica `cancelled` no banco (`appointments_repo.cancel`);
  - a equipe recebe `booking.team_cancel_failed` (via `inbox.notify_team`, com o contato);
  - a nova continua confirmada e o cliente recebe a confirmação de hoje;
  - a antiga não conta no limite nem aparece em `upcoming_for_contact`.
- [ ] **Step 2: Rodar** → FAIL.
- [ ] **Step 3: Implementar.** **Ruling a registrar:** `scheduling.cancel` diz que marcar a linha local cancelada com o evento ainda na agenda é a pior falha, porque o agente acharia o horário livre. Aqui isso não acontece: a disponibilidade vem da agenda (`busy_periods`), que continua mostrando o evento até a equipe apagar. O índice de horário do banco libera a linha, mas a agenda bloqueia.
- [ ] **Step 4: Rodar** → PASS, e a suíte.
- [ ] **Step 5: Commit.** `fix: remarcação com cancelamento recusado avisa a equipe e não conta no limite`.

---

### Task 8: Revisão, PR e merge

- [ ] **Step 1:** `ruff check .`, `pyright`, a suíte inteira com os bancos descartáveis e sem eles → verde.
- [ ] **Step 2:** Revisão do Codex (gpt-6.1-sol, só leitura, `< /dev/null`), com um brief em `docs/superpowers/reviews/`, até nota ≥ 4 com recomendação de merge; cada achado P1/P2 com teste que falhou antes. Revisor novo do branch (opus) com o Review Focus acima e as linhas `Ruling:`.
- [ ] **Step 3:** Push, PR sem coautoria, CI verde. O Paulo faz o merge.

---

### Task 9: Ligar em produção

- [ ] **Step 1:** Depois do deploy, os dois recursos continuam desligados. Conferir: uma conversa de agendamento no modo `suggest` funciona como antes.
- [ ] **Step 2:** **O Paulo decide os valores.** Com o ok dele, no `.env` do VPS: `BOOKING_MODE=choose_day` e o `MAX_UPCOMING_APPOINTMENTS` escolhido. Reiniciar o bot.
- [ ] **Step 3:** O Paulo testa pelo WhatsApp, num `/teste`:
  - escolher o dia e o horário por toque;
  - escrever "quinta" e "às 10h";
  - remarcar;
  - bater no limite.
