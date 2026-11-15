# Agenda no painel — Incremento 1 (Ver) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a aba Agenda do painel passa a mostrar também os eventos do Google Agenda da equipe (inclusive os marcados à
mão), lidos por um espelho que um worker sincroniza. Nada vira agendamento e nada muda para o bot.

**Architecture:** os adaptadores do Google e da Microsoft passam a ler todas as páginas e a devolver os campos novos de
`CalendarEvent`. Um worker (`core/services/calendar_sync.py`) copia os eventos de cada agenda da equipe para
`calendar_events` (título, participantes e link cifrados) e registra o estado em `calendar_sync_state`. O
`GET /painel/api/agenda` devolve esses eventos ao lado dos agendamentos, e o `painel.js` os mostra na mesma lista.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (async), Postgres, httpx, JS sem framework no painel, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-agenda-no-painel-design.md`, versão 5, local. Este plano cobre só o
**incremento 1** (seção "Incrementos").

## Global Constraints

- Commits e PR só com a autoria do Paulo: nunca `Co-Authored-By` nem "Generated with".
- O merge é do Paulo. O incremento só termina depois de testado em produção.
- Migração `infra/migrations/0012_calendar_events.sql`, que pode rodar de novo sem erro (`IF NOT EXISTS`).
- `title`, `attendees` e `meeting_url` do espelho são campos cifrados (`core/sealed_fields.py`), com a coluna antiga
  travada vazia desde a criação (`CHECK (<col> IS NULL)`), como `media.filename`.
- Nenhum título, participante ou link em log. Log de erro só com o id da agenda e um código curto.
- Datas de dia inteiro e conversões usam `settings.office_timezone`, nunca o fuso do processo.
- Janela do espelho: de agora − 14 dias até agora + 30 dias. Retenção: apagar linhas com `ends_at` há mais de 14 dias, em
  toda passada, mesmo sem agenda configurada.
- `CALENDAR_SYNC_MINUTES`: padrão 5, faixa 1 a 15 (validado na inicialização, como as outras faixas).
- Lista do provedor incompleta (falha em qualquer página) nunca é aplicada: a agenda fica como estava.
- Toda a suíte, `ruff check .` e `pyright` limpos antes de cada commit.

## Review Focus

1. Uma agenda com mais de 250 eventos na janela (duas páginas ou mais): todos aparecem, e nenhum some do espelho.
2. Um evento de dia inteiro com o processo em UTC: aparece no dia certo de Belém, como "o dia todo", e nunca às 21:00
   do dia anterior.
3. Uma agenda que o Google recusa (403/404) enquanto outra responde: a que respondeu atualiza, e a recusada mantém o
   que tinha, com a faixa "desatualizada".
4. Um evento que o bot marcou (já em `appointments`) não aparece duas vezes na Agenda.
5. O mesmo convite nas agendas de duas pessoas da equipe aparece uma vez só, com as duas pessoas.

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `core/integrations/workspace/models.py` | campos novos de `CalendarEvent` |
| `core/integrations/workspace/google.py`, `microsoft.py` | paginação completa e preenchimento dos campos |
| `infra/migrations/0012_calendar_events.sql` | tabelas do espelho e do estado |
| `core/sealed_fields.py` | três campos novos |
| `core/repositories/calendar_mirror.py` (novo) | leitura e escrita do espelho e do estado |
| `core/services/calendar_sync.py` (novo) | `team_calendars()`, uma passada, o worker e o pedido manual |
| `core/config.py`, `.env.example` | `CALENDAR_SYNC_MINUTES` |
| `core/metrics.py` | métricas da sincronização |
| `core/web/app.py` | liga o worker |
| `core/web/panel.py` | `GET /agenda` com `events` e `sync`; `POST /agenda/sync` |
| `core/web/static/painel/painel.js`, `painel.css`, `index.html` | eventos na lista, faixa e botão |
| `docs/CRIPTOGRAFIA.md` | passo de rollback: apagar o espelho |

---

### Task 1: O provedor lê tudo e diz mais sobre cada evento

**Files:**
- Modify: `core/integrations/workspace/models.py` (`CalendarEvent`)
- Modify: `core/integrations/workspace/google.py` (`_to_event`, `list_events`)
- Modify: `core/integrations/workspace/microsoft.py` (`_to_event`, `list_events`)
- Test: `tests/test_workspace_calendar_events.py` (novo)

**Interfaces:**
- Produces: `CalendarEvent` com os campos novos, todos com padrão (os usos atuais não mudam):
  - `all_day: bool = False`;
  - `status: Literal["confirmed", "tentative"] = "confirmed"`;
  - `etag: str | None = None`;
  - `ical_uid: str | None = None`;
  - `original_start: datetime | None = None`;
  - `organizer_is_self: bool = False`.
- `list_events(calendar_id, start, end) -> list[CalendarEvent]` mantém a assinatura e passa a ler todas as páginas.

- [ ] **Step 1: Escrever os testes que falham.** Os testes trocam `_request` do adaptador por um falso que responde por
  página.

```python
async def test_google_reads_every_page(monkeypatch): ...
    # página 1: 250 itens + nextPageToken "p2"; página 2: 3 itens, sem token → 253 eventos
async def test_google_error_on_second_page_raises_and_returns_nothing(monkeypatch): ...
    # página 2 levanta WorkspaceError → list_events levanta, nenhuma lista parcial
async def test_google_all_day_is_a_civil_date_in_the_office_zone(monkeypatch): ...
    # TZ do processo UTC; {"start":{"date":"2026-10-23"},"end":{"date":"2026-10-24"}}
    # → all_day True, start == 2026-10-23 00:00 America/Belem, end == 2026-10-24 00:00 America/Belem
async def test_google_fills_status_etag_ical_uid_original_start_and_organizer(monkeypatch): ...
    # status "tentative"; etag '"3"'; iCalUID "abc@google.com";
    # originalStartTime {"dateTime": ...} → original_start; organizer {"self": True} → organizer_is_self True
async def test_google_skips_cancelled_items(monkeypatch): ...
async def test_microsoft_follows_next_link(monkeypatch): ...
    # "@odata.nextLink" na página 1 → pede a URL do link; 2 páginas → todos
async def test_microsoft_fills_all_day_etag_ical_uid_and_is_organizer(monkeypatch): ...
    # isAllDay True; "@odata.etag"; iCalUId; isOrganizer; showAs "tentative" → status "tentative"
```

- [ ] **Step 2: Rodar e ver falhar.**
  Run: `.venv/bin/python -m pytest -q -p no:warnings -o addopts="" tests/test_workspace_calendar_events.py`
  Expected: FAIL (campos inexistentes, só a primeira página lida).

- [ ] **Step 3: Implementar.**
  - **Google:**
    - laço por `pageToken`/`nextPageToken`, com os mesmos parâmetros de hoje;
    - `moment()` de dia inteiro: `datetime.combine(date, time(0), ZoneInfo(settings.office_timezone))`;
    - `all_day` = `"date" in raw["start"]`;
    - `status` = `"tentative"` se `raw["status"] == "tentative"`, senão `"confirmed"`;
    - `etag` = `raw.get("etag")`;
    - `ical_uid` = `raw.get("iCalUID")`;
    - `original_start` = o `originalStartTime` pelo mesmo `moment()`;
    - `organizer_is_self` = `bool((raw.get("organizer") or {}).get("self"))`.
  - **Microsoft:**
    - laço por `@odata.nextLink` (a URL completa; `_request` ganha um parâmetro `url` absoluto, ou um método `_get_url`);
    - `all_day` = `raw.get("isAllDay")`;
    - `status` = `"tentative"` se `raw.get("showAs") == "tentative"`;
    - `etag` = `raw.get("@odata.etag")`;
    - `ical_uid` = `raw.get("iCalUId")`;
    - `original_start` = `_graph_datetime(raw.get("originalStart"))`, quando houver;
    - `organizer_is_self` = `bool(raw.get("isOrganizer"))`.

- [ ] **Step 4: Rodar e ver passar.** O mesmo comando, mais `tests/test_scheduling*.py` e `tests/test_booking*.py`, que
  usam `list_events`/`busy_periods`. Expected: tudo PASS.

- [ ] **Step 5: Commit.**
  `git commit -m "feat(agenda): leitura completa das agendas e campos novos dos eventos"`

---

### Task 2: O espelho no banco

**Files:**
- Create: `infra/migrations/0012_calendar_events.sql`
- Modify: `core/sealed_fields.py` (`FIELDS`)
- Create: `core/repositories/calendar_mirror.py`
- Modify: `docs/CRIPTOGRAFIA.md` (rollback: apagar `calendar_events`, `calendar_ignores`, `calendar_sync_state`)
- Test: `tests/test_calendar_mirror_pg.py` (novo; Postgres real, pula sem `TEST_DATABASE_URL`)
- Modify: os testes que fixam o número de campos cifrados (`tests/test_sealed_fields.py`, `tests/test_sealing_pg.py`),
  passando a incluir os três campos novos.

**Interfaces:**
- Produces (em `core/repositories/calendar_mirror.py`):
  - `async def replace_window(conn, calendar_id: str, provider: str, staff_id: int | None, start: datetime, end:
    datetime, events: list[CalendarEvent]) -> int`: insere ou atualiza cada evento por
    `(calendar_id, provider_event_id)` e apaga as linhas daquela agenda com início na janela que não vieram. Devolve
    quantas ficaram.
  - `async def record_attempt(conn, calendar_id: str) -> None`, `async def record_success(conn, calendar_id: str) -> None`
    e `async def record_error(conn, calendar_id: str, code: str) -> None`: o estado em `calendar_sync_state`. O
    `record_success` também grava `last_attempt_finished_at`, e o `record_error` grava `last_error_at` e
    `last_attempt_finished_at`.
  - `async def forget_calendars_except(conn, calendar_ids: list[str]) -> int`: apaga o espelho e o estado das agendas
    fora do conjunto.
  - `async def purge_old(conn, now: datetime) -> int`: retenção de 14 dias.
  - `async def events_between(conn, start: datetime, end: datetime) -> list[dict]`: os eventos **sem agendamento
    confirmado com o mesmo `provider_event_id`**, agrupados por `event_key`. Cada item tem `key`, `starts_at`,
    `ends_at`, `all_day`, `status`, `staff_ids` (ordenados; nulo para a Agenda geral) e `title` (decifrado).
  - `async def sync_states(conn) -> list[dict]`: as linhas de `calendar_sync_state`.
  - `def event_key(event: CalendarEvent) -> str`: `f"{ical_uid}|{original_start.isoformat() if original_start else ''}"`;
    sem `ical_uid`, `f"{calendar_id}|{provider_event_id}"`.

- [ ] **Step 1: Migração**, com as colunas da spec (seção 3):
  - `calendar_events`:
    - `id`, `calendar_id`, `provider`, `provider_event_id`, `event_key`, `staff_id`;
    - `starts_at`, `ends_at`, `all_day`, `status` CHECK in (`confirmed`,`tentative`), `organizer_is_self`, `etag`;
    - `title`/`title_sealed`, `attendees`/`attendees_sealed`, `meeting_url`/`meeting_url_sealed`, cada coluna antiga
      com `CHECK (… IS NULL)`;
    - `suggested_contact_id`, `synced_at`;
    - `UNIQUE (calendar_id, provider_event_id)`, índices `(starts_at)` e `(event_key)`.
  - `calendar_ignores`: `event_key` PK, `panel_user_id`, `created_at`.
  - `calendar_sync_state`: `calendar_id` PK, `last_attempt_at`, `last_attempt_finished_at`, `last_success_at`,
    `last_error`, `last_error_at`.
  - `FIELDS` ganha `Field("calendar_events","title")`, `Field("calendar_events","attendees")` e
    `Field("calendar_events","meeting_url")`.

- [ ] **Step 2: Testes que falham.**

```python
async def test_replace_window_inserts_updates_and_drops_what_did_not_come(conn): ...
async def test_rows_of_another_calendar_are_untouched(conn): ...
async def test_title_attendees_and_link_are_sealed_on_disk(conn): ...
    # SELECT title, title_sealed → (None, bytes); events_between devolve o título legível
async def test_an_event_already_booked_by_the_bot_is_left_out(conn): ...
    # appointment confirmed com o mesmo provider_event_id → fora de events_between
async def test_the_same_invite_in_two_calendars_is_one_item_with_both_people(conn): ...
async def test_purge_old_drops_events_ended_more_than_14_days_ago(conn): ...
async def test_forget_calendars_except_drops_mirror_and_state(conn): ...
async def test_sync_state_records_attempt_success_and_error(conn): ...
async def test_the_migration_runs_twice(conn): ...
```

- [ ] **Step 3: Ver falhar.**
  Run: `TEST_DATABASE_URL=… .venv/bin/python -m pytest -q -p no:warnings -o addopts="" tests/test_calendar_mirror_pg.py`.
  Expected: FAIL (módulo inexistente).

- [ ] **Step 4: Implementar** `calendar_mirror.py`:
  - grava com `sealed_fields.write`;
  - lê com `pair_sql`/`open_dict`;
  - os participantes são JSON (`[{email, name}]`) antes de cifrar;
  - agrupamento por `event_key` em Python, depois da consulta ordenada por início.

- [ ] **Step 5: Ver passar**, rodando também `tests/test_sealed_fields.py tests/test_sealing_pg.py`. Expected: PASS.

- [ ] **Step 6: Commit.** `git commit -m "feat(agenda): espelho cifrado das agendas da equipe"`

---

### Task 3: A sincronização

**Files:**
- Create: `core/services/calendar_sync.py`
- Modify: `core/config.py` (`calendar_sync_minutes: int = 5`, guarda de faixa 1 a 15), `.env.example`
- Modify: `core/metrics.py`
- Modify: `core/web/app.py` (worker)
- Test: `tests/test_calendar_sync_pg.py` (novo; Postgres real e um `CalendarPort` falso), mais o teste de ordem dos
  workers (`tests/test_data_format.py`, `tests/test_test_mode.py`), que passa a contar o worker novo.

**Interfaces:**
- Consumes: Task 1 (`list_events`) e Task 2 (todo o `calendar_mirror`).
- Produces:
  - `def team_calendars(staff: list[StaffMember]) -> list[tuple[str, int | None]]`: `(calendar_id, staff_id)`, sem
    repetir. A recepção (`settings.reception_calendar_id`) e as de `settings.staff_calendar_ids` sem dono ficam com
    `None`; as `staff.calendar_id` da equipe ativa ficam com o id da pessoa.
  - `async def sync_once(conn) -> dict[str, int]`: uma passada em todas as agendas. Devolve `{"ok", "error",
    "skipped"}`.
  - `async def run_calendar_sync_worker(stop_event: asyncio.Event) -> None`.
  - `def request_sync() -> None`: acorda o worker (um `asyncio.Event` do módulo).
  - Métricas `CALENDAR_SYNC_RUNS = Counter("calendar_sync_runs_total", …, ["result"])` e
    `CALENDAR_SYNC_LAST_SUCCESS = Gauge("calendar_sync_last_success", …)`.

- [ ] **Step 1: Testes que falham.**

```python
async def test_each_calendar_is_mirrored_with_its_owner(conn, fake_calendar): ...
async def test_a_failing_calendar_keeps_its_rows_and_records_the_error(conn, fake_calendar): ...
    # agenda A levanta WorkspaceError, B responde → A intacta, last_error "provider_error"; B atualizada
async def test_a_calendar_already_being_synced_is_skipped(conn, fake_calendar): ...
    # outra conexão segura pg_advisory_lock(7411304, hashtext('A')) → resultado "skipped" para A
async def test_retention_runs_with_no_calendar_configured(conn, monkeypatch): ...
async def test_a_person_deactivated_loses_her_calendar_rows(conn, fake_calendar): ...
async def test_team_calendars_dedupes_reception_staff_and_setting(): ...
async def test_request_sync_wakes_the_worker_early(monkeypatch): ...
async def test_logs_carry_no_title(conn, fake_calendar, caplog): ...
```

- [ ] **Step 2: Ver falhar.**

- [ ] **Step 3: Implementar.**
  - Por agenda:
    - `pg_try_advisory_lock(7411304, hashtext(calendar_id))` de sessão, liberado no `finally`;
    - `record_attempt`;
    - `list_events` (fora de transação);
    - numa transação: `replace_window` e `record_success`;
    - erro de provedor: `record_error("provider_error")`; erro de permissão (`WorkspaceAuthError`):
      `record_error("no_access")`.
  - No fim da passada: `forget_calendars_except` e `purge_old`, sempre.
  - Métrica: `ok` se tudo deu certo, `partial` se alguma agenda falhou, `error` se todas falharam, `skipped` sem
    nenhuma agenda.
  - O worker segue o padrão do `run_reminder_worker` e espera `calendar_sync_minutes` ou `request_sync()`, o que vier
    antes.
  - Sem provedor de agenda configurado (`workspace.configuration_report()` sem `calendar`), só roda a retenção.

- [ ] **Step 4: Ver passar**, e a suíte inteira. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): sincronização periódica das agendas"`

---

### Task 4: A API

**Files:**
- Modify: `core/web/panel.py` (`agenda`; novo `agenda_sync`)
- Test: `tests/test_panel_agenda.py` (novo; o cliente de `tests/test_panel.py`, com o repositório falso)

**Interfaces:**
- Consumes: Task 2 (`events_between`, `sync_states`) e Task 3 (`request_sync`, `team_calendars`).
- Produces:
  - `GET /painel/api/agenda` mantém `appointments` e `staff` e acrescenta:
    - `events`: os itens de `events_between` no mesmo período;
    - `sync`:

      ```
      {
        "calendars": [{"calendar_id", "staff_id", "last_success_at", "last_error", "last_error_at"}],
        "stale": bool,
        "requested_at": str | None,
        "done": bool
      }
      ```

  - `stale` é verdadeiro se alguma agenda de `team_calendars()`:
    - nunca teve sucesso;
    - tem `last_success_at` mais velho que `3 × calendar_sync_minutes`;
    - ou tem `last_error_at` depois do `last_success_at`.

    Sem agendas: `calendars: []` e `stale: false`.
  - `POST /painel/api/agenda/sync`: guarda `requested_at` (no módulo `calendar_sync`), chama `request_sync()` e responde
    `{"requested_at": …}`. `done` é verdadeiro quando todas as agendas têm `last_attempt_finished_at > requested_at`.
  - O `calendar_id` sai na resposta só para a tela dizer qual agenda falhou.

- [ ] **Step 1: Testes que falham.**

```python
def test_agenda_carries_events_and_sync(client, fake_mirror): ...
def test_stale_when_a_calendar_never_synced_or_failed_after_success(client, fake_mirror): ...
def test_not_stale_with_no_calendar(client, fake_mirror): ...
def test_sync_request_returns_requested_at_and_wakes_the_worker(client, fake_mirror): ...
def test_done_after_every_calendar_finished_after_the_request(client, fake_mirror): ...
def test_sync_request_needs_a_session(client): ...
```

- [ ] **Step 2: Ver falhar. Step 3: Implementar. Step 4: Ver passar**, com `tests/test_panel.py` e
  `tests/test_agenda_query.py`.

- [ ] **Step 5: Commit.** `git commit -m "feat(agenda): eventos e estado da sincronização na API do painel"`

---

### Task 5: A tela

**Files:**
- Modify: `core/web/static/painel/painel.js` (`load`, `renderAgenda`, os chips e a nota)
- Modify: `core/web/static/painel/painel.css` (selo e faixa)
- Modify: `core/web/static/painel/index.html` (faixa de desatualizada e botão "Atualizar agenda" na seção da Agenda)
- Test: `tests/test_panel.py` (o HTML tem os ids novos); conferência no Chromium sem interface

**Interfaces:**
- Consumes: Task 4 (o JSON de `/agenda` e `/agenda/sync`).

- [ ] **Step 1: Teste que falha:** `test_the_agenda_has_the_stale_banner_and_the_sync_button`. O `index.html` servido
  tem `id="agenda-stale"` e `id="agenda-sync-btn"`.

- [ ] **Step 2: Implementar.**
  - `load()` guarda `state.events` e `state.sync`.
  - `renderAgenda()` junta agendamentos e eventos por dia, ordenados pela hora.
  - Um **evento** usa a mesma linha visual:
    - a pessoa vem de `staff_ids[0]`, ou "Agenda geral" quando nulo; com mais de uma, os nomes separados por " · ";
    - o título é o do evento;
    - o selo "do Google";
    - sem clique.
  - Dia inteiro: uma linha "o dia todo" no topo do dia.
  - O filtro por pessoa vale para os eventos: o evento aparece se alguma das suas pessoas está ligada.
  - Faixa `#agenda-stale` visível quando `sync.stale`, com o texto "Agenda desatualizada: {nomes das agendas com
    erro ou sem sucesso}".
  - O botão `#agenda-sync-btn` faz `POST /agenda/sync` e consulta `/agenda` a cada 3 s até `sync.done`, ou por 60 s.
  - Tudo com `esc()`: os títulos vêm de fora.

- [ ] **Step 3: Ver passar** `tests/test_panel.py` e conferir no Chromium sem interface. A API falsa é a do
  `scratchpad/painel-real-demo/fake-api.js`, mais `events` e `sync`. Conferir:
  - um evento normal, um de dia inteiro e um convite em duas agendas;
  - a faixa de desatualizada;
  - o botão de atualizar.

- [ ] **Step 4: Commit.** `git commit -m "feat(agenda): eventos do Google na aba Agenda do painel"`

---

## Depois das tasks

1. A suíte inteira, `ruff` e `pyright` limpos.
2. Push e PR "feat(agenda): ver os eventos do Google na Agenda (incremento 1)", sem coautoria.
3. Pedir o merge ao Paulo, sem fazer o merge.
4. Depois do deploy, conferir no painel que a reunião da Ellen (22/10 16:30) e os outros eventos da semana aparecem, e
   que a faixa de desatualizada não aparece.
