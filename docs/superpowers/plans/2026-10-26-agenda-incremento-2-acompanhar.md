# Agenda no painel — Incremento 2 (Acompanhar o Google) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** as reuniões que o bot marcou passam a acompanhar o que muda no Google.
- **Movida:** o lembrete sai no horário novo.
- **Apagada, com prova:** a reunião é cancelada no sistema.
- **Virou dia inteiro ou deixou de ser da agenda:** fica "precisa de atenção", sem lembrete.

Botões e ofertas velhos deixam de agir sobre o horário novo.

**Architecture:** a passada da sincronização (`core/services/calendar_sync.py`) ganha um passo de reconciliação depois do
espelho. Os botões sobre um agendamento passam a levar o horário (`appt2:`/`outcome2:`), conferido no clique. O
lembrete e a pergunta "como foi?" reivindicam a linha com o horário lido, sob as travas do contato e do evento.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (async), Postgres, httpx, pytest; JS do painel.

**Spec:** `docs/superpowers/specs/2026-10-22-agenda-no-painel-design.md` (local), seção "Incremento 2: Acompanhar o
Google (contrato)". Revisão do Codex só dessa seção em `.docs/REVISAO_SPEC_AGENDA_INC2_RODADA_1.md`, com os três P2
corrigidos na spec.

## Global Constraints

- Commits e PR só com a autoria do Paulo; o merge só depois de perguntar "posso fazer o merge?".
- Migração `infra/migrations/0013_appointments_sync.sql`, que pode rodar de novo (`IF NOT EXISTS`).
- **Nunca cancelar sem prova:**
  - `Gone` cancela;
  - `NotFound` cancela só na segunda passada seguida em que a listagem daquela agenda deu certo;
  - qualquer outro erro não muda nada.
- Sem mensagem ao contato em nenhuma mudança vinda do Google.
- `done` e `no_show` nunca são mexidos.
- Funil, ao cancelar: recua `agendado → respondido` só sem outro agendamento confirmado no futuro do mesmo contato.
- **Travas:** contato `appointments.lock_contact` e depois evento
  `pg_advisory_xact_lock(7411303, hashtext(calendar_id || ':' || provider_event_id))`, sempre nessa ordem.
- **Formato dos botões:**
  - `appt2:<ação>:<id>:<stamp>` e `outcome2:<resultado>:<id>:<stamp>`, com `stamp = int(starts_at.timestamp() // 60)`;
  - os botões antigos `appt:`/`outcome:` valem só com `moved_at IS NULL`;
  - um clique inválido responde `scheduling.reminder_stale` (cliente) ou nada (equipe).
- Logs sem título nem participante.
- A suíte inteira, `ruff` e `pyright` limpos antes de cada commit.

## Review Focus

1. Uma reunião do bot movida no Google para depois da janela de 30 dias: o horário é atualizado (via `get_event`) e
   nunca cancelado.
2. O Google responde 404 uma vez para uma reunião (perda de acesso momentânea) e depois volta a mostrá-la: nada é
   cancelado.
3. Um lembrete selecionado às 09:00 para uma reunião das 10:00 que a sincronização moveu para amanhã no mesmo minuto:
   nenhum lembrete com o horário velho sai, e o novo sai amanhã.
4. O cliente toca "Cancelar" num lembrete de antes da mudança de horário: nada é cancelado, e a resposta é a de botão
   velho.
5. Um contato com duas reuniões; uma é apagada no Google: a outra continua, e o funil continua em "Conversa agendada".

---

### Task 1: O provedor lê um evento e separa erro passageiro

**Files:**
- Modify: `core/integrations/workspace/errors.py`, `models.py`, `base.py` (`CalendarPort`), `google.py`, `microsoft.py`
- Modify: `core/services/calendar_sync.py` (erro passageiro vira `provider_error`)
- Test: `tests/test_workspace_calendar_events.py`

**Interfaces:**
- Produces:
  - `class WorkspaceTransient(WorkspaceError)`. `WorkspaceRateLimited` passa a herdar dela. Também são lançadas como
    ela: 5xx, timeout/erro de rede, e o 403 do Google cujo corpo tenha `error.errors[].reason` igual a
    `rateLimitExceeded` ou `userRateLimitExceeded` (lido sem logar o corpo).
  - `class Gone` e `class NotFound` (sentinelas em `models.py`, sem campos).
  - `async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent | Gone | NotFound` nos dois
    adaptadores e no `CalendarPort`.
    - **Google:** GET `.../calendars/{id}/events/{event_id}`. O corpo com `status == "cancelled"` dá `Gone`; 410 dá
      `Gone`; 404 dá `NotFound`.
    - **Microsoft:** GET `/users/{u}/events/{event_id}`. 404 dá `NotFound`; o Graph não marca cancelado, porque o
      evento somem.
    - O 404/410 vem como `WorkspaceError` com `status` do `_request` de hoje: `get_event` captura pelo `status`.

- [ ] **Step 1: Testes que falham** (o `_request` falso de `test_workspace_calendar_events.py`, agora capaz de
  responder status):

```python
async def test_google_get_event_returns_the_event(monkeypatch): ...
async def test_google_get_event_cancelled_body_is_gone(monkeypatch): ...  # status "cancelled", sem start/end
async def test_google_get_event_410_is_gone_and_404_is_not_found(monkeypatch): ...
async def test_google_get_event_500_raises(monkeypatch): ...
async def test_google_403_rate_limit_is_transient_and_403_permission_is_auth(monkeypatch): ...
    # testa GoogleWorkspace._request com um _http() falso: corpo {"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}
async def test_microsoft_get_event_404_is_not_found(monkeypatch): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar**, com `tests/test_calendar_sync_pg.py`.
- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): ler um evento e separar erro passageiro"`

---

### Task 2: Os dados e o repositório

**Files:**
- Create: `infra/migrations/0013_appointments_sync.sql`
- Modify: `core/repositories/appointments.py`, `core/models.py` (`Appointment` com os campos novos, padrão `None`),
  `core/services/funnel.py`
- Test: `tests/test_appointments_sync_pg.py` (novo)

**Interfaces:**
- **Migração:**
  - `appointments` ganha `provider_etag VARCHAR(255)`, `provider_missing_at TIMESTAMPTZ`, `sync_problem VARCHAR(20)
    CHECK (sync_problem IN ('all_day','not_organizer','start_collision'))` e `moved_at TIMESTAMPTZ`;
  - `offers` ganha `replaces_starts_at TIMESTAMPTZ`.
- **`appointments.py` produz:**
  - `async def sync_targets(conn, calendar_id: str) -> list[Appointment]`: os confirmados com `provider_event_id`
    daquela agenda.
  - `async def calendars_with_targets(conn) -> list[str]`: as agendas que têm alvo, para conferir as que saíram do
    conjunto.
  - `async def lock_event(conn, calendar_id: str, provider_event_id: str) -> None`.
  - `async def apply_move(conn, appointment_id: int, expected: tuple[datetime, str, str | None], start: datetime, end:
    datetime, etag: str | None) -> bool`:
    - só se `(starts_at, status, provider_etag)` ainda é o esperado;
    - grava o horário, a etag e `moved_at = clock_timestamp()`, limpa `start_collision` e `provider_missing_at`;
    - zera `reminder_sent_at`, `attendance_confirmed_at` e `outcome_asked_at` se `end > NOW()`.
  - `async def apply_etag(conn, appointment_id, expected, etag) -> bool`: o mesmo cuidado. Também limpa
    `provider_missing_at` e, se o problema era `start_collision`, limpa o `sync_problem`.
  - `async def set_sync_problem(conn, appointment_id, expected, problem: str | None) -> bool`.
  - `async def mark_missing(conn, appointment_id, expected) -> bool`: grava `provider_missing_at` se estava nulo.
  - `async def cancel_from_provider(conn, appointment_id, expected) -> bool`.
  - `due_reminders` ganha `AND sync_problem IS NULL`.
  - `async def claim_reminder(conn, appointment_id: int, starts_at: datetime) -> Appointment | None` e
    `async def claim_outcome(conn, appointment_id: int, starts_at: datetime) -> Appointment | None`: as duas instruções
    `UPDATE … RETURNING` da spec.
- **`funnel.py` produz** `async def step_back_after_cancel(conn, contact_id: int | None) -> bool`: só recua se
  `count_upcoming(conn, contact_id) == 0`.

- [ ] **Step 1: Testes que falham:**

```python
async def test_apply_move_needs_the_snapshot_and_resets_the_markers(conn): ...
async def test_apply_move_to_the_past_keeps_outcome_asked(conn): ...
async def test_apply_etag_clears_a_start_collision(conn): ...
async def test_claim_reminder_only_with_the_same_start(conn): ...
async def test_claim_outcome_only_ended_unasked_same_start(conn): ...
async def test_due_reminders_skips_sync_problem(conn): ...
async def test_step_back_only_without_another_upcoming(conn): ...
async def test_the_migration_runs_twice(conn): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar.**
- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): dados para acompanhar as reuniões no Google"`

---

### Task 3: A reconciliação

**Files:**
- Create: `core/services/calendar_reconcile.py`
- Modify: `core/services/calendar_sync.py` (chama a reconciliação dentro da passada de cada agenda, depois do espelho)
- Test: `tests/test_calendar_reconcile_pg.py` (novo; Postgres real e um `CalendarPort` falso com `list_events` e
  `get_event`)

**Interfaces:**
- Consumes: Task 1 (`get_event`, `Gone`, `NotFound`) e Task 2 (todo o repositório).
- Produces: `async def reconcile_calendar(conn, calendar_id: str, listed: dict[str, CalendarEvent] | None, snapshot:
  list[Appointment], listing_ok: bool) -> int`.
  - Devolve quantos erros houve. A passada da agenda só grava `last_success_at` se a listagem e a reconciliação não
    tiveram erro.
  - `listed` é `None` quando a listagem falhou.
  - O `calendar_sync` lê o `snapshot` (via `sync_targets`) **antes** de chamar `list_events`.
  - As agendas que saíram do conjunto são conferidas no fim da passada, só por `get_event` (`listing_ok=False`, então
    `NotFound` nunca cancela por ali).
- Regras:
  - as da spec, passo 3, uma transação por agendamento, com `lock_contact` e depois `lock_event`;
  - a colisão de início num savepoint vira `set_sync_problem("start_collision")`;
  - auditoria `sync_update`/`sync_cancel` com o repositório `audit` de hoje (`provider`, `capability="calendar"`,
    `target=event_id`, `contact_id`, `succeeded=True`);
  - o cancelamento usa `funnel.step_back_after_cancel`.

- [ ] **Step 1: Testes que falham:**

```python
async def test_moved_in_google_updates_and_resets_the_reminder(conn, fake): ...
async def test_moved_beyond_the_window_is_found_by_get_event(conn, fake): ...
async def test_gone_cancels_and_steps_the_funnel_back(conn, fake): ...
async def test_gone_with_another_upcoming_keeps_the_stage(conn, fake): ...
async def test_not_found_twice_with_good_listings_cancels(conn, fake): ...
async def test_not_found_then_back_cancels_nothing(conn, fake): ...
async def test_not_found_when_the_listing_failed_never_counts(conn, fake): ...
async def test_a_get_event_error_changes_nothing_and_fails_the_pass(conn, fake): ...
async def test_changed_locally_after_the_snapshot_is_left_for_next_pass(conn, fake): ...
async def test_all_day_and_not_organizer_become_sync_problem_and_clear(conn, fake): ...
async def test_start_collision_and_its_clearing(conn, fake): ...
async def test_done_and_no_show_are_never_touched(conn, fake): ...
async def test_a_calendar_out_of_the_set_is_checked_by_get_event(conn, fake): ...
async def test_moved_after_outcome_asked_is_reconciled(conn, fake): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar**, com `tests/test_calendar_sync_pg.py`.
- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): reuniões do bot acompanham o Google"`

---

### Task 4: Botões e ofertas com o horário

**Files:**
- Create: `core/services/appointment_taps.py`
- Modify:
  - `core/services/attendance.py`: `reminder_keyboard`, confirmação de cancelamento, "como foi?", `handle_tap`,
    `handle_team_tap`;
  - `core/services/reminders.py`: os códigos do modelo do WhatsApp;
  - `core/handlers/message.py`: `_choose_keyboard`, `_limit_reply`, o roteamento de `appt2:`/`outcome2:` e
    `appt:move`;
  - `core/services/scheduling.py` e `core/repositories/offers.py`: `replaces_starts_at`.
- Test: `tests/test_appointment_taps.py` (novo); ajustar os testes que fixam os códigos antigos (`grep -rn
  "appt:ok\|appt:move\|appt:cancel\|outcome:" tests/`).

**Interfaces:**
- Produces:
  - `def tap(action: str, appointment: Appointment) -> str`: `appt2:<action>:<id>:<stamp>`;
  - `def outcome_tap(result: str, appointment: Appointment) -> str`;
  - `def parse(data: str) -> tuple[str, int, int | None]`: `(ação, id, stamp)`, com `stamp=None` nos formatos
    antigos;
  - `def still_valid(appointment: Appointment, stamp: int | None) -> bool`: com `stamp`, o `starts_at` atual dá o
    mesmo `stamp`; sem `stamp`, `moved_at is None`.
  - `remember_offer` grava `replaces_starts_at` com o horário atual da reunião substituída. `take_offer` devolve
    `stale_reschedule` se a reunião não tem mais esse horário. Ofertas sem `replaces_starts_at` seguem como hoje.

- [ ] **Step 1: Testes que falham:**

```python
def test_tap_and_parse_round_trip_and_old_format(): ...
def test_still_valid_by_stamp_and_old_buttons_only_if_never_moved(): ...
async def test_a_reminder_cancel_tap_from_before_a_move_cancels_nothing(...): ...
async def test_an_old_outcome_tap_after_a_move_records_nothing(...): ...
async def test_menu_and_limit_buttons_use_the_new_format(...): ...
async def test_whatsapp_reminder_template_payloads_use_the_new_format(...): ...
async def test_an_offer_for_a_moved_meeting_is_stale(...): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar**, com `tests/test_desk_flow.py`,
  `tests/test_attendance*.py`, `tests/test_reminders*.py` e `tests/test_choose_day.py`.
- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): botões e ofertas conferem o horário da reunião"`

---

### Task 5: Lembrete, "como foi?" e cancelamento pelo bot sob as travas

**Files:**
- Modify: `core/services/reminders.py` (`_remind`), `core/services/attendance.py` (`ask_outcomes` e o `cancel_yes` de
  `handle_tap`)
- Test: `tests/test_reminder_claims_pg.py` (novo; Postgres real, com o envio falso)

**Interfaces:**
- Consumes: Task 2 (`claim_reminder`, `claim_outcome`, `lock_event`, `step_back_after_cancel`) e Task 4 (`tap`).
- **Lembrete:** numa transação por agendamento, `lock_contact` e `lock_event`; `claim_reminder` com o `starts_at` lido
  (sem linha, pula); envia com os dados do retorno.
- **"Como foi?":** mesmo padrão, com `claim_outcome`.
- **Cancelamento pelo bot (`cancel_yes`):** `lock_contact` e `lock_event`; relê; `still_valid`; cancela; e
  `step_back_after_cancel` no lugar do `funnel.advance("cancelled")`.

- [ ] **Step 1: Testes que falham:**

```python
async def test_a_reminder_for_a_meeting_moved_meanwhile_is_not_sent(conn): ...
async def test_the_reminder_goes_with_the_new_time_after_a_move(conn): ...
async def test_an_outcome_question_for_a_meeting_moved_to_tomorrow_is_not_asked(conn): ...
async def test_bot_cancel_keeps_the_stage_when_another_meeting_remains(conn): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar.**
- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): lembrete e pergunta só com o horário certo"`

---

### Task 6: "Precisa de atenção" no painel

**Files:**
- Modify:
  - `core/repositories/funnel.py` (`appointments_between` devolve `sync_problem`);
  - `core/web/static/painel/painel.js` (o selo na linha);
  - `core/web/static/painel/painel.css`.
- Test: `tests/test_agenda_query.py`; conferência no Chromium sem interface.

- [ ] **Step 1: Teste que falha:** `test_the_agenda_query_carries_the_sync_problem` (o SQL pede `a.sync_problem`).
- [ ] **Step 2: Implementar.**
  - Selo `precisa de atenção`, com o motivo:
    - `all_day`: "virou dia inteiro no Google";
    - `not_organizer`: "a agenda não organiza mais";
    - `start_collision`: "horário igual a outro agendamento".
  - O selo vai no lugar do status, na cor de alerta.
- [ ] **Step 3: Ver passar e conferir no Chromium.**
- [ ] **Step 4: Commit.** `git commit -m "feat(agenda): agendamento que precisa de atenção no painel"`

---

## Depois das tasks

1. Suíte inteira, `ruff` e `pyright`.
2. Revisor independente (Opus) sobre o branch.
3. Uma passada de correção.
4. Push e PR, perguntando antes de fazer o merge.
5. Em produção:
   - conferir que a passada roda sem erro e que `appointments` não muda sem motivo;
   - teste real com `/teste`: marcar pelo bot, mover o evento no Google, esperar a passada e conferir o horário novo
     no painel e o lembrete.
