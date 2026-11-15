# Revisão — criptografia em repouso do agente

Branch `feat/criptografia-repouso`, diff `git diff origin/main..HEAD`. Spec:
`docs/superpowers/specs/2026-10-10-criptografia-em-repouso-design.md`. Plano:
`docs/superpowers/plans/2026-10-13-criptografia-em-repouso.md`. Guia:
`docs/CRIPTOGRAFIA.md`.

O Postgres e o Redis do agente passam para um volume LUKS2 cuja chave fica no
AWS SSM e é buscada a cada boot por `deploy/luks/converge-agent-unlock`. O
override `deploy/docker-compose.encrypted.yml` (ligado por `COMPOSE_FILE` no
`.env` do VPS) aponta os dois serviços para o volume, com
`create_host_path: false`.

## Onde olhar com mais cuidado

1. **A chave nunca ir ao disco, a um log ou à linha de comando** — nem a
   credencial da AWS. Olhe `fetch_key`, `open_volume` e o `create-volume.sh`.
2. **Sem o volume montado, o Postgres nunca sobe vazio** — inclusive num deploy
   (push no `main`) com o volume trancado, e quando `/srv/converge-agent` existe
   mas não está montado.
3. **A unit do systemd:** a ordem em relação ao Docker, o que acontece se o
   Docker subir a stack antes do script (restart policy), se a AWS demorar no
   boot, e se a unit atrapalhar a outra stack do servidor (construtora).
4. **Idempotência:** rodar o script com o volume já aberto ou já montado; o
   `create-volume.sh` nunca formatar um volume existente.
5. **Falhas reais:** credencial revogada, IP mudado, AWS fora — o script desiste
   em tempo finito e com mensagem útil.

## Como rodar

`.venv/bin/python -m pytest -p no:warnings -o addopts="" tests/test_luks.py tests/test_deploy.py`
(scripts testados com binários falsos; `shellcheck` limpo).

## Formato

Em português. Para cada achado: arquivo:linha, gravidade (P1 bloqueia, P2
corrigir antes do merge, P3 pode esperar), cenário concreto e correção. No fim,
nota de 1 a 5 e se recomenda o merge.
