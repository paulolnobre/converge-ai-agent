# Revisão — fotos, arquivos e áudios no Inbox

Branch `feat/midia-inbox`, diff `git diff main..HEAD`. Spec (local):
`docs/superpowers/specs/2026-10-22-midia-no-inbox-design.md`. Plano:
`docs/superpowers/plans/2026-10-22-midia-no-inbox.md`. Decisões tomadas na
execução, contra o plano: as linhas `Ruling:` de
`.superpowers/sdd/2026-10-22-midia-no-inbox/progress.md`.

Com `MEDIA_ENABLED=true`, o bot guarda o que o cliente manda (foto, áudio,
vídeo, documento, figurinha) cifrado no disco, mostra no Inbox e abre um pedido
à equipe; a limpeza apaga no prazo (áudio 48 h, até 168 h com atendimento
aberto; o resto 90 dias), com o nome do arquivo junto. Desligado por padrão:
**com a captura desligada, nada muda para o cliente** — a limpeza e a exibição
funcionam sempre.

## Onde olhar com mais cuidado

1. **A ordem no turno** (`core/handlers/message.py`, `core/services/media.py`):
   a spec §2 e a tabela da §4; replay de um update já tratado
   (`media_events`), edição de legenda, álbum, update velho, limite de taxa,
   conversa em atendimento; o savepoint de `media_repo.record`.
2. **Disco e cifra** (`core/services/media_store.py`, `core/crypto.py`): escrita
   atômica, AAD presa ao nome, teto total e piso de espaço livre.
3. **O endpoint** (`core/web/panel_media.py`): acesso só ao contato certo e só
   enquanto disponível, Range, tipo pelos bytes (nada servido inline que possa
   rodar script), as 4 vagas e o 503, a vaga devolvida em toda saída.
4. **A limpeza e a recifra** (`core/services/media_cleanup.py`): a trava
   `pg_advisory_xact_lock(7411302, id)` em conexões do pool (sem autocommit) —
   alguma trava pode durar mais que o item?; os itens 1–5 da §7; `purge`,
   `inventory`; `update_queue.media_payload_counts` e
   `normalize_media_payloads`.
5. **Privacidade**: nenhum nome de arquivo ou byte em log; o nome some com o
   arquivo; `/teste` e a exclusão de contato apagam os arquivos.
6. **Formato de dados e implantação** (`core/services/data_format.py`,
   `deploy/deploy.sh`, `deploy/docker-compose.encrypted.yml`,
   `docs/CRIPTOGRAFIA.md`, `docs/BACKUP.md`): o marcador só vai a 2 com a
   captura ligada; a volta ao formato 1 (6 passos).
7. **A tela** (`core/web/static/painel/inbox.js`): áudio Ogg no Safari do
   iPhone sempre com "Baixar" ao lado; documento sem nome vira `arquivo`.

## Como rodar

`.venv/bin/python -m pytest -q -p no:warnings -o addopts="" tests/test_media_types.py tests/test_media_store.py tests/test_media_capture.py tests/test_media_flow.py tests/test_media_handoff.py tests/test_media_download.py tests/test_panel_media.py tests/test_media_cleanup_worker.py tests/test_data_format.py tests/test_deploy.py`
(a suíte inteira trava no sandbox; rode arquivos específicos). Os testes em
Postgres e Redis (`tests/test_media_pg.py`, `tests/test_media_cleanup.py`,
`tests/test_media_queues.py`) ficam pulados sem `TEST_DATABASE_URL` /
`TEST_REDIS_URL`; a suíte inteira passou em bancos descartáveis (1185 passed).

## Formato

Em português. Para cada achado: arquivo:linha, gravidade (P1 bloqueia, P2
corrigir antes do merge, P3 pode esperar), cenário concreto e correção. No fim,
nota de 1 a 5 e se recomenda o merge.
