# Criptografia dos campos sensíveis — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o Postgres e as filas do Redis guardam só texto cifrado nos campos de cliente, com a chave fora do banco, sem quebrar o bot, o painel nem as voltas de deploy.

**Architecture:** um módulo `core/crypto.py` (AES-256-GCM com contexto autenticado, HMAC para impressões digitais) e um registro único `core/sealed_fields.py` que descreve cada campo cifrado e dá as funções de ler e escrever segundo a tabela de estados. Os repositórios usam o registro; o `cli.seal_data` usa o mesmo registro para converter, conferir, travar e recifrar. Marcadores de formato no Postgres e no Redis, conferidos pelo `deploy.sh`, impedem que uma imagem antiga suba sobre dados cifrados.

**Tech Stack:** Python 3.12, `cryptography` (já dependência), psycopg 3 async, redis-py async, Bash (`deploy.sh`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-criptografia-campos-design.md`

## Global Constraints

- AES-256-GCM, nonce de 96 bits de `os.urandom`, tag de 128 bits. Envelope: `versão do formato (1 byte, = 1) ‖ versão da chave (1 byte) ‖ nonce (12) ‖ texto cifrado ‖ tag (16)`; menos de 30 bytes é recusado.
- Contexto (AAD) = `"<tabela>.<coluna>"`, ou `"queue"` e `"queue.quarantine"`.
- Impressão digital: HMAC-SHA256 com `DATA_INDEX_KEY` sobre `b"converge-fp-v1\x00" + kind + b"\x00" + canônico`.
- `DATA_KEYS` = `1:<base64 de 32 bytes>[,2:<...>]`, versões 1–255 sem repetição; a primeira cifra. `DATA_INDEX_KEY` = base64 de 32 bytes. Obrigatórias em produção; nunca impressas, nem em erro.
- `seal(None)` é `None`; `''` é cifrado.
- Colunas `NOT NULL` com sentinela `''`: `sessions.message`, `appointments.title`, `mail_drafts.body_text`. Todas as outras do registro são opcionais, com sentinela `NULL`.
- Migrações só aditivas. Esta é a `0009`; a do app da equipe é a `0010`.
- Nenhum log, exceção, saída de comando ou linha de auditoria leva conteúdo de cliente; erros de criptografia levam tabela, coluna e id.
- Formato de dados desta imagem: `1` (label `org.converge.data-format`).
- Commits sem coautoria.

## Review Focus

1. **Um caminho de escrita esquecido** grava texto na coluna antiga. Antes da trava, isso é vazamento silencioso; depois dela, é erro de banco em produção. O teste-sentinela da Task 3 percorre o registro e falha se algum `INSERT`/`UPDATE` em `core/` ou `cli/` cita uma coluna cifrada sem passar pelo registro.
2. **A volta automática do deploy** sobe a imagem de formato 0 depois que a de formato 1 escreveu. A Task 9 testa a volta com o banco já em formato 1.
3. **Uma lista do painel ou da Inbox com uma linha ilegível** derruba a tela inteira. A Task 4 testa a Inbox e o funil com um envelope adulterado.
4. **A conversão concorrente:** o painel edita um contato enquanto o `seal_data` converte. A Task 7 testa a atualização condicional que perde a corrida.
5. **Um teste antigo com cursor falso** (`tests/test_booking.py`, `tests/test_scheduling.py`) passa a receber linhas de formato diferente e quebra por formato de tupla, não por comportamento. As Tasks 3–5 ajustam os fakes junto com o mapeador, e o ledger anota cada ajuste.

---

### Task 1: O módulo de criptografia e as chaves

**Files:**
- Create: `core/crypto.py`, `cli/data_keys.py`, `deploy/data-keys.sh`, `tests/test_crypto.py`
- Modify: `core/config.py` (campos e validação), `tests/conftest.py` (chaves de teste), `tests/test_config.py`, `.github/workflows/ci-cd.yml` (envia `deploy/data-keys.sh`)

**Interfaces:**
- Produces:
  - `core.crypto.SealedDataError(Exception)`
  - `seal(context: str, text: str | None) -> bytes | None`
  - `open_sealed(context: str, data: bytes | None) -> str | None`
  - `key_version_of(data: bytes) -> int`
  - `fingerprint(kind: Literal["phone", "email"], value: str | None) -> bytes | None`
  - `canonical_phone(value: str) -> str | None`, `canonical_email(value: str) -> str | None`
  - `seal_text(context: str, text: str) -> str` e `open_text(context: str, data: str) -> str`: os mesmos, em base64, para o Redis
  - `reset_keys() -> None`: esquece as chaves em cache (para os testes e a rotação)
  - `settings.data_keys: str`, `settings.data_index_key: str`

- [ ] **Step 1: Testes do módulo (falham)** em `tests/test_crypto.py`:

```python
def test_round_trip_keeps_text_empty_accents_and_emoji():
    for text in ["", "Olá, João", "📅 amanhã às 9h"]:
        assert open_sealed("sessions.message", seal("sessions.message", text)) == text
    assert seal("contacts.name", None) is None and open_sealed("contacts.name", None) is None

def test_same_text_seals_differently(): assert seal("c.n", "x") != seal("c.n", "x")

def test_tampered_truncated_or_moved_envelope_is_refused_without_the_text():
    box = seal("contacts.phone", "5591988887777")
    for bad, ctx in [(box[:-1] + bytes([box[-1] ^ 1]), "contacts.phone"),
                     (box[:29], "contacts.phone"), (box, "contacts.name")]:
        with pytest.raises(SealedDataError) as caught:
            open_sealed(ctx, bad)
        assert "5591988887777" not in str(caught.value) and caught.value.__cause__ is None

def test_an_old_key_still_opens_after_rotation(monkeypatch):  # 1 escreve; depois "2:..,1:.." abre o antigo e escreve na 2
def test_unknown_key_version_is_refused():
def test_phone_fingerprint_is_the_same_in_any_format():
    assert fingerprint("phone", "+55 (91) 98888-7777") == fingerprint("phone", "91988887777") \
        == fingerprint("phone", "5591988887777")
def test_email_fingerprint_trims_and_lowercases_and_refuses_inner_spaces():
    assert fingerprint("email", " Ana@X.com ") == fingerprint("email", "ana@x.com")
    assert fingerprint("email", "a b@x.com") is None
def test_kinds_never_collide(): assert fingerprint("email", "5591988887777") != fingerprint("phone", "5591988887777")
def test_redis_text_form_round_trips(): assert open_text("queue", seal_text("queue", '{"a":1}')) == '{"a":1}'
```

- [ ] **Step 2: Rodar.** `.venv/bin/python -m pytest -q tests/test_crypto.py` → FAIL (`ModuleNotFoundError: core.crypto`).

- [ ] **Step 3: Implementar `core/crypto.py`.** `AESGCM` de `cryptography.hazmat.primitives.ciphers.aead`; `hmac`/`hashlib` da biblioteca padrão. As chaves são lidas de `settings` na primeira chamada e guardadas num cache do módulo. `canonical_phone` usa `contact_details._normalise_phone` (que devolve `+55 DD NNNNN-NNNN`) e tira tudo que não é dígito, resultando `55DDNNNNNNNNN`; se ele devolver `None`, usa só os dígitos (e `None` se não houver dígito). Toda exceção de abertura vira `SealedDataError(...)` com `raise ... from None`.

- [ ] **Step 4: Rodar.** O mesmo comando → PASS.

- [ ] **Step 5: Configuração (falha primeiro).** Em `tests/test_config.py`:
  - `test_production_requires_both_data_keys`: `Settings(app_env="production", ..., data_keys="")` levanta `ValueError` com "DATA_KEYS"; o mesmo para `DATA_INDEX_KEY`;
  - `test_malformed_data_keys_are_refused_without_the_value`: versão repetida, versão 0 ou 256, base64 inválido, e chave de 16 bytes, cada uma levanta, e a mensagem não contém o valor;
  - `test_development_runs_without_data_keys`.

  Em `core/config.py`, os campos `data_keys: str = ""` e `data_index_key: str = ""`, e a validação dentro de `_guardrails` (produção exige as duas) mais um validador de formato que roda sempre que o valor não é vazio. Em `tests/conftest.py`, uma fixture `autouse` põe chaves de teste fixas (`1:` + base64 de 32 bytes conhecidos) e chama `crypto.reset_keys()` antes e depois.

- [ ] **Step 6: `cli/data_keys.py` e `deploy/data-keys.sh`.**
  - `python -m cli.data_keys check`: confere o formato das duas chaves carregadas e imprime só "DATA_KEYS: 1 chave(s), escreve com a versão N" e "DATA_INDEX_KEY: ok". Teste `test_data_keys_check_never_prints_a_key` em `tests/test_crypto.py`.
  - `deploy/data-keys.sh <env-file>` (no VPS, como `deploy`): se o arquivo já tem `DATA_KEYS=` ou `DATA_INDEX_KEY=`, sai com erro sem mudar nada; senão, acrescenta as duas com `head -c 32 /dev/urandom | base64 -w0`, sem imprimir nada além de "chaves gravadas em <arquivo>". Teste em `tests/test_crypto.py` com um arquivo temporário: grava as duas linhas, com valores de 44 caracteres; a saída não contém nenhum dos dois; a segunda execução recusa.
  - O CI envia `deploy/data-keys.sh` para `/opt/converge-agent/` junto com os outros scripts.

  **Ruling a registrar:** a spec pede `python -m cli.data_keys new` gravando no `.env`, mas o container não escreve o `.env` do host. A geração fica no script do host, e o `cli.data_keys` só confere.

- [ ] **Step 7: Suíte e commit.** `.venv/bin/python -m pytest -q -p no:warnings -o addopts=""` → tudo passa. `git commit -m "feat: módulo de criptografia dos campos e chaves DATA_KEYS"`.

---

### Task 2: O registro dos campos e a migração 0009

**Files:**
- Create: `core/sealed_fields.py`, `infra/migrations/0009_sealed_fields.sql`, `tests/test_sealed_fields.py`, `tests/test_sealed_pg.py`

**Interfaces:**
- Consumes: `seal`, `open_sealed`, `fingerprint`, `SealedDataError` (Task 1).
- Produces, em `core/sealed_fields.py`:
  - `@dataclass(frozen=True) class Field: table: str; column: str; required: bool` com `context` (= `f"{table}.{column}"`) e `sealed` (= `f"{column}_sealed"`).
  - `FIELDS: tuple[Field, ...]`: os 13 da spec (`contacts`: name, email, phone, organization, notes, origin_detail, interest; `sessions.message`; `appointments`: title, meeting_url; `automation_queue.reason`; `mail_threads.subject`; `mail_drafts.body_text`), com `required=True` só nos três `NOT NULL`.
  - `field(table: str, column: str) -> Field`
  - `select_sql(alias: str, f: Field) -> str`: `"{alias}.{column}, {alias}.{sealed}"`, os dois lado a lado.
  - `read(f: Field, legacy: str | None, sealed: bytes | None) -> str | None`: a tabela de estados (obrigatório: `sealed` se não for `None`, senão `legacy`; opcional: `legacy` se não for `None`, senão abre `sealed`).
  - `read_lenient(f: Field, legacy, sealed, *, row_id) -> str | None`: igual, mas em `SealedDataError` loga `"Campo ilegível: %s.%s id=%s"` (sem conteúdo) e devolve `UNREADABLE = "[ilegível]"`.
  - `write(f: Field, value: str | None) -> dict[str, object]`: `{column: "" if f.required else None, sealed: seal(f.context, value)}`.
  - `fingerprints(phone: str | None, email: str | None) -> dict[str, bytes | None]`: `{"phone_fp": ..., "email_fp": ...}`.

- [ ] **Step 1: Testes do registro (falham)** em `tests/test_sealed_fields.py`:
  - `test_registry_lists_exactly_the_spec_fields`: o conjunto `(table, column)` é exatamente os 13 acima, e `required` é verdadeiro só em `sessions.message`, `appointments.title`, `mail_drafts.body_text`;
  - `test_read_follows_the_state_table`: obrigatório com `sealed` preenchido ignora `legacy=''`; obrigatório sem `sealed` devolve `legacy` (inclusive `''`); opcional com `legacy=''` devolve `''`; opcional com `legacy=None` abre `sealed`; opcional com os dois `None` devolve `None`;
  - `test_write_empties_the_legacy_column_by_kind`;
  - `test_read_lenient_marks_an_unreadable_value_and_logs_no_content` (com `caplog`);
  - `test_migration_has_a_sealed_column_for_every_field`: lê `0009_sealed_fields.sql` e acha `ADD COLUMN IF NOT EXISTS <column>_sealed BYTEA` para cada campo, mais `phone_fp`, `email_fp`, `appointments.test_mode` e `CREATE TABLE IF NOT EXISTS data_format`.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar** `core/sealed_fields.py` e `0009_sealed_fields.sql`:
  - as colunas `BYTEA` de cada campo;
  - `contacts.phone_fp BYTEA`, `contacts.email_fp BYTEA`, com `CREATE INDEX IF NOT EXISTS` em cada uma;
  - `appointments.test_mode BOOLEAN NOT NULL DEFAULT FALSE`;
  - `CREATE TABLE IF NOT EXISTS data_format (version SMALLINT NOT NULL, CHECK (version >= 0))` com no máximo uma linha (`CREATE UNIQUE INDEX ... ON data_format ((true))`).

  Nenhum `UPDATE` de dados na migração.

- [ ] **Step 4: Rodar** → PASS.

- [ ] **Step 5: Postgres real.** `tests/test_sealed_pg.py`, no padrão de `tests/test_test_mode_pg.py` (pulado sem `TEST_DATABASE_URL`, cada teste em rollback): `test_migration_0009_applies_on_top_of_0008`, conferindo as colunas pelo `information_schema`. Para rodar: um Postgres descartável em Docker (porta escolhida pelo Docker), com `infra/init.sql` e todas as migrações, e `TEST_DATABASE_URL` apontando para ele. O comando de subida vai para o ledger.

- [ ] **Step 6: Commit.** `feat: registro dos campos cifrados e migração 0009`.

---

### Task 3: Contatos

**Files:**
- Modify: `core/repositories/contacts.py` (`_COLUMNS`, `_to_contact`, `get_or_create`, `update_details`), `core/repositories/funnel.py` (`_LEAD_COLUMNS`, `list_leads`, `lead`, `update_lead`, `appointments_between`), `core/repositories/inbox.py` (`_LIST`, `conversation`, `set_origin`), `core/repositories/briefing.py` (`appointments_between`, `new_contacts_since`, `open_handoffs`, `stale_proposals`, `silent_leads`), `core/repositories/automations.py` (as consultas que trazem nome), `core/repositories/handoffs.py` (nome do contato), `core/repositories/test_mode.py` (`create`), `cli/load_content.py` (se ler campos de contato)
- Test: `tests/test_sealed_pg.py`, `tests/test_sealed_writes.py`; ajustes em fakes de `tests/test_booking.py` e `tests/test_scheduling.py` se quebrarem por formato de linha

**Interfaces:**
- Consumes: `FIELDS`, `field`, `select_sql`, `read`, `read_lenient`, `write`, `fingerprints` (Task 2).
- Produces: `contacts.find_by_phone(conn, phone: str) -> Contact | None` e `contacts.find_by_email(conn, email: str) -> Contact | None`, pela impressão digital. **Ruling a registrar:** nenhum código busca contato por telefone ou e-mail hoje; as duas funções existem porque a spec exige que a busca funcione sem texto claro.

- [ ] **Step 1: Teste-sentinela (falha).** `tests/test_sealed_writes.py::test_no_sql_writes_a_sealed_column_in_clear`: para cada `.py` em `core/` e `cli/`, acha cada `INSERT INTO <tabela>`/`UPDATE <tabela>` cuja tabela está no registro e, no trecho até o fim da string SQL, cada coluna cifrada citada como alvo (`col =` ou na lista do `INSERT`). Ela só pode aparecer como `<col>_sealed`, ou ao lado da própria `<col>_sealed` (a escrita do sentinela `''`/`NULL` que o `write` gera). Exceção explícita: `cli/seal_data.py`. A mensagem lista arquivo e linha de cada violação. Hoje ele falha em `contacts.py`, `funnel.py`, `inbox.py`, `test_mode.py`, `appointments.py`, `automations.py`, `mailbox.py`. As Tasks 3, 4 e 5 o deixam verde.

- [ ] **Step 2: Postgres real (falham)** em `tests/test_sealed_pg.py`:
  - `test_a_new_contact_is_stored_sealed`: `get_or_create(conn, "wa:5591988887777", name="Maria")`, depois `update_details(..., email="Maria@X.com", phone="(91) 98888-7777", notes="prefere manhã")`. A linha crua tem `name`, `email`, `phone`, `notes` `NULL`; nenhuma coluna `BYTEA` contém `b"Maria"`; `get_by_id` devolve os valores originais.
  - `test_a_contact_is_found_by_phone_or_email_in_another_format`: `find_by_phone(conn, "+55 91 98888-7777")` e `find_by_email(conn, " maria@x.com")` acham a linha.
  - `test_an_unconverted_contact_is_read_from_the_legacy_columns`: `INSERT` cru com `name='João'` e `_sealed` `NULL`; `get_by_id` devolve "João".
  - `test_the_name_is_filled_once_also_when_sealed`: com `name_sealed` preenchido e `name` `NULL`, `get_or_create(..., name="🔥Zé🔥")` não troca o nome.
  - `test_panel_edits_go_sealed`: `funnel.update_lead(conn, id, {"interest": "Clínica", "phone": "91 98888 7777"})` grava `interest_sealed`, `phone_sealed` e `phone_fp`, com `interest` e `phone` `NULL`; `funnel.lead` devolve os valores.
  - `test_origin_detail_goes_sealed`: `inbox.set_origin(...)`.
  - `test_test_mode_creates_a_sealed_contact`: `test_mode.create(...)`.
  - `test_a_list_survives_an_unreadable_name`: um contato com `name_sealed` adulterado; `funnel.list_leads` e `inbox.list_conversations` devolvem a lista com `"[ilegível]"` naquele item.

- [ ] **Step 3: Rodar** com o Postgres descartável → FAIL.

- [ ] **Step 4: Implementar.**
  - Cada `SELECT` que traz um campo de contato passa a trazer o par via `select_sql`, e o mapeador decide com `read`. Numa lista, usa `read_lenient`.
  - Cada escrita monta as colunas com `write`, e `phone`/`email` também com `fingerprints`.
  - O "preenche o nome uma vez só" de `get_or_create` compara o nome **lido** (`read`), não `row[2]`.
  - `update_lead` separa os campos do registro (`interest`, `notes`, `organization`, `email`, `phone`) dos outros (`stage`, `owner_staff_id`).
  - Se `tests/test_booking.py` ou `tests/test_scheduling.py` quebrarem por formato de tupla, o fake ganha as colunas novas, sem mudar asserções. Cada ajuste vai para o ledger.

- [ ] **Step 5: Rodar** os testes de Postgres e a suíte inteira → PASS. O sentinela ainda falha nas tabelas das Tasks 4 e 5, só nelas.

- [ ] **Step 6: Commit.** `feat: contatos cifrados`.

---

### Task 4: Conversas

**Files:**
- Modify: `core/repositories/contacts.py` (`record_message`, `recent_history`), `core/repositories/inbox.py` (`_LIST` e a prévia, `_MESSAGE`, `messages`), `core/repositories/funnel.py` e `core/repositories/automations.py` (leituras de `sessions.message`), `core/services/handoff.py` (se ler `message` por SQL), `core/repositories/test_mode.py` (`seed_yesterday`)
- Test: `tests/test_sealed_pg.py`, `tests/test_inbox.py`

**Interfaces:**
- Consumes: Task 2.
- Produces: `inbox.preview(text: str | None, limit: int = 120) -> str | None`.

- [ ] **Step 1: Testes (falham):**
  - `test_messages_are_stored_sealed` (Postgres): `record_message(..., "quero agendar amanhã")`; a linha crua tem `message = ''` e nenhum `BYTEA` com o texto; `recent_history` devolve o texto, na ordem.
  - `test_the_inbox_preview_is_cut_in_python` (Postgres): uma mensagem de 300 caracteres; a lista da Inbox traz os 120 primeiros.
  - `test_an_unreadable_message_does_not_break_the_inbox` (Postgres): `message_sealed` adulterado; `inbox.messages` e a lista devolvem `"[ilegível]"` naquele item.
  - `test_an_unconverted_message_is_read_from_the_legacy_column` (Postgres): `INSERT` cru com `message='oi'`.
  - `test_preview_cuts_at_120` (sem banco, em `tests/test_inbox.py`).
  - `test_test_mode_seed_is_sealed` (Postgres): `seed_yesterday`.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar.** A prévia sai do SQL (`left(last.message, 120)` vira `last.message, last.message_sealed`) e é cortada por `preview` depois de `read_lenient`. Leituras de histórico pelo bot (`recent_history`) usam `read`: uma mensagem ilegível no histórico do modelo é um erro que sobe, não um texto inventado.

- [ ] **Step 4: Rodar** → PASS, e a suíte inteira.

- [ ] **Step 5: Commit.** `feat: conversas cifradas`.

---

### Task 5: Agendamentos, fila de automações e e-mail

**Files:**
- Modify: `core/repositories/appointments.py` (`_COLUMNS`, `_to_appointment`, `_insert`), `core/repositories/test_mode.py` (`orphan_appointments`, `forget_appointment` passam a usar `test_mode`), `core/services/test_mode.py` e quem cria agendamento de teste (marca `test_mode=True`), `core/repositories/funnel.py` (`appointments_between`, a consulta de `meeting_url`), `core/repositories/briefing.py` (`appointments_between`), `core/repositories/automations.py` (`enqueue`, `pending`, `reasons`), `core/repositories/mailbox.py` (`upsert_thread`, `open_threads`, `save_draft`, `approve_draft`)
- Test: `tests/test_sealed_pg.py`, `tests/test_test_mode_pg.py`, `tests/test_mailbox.py`

**Interfaces:**
- Consumes: Task 2. `Appointment` ganha `test_mode: bool = False` em `core/models.py`.

- [ ] **Step 1: Testes (falham):**
  - `test_appointments_are_stored_sealed` (Postgres): `record(...)` com título "Conversa com Maria" e um `meeting_url`; colunas antigas `''`/`NULL`; `get_by_id` devolve os dois.
  - `test_a_test_appointment_is_found_by_its_mark_not_its_title` (Postgres, em `tests/test_test_mode_pg.py`): um agendamento de teste órfão, com título cifrado, aparece em `orphan_appointments`; um agendamento de cliente com título que começa com `[TESTE] ` mas `test_mode=FALSE` não aparece, e `forget_appointment` não o apaga.
  - `test_the_booking_of_a_test_contact_is_marked` (Postgres): agendar para um contato com `test_mode` grava `test_mode=TRUE`.
  - `test_automation_reason_is_sealed` (Postgres).
  - `test_mail_subject_and_draft_are_sealed` (Postgres): `upsert_thread` duas vezes mantém o primeiro assunto (o `COALESCE` sobre `subject_sealed`); `approve_draft` devolve o corpo original pelo `RETURNING`.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar.** O `COALESCE(mail_threads.subject, EXCLUDED.subject)` vira `COALESCE(mail_threads.subject_sealed, EXCLUDED.subject_sealed)`, e `approve_draft` devolve `body_text, body_text_sealed` pelo `RETURNING`.

- [ ] **Step 4: Rodar** → PASS. **O sentinela da Task 3 fica verde aqui.** Suíte inteira.

- [ ] **Step 5: Commit.** `feat: agendamentos, automações e e-mail cifrados`.

---

### Task 6: Logs e auditoria sem conteúdo

**Files:**
- Modify: `core/services/documents.py:70-105`, `core/integrations/workspace/google.py:120-140`, `core/integrations/workspace/microsoft.py:135-160`, `core/integrations/workspace/oauth.py:104`
- Test: `tests/test_documents.py`, `tests/test_connectors.py`

- [ ] **Step 1: Testes (falham):**
  - `test_a_failed_upload_logs_and_audits_no_file_name`: um upload que falha com o nome "Contrato — Maria Silva.pdf"; nem o `caplog` nem a chamada a `audit.record` contêm "Maria"; `target` é `None`; `detail` é a classe do erro.
  - `test_a_failed_send_audits_the_file_id_not_the_name`.
  - `test_a_connector_error_keeps_the_path_out_of_the_traceback` (Google e Microsoft): uma requisição que falha (httpx simulado) para `/calendars/maria@x.com/events`; `logger.exception` do erro, formatado com o formatador de `core/logger.py` (traceback incluído), não contém `maria@x.com`; o mesmo para um 403 e um 500.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar.** As mensagens passam a ser `f"Google {method} {host} falhou ({status})"` e `f"Graph {method} falhou ({status})"`, sem caminho nem query, e com `raise ... from None`. `detail` da auditoria vira `type(exc).__name__` mais o status, quando houver.

- [ ] **Step 4: Rodar** → PASS, e a suíte.

- [ ] **Step 5: Commit.** `fix: logs e auditoria sem nomes de arquivo nem URLs`.

---

### Task 7: `cli.seal_data` no Postgres

**Files:**
- Create: `cli/seal_data.py`, `core/services/sealing.py`, `tests/test_sealing_pg.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces, em `core/services/sealing.py`:
  - `convert(conn, batch: int = 500) -> dict[str, int]`: linhas convertidas por campo;
  - `mark_test_appointments(conn) -> int`;
  - `scrub_audit(conn) -> int`;
  - `check(conn, key_version: int | None = None) -> list[Problem]`, com `@dataclass class Problem: item: int; table: str; column: str; row_id: int | None; reason: str`;
  - `install_locks(conn) -> None`;
  - `rekey(conn) -> int`;
  - `reindex(conn) -> int`.
- `python -m cli.seal_data`: converte, marca, limpa, confere e, se limpo, trava; sai com 1 e lista os problemas se não. `--check [--key-version N]` só confere. `--rekey`, `--reindex`. A saída nunca tem conteúdo.

- [ ] **Step 1: Testes em Postgres real (falham)** em `tests/test_sealing_pg.py`:
  - `test_convert_seals_old_rows_and_empties_them`: um contato, uma mensagem e um agendamento em texto (`INSERT` cru); depois do `convert`, as colunas antigas estão vazias, `_sealed` abre, e `phone_fp` bate.
  - `test_convert_keeps_an_old_empty_string`: `message = ''` antigo e `notes = ''` antigo; depois, a leitura devolve `''` nos dois, e não `None`.
  - `test_convert_twice_changes_nothing`.
  - `test_convert_loses_the_race_to_a_new_write`: lê a linha, e antes da escrita condicional outra conexão grava pelo repositório; o valor do repositório fica.
  - `test_a_test_booking_written_by_the_old_image_is_marked`: órfão e ligado a contato em teste; um agendamento de cliente nunca é marcado.
  - `test_scrub_audit_removes_file_names_from_failed_uploads`.
  - `test_check_reports_each_problem_by_item`, um por item da spec: texto que sobrou; `_sealed` faltando numa coluna obrigatória; envelope que não abre; `phone_fp` que não bate; agendamento `[TESTE]` (título decifrado) sem a marca; falha de upload com `target`; e as travas faltando depois da primeira conversão limpa. A saída não contém nenhum texto dos dados.
  - `test_locks_refuse_clear_text_after_a_clean_check`: depois do `seal_data` limpo, um `UPDATE contacts SET name='x'` levanta `CheckViolation`; rodar de novo não duplica as restrições.
  - `test_rekey_moves_everything_to_the_new_key_and_spares_a_fresh_write`.
  - `test_reindex_recomputes_fingerprints`.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar.** As atualizações condicionais seguem a spec ("A conversão"): `WHERE id = %s AND <col>_sealed IS NULL AND <col> = %s` nas obrigatórias e `WHERE id = %s AND <col> = %s` nas opcionais. O `--rekey` compara o envelope lido (`AND <col>_sealed = %s`). As travas se chamam `<tabela>_<coluna>_sealed_only`, são criadas `NOT VALID` e depois validadas, e só se não existirem em `pg_constraint`.

- [ ] **Step 4: Rodar** → PASS, e a suíte.

- [ ] **Step 5: Commit.** `feat: seal_data converte, confere, trava e recifra`.

---

### Task 8: As filas do Redis

**Files:**
- Modify: `core/integrations/update_queue.py`, `core/agent.py`, `core/web/app.py` (`/ready`), `core/services/sealing.py` (parte do Redis), `cli/seal_data.py`
- Create: `cli/queue_quarantine.py`, `tests/test_queue_sealing.py`

**Interfaces:**
- Consumes: `seal_text`, `open_text`, `SealedDataError`, `key_version_of` (Task 1); `check` e `rekey` (Task 7).
- Produces:
  - `update_queue.encode_update(update: dict) -> str` (cifra);
  - `update_queue.decode_update(payload: str) -> dict`: abre um envelope, ou aceita JSON puro que começa com `{`; levanta `QueueItemError(reason: str)` se não abrir;
  - `update_queue.quarantine(payload: str, reason: str) -> bool`: o script Lua, `LPUSH` antes de `LREM`;
  - `update_queue.pending_quarantine() -> int`;
  - `update_queue.retry_pending_quarantine() -> int`;
  - `update_queue.seal_stored_lists(rekey: bool = False) -> int` (`WATCH`/`MULTI`, a fila adiada, a de tentativas esgotadas e a quarentena);
  - `update_queue.check_lists(key_version: int | None) -> list[str]`;
  - `settings.queue_quarantine_name: str = "<prefixo>:queue:quarantine"`, no padrão dos nomes de fila que já existem;
  - `agent.worker_alive() -> bool`.

- [ ] **Step 1: Testes (falham)**, com um Redis falso em memória que suporta `eval` do script, `WATCH`/`MULTI`/`EXEC` e listas. Se o projeto não tiver um, usar `fakeredis` (`fakeredis[lua]`) como dependência de teste em `requirements-dev.txt`, ou no `pip install` do CI se não houver esse arquivo. **Ruling a registrar** se entrar.
  - `test_enqueued_payload_holds_no_clear_text`;
  - `test_an_old_json_payload_is_still_read`;
  - `test_a_tampered_item_goes_to_quarantine_sealed_and_the_worker_goes_on`: dois itens, o primeiro adulterado; o worker processa o segundo; a quarentena tem um item que abre com o contexto `queue.quarantine` e devolve o payload original; nenhum texto claro na quarentena;
  - `test_invalid_old_json_with_customer_text_is_not_kept_in_clear`;
  - `test_a_failed_quarantine_write_keeps_the_item_in_processing`: o `eval` levanta `OutOfMemory`; o item continua na fila de processamento; `pending_quarantine() == 1`; `/ready` responde 503 com "quarentena pendente"; depois que o `eval` volta a funcionar, `retry_pending_quarantine()` move o item, e `/ready` volta a 200, sem reiniciar;
  - `test_ready_is_not_ready_when_the_worker_died`;
  - `test_stored_lists_are_sealed_and_a_concurrent_change_is_retried`: a fila adiada com JSON puro; uma mudança na lista entre o `WATCH` e o `EXEC` faz a rodada repetir; no fim, tudo cifrado;
  - `test_rekey_and_check_cover_the_lists_and_the_quarantine`;
  - `test_quarantine_list_shows_reason_and_size_never_content` (`cli.queue_quarantine list`), e `drop` apaga os escolhidos.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar.**
  - `enqueue_update` usa `encode_update`.
  - Em `agent._handle_one`, a decodificação entra no `try`. Um `QueueItemError` chama `quarantine(payload, reason)` e, se der `False`, guarda o payload na lista em memória de pendentes.
  - Um laço do worker chama `retry_pending_quarantine` a cada 30 s.
  - `/ready` acrescenta `worker` (vivo ou não) e `quarantine_pending` (contagem), e responde 503 se o worker morreu ou se há pendentes.
  - `seal_data` (Task 7) passa a chamar `seal_stored_lists` e `check_lists`; o `--rekey` também.

- [ ] **Step 4: Rodar** → PASS, e a suíte.

- [ ] **Step 5: Commit.** `feat: filas do Redis cifradas, com quarentena`.

---

### Task 9: O formato dos dados e o deploy

**Files:**
- Create: `core/services/data_format.py`, `cli/queue_unseal.py`
- Modify: `core/web/app.py` (`lifespan`), `Dockerfile` (`LABEL org.converge.data-format="1"`), `deploy/deploy.sh`
- Test: `tests/test_deploy.py`, `tests/test_data_format.py`

**Interfaces:**
- Produces:
  - `data_format.IMAGE_FORMAT = 1`;
  - `data_format.raise_markers(conn) -> None`: grava `max(atual, 1)` no Postgres e no Redis (`SET data_format` por um script Lua que só sobe);
  - `cli.queue_unseal`: reescreve as filas em JSON puro, com a mesma transação otimista, e só no fim apaga a chave `data_format` do Redis.
  - Ele recusa rodar com o bot de pé. Para saber, o worker passa a gravar no Redis a chave `worker-heartbeat` (com expiração de 60 s) a cada 15 s. O `queue_unseal` recusa se a chave existe ou se a fila de processamento não está vazia.

- [ ] **Step 1: Testes (falham):**
  - `test_markers_are_raised_before_the_workers_start` (`tests/test_data_format.py`, com o `lifespan` e fakes): a ordem das chamadas é `raise_markers` antes de qualquer `create_task`.
  - `test_markers_never_go_down`.
  - `test_queue_unseal_rewrites_lists_as_json_and_then_drops_the_marker`, e `test_queue_unseal_refuses_while_a_worker_is_alive`.
  - Em `tests/test_deploy.py`, no padrão de `test_release_records_only_a_fully_configured_image` (binários falsos de `docker`):
    - `test_a_new_image_below_the_stored_format_is_refused_before_any_change`: imagem nova de formato 0, Postgres com 1; o script sai com erro antes do backup e da migração, e a imagem que está rodando não é tocada.
    - `test_a_rollback_to_a_lower_format_stops_the_bot`: imagem nova de formato 1 que não fica saudável, anterior de formato 0, Postgres com 1; o script chama `docker compose stop reception-bot` e não sobe a anterior.
    - `test_a_rollback_with_the_same_format_brings_back_the_previous_image`.
    - `test_the_redis_marker_alone_blocks_a_lower_image`: Postgres com 0 (restaurado), Redis com 1.
    - `test_a_missing_data_format_table_counts_as_zero`.
    - `test_seal_data_runs_after_the_tag_and_a_failure_does_not_roll_back`: o `seal_data` falso sai com 1 depois da tag gravada; o script sai com erro, `.deployed-tag` é a nova, e nada de `rollback`.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar `deploy.sh`.**
  - Uma função `image_format <tag>`: `docker image inspect` do label, vazio = 0.
  - Uma função `required_format`: o maior entre `SELECT version FROM data_format` (via `docker compose exec -T postgres psql`, com `to_regclass('data_format')` para tratar a tabela ausente como 0) e `docker compose exec -T redis redis-cli GET data_format` (vazio = 0).
  - Antes do backup, se `image_format $new < required_format`, sai com `::error::` e não muda nada.
  - O `rollback` confere `image_format $prev`: se for menor, `docker compose stop reception-bot` e a mensagem da spec.
  - Depois do `mv` da tag: `docker compose exec -T reception-bot python -m cli.seal_data`; se falhar, `::error::` e `exit 1`, sem `rollback`.

  **Ruling a registrar:** a spec diz "não sobe a imagem e para o bot" para qualquer subida de formato menor. Num deploy novo, o bot que está rodando é de formato igual ou maior e está são; parar ele não protege nada. O deploy novo é recusado sem mudar nada; só a volta para o bot.

- [ ] **Step 4: Rodar** → PASS, e a suíte.

- [ ] **Step 5: Commit.** `feat: formato dos dados no Postgres e no Redis, e o deploy confere`.

---

### Task 10: Guia de operação

**Files:**
- Modify: `docs/CRIPTOGRAFIA.md` (seção nova "Criptografia na aplicação"), `docs/superpowers/specs/2026-10-15-criptografia-campos-design.md` (só o anexo, se o inventário real diferir)

- [ ] **Step 1:** A seção cobre:
  - o que é cifrado;
  - as duas chaves, onde ficam e a cópia no gerenciador de senhas;
  - gerar as chaves (`deploy/data-keys.sh`);
  - `cli.data_keys check`;
  - a rotação (os 6 passos da spec);
  - a troca da chave de índice;
  - `seal_data` e `--check`;
  - a quarentena (`cli.queue_quarantine`);
  - voltar ao formato 0 (os 4 passos da spec);
  - o que ela não protege.
- [ ] **Step 2:** `tests/test_luks.py` e os outros testes de documentação, se houver, continuam passando. Commit: `docs: guia da criptografia na aplicação`.

---

### Task 11: Revisão, PR e merge

- [ ] **Step 1:** `ruff check .`, `pyright`, a suíte inteira e os testes de Postgres com o banco descartável → tudo verde.
- [ ] **Step 2:** Revisão do Codex (gpt-6.1-sol, só leitura, `< /dev/null`), com um brief em `docs/superpowers/reviews/` focado em:
  - a chave nunca em log nem em erro;
  - o sentinela de escrita;
  - a tabela de estados;
  - o deploy e o formato;
  - a quarentena.

  Rodadas até nota ≥ 4 com recomendação de merge; cada achado P1/P2 corrigido com um teste que falhou antes.
- [ ] **Step 3:** Revisor novo do branch inteiro (opus), com o Review Focus acima e as linhas `Ruling:` do ledger.
- [ ] **Step 4:** Push, PR sem coautoria, CI verde. **O Paulo faz o merge só depois do Step 1 da Task 12.**

---

### Task 12: Implantação em produção

- [ ] **Step 1: As chaves, antes do merge.** No VPS, como `deploy`: `bash /opt/converge-agent/data-keys.sh /opt/converge-agent/.env` (o script chega no VPS pelo CI de um merge anterior, ou é enviado à mão). A imagem de hoje ignora as variáveis novas (`extra="ignore"`). Conferir só pelo tamanho (44 caracteres em cada). **Pedir ao Paulo que copie as duas para o gerenciador de senhas**, lendo ele mesmo no terminal dele; nada é impresso nesta sessão.
- [ ] **Step 2: Merge (Paulo) e deploy pelo CI.** O deploy:
  1. confere o formato;
  2. faz o backup;
  3. roda a `0009`;
  4. sobe a imagem, que grava o formato 1;
  5. grava a tag;
  6. roda o `seal_data`.
- [ ] **Step 3: Conferir:**
  - `docker compose exec -T reception-bot python -m cli.seal_data --check` → limpo;
  - `cli.data_keys check` → ok;
  - uma consulta crua em `sessions` e `contacts` mostra colunas antigas vazias e `_sealed` com bytes;
  - `redis-cli LRANGE` nas filas mostra só base64;
  - as travas estão em `pg_constraint`;
  - `/ready` 200;
  - o bot responde;
  - a Inbox e o funil mostram as conversas e os nomes;
  - um `/teste` e um `/sair` funcionam.
- [ ] **Step 4: FAQ e site.** O item "Criptografia em repouso" ganha: "As conversas e os dados de contato também são cifrados pela própria aplicação, com a chave fora do banco." Atualizar `tests/test_knowledge.py` se o trecho exigir a frase, a política de privacidade (seção 9) e a memória `security-claims`. PRs sem coautoria.
- [ ] **Step 5:** Diagrama `.docs/arquitetura.html`: o card "Texto legível para quem lê o banco" vira um card de operação "Campos cifrados na aplicação".
