# Fotos, arquivos e áudios no Inbox — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** guardar, cifrado e por prazo limitado, o que o cliente envia (foto,
documento, vídeo, áudio, figurinha) no WhatsApp e no Telegram, e mostrar no
Inbox do painel, com a captura atrás de `MEDIA_ENABLED`.

**Architecture:** a captura é um passo do `process_message`, depois do limite
de mensagens, que baixa em streaming com teto, cifra com o envelope de
`core/crypto` e grava num arquivo no volume LUKS. O registro em `media` aponta
para uma linha própria em `sessions`. Um worker de limpeza, sempre ligado,
aplica os prazos. O painel serve os arquivos por um endpoint autenticado com
`Range`, tipo verificado pelos bytes e concorrência limitada.

**Tech Stack:** Python 3.12, FastAPI, psycopg3 async, httpx (streaming), Redis
(fila), AES-256-GCM (`cryptography`), pytest + Postgres descartável
(`TEST_DATABASE_URL`), JS puro no painel.

**Spec:** `docs/superpowers/specs/2026-10-22-midia-no-inbox-design.md` (local,
não versionada; aprovada pelo Paulo e pelo Codex, 5/5 na rodada 9). As seções
citadas abaixo (§N) são dela.

**Base:** `main` depois do merge do PR #25 (agendamento: migração `0010`,
`message.py`). Com o #25 ainda aberto, cortar o branch de `feat/escolher-dia`
e rebasear depois do merge.

## Global Constraints

- Migração só aditiva: `infra/migrations/0011_media.sql`. O app da equipe
  passa a `0012`.
- Com `MEDIA_ENABLED=false`, os testes de hoje passam sem mudança.
- Log nunca leva conteúdo, nome de arquivo, legenda ou bytes: só `id`,
  `storage_key`, `source_id`, `status` e o motivo técnico.
- Textos ao cliente e ao painel vêm de `content/messages.toml`, em português,
  copiados da §4 da spec.
- `ruff check .` e `pyright` limpos antes de cada commit; a suíte inteira com
  `TEST_DATABASE_URL` antes do último commit de cada tarefa.
- Commits sem linha de coautoria.
- Prazos e faixas exatos (§10): `MAX_MEDIA_BYTES` 20971520 (1 MB a 20 MB),
  `MEDIA_MAX_TOTAL_MB` 3072, `MEDIA_MIN_FREE_MB` 2048,
  `MEDIA_DOWNLOAD_SECONDS` 60 (5 a 120), `MEDIA_AUDIO_HOURS` 48 (1 a 48),
  `MEDIA_AUDIO_MAX_HOURS` 168 (de `MEDIA_AUDIO_HOURS` a 168),
  `MEDIA_RETENTION_DAYS` 90 (1 a 90), `MEDIA_EVENT_DAYS` 90 (constante),
  `MEDIA_CLEANUP_MINUTES` 15 (1 a 15), `MEDIA_SERVE_CONCURRENCY` 4 (1 a 8).
- Advisory locks: `pg_advisory_xact_lock(7411302, media.id)` (recifra ×
  limpeza); não reutilizar `7411301` (agendamento) nem `7411203` (migrate).

## Review Focus

1. Uma nota de voz do WhatsApp (Ogg/Opus) aberta no Safari do iPhone, que pode
   não tocar Ogg: o player aparece **e** um link "Baixar" fica sempre ao lado,
   para o arquivo não ficar inacessível (Task 9).
2. O arquivo apagado pela limpeza entre o endpoint ler o registro e ler o
   disco: 404, e o registro continua `expired` (a marca `missing` do endpoint é
   condicional a `status = 'stored'`) (Task 8).
3. Nome de arquivo com `../../etc/passwd`, emoji, 300 caracteres ou só espaços:
   o cabeçalho sai limpo, com no máximo 120 caracteres, e cai em `arquivo`
   quando nada sobra (Task 3).
4. Uma conversa com 200 linhas no Inbox: as mídias vêm numa consulta só, não
   uma por linha (Task 8).
5. Documento do WhatsApp sem `filename` e com `mime_type` `application/pdf`:
   vira download com o nome `arquivo`, nunca inline (Task 3 e Task 8).

---

### Task 1: Configuração, cifra de bytes, migração e campo cifrado

**Files:**
- Modify: `core/config.py` (campos e `_guardrails`), `core/crypto.py`,
  `core/sealed_fields.py` (`FIELDS`), `.env.example`
- Create: `infra/migrations/0011_media.sql`
- Test: `tests/test_config.py`, `tests/test_crypto.py`,
  `tests/test_media_pg.py` (novo)

**Interfaces:**
- Produces:
  - `settings.media_enabled: bool`, `media_dir: str`, `max_media_bytes: int`,
    `media_max_total_mb: int`, `media_min_free_mb: int`,
    `media_download_seconds: int`, `media_audio_hours: int`,
    `media_audio_max_hours: int`, `media_retention_days: int`,
    `media_cleanup_minutes: int`, `media_serve_concurrency: int`, com os
    padrões e faixas das Global Constraints.
  - `core.config.MEDIA_EVENT_DAYS = 90` (constante de módulo).
  - `crypto.seal_bytes(context: str, data: bytes) -> bytes` e
    `crypto.open_bytes(context: str, data: bytes) -> bytes`: o mesmo envelope
    de `seal`/`open_sealed` (formato, versão, nonce, tag), sem `encode`/
    `decode`; `open_bytes` levanta `SealedDataError` como `open_sealed`.
  - `Field("media", "filename")` em `sealed_fields.FIELDS`.
  - Tabelas `media` e `media_events` exatamente como §3 da spec.

- [ ] **Step 1: Testes que falham**

```python
# tests/test_config.py
@pytest.mark.parametrize("field, bad", [
    ("max_media_bytes", 20971521), ("max_media_bytes", 1048575),
    ("media_download_seconds", 4), ("media_download_seconds", 121),
    ("media_audio_hours", 49), ("media_audio_hours", 0),
    ("media_retention_days", 91), ("media_cleanup_minutes", 16),
    ("media_serve_concurrency", 9),
])
def test_media_settings_out_of_range_refuse_to_start(field, bad): ...
    # Settings(**base, **{field: bad}) levanta ValueError

def test_audio_ceiling_below_the_basic_hold_refuses_to_start(): ...
    # media_audio_hours=48, media_audio_max_hours=47 → ValueError

def test_media_capture_without_a_team_group_refuses_to_start(): ...
    # media_enabled=True, team_chat_id="" → ValueError com "TEAM_CHAT_ID"

def test_media_defaults(): ...
    # media_enabled False, max_media_bytes 20971520, media_max_total_mb 3072,
    # media_min_free_mb 2048, media_audio_hours 48, media_audio_max_hours 168,
    # media_retention_days 90, media_cleanup_minutes 15, media_serve_concurrency 4

# tests/test_crypto.py
def test_bytes_round_trip_in_the_same_envelope(): ...
    # open_bytes("media:k", seal_bytes("media:k", b"\x00\xffabc")) == b"\x00\xffabc"
    # key_version_of(sealed) == current_key_version()

def test_bytes_sealed_for_one_file_do_not_open_as_another(): ...
    # open_bytes("media:b", seal_bytes("media:a", b"x")) levanta SealedDataError
```

`tests/test_media_pg.py` (pula sem `TEST_DATABASE_URL`, mesmo padrão de
`tests/test_booking_pg.py`):

```python
async def test_media_rows_follow_the_contact(conn): ...
    # contato + linha em sessions + media(status='stored', ...);
    # DELETE FROM contacts → media vazia (CASCADE)
async def test_source_id_is_unique(conn): ...        # segundo INSERT → UniqueViolation
async def test_filename_in_clear_is_refused(conn): ... # filename='x' → CheckViolation
async def test_a_3gb_declared_size_fits(conn): ...     # declared_bytes=3*1024**3 grava
async def test_unknown_status_is_refused(conn): ...    # status='lost' → CheckViolation
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `.venv/bin/python -m pytest -q tests/test_config.py tests/test_crypto.py tests/test_media_pg.py`
Expected: FAIL (`media_enabled` inexistente, `seal_bytes` inexistente, tabela `media` inexistente).

- [ ] **Step 3: Implementar**

- Campos e faixas em `Settings`; as checagens no `_guardrails` existente, com
  mensagens nomeando a variável (`"MEDIA_AUDIO_HOURS must be 1 to 48."`).
- `seal_bytes`/`open_bytes` ao lado de `seal`/`open_sealed`, reaproveitando
  `_load`, `_FORMAT`, `_HEADER`, `_NONCE`, `_MIN_LENGTH`.
- `0011_media.sql`: as duas tabelas da §3, os índices, e o CHECK de trava já
  na criação, com o nome que `sealing._lock_name` produz
  (`media_filename_sealed_only CHECK (filename IS NULL)`), para
  `install_locks` não tentar de novo. Seguro de repetir (`IF NOT EXISTS`).
- Aplicar a migração no banco de teste: `bash $W/testdb.sh > $W/testdb.url`
  (recria do zero, com todas as migrações).
- `.env.example`: as variáveis da §10, comentadas, `MEDIA_ENABLED=false`.

- [ ] **Step 4: Rodar e ver passar**

Run: o comando do Step 2 com `TEST_DATABASE_URL=$(cat $W/testdb.url)`.
Expected: PASS. E `python -m cli.seal_data --check` no banco de teste continua
sem problema para `media.filename` (tabela vazia).

- [ ] **Step 5: Commit** — `feat(midia): configuração, cifra de bytes e tabelas de mídia`

---

### Task 2: Downloads com teto, prazo e resultado tipado

**Files:**
- Create: `core/integrations/media_download.py`
- Modify: `core/integrations/whatsapp.py`, `core/integrations/telegram.py`,
  `core/channels.py`
- Test: `tests/test_media_download.py` (novo), `tests/test_whatsapp_client.py`

**Interfaces:**
- Consumes: `settings.max_media_bytes`, `settings.media_download_seconds` (Task 1).
- Produces:
  - `media_download.Downloaded(data: bytes, mime: str | None)`,
    `TooLarge(declared: int | None)`, `Failed(reason: str)` (dataclasses
    congeladas) e `Download = Downloaded | TooLarge | Failed`.
  - `async def fetch_capped(client: httpx.AsyncClient, url: str, *, headers:
    dict | None = None, max_bytes: int) -> Download` — `client.stream("GET")`,
    soma os pedaços e para (`TooLarge(None)`) ao passar de `max_bytes`;
    status não-2xx → `Failed("status <n>")`; `httpx.HTTPError` → `Failed`.
  - `whatsapp.fetch_media(media_id: str, max_bytes: int) -> Download` —
    consulta a Meta, `file_size` acima do teto → `TooLarge(file_size)` sem
    baixar; depois `fetch_capped`.
  - `telegram.fetch_file(file_id: str, *, max_bytes: int, declared: int | None
    = None, token: str | None = None) -> Download` — `declared` acima do teto
    → `TooLarge` sem `getFile`; `file_size` do `getFile` acima → `TooLarge`.
  - `channels.fetch_attachment(file_id: str) -> Download` — escolhe o canal
    pelo prefixo `wa-media:` e aplica `asyncio.timeout(settings.
    media_download_seconds)`; estouro → `Failed("timeout")`. Não captura
    `asyncio.CancelledError`.
  - `channels.download_voice(file_id) -> bytes | None` mantém a assinatura e
    passa a usar `fetch_attachment` com `settings.max_voice_bytes`.

- [ ] **Step 1: Testes que falham** (`httpx.MockTransport`)

```python
async def test_a_stream_over_the_cap_stops_and_is_too_large(): ...
    # servidor manda 3 pedaços de 8 MB com max_bytes=20 MB → TooLarge(None);
    # o transporte registra que o 3º pedaço não foi lido inteiro
async def test_a_declared_size_over_the_cap_is_never_downloaded(): ...
    # WhatsApp: lookup com file_size=25 MB → TooLarge(25*1024**2); nenhum GET na url do arquivo
async def test_telegram_declared_size_skips_getfile(): ...
    # fetch_file(..., declared=30 MB) → TooLarge, zero requisições
async def test_a_slow_download_fails_with_timeout(monkeypatch): ...
    # media_download_seconds=5 (fixar via monkeypatch para 0.05 s no teste) → Failed("timeout")
async def test_cancellation_is_not_swallowed(): ...
    # task.cancel() durante fetch_attachment → CancelledError propaga
async def test_a_server_error_is_failed_without_content_in_the_reason(): ...
    # 500 com corpo "segredo" → Failed("status 500"); "segredo" não aparece no reason
async def test_download_voice_keeps_its_contract(): ...
    # ok → bytes; TooLarge/Failed → None
```

- [ ] **Step 2: Rodar e ver falhar** — `pytest -q tests/test_media_download.py tests/test_whatsapp_client.py`; Expected: FAIL (módulo inexistente).
- [ ] **Step 3: Implementar** as interfaces acima. `download_media` antigo sai; quem o usava passa por `fetch_attachment`.
- [ ] **Step 4: Rodar e ver passar** — o mesmo comando; PASS, e `tests/test_audio*.py` sem mudança.
- [ ] **Step 5: Commit** — `feat(midia): download em streaming com teto e prazo`

---

### Task 3: O anexo de uma mensagem, o tipo pelos bytes e o nome limpo

**Files:**
- Create: `core/services/media_types.py`
- Modify: `core/integrations/whatsapp_updates.py`
- Test: `tests/test_media_types.py` (novo), `tests/test_whatsapp_updates.py`

**Interfaces:**
- Consumes: `settings.media_enabled`, `settings.max_media_bytes` (Task 1).
- Produces (em `media_types`, tudo puro):
  - `Kind = Literal["image", "audio", "video", "document", "sticker"]`
  - `@dataclass(frozen=True) class Attachment: kind: Kind; file_id: str;
    source_id: str; mime_type: str | None; filename: str | None;
    declared_bytes: int | None`
  - `def attachment_of(message: dict) -> Attachment | None` — WhatsApp: lê
    `message["_media"]`; Telegram: `photo` (o maior com `file_size` ≤
    `max_media_bytes`, senão o maior), `document`, `video`, `video_note`,
    `voice`, `audio`, `sticker`; `source_id` como §1.
  - `def sniff(data: bytes, kind: Kind) -> tuple[str, bool]` — `(mime,
    inline)` pela tabela da §6; `inline` só quando a assinatura bate e combina
    com o `kind`.
  - `def disposition(filename: str | None, *, inline: bool) -> str` — valor do
    `Content-Disposition` (§6): sem controle, aspas, `/`, `\`; RFC 5987 para
    não-ASCII (`filename*=UTF-8''...`) com `filename="..."` ASCII de reserva;
    120 caracteres; vazio → `arquivo`.
- `whatsapp_updates._convert`: com `settings.media_enabled`, `image`,
  `document`, `video`, `audio`, `sticker` ganham `message["_media"]` (§1) e a
  legenda vira `message["caption"]`; o áudio continua também em `voice`. Com a
  flag desligada, a saída é a de hoje, byte a byte.

- [ ] **Step 1: Testes que falham**

```python
# tests/test_whatsapp_updates.py
@pytest.mark.parametrize("kind", ["image", "document", "video", "audio", "sticker"])
def test_media_is_carried_when_capture_is_on(monkeypatch, kind): ...
    # _media == {"kind": kind, "file_id": "wa-media:M1", "mime_type": ...,
    #            "filename": (doc) ..., "declared_bytes": None,
    #            "source_id": "wa:WAMID:M1"}; caption presente p/ image/document/video
def test_with_capture_off_the_update_is_exactly_today(monkeypatch): ...
    # para os 5 tipos: to_updates(payload) == o resultado gravado de hoje (sem _media, sem caption)

# tests/test_media_types.py
def test_telegram_photo_takes_the_largest_size_within_the_cap(): ...
def test_telegram_ids_include_the_file_unique_id(): ...  # "tg:42:7:AQAD"
def test_a_text_message_has_no_attachment(): ...
@pytest.mark.parametrize("head, kind, expected", [
    (b"\xff\xd8\xff\xe0", "image", ("image/jpeg", True)),
    (b"\x89PNG\r\n\x1a\n", "image", ("image/png", True)),
    (b"GIF89a", "sticker", ("image/gif", True)),
    (b"RIFF\x00\x00\x00\x00WEBP", "sticker", ("image/webp", True)),
    (b"OggS\x00", "audio", ("audio/ogg", True)),
    (b"\xff\xf1\x50", "audio", ("audio/aac", True)),
    (b"ID3\x04", "audio", ("audio/mpeg", True)),
    (b"\x00\x00\x00\x18ftypmp42", "video", ("video/mp4", True)),
    (b"\x00\x00\x00\x18ftypM4A ", "audio", ("audio/mp4", True)),
    (b"\x00\x00\x00\x14ftypqt  ", "video", ("application/octet-stream", False)),
    (b"%PDF-1.7", "document", ("application/octet-stream", False)),
    (b"<html><script>", "image", ("application/octet-stream", False)),
    (b"\x89PNG\r\n\x1a\n", "document", ("application/octet-stream", False)),
])
def test_type_comes_from_the_bytes_and_must_match_the_kind(head, kind, expected): ...
@pytest.mark.parametrize("name, ascii_part", [
    ("../../etc/passwd", "....etcpasswd"), ('a"b\r\nc.pdf', "abc.pdf"),
    ("   ", "arquivo"), (None, "arquivo"), ("x" * 300, "x" * 120),
])
def test_the_download_name_is_clean(name, ascii_part): ...
def test_a_non_ascii_name_uses_rfc5987(): ...  # "relatório 📄.pdf" → contém "filename*=UTF-8''relat%C3%B3rio"
def test_a_pdf_without_a_name_downloads_as_arquivo(): ...  # Review Focus 5
```

- [ ] **Step 2: Rodar e ver falhar** — `pytest -q tests/test_media_types.py tests/test_whatsapp_updates.py`; FAIL.
- [ ] **Step 3: Implementar.** Para o teste "exatamente hoje", gravar o retorno atual de `to_updates` para os 5 tipos **antes** de mexer em `_convert` e compará-lo literalmente.
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `feat(midia): anexo, tipo pelos bytes e nome de download`

---

### Task 4: O disco cifrado e os limites de espaço

**Files:**
- Create: `core/services/media_store.py`
- Test: `tests/test_media_store.py` (novo; `tmp_path` como `MEDIA_DIR`)

**Interfaces:**
- Consumes: `crypto.seal_bytes/open_bytes` (Task 1), `settings.media_dir`.
- Produces:
  - `def path_for(key: UUID) -> Path` — `MEDIA_DIR/<hex[:2]>/<hex>.bin`.
  - `def write(key: UUID, data: bytes) -> int` — `seal_bytes(f"media:{key}",
    data)`, grava em `.tmp-<uuid>` na mesma pasta, `fsync`, `os.replace`;
    devolve os bytes gravados; numa exceção, apaga o temporário e relança.
  - `def read(key: UUID) -> bytes` — abre o envelope; `FileNotFoundError` e
    `SealedDataError` sobem.
  - `def delete(key: UUID) -> bool` — `False` se já não existia.
  - `def free_bytes() -> int` — `os.statvfs(MEDIA_DIR)`.
  - `def entries() -> Iterator[Entry]` com `Entry(path: Path, key: UUID | None,
    temporary: bool, age_seconds: float)` — todos os `.bin` e `.tmp-*`.
  - `def has_room(size: int, stored_total: int) -> bool` —
    `stored_total + size ≤ media_max_total_mb·MiB` **e** `free_bytes() - size
    ≥ media_min_free_mb·MiB`.

- [ ] **Step 1: Testes que falham**

```python
def test_written_bytes_read_back_and_are_sealed_on_disk(tmp_path): ...
    # read(k) == data; path_for(k).read_bytes() não contém data; nenhum .tmp-* sobra
def test_a_file_copied_under_another_key_does_not_open(tmp_path): ...  # SealedDataError
def test_a_failed_rename_leaves_no_temporary(tmp_path, monkeypatch): ...
    # os.replace levanta OSError → write relança e a pasta não tem .tmp-*
def test_the_path_says_nothing_but_the_key(tmp_path): ...  # str(path) só tem hex, "/" e ".bin"
def test_room_respects_the_total_and_the_free_floor(monkeypatch): ...
    # media_max_total_mb=1: has_room(600 KiB, 500 KiB) False; free_bytes 2047 MiB → False
def test_entries_lists_files_and_temporaries_with_age(tmp_path): ...
```

- [ ] **Step 2: Rodar e ver falhar.** — `pytest -q tests/test_media_store.py`; FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** — `feat(midia): arquivos cifrados no disco`

---

### Task 5: Registro, eventos e a captura

**Files:**
- Create: `core/repositories/media.py`, `core/services/media.py`
- Modify: `core/repositories/contacts.py` (`record_message` devolve o `id`),
  `content/messages.toml` e `core/content.py` (`media.line_*`)
- Test: `tests/test_media_capture.py` (novo, com fakes), `tests/test_media_pg.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `contacts_repo.record_message(...) -> int` (o `id` de `sessions`; os
    fakes de teste existentes passam a devolver um inteiro crescente).
  - Em `core/repositories/media.py`:
    - `async def event_seen(conn, update_id: str) -> bool`
    - `async def mark_event(conn, update_id: str) -> None`
    - `async def exists(conn, source_id: str) -> bool`
    - `@dataclass class NewMedia: contact_id; chat_id; source_id; kind;
      mime; declared_bytes; stored_bytes; filename; storage_key; status;
      stored_at; hold_until; expires_at`
    - `async def record(conn, line: str, media: NewMedia) -> int | None` —
      num savepoint: `record_message(..., "user", line, "media")` e
      `INSERT ... ON CONFLICT (source_id) DO NOTHING RETURNING id`; sem `id`,
      desfaz o savepoint de propósito (a linha some junto) e devolve `None`;
      `filename` com `sealed_fields.write`.
    - `async def stored_total(conn) -> int`
    - `AVAILABLE_SQL: str` — o predicado da §7 sobre o alias `m`
      (`m.status = 'stored' AND NOW() < m.expires_at AND (m.hold_until IS
      NULL OR NOW() < m.hold_until OR EXISTS (atendimento open/claimed com
      h.chat_id = m.chat_id))`).
  - Em `core/services/media.py`:
    - `@dataclass(frozen=True) class Captured: kind: Kind; status: str;
      audio: bytes | None` e `class SameAttachment` (singleton
      `SAME_ATTACHMENT`).
    - `def deadlines(kind: Kind, *, stored_at: datetime | None, created_at:
      datetime) -> tuple[datetime | None, datetime]` — `(hold_until,
      expires_at)` pela §7.
    - `async def guard_event(conn, update: dict, message: dict) -> Literal[
      "replayed", "too_old"] | None` — o passo 1a, com esta regra (decisão
      do plano, ver nota): só olha updates com `attachment_of(message)`;
      marca já existente → `"replayed"`; sem `MEDIA_ENABLED` e sem `_media` no
      payload → `None` (hoje); mensagem (ou `edit_date`) com mais de
      `MEDIA_EVENT_DAYS` → `"too_old"`; com `MEDIA_ENABLED`, grava a marca e
      devolve `None`.
    - `def normalized(message: dict) -> dict` — passo 1b: sem
      `MEDIA_ENABLED`, devolve a mensagem sem `_media` e, quando tinha
      `_media` (WhatsApp), sem `caption`; com a flag, devolve a mesma.
    - `async def capture(conn, contact: Contact, chat_id, message: dict, *,
      edited: bool) -> Captured | SameAttachment` — §2 "`media.capture`". O
      `mime` gravado é o de `sniff(data, kind)[0]` (o tipo verificado; um tipo
      que não abre inline fica `application/octet-stream`), nunca o declarado.
    - `async def record_without_download(conn, contact: Contact, chat_id,
      attachment: Attachment, status: Literal["late", "rate_limited"]) ->
      None` — a regra de anexo da §2.

  **Nota (decisão do plano sobre o passo 1a):** o Telegram não tem `_media`
  no payload; a marca é consultada para qualquer update com anexo (só
  leitura), mas só é **gravada** com a captura ligada. Assim um evento
  concluído com a captura ligada continua protegido depois de desligá-la, e
  com a captura sempre desligada nada muda.

- [ ] **Step 1: Testes que falham**

`tests/test_media_capture.py` (fakes de `media_repo`, `media_store` e
`channels.fetch_attachment`; `settings.media_enabled=True`):

```python
async def test_a_photo_is_downloaded_sealed_and_recorded_once(): ...
    # Captured(kind="image", status="stored", audio=None); 1 linha "(enviou uma foto)" intent "media";
    # NewMedia.stored_at preenchido, hold_until None, expires_at = stored_at + 90 dias
async def test_an_audio_keeps_its_bytes_for_the_transcript(): ...
    # Captured.audio == bytes baixados; hold_until = +48 h; expires_at = +168 h
async def test_a_known_attachment_is_the_same_attachment(): ...
    # exists() True → SAME_ATTACHMENT; fetch_attachment não chamado; nada gravado
async def test_too_large_failed_and_no_space_are_recorded_without_a_file(): ...
    # TooLarge → status "too_large"; Failed → "failed"; has_room False → "no_space";
    # storage_key None; expires_at = created_at + 90 dias
async def test_an_oserror_on_write_is_failed_and_the_audio_still_has_bytes(): ...
    # media_store.write levanta OSError → Captured(status="failed", audio=bytes)
async def test_cancellation_during_capture_is_not_swallowed(): ...
async def test_a_conflict_on_insert_leaves_the_file_as_an_orphan(): ...
    # record() devolve None → Captured(status="failed"); o arquivo escrito continua no disco (órfão)
async def test_guard_replayed_too_old_and_marked(monkeypatch): ...
    # event_seen True → "replayed"; date 91 dias atrás → "too_old" (sem marca);
    # recente com flag → None e mark_event chamado; flag off sem _media → None e sem consulta de idade
async def test_normalized_strips_media_and_the_whatsapp_caption_when_off(monkeypatch): ...
```

`tests/test_media_pg.py` (Postgres real):

```python
async def test_record_rolls_the_line_back_on_conflict(conn): ...
    # record() duas vezes com o mesmo source_id → 2ª None; sessions tem 1 linha de mídia
async def test_the_audio_rule_in_sql(conn): ...
    # áudio com hold_until no passado e sem atendimento → AVAILABLE_SQL falso;
    # com handoff_sessions open no chat_id → verdadeiro; expires_at no passado → falso mesmo assim
async def test_events_mark_and_seen(conn): ...
async def test_a_mark_rolls_back_with_the_turn(conn): ...
    # mark_event dentro de uma transação desfeita → event_seen False (o turno que cai é reprocessado)
```

- [ ] **Step 2: Rodar e ver falhar.** `pytest -q tests/test_media_capture.py tests/test_media_pg.py`; FAIL.
- [ ] **Step 3: Implementar.** `record_message` passa a ter `RETURNING id`; ajustar os fakes de `record_message` em `tests/test_desk_flow.py`, `tests/test_inbox.py` e onde mais o `grep -rn "def record_message" tests/` achar, para devolver um inteiro. Textos `media.line_image`… `media.line_sticker` copiados da §2.
- [ ] **Step 4: Rodar e ver passar**, mais a suíte inteira (o `record_message` mudou).
- [ ] **Step 5: Commit** — `feat(midia): registro, eventos e captura`

---

### Task 6: O pedido à equipe, os avisos e os textos

**Files:**
- Modify: `core/services/handoff.py`, `core/services/inbox.py`,
  `content/messages.toml`, `core/content.py`
- Test: `tests/test_handoff.py` (ou o arquivo de testes de handoff existente),
  `tests/test_inbox.py`, `tests/test_content.py`

**Interfaces:**
- Produces:
  - `async def handoff.open_for_media(conn, contact: Contact, chat_id, kind:
    Kind) -> Literal["opened", "existing"]` — §4.1. Existente (`open` ou
    `claimed`): `handoffs_repo.touch`, `"existing"`, nenhum aviso. Senão:
    `handoffs_repo.open_session(..., reason=content.render(
    "inbox.reason_media", tipo=content.text(f"media.kind_{kind}")))`, depois
    o aviso `inbox.needs_person` (com `test_marks.team_text`); falha no aviso
    vai para o log e o resultado continua `"opened"`.
  - `async def handoff.held_media(conn, contact: Contact, chat_id, *, notify:
    bool) -> bool` — `True` se a conversa está `claimed`: atualiza a
    atividade e, com `notify`, chama `inbox.ping_owner`; **não grava linha**
    (a captura já gravou).
  - `inbox.notify_new_conversation` dividido em
    `inbox_repo.claim_conversation_notice` (já existe) +
    `async def inbox.send_new_conversation_notice(contact: Contact) -> bool`;
    `notify_new_conversation` continua existindo, chamando os dois.
  - Textos (§4, verbatim): `media.received`, `media.not_received`,
    `media.kind_image` "uma foto", `media.kind_document` "um documento",
    `media.kind_video` "um vídeo", `media.kind_audio` "um áudio",
    `inbox.reason_media` "enviou {tipo}" (placeholder `{tipo}` em
    `_PLACEHOLDERS`), `media.expired`, `media.too_large`, `media.failed`,
    `media.no_space`, `media.rate_limited`, `media.late`, `media.missing`
    (frases curtas para o painel, ex.: "arquivo expirado", "grande demais
    para guardar", "não foi possível baixar", "sem espaço para guardar",
    "não guardado: muitas mensagens seguidas", "não guardado: mensagem
    atrasada", "arquivo indisponível").

- [ ] **Step 1: Testes que falham**

```python
async def test_open_for_media_opens_once_and_notifies_once(): ...
    # 1ª chamada "opened" + 1 post ao grupo; 2ª "existing", touch chamado, nenhum post
async def test_a_failed_notice_keeps_the_request(): ...
    # post_message devolve None → "opened"; a sessão existe
async def test_a_claimed_conversation_is_existing_without_notice(): ...
async def test_held_media_pings_the_owner_without_recording(): ...
    # claimed → True, ping_owner chamado, record_message não chamado
async def test_test_contact_notices_carry_the_teste_mark(): ...
def test_media_texts_and_placeholders_validate(): ...  # content.validate() passa
```

- [ ] **Step 2–4:** ver falhar, implementar, ver passar.
- [ ] **Step 5: Commit** — `feat(midia): pedido à equipe, avisos e textos`

---

### Task 7: A captura no turno

**Files:**
- Modify: `core/handlers/message.py` (`process_message`, `_text_of`),
  `core/repositories/test_mode.py` (`delete` apaga os arquivos)
- Test: `tests/test_media_flow.py` (novo, harness `Desk` de
  `tests/test_desk_flow.py`, como `tests/test_choose_day.py`), `tests/test_test_mode.py`

**Interfaces:**
- Consumes: Tasks 2, 3, 5, 6.
- Produces: o comportamento das §2 e §4. Pontos que o código precisa
  respeitar:
  - Ordem: `lock_chat` → `media.guard_event` (fim do turno em
    `"replayed"`/`"too_old"`, devolvendo essa string como intenção) →
    `message = media.normalized(message)` → atrasado (com anexo e flag:
    `record_without_download(..., "late")` antes de `_record_expired`) →
    `_note_inbound` → `/teste` → `_allowed` (recusada, segurada, com anexo e
    flag: `record_without_download(..., "rate_limited")`; nunca baixa) →
    com anexo e flag: `get_or_create`, `claim_conversation_notice`,
    `media.capture(..., edited=...)` → o resto como hoje.
  - `edited = "edited_message" in update and "message" not in update`.
  - `async def _text_of(message: dict, *, skip_media: bool = False, audio:
    bytes | None | Literal["none"] = "none") -> str | None` — com
    `skip_media`, só `caption` (o `SameAttachment`); com `audio` vindo da
    captura (bytes ou `None`), transcreve esses bytes e **não baixa de
    novo**; o padrão `"none"` é o caminho de hoje. A regra de hoje de
    duração (`max_voice_seconds`) continua: acima dela não há transcrição,
    mesmo com os bytes guardados.
  - `SameAttachment` sem legenda nova → turno termina em silêncio.
  - Sem texto (sem legenda e sem transcrição), conforme §4: figurinha →
    calado; `held_media(notify=True)` verdadeiro → calado; guardado →
    `open_for_media`: `"opened"` → `media.received` (gravado como
    `assistant`), `"existing"` → calado; não guardado → `media.not_received`.
    A marca de conversa nova: com `"opened"`, fica consumida sem aviso; nos
    outros casos, se foi tomada, `send_new_conversation_notice`.
  - Com texto: o fluxo de hoje (o `notify_new_conversation` de hoje encontra
    a marca já tomada e não repete).
  - `test_mode.delete`: antes do `DELETE`, lê os `storage_key` do contato
    (`SELECT storage_key FROM media WHERE contact_id = %(id)s AND storage_key
    IS NOT NULL`); se o `DELETE` apagou, `media_store.delete` em cada um, na
    hora. **Decisão do plano:** apaga dentro da transação do turno, não
    depois do commit (não há gancho de pós-commit); um rollback deixa
    registros `missing` de um contato de teste, que nunca reexpõem nada.

- [ ] **Step 1: Testes que falham** (`tests/test_media_flow.py`; fakes de
  `fetch_attachment`, `media_store`, `media_repo`; flag ligada salvo dito)

```python
async def test_a_photo_without_caption_opens_a_request_and_confirms(): ...
    # 1 sessão aberta; desk.sent[-1] == content.text("media.received"); aviso needs_person, sem new_conversation
async def test_an_album_of_six_gives_one_request_one_notice_one_confirmation(): ...
async def test_a_caption_goes_through_the_bot(): ...  # triagem roda com a legenda; nenhuma sessão aberta
async def test_a_caption_that_is_a_command_still_captures(): ...  # "/id" responde o id e há registro de mídia
async def test_a_transcribed_audio_with_no_space_follows_the_transcript(): ...
    # capture → status no_space, audio=bytes; transcribe chamado com esses bytes; sem media.not_received
async def test_an_audio_is_downloaded_once(): ...  # fetch_attachment chamado 1 vez; download_voice nunca
async def test_an_untranscribed_stored_audio_opens_a_request(): ...
async def test_not_stored_without_text_asks_to_resend(): ...  # media.not_received
async def test_a_sticker_is_silent(): ...  # nada enviado ao cliente; aviso new_conversation se primeira mensagem
async def test_a_held_conversation_is_silent_and_the_owner_is_pinged(): ...
async def test_rate_limited_never_downloads(): ...  # _allowed False + held → registro rate_limited; fetch não chamado
async def test_a_late_whatsapp_media_is_recorded_without_download(): ...  # status late; nada enviado
async def test_replay_ends_the_turn_before_the_limit(): ...
    # event_seen True → "replayed"; _allowed não chamado; nada gravado nem enviado
async def test_a_media_update_older_than_90_days_ends_the_turn(): ...
async def test_a_telegram_caption_edit_of_an_audio_is_text_only(): ...
    # edited_message com mesmo file_unique_id → nenhum download, nenhuma transcrição; a legenda vai à triagem
async def test_a_caption_removed_by_an_edit_is_silent(): ...
async def test_an_edit_with_a_new_file_captures_again(): ...
async def test_capture_off_is_today(monkeypatch): ...
    # flag off: foto WA convertida com flag on (com _media e caption) → _CANNOT_READ, legenda não vai à triagem;
    # Telegram foto → _CANNOT_READ como hoje; event_seen True → "replayed"
async def test_a_telegram_contact_is_created_before_capture(): ...
async def test_a_caption_edit_while_rate_limited_and_held_keeps_the_file(): ...
    # anexo já stored; edited_message com o limite esgotado e a conversa assumida →
    # nenhum registro nem linha de mídia nova; o registro continua stored; a legenda vai a quem segura
async def test_a_long_audio_is_stored_but_not_transcribed(): ...
    # duration > max_voice_seconds → stored, sem transcrição, abre pedido
```

`tests/test_test_mode.py`: `test_ending_a_test_deletes_its_files_at_once` — a
fake de `media_store.delete` recebe as chaves do contato de teste.

- [ ] **Step 2: Rodar e ver falhar** — `pytest -q tests/test_media_flow.py tests/test_test_mode.py`.
- [ ] **Step 3: Implementar.** Adicionar ao fixture `desk` (ou a um fixture
  novo em `tests/test_media_flow.py`) os fakes de `media_repo.event_seen`/
  `mark_event` e de `inbox_repo.claim_conversation_notice`, para que os testes
  de hoje continuem sem banco.
- [ ] **Step 4: Rodar e ver passar** — os dois arquivos e a suíte inteira
  (com a flag desligada, nenhum teste existente muda).
- [ ] **Step 5: Commit** — `feat(midia): captura no turno do bot`

---

### Task 8: A API do Inbox e o endpoint dos arquivos

**Files:**
- Create: `core/web/panel_media.py`
- Modify: `core/web/panel.py` (`_conversation`, rota nova),
  `core/repositories/media.py` (consultas do painel)
- Test: `tests/test_panel_media.py` (novo)

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces:
  - `async def media_repo.for_sessions(conn, session_ids: list[int]) ->
    dict[int, dict]` — **uma** consulta; cada item `{id, kind, mime,
    stored_bytes, declared_bytes, filename, status, available, inline,
    status_text}`, com `filename` por `read_lenient`, `available` por
    `AVAILABLE_SQL`, `inline` pelo `mime` guardado (o tipo verificado na
    captura) e `status_text` = `content.text(f"media.{status}")` quando não
    está disponível (`media.expired` para um `stored` já vencido).
  - `_conversation` anexa `message["media"]` às linhas que têm mídia.
  - `async def media_repo.servable(conn, contact_id: int, media_id: int) ->
    Servable | None` — o registro só se for daquele contato e passar
    `AVAILABLE_SQL`; `Servable(id, kind, storage_key, filename)`.
  - `async def media_repo.mark_missing(conn, media_id: int) -> None` —
    `UPDATE ... SET status = 'missing' WHERE id = %s AND status = 'stored'`.
  - `def panel_media.parse_range(header: str | None, total: int) -> tuple[int,
    int] | Literal["unsatisfiable"] | None` — §6 "Range" (`None` = 200 inteiro).
  - `async def panel_media.serve(conn, contact_id: int, media_id: int, *,
    method: str, range_header: str | None) -> Response` — 404 nos casos da §6
    (arquivo ausente → `mark_missing`; envelope ilegível → log com o `id`);
    `sniff` + `disposition`; cabeçalhos de segurança da §6 em toda resposta,
    inclusive 404/416/503; semáforo `MEDIA_SERVE_CONCURRENCY` tomado antes de
    ler, espera de 5 s → 503 com `Retry-After: 5`, liberado no `finally` do
    gerador do `StreamingResponse` (ou na hora, no `HEAD` e nos erros).
  - Rota em `panel.py`: `@router.api_route("/api/inbox/{contact_id}/media/{media_id}",
    methods=["GET", "HEAD"])`, abrindo a conexão e chamando `serve`.

- [ ] **Step 1: Testes que falham**

```python
def test_without_a_session_it_is_401(client): ...  # fixtures de tests/test_panel.py, deslogado
async def test_a_record_of_another_contact_is_404(): ...
async def test_expired_but_still_on_disk_is_404(): ...
async def test_a_file_deleted_meanwhile_is_404_and_stays_expired(): ...  # Review Focus 2
async def test_a_corrupt_envelope_is_404_without_detail(): ...
async def test_a_png_is_inline_and_html_as_image_downloads(): ...
@pytest.mark.parametrize("header, status, content_range", [
    ("bytes=0-99", 206, "bytes 0-99/1000"), ("bytes=-100", 206, "bytes 900-999/1000"),
    ("bytes=900-", 206, "bytes 900-999/1000"), ("bytes=999999999-", 416, "bytes */1000"),
    ("bytes=0-1,5-6", 200, None), ("lixo", 200, None),
])
async def test_ranges(header, status, content_range): ...
async def test_head_has_headers_and_no_body(): ...
async def test_a_fifth_request_with_four_slow_clients_is_503(): ...
async def test_a_dropped_connection_gives_the_slot_back(): ...
async def test_security_headers_on_200_206_404_416_503(): ...
async def test_the_conversation_carries_media_in_one_query(): ...  # Review Focus 4: 200 linhas → 1 chamada a for_sessions
```

- [ ] **Step 2–4:** ver falhar, implementar, ver passar
  (`pytest -q tests/test_panel_media.py tests/test_panel.py tests/test_inbox.py`).
- [ ] **Step 5: Commit** — `feat(midia): arquivos no Inbox pelo painel`

---

### Task 9: A tela do Inbox

**Files:**
- Modify: `core/web/static/painel/inbox.js`, `core/web/static/painel/painel.css`
- Test: verificação no navegador (o painel não tem testes de JS)

**Interfaces:**
- Consumes: `message.media` e o endpoint (Task 8).
- Produces, em `renderMessages`, para uma linha com `m.media` (§6 "Tela"):
  - `available` e `inline` de imagem/figurinha → `<img>` com
    `src="/painel/api/inbox/<contato>/media/<id>"`, clique abre em aba nova;
  - áudio/vídeo disponíveis → `<audio|video controls preload="none">` **e**
    um link "Baixar" ao lado (Review Focus 1);
  - documento ou tipo não inline → cartão com nome, tipo, tamanho e "Baixar";
  - indisponível → `media.status_text` (do servidor) com o nome do tipo e o
    tamanho ("arquivo expirado — foto, 1,2 MB"); o nome do tipo vem de um mapa
    fixo no JS (`{image: "foto", audio: "áudio", video: "vídeo", document:
    "documento", sticker: "figurinha"}`), o tamanho de `stored_bytes` ou
    `declared_bytes`;
  - `error` no elemento ou clique em "Baixar" → `api(url, {method: "HEAD"})`;
    404 troca por "arquivo indisponível — reabra a conversa", 503 por "tente
    de novo em instantes"; 401 já leva ao login pelo `api()`;
  - URLs montadas só com ids numéricos; texto com `textContent`.

- [ ] **Step 1:** Montar dados de teste: no banco de teste, um contato com
  foto, áudio, PDF e um `expired`; subir o app local com `MEDIA_DIR` num
  diretório temporário.
- [ ] **Step 2:** Capturar a conversa no Chromium headless a 1440 px e 390
  px; conferir as quatro formas e o link "Baixar" do áudio.
- [ ] **Step 3:** Apagar o arquivo do áudio com a conversa aberta e conferir a
  troca para "arquivo indisponível — reabra a conversa".
- [ ] **Step 4: Commit** — `feat(midia): fotos, players e downloads no Inbox`

---

### Task 10: Limpeza, `cli.media`, recifra e métricas

**Files:**
- Create: `core/services/media_cleanup.py`, `cli/media.py`
- Modify: `core/web/app.py` (worker), `core/metrics.py`,
  `core/integrations/update_queue.py` (inventário e normalização),
  `cli/seal_data.py` (`--rekey` recifra arquivos)
- Test: `tests/test_media_cleanup.py` (novo; Postgres real + `tmp_path`),
  `tests/test_media_queues.py` (novo; Redis real, pula sem `TEST_REDIS_URL`)

**Interfaces:**
- Consumes: Tasks 1, 4, 5.
- Produces:
  - `async def media_cleanup.sweep(conn) -> dict[str, int]` — os itens 1 a 5
    da §7 "Worker", em lotes de 200; cada arquivo com registro tratado sob
    `pg_advisory_xact_lock(7411302, id)` relendo o `status`, numa transação
    própria; erro num item não para os outros.
  - `async def run_media_cleanup_worker(stop_event: asyncio.Event) -> None` —
    padrão de `run_reminder_worker`, a cada `media_cleanup_minutes`; registra
    em `MEDIA_CLEANUP_RUNS.labels(result)` e `MEDIA_CLEANUP_LAST_SUCCESS`; 24 h
    sem passada boa → `logger.critical` a cada passada. Ligado em
    `app.py` **sempre**, ao lado dos outros workers.
  - `MEDIA_CLEANUP_RUNS = Counter("media_cleanup_runs_total", ..., ["result"])`,
    `MEDIA_CLEANUP_LAST_SUCCESS = Gauge("media_cleanup_last_success", ...)`.
  - `async def media_cleanup.rekey_files(conn) -> int` — sob o mesmo lock,
    reescreve os `stored` que não estão na chave atual (lê, abre, `write`
    com a mesma `storage_key`); chamado pelo `--rekey` de `cli.seal_data`, que
    soma ao total impresso.
  - `async def media_cleanup.purge(conn) -> dict[str, int]` — apaga todos os
    arquivos e temporários, marca todos os registros `expired`, zera
    `filename_sealed`.
  - `async def media_cleanup.inventory(conn) -> dict[str, object]` — contagem
    e bytes por status e por versão de chave (lendo o cabeçalho de cada
    arquivo), órfãos, temporários, ilegíveis, e `queued_media_updates`.
  - `async def update_queue.media_payload_counts() -> dict[str, int]` —
    payloads com `_media` em ativa, adiada, processamento, dead-letter e
    quarentena (abrindo o envelope interno da quarentena); envelope que não
    abre conta.
  - `async def update_queue.normalize_media_payloads(seen: Callable[[str],
    Awaitable[bool]]) -> dict[str, int]` — só dead-letter e quarentena, via
    `_rewrite`: mais de `MEDIA_EVENT_DAYS` → sai; `seen(update_id)` → sai;
    senão tira `_media` e `caption`; ilegível fica.
  - `cli/media.py`: `status`, `cleanup`, `purge`, `normalize-queues`
    (argparse, mesmo estilo de `cli/seal_data.py`; `purge` termina
    imprimindo o `status`).

- [ ] **Step 1: Testes que falham**

```python
async def test_audio_of_49h_without_a_request_goes_and_loses_its_name(): ...
async def test_audio_with_an_open_request_stays(): ...
async def test_audio_of_169h_goes_even_with_a_request(): ...
async def test_a_photo_of_91_days_goes_and_loses_its_name(): ...
async def test_a_too_large_of_91_days_loses_its_name(): ...
async def test_a_vanished_file_is_missing_without_name(): ...
async def test_orphans_and_temporaries_of_2h_go_and_of_10min_stay(): ...
async def test_a_file_of_an_expired_record_is_deleted(): ...
async def test_events_older_than_90_days_go(): ...
async def test_rekey_and_cleanup_do_not_interleave(): ...
    # rekey lê; sweep apaga e marca expired; rekey retoma → sob o lock, rekey relê, vê expired e não grava
async def test_rekey_moves_files_to_the_current_key_and_they_open(): ...
async def test_purge_empties_the_folder_and_the_names(): ...
async def test_the_worker_runs_with_capture_off(): ...
# tests/test_media_queues.py
async def test_counts_cover_every_list_and_unreadable_counts(): ...
    # item reservado (processing) conta; ilegível conta
async def test_normalize_drops_done_and_old_and_strips_the_rest(): ...
```

- [ ] **Step 2–4:** ver falhar, implementar, ver passar.
- [ ] **Step 5: Commit** — `feat(midia): limpeza, inventário e recifra dos arquivos`

---

### Task 11: Formato de dados, implantação e documentação

**Files:**
- Modify: `core/services/data_format.py`, `Dockerfile` (label `2`),
  `deploy/deploy.sh`, `deploy/docker-compose.encrypted.yml`, `docs/BACKUP.md`,
  `docs/CRIPTOGRAFIA.md`
- Test: `tests/test_data_format.py`, `tests/test_deploy_script.py` (ou o
  teste de deploy existente)

**Interfaces:**
- Produces:
  - `data_format.IMAGE_FORMAT = 2` (o que a imagem lê) e
    `def startup_format() -> int` — `2` com `settings.media_enabled`, senão `1`;
    `raise_markers` sobe os dois marcadores para `startup_format()` (nunca
    baixa).
  - `Dockerfile`: `LABEL org.converge.data-format="2"`.
  - Overlay criptografado: bind de `/srv/converge-agent/media` em
    `/data/media` no `reception-bot`, `create_host_path: false`.
  - `deploy.sh`: com o overlay ativo (`COMPOSE_FILE` contém
    `docker-compose.encrypted.yml`), `test -d /srv/converge-agent/media` antes
    de trocar a versão; faltando, erro claro e para.
  - `docs/BACKUP.md`: no restore, `python -m cli.media purge` com o app
    parado, antes de subir o banco restaurado; a mídia não tem backup.
  - `docs/CRIPTOGRAFIA.md`: o `--rekey` inclui os arquivos; retirar chave só
    com `cli.media status` zerado naquela versão; o procedimento de voltar à
    imagem de formato 1 (§7 "Rollback", os 6 passos).

- [ ] **Step 1: Testes que falham**

```python
async def test_markers_go_to_2_with_capture_on_before_the_workers(): ...
async def test_markers_stay_at_least_1_and_keep_a_2_with_capture_off(): ...
def test_the_image_reads_format_2(): ...   # IMAGE_FORMAT == 2 e o label do Dockerfile == "2"
def test_deploy_stops_without_the_media_folder_under_the_encrypted_overlay(): ...
```

- [ ] **Step 2–4:** ver falhar, implementar, ver passar; `docker compose -f
  docker-compose.yml -f deploy/docker-compose.encrypted.yml config` válido.
- [ ] **Step 5: Commit** — `feat(midia): formato de dados 2, pasta no volume cifrado e runbooks`

---

### Task 12: Revisão final e PR

- [ ] Revisor final (modelo mais capaz) sobre o branch inteiro, com a spec, o
  plano, a Review Focus e as linhas `Ruling:` do ledger.
- [ ] Rodadas do Codex (`gpt-6.1-sol`, só leitura, arquivos de teste
  indicados) até 4/5 ou mais com merge recomendado.
- [ ] Uma passada de correção (cada correção RED→GREEN, suíte inteira verde).
- [ ] Push e PR, sem coautoria; o Paulo faz o merge.

### Depois do merge (implantação, §13 — passos manuais com o Paulo)

1. No VPS, antes do deploy: criar `/srv/converge-agent/media` no volume LUKS,
   com o dono do usuário do app; conferir espaço livre.
2. Deploy com `MEDIA_ENABLED=false`.
3. Publicar a política de privacidade nova (site, §11), em PR no site.
4. `MEDIA_ENABLED=true`; subir o FAQ novo (`content/faq.md`, §11).
5. `/teste` no WhatsApp e no Telegram: foto com e sem legenda, PDF, áudio
   curto, figurinha, álbum, edição de legenda; abrir no Inbox, inclusive no
   Safari do iPhone; `/sair` e conferir que os arquivos sumiram.
6. Atualizar a memória de afirmações de segurança.
