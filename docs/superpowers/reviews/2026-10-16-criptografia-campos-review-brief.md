# Revisão — criptografia dos campos na aplicação

Branch `feat/criptografia-campos`, diff `git diff origin/main..HEAD`. Spec:
`docs/superpowers/specs/2026-10-15-criptografia-campos-design.md`. Plano:
`docs/superpowers/plans/2026-10-15-criptografia-campos.md`. Guia:
`docs/CRIPTOGRAFIA.md` (seção "Criptografia na aplicação").

O app passa a cifrar os campos de cliente (AES-256-GCM, contexto autenticado
`tabela.coluna`) e as filas do Redis. Cada campo tem a coluna antiga (que fica
vazia) e `<campo>_sealed`. O `cli.seal_data` converte as linhas antigas com
atualização condicional, confere e instala travas `CHECK`. Marcadores de formato
no Postgres e no Redis, conferidos pelo `deploy.sh`, impedem uma imagem antiga
de subir sobre dados cifrados.

## Onde olhar com mais cuidado

1. **Chave e conteúdo nunca em log, erro ou saída** — `core/crypto.py`
   (`from None`), `core/sealed_fields.py` (`read_lenient`), `core/services/sealing.py`
   (`Problem`), os conectores (`WorkspaceError` sem caminho), `cli/*`.
2. **Escrita em claro esquecida** — `tests/test_sealed_writes.py` varre o SQL;
   confira se algum caminho escapa dele (SQL montado em tempo de execução,
   `update_details`, `funnel.update_lead`, `test_mode.create`).
3. **A tabela de estados** (`sealed_fields.read`) e a conversão condicional
   (`sealing.convert`, `rekey`, `reindex`): corridas com a imagem nova, `''`
   antigo, linhas meio convertidas.
4. **O deploy** (`deploy/deploy.sh`): formato da imagem pelo label, formato
   guardado no Postgres e no Redis, recusa antes de mudar algo, volta que para o
   bot, `seal_data` depois da tag sem volta.
5. **A fila** (`core/integrations/update_queue.py`, `core/agent.py`): quarentena
   atômica em Lua, item pendente com o worker vivo, reescrita com `WATCH`, o
   `/ready`.

## Como rodar

- `.venv/bin/python -m pytest -q -p no:warnings -o addopts="" tests/test_crypto.py tests/test_sealed_fields.py tests/test_sealed_writes.py tests/test_deploy.py tests/test_connectors.py tests/test_documents.py`
  (a suíte inteira trava no sandbox; rode arquivos específicos).
- Os testes de Postgres e Redis (`*_pg.py`, `test_queue_sealing.py`,
  `test_data_format.py`) ficam pulados sem `TEST_DATABASE_URL`/`TEST_REDIS_URL`;
  eles passaram em bancos descartáveis (835 passed).

## Formato

Em português. Para cada achado: arquivo:linha, gravidade (P1 bloqueia, P2
corrigir antes do merge, P3 pode esperar), cenário concreto e correção. No fim,
nota de 1 a 5 e se recomenda o merge.
