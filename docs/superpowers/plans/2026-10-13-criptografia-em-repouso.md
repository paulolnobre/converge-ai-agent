# Criptografia em repouso do agente — plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Postgres e o Redis do agente passam a viver num volume LUKS2. A chave fica no AWS SSM e é buscada a cada boot, direto para a memória.

**Architecture:**
- **Código versionado em `deploy/luks/`:**
  - o script `converge-agent-unlock` busca a chave com `curl --aws-sigv4` e a entrega por `stdin` ao `cryptsetup`;
  - ele monta o volume e sobe a stack;
  - uma unit do systemd o roda no boot, só como ordem.
- **O override `deploy/docker-compose.encrypted.yml`** troca os volumes nomeados por *bind mounts* com `create_host_path: false`. Ele só vale no VPS, via `COMPOSE_FILE` no `.env` do stack.
- **A migração e o reboot** são passos de operação, com conferência e volta atrás.

**Tech Stack:** bash, systemd, cryptsetup 2.8 (no VPS), curl 8.18 (`--aws-sigv4`), Docker Compose, pytest (testes com binários falsos, no padrão de `tests/test_deploy.py`), PyYAML 6.

**Spec:** `docs/superpowers/specs/2026-10-10-criptografia-em-repouso-design.md` (aprovada no PR #17).

## Global Constraints

- **Branch e commits:** branch `feat/criptografia-repouso`, a partir do `main`. Commits **sem** `Co-Authored-By` (regra do Paulo).
- **AWS:**
  - parâmetro `/converge/agent/luks-key`, região `sa-east-1`, `SecureString`;
  - credenciais em `/etc/converge-luks-aws.env` (`root:root`, `600`), com `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `LUKS_SSM_PARAMETER`. Já existe e foi testado.
- **O volume:**
  - arquivo `/var/lib/converge-agent.img`, 10 GB;
  - LUKS2 com `--cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id`;
  - mapeamento `converge-agent`, ext4;
  - montagem em `/srv/converge-agent` (opção `noatime`);
  - dados em `postgres/` e `redis/`.
- **A chave nunca toca o disco:** sempre por pipe até `cryptsetup … --key-file=-`. Nenhum `>`, `tee`, arquivo temporário ou `echo` da chave. Nada é logado.
- **Tentativas no boot:** até **600 s** no total.
- **A unit do systemd:** `After=network-online.target docker.service`, `Wants=network-online.target`, **sem** `Requires=`. A construtora nunca depende dela.
- **O override:** `create_host_path: false` nos dois serviços. Ativado só por `COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml` no `/opt/converge-agent/.env`.
- **Nunca imprimir credenciais** nem a chave. Conferir arquivos só por tamanho, dono e permissão.
- **Testes:** `.venv/bin/python -m pytest -p no:warnings -o addopts=""`, sem `AWS_*`/`RESTIC_*` no shell.
- **Operações no VPS** (Tasks 4–6): só nos horários combinados com o Paulo. O fuso é o de Belém (UTC−3).

## Review Focus

1. **Volume não montado no deploy:** um deploy roda (push no `main`) com o override ativo e o volume ainda trancado. O `postgres` não sobe, e o `deploy.sh` volta à imagem anterior sem criar banco vazio. Teste na Task 1: o override não deixa pasta ser criada.
2. **Script rodado duas vezes:** o volume já está aberto e montado (ex.: alguém rodou à mão). O script não tenta abrir de novo e só sobe a stack. Teste na Task 2.
3. **Credencial com erro na AWS:** a AWS responde 4xx (credencial revogada, IP mudou). O script não fica tentando para sempre, desiste em 600 s com erro claro no `journalctl` e não chama `cryptsetup`. Teste na Task 2.
4. **`COMPOSE_FILE` depois de um deploy:** um deploy reescreve o `.env`. O `COMPOSE_FILE` continua lá. Teste na Task 1, sobre o `deploy.sh`.
5. **Dono dos arquivos na cópia:** os dados copiados mantêm os donos numéricos (o Postgres roda como uid 70 dentro do container). `rsync --numeric-ids` na Task 5, conferido por `ls -ln` antes de subir.

---

### Task 1: O override do Compose e a entrega pelo CI

**Files:**
- Create: `deploy/docker-compose.encrypted.yml`
- Modify: `.github/workflows/ci-cd.yml` (passo "Ship files")
- Test: `tests/test_luks.py` (novo), `tests/test_deploy.py`

**Interfaces:**
- Produces:
  - o arquivo `docker-compose.encrypted.yml` em `/opt/converge-agent/` a cada deploy;
  - os serviços `postgres` e `redis` com bind mounts de `/srv/converge-agent/postgres` → `/var/lib/postgresql/data` e de `/srv/converge-agent/redis` → `/data`.

- [ ] **Step 1: Testes que falham** em `tests/test_luks.py`:
  - `test_the_override_binds_the_encrypted_volume`: carregue o YAML. `services.postgres.volumes` contém um item `type: bind`, com `source: /srv/converge-agent/postgres`, `target: /var/lib/postgresql/data` e `bind: {create_host_path: false}`. O mesmo para o `redis` (`/srv/converge-agent/redis` → `/data`). O override **não** redefine o `init.sql` nem outros campos além de `volumes`.
  - `test_the_base_compose_still_uses_named_volumes`: `deploy/docker-compose.prod.yml` continua com `postgres_data:` e `redis_data:`. Antes da migração, nada muda.
  - `test_ci_ships_the_override_and_the_luks_scripts`: o texto de `ci-cd.yml` contém `deploy/docker-compose.encrypted.yml` e `deploy/luks/`, ambos com destino em `/opt/converge-agent/`.
- [ ] **Step 2:** Em `tests/test_deploy.py`, o teste `test_a_release_keeps_compose_file_in_env` copia o padrão do `test_release_records_only_a_fully_configured_image`. Com `.env` contendo `COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml`, depois do release o `.env` ainda tem essa linha, e o `IMAGE_TAG` novo.
- [ ] **Step 3:** rode os dois arquivos. Esperado: FAIL nos três de `test_luks.py`. O de `test_deploy.py` pode passar de primeira, porque documenta um comportamento existente que precisa continuar.
- [ ] **Step 4:** crie o override. No `ci-cd.yml`, em "Ship files", acrescente:
  - `rsync -a deploy/docker-compose.encrypted.yml "$VPS:/opt/converge-agent/docker-compose.encrypted.yml"`;
  - `rsync -a deploy/luks/ "$VPS:/opt/converge-agent/luks/"`;
  - um comentário: o override só vale com `COMPOSE_FILE` no `.env`.
- [ ] **Step 5:** a suíte inteira passa.
- [ ] **Step 6: Commit** `feat: override do compose para o volume criptografado`

### Task 2: Destravar no boot

**Files:**
- Create: `deploy/luks/converge-agent-unlock`, `deploy/luks/converge-agent-volume.service`, `deploy/luks/install.sh`, `deploy/luks/create-volume.sh`
- Test: `tests/test_luks.py`

**Interfaces:**
- Consumes: `/etc/converge-luks-aws.env` (das constantes globais).
- Produces:
  - `/usr/local/sbin/converge-agent-unlock`: sem argumentos; sai 0 com o volume montado e a stack de pé, e ≠0 depois de 600 s sem conseguir;
  - a unit `converge-agent-volume.service`, habilitada;
  - `create-volume.sh`, rodado uma vez, que cria e formata o volume.

**O script `converge-agent-unlock`:**
- **Configurável por ambiente**, com padrões de produção, para os testes:
  - `LUKS_ENV=/etc/converge-luks-aws.env`
  - `LUKS_IMAGE=/var/lib/converge-agent.img`
  - `LUKS_NAME=converge-agent`
  - `LUKS_MOUNT=/srv/converge-agent`
  - `STACK_DIR=/opt/converge-agent`
  - `UNLOCK_BUDGET=600`
  - `UNLOCK_WAIT=5` (espera inicial, dobrando até 60)
- **O orçamento** é contado **somando as esperas** feitas, não pelo relógio. Fica determinístico e testável, e o `curl` tem `--max-time 20` por tentativa.
- **Já montado:** se `mountpoint -q "$LUKS_MOUNT"`, pula direto para subir a stack.
- **Já aberto e não montado:** se `/dev/mapper/$LUKS_NAME` existe, só monta.
- **Senão, abre.** Busca e abertura num pipe só, assim:

```bash
curl -fsS --max-time 20 --aws-sigv4 "aws:amz:${AWS_DEFAULT_REGION}:ssm" \
     --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
     -H "X-Amz-Target: AmazonSSM.GetParameter" -H "Content-Type: application/x-amz-json-1.1" \
     -d "{\"Name\":\"${LUKS_SSM_PARAMETER}\",\"WithDecryption\":true}" \
     "https://ssm.${AWS_DEFAULT_REGION}.amazonaws.com/" \
  | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["Parameter"]["Value"])' \
  | cryptsetup open --type luks2 --key-file=- "$LUKS_IMAGE" "$LUKS_NAME"
```

  Com `set -o pipefail`. Uma falha em qualquer etapa conta como tentativa.
- **Depois de abrir:**
  - `mount -o noatime /dev/mapper/$LUKS_NAME $LUKS_MOUNT`;
  - `docker compose --project-directory "$STACK_DIR" up -d`, que lê o `COMPOSE_FILE` do `.env` dali.
- **Mensagens:** só "tentativa N falhou", "volume aberto", "stack de pé" e o erro final. Nunca a resposta do `curl`.

**A unit:**
- `[Unit]` com `Description`, `Wants=network-online.target` e `After=network-online.target docker.service`;
- `[Service]` com `Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=/usr/local/sbin/converge-agent-unlock` e `TimeoutStartSec=700`;
- `[Install]` com `WantedBy=multi-user.target`.

**`install.sh`** (root, idempotente):
- copia o script com `install -m 750 -o root -g root`;
- copia a unit para `/etc/systemd/system/`;
- roda `systemctl daemon-reload` e `systemctl enable converge-agent-volume.service`;
- **não** inicia nada.

**`create-volume.sh`** (root, uma vez):
- **Recusa** se `$LUKS_IMAGE` já existe.
- `fallocate -l 10G`, depois `chmod 600`.
- `luksFormat` com os parâmetros das constantes globais, recebendo a chave pelo mesmo pipe de `curl` e `python3`, com `--batch-mode` e `--key-file=-`.
- Abre, roda `mkfs.ext4 -L converge-agent`, monta e cria `postgres/` e `redis/`.
- **Não** sobe a stack.

- [ ] **Step 1: Testes que falham** em `tests/test_luks.py`. Usam binários falsos num `PATH` temporário (`curl`, `python3` de verdade, `cryptsetup`, `mount`, `mountpoint`, `docker`, `sleep`), que gravam as chamadas num log, como em `test_deploy.py`. O `curl` falso devolve `{"Parameter":{"Value":"chave-de-teste"}}`, e o `cryptsetup` falso grava num arquivo o que leu do `stdin`.
  - `test_unlock_passes_the_key_only_through_stdin`:
    - o `cryptsetup` recebeu `chave-de-teste` pelo `stdin`, com `--key-file=-`;
    - a chave **não** aparece em nenhum arquivo do diretório de trabalho além do log do `cryptsetup` falso;
    - a chave não aparece na saída do script.
  - `test_unlock_mounts_and_starts_the_stack`: depois de abrir, roda `mount -o noatime /dev/mapper/converge-agent <mount>` e `docker compose --project-directory <stack> up -d`, nessa ordem.
  - `test_unlock_when_already_mounted_only_starts_the_stack`: com o `mountpoint` falso respondendo 0, não chama `curl` nem `cryptsetup`, mas chama `docker compose … up -d`.
  - `test_unlock_gives_up_after_its_budget`:
    - com o `curl` falso sempre falhando (sai 22), `UNLOCK_BUDGET=3`, `UNLOCK_WAIT=1` e um `sleep` falso que só registra a chamada, o script sai ≠0 depois de esperas que somam ≥ 3 (1 + 2), com no máximo 3 tentativas;
    - nunca chama `cryptsetup`;
    - a última linha diz que desistiu.
  - `test_the_unit_only_orders_after_docker`: o texto da unit tem `After=network-online.target docker.service` e **não** tem `Requires=`, nem `BindsTo=`, nem `Before=docker.service`.
  - `test_no_script_writes_the_key_to_disk`: nos três scripts de `deploy/luks/`, nenhuma linha que contenha `Value` ou `key` redireciona para arquivo (`>`, `>>`, `tee`), e o `--key-file` é sempre `-`. Leitura do texto.
  - `test_create_volume_refuses_an_existing_image`: com `LUKS_IMAGE` apontando para um arquivo que já existe, sai ≠0 sem chamar `cryptsetup`.
- [ ] **Step 2:** rode `tests/test_luks.py`. Esperado: FAIL.
- [ ] **Step 3:** escreva os quatro arquivos, com `set -euo pipefail` nos scripts e `chmod +x` nos executáveis.
- [ ] **Step 4:** passe `shellcheck` nos três scripts, se estiver disponível (`shellcheck deploy/luks/*.sh deploy/luks/converge-agent-unlock`). Sem avisos de nível *warning* ou acima. Depois, a suíte inteira.
- [ ] **Step 5:** acrescente uma seção curta em `docs/OPERACAO.md` (crie se não existir): o que é o volume, onde está a chave, como destravar à mão (`sudo /usr/local/sbin/converge-agent-unlock`), como ver o log (`journalctl -u converge-agent-volume`) e como revogar a credencial na AWS.
- [ ] **Step 6: Commit** `feat: destravar o volume criptografado do agente no boot`

### Task 3: Revisão, PR e merge

- [ ] **Step 1:** ruff, pyright e a suíte inteira, tudo limpo.
- [ ] **Step 2:** revisão do Codex (`gpt-6.1-sol`, só leitura, `< /dev/null`). Pedir foco em:
  - a chave nunca ir ao disco;
  - o comportamento sem o volume (banco vazio);
  - a ordem do systemd.

  Corrigir os P1 e P2, com teste antes de cada correção, até a nota ≥ 4 e a recomendação de merge.
- [ ] **Step 3:** push e PR. Depois do merge do Paulo, conferir no VPS que `/opt/converge-agent/docker-compose.encrypted.yml` e `/opt/converge-agent/luks/` chegaram. O `.env` **ainda sem** `COMPOSE_FILE`: nada mudou para o bot.

### Task 4: Preparar o volume no VPS (sem parada)

- [ ] **Step 1:** `sudo bash /opt/converge-agent/luks/install.sh`. Conferir:
  - `systemctl is-enabled converge-agent-volume` responde `enabled`;
  - o script está em `/usr/local/sbin`, com `750 root:root`.
- [ ] **Step 2:** `sudo bash /opt/converge-agent/luks/create-volume.sh`. Conferir:
  - `cryptsetup luksDump /var/lib/converge-agent.img` mostra LUKS2, `aes-xts-plain64`, chave de 512 bits e `argon2id`. Só os metadados; o dump não contém a chave;
  - `df -h /srv/converge-agent` mostra cerca de 10 GB;
  - as pastas `postgres/` e `redis/` existem.
- [ ] **Step 3: teste do caminho de boot sem reboot:**
  - `umount /srv/converge-agent && cryptsetup close converge-agent`;
  - com o volume fechado, `lsattr -d /srv/converge-agent` mostra o `i`, e `mkdir /srv/converge-agent/x` falha;
  - `systemctl start converge-agent-volume`, que precisa voltar com o volume montado;
  - `findmnt -n -o SOURCE --mountpoint /srv/converge-agent` responde `/dev/mapper/converge-agent` (é o que o script compara);
  - `journalctl -u converge-agent-volume` só com as mensagens previstas.

  A stack ainda usa os volumes antigos. O script recria o `postgres` e o `redis`
  depois de montar, então os dois reiniciam por alguns segundos: rodar num
  momento sem conversa em andamento.

### Task 5: Migração (janela de 1–2 min, horário combinado com o Paulo)

- [ ] **Step 1:** antes da janela, guardar as contagens:

```sql
SELECT (SELECT count(*) FROM contacts), (SELECT count(*) FROM sessions),
       (SELECT count(*) FROM appointments), (SELECT count(*) FROM knowledge_chunks),
       (SELECT count(*) FROM staff);
```

- [ ] **Step 2:** `sudo -u deploy /opt/converge-agent/backup.sh /opt/converge-agent pre-criptografia`. Snapshot confirmado.
- [ ] **Step 3: início da parada.** `cd /opt/converge-agent && docker compose stop reception-bot postgres redis`.
- [ ] **Step 4: cópia.** Antes, `findmnt -n -o SOURCE --mountpoint /srv/converge-agent` precisa responder `/dev/mapper/converge-agent`; se não, parar. De cada volume (`converge-agent_postgres_data`, `converge-agent_redis_data`):

```bash
rsync -aHAX --numeric-ids /var/lib/docker/volumes/converge-agent_<v>_data/_data/ /srv/converge-agent/<pasta>/
```

  Conferir com `ls -ln` que os donos numéricos batem com os da origem.
- [ ] **Step 5:** acrescentar `COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml` ao `.env`, sem tocar no resto. Depois, `docker compose up -d`.
- [ ] **Step 6: conferência.**
  - `docker inspect converge-agent-postgres-1` mostra o bind em `/srv/converge-agent/postgres`;
  - as mesmas contagens do Step 1;
  - `/ready` responde 200;
  - o painel abre;
  - uma mensagem pelo WhatsApp é respondida.

  **Fim da parada.**
- [ ] **Volta atrás, se algo falhar no Step 6:** tirar a linha do `COMPOSE_FILE` do `.env` e rodar `docker compose up -d`. Os volumes antigos estão intactos.
- [ ] **Step 7:** anotar a data. Os volumes antigos são removidos em 7 dias (`docker volume rm converge-agent_postgres_data converge-agent_redis_data`), na Task 7.

### Task 6: Teste de reboot (horário combinado com o Paulo)

- [ ] **Step 1:** avisar o Paulo, porque a construtora também reinicia. `sudo reboot`.
- [ ] **Step 2:** sem ninguém mexer, até 10 min depois, conferir:
  - `systemctl status converge-agent-volume` está *active (exited)*;
  - o volume está montado;
  - as stacks do agente e da construtora estão de pé;
  - `/ready` do bot responde 200;
  - a construtora responde;
  - uma mensagem de teste é respondida.
- [ ] **Step 3:** se não destravar, rodar `sudo /usr/local/sbin/converge-agent-unlock` e ler o `journalctl` para corrigir antes de seguir.

### Task 7: Dizer a verdade nova

**Files:**
- Modify (repo do site): `index.html`, no item "Backups criptografados" do bloco de segurança, e na introdução do bloco.
- Modify (repo do agente): `content/faq.md`, na resposta "Como a Converge protege os dados e cumpre a LGPD?".
- Modify (fora do git): a memória `security-claims.md` e o `.docs/arquitetura.html`, no card "Banco sem criptografia em repouso" dos pontos fracos.

- [ ] **Step 1:** o site ganha o item **"Criptografia em repouso"**: "O banco e a fila do atendimento ficam num volume criptografado (AES-256); a chave fica fora do servidor e é buscada a cada inicialização." O item de backups não muda.
- [ ] **Step 2:** o FAQ ganha a mesma frase num item novo, depois de "Criptografia em trânsito". Em `tests/test_knowledge.py`, o trecho de proteção passa a conter "em repouso".
- [ ] **Step 3:** a suíte do agente passa. Abrir os PRs dos dois repositórios.
- [ ] **Step 4:**
  - na memória `security-claims`, mover "DB NOT encrypted at rest" para o que é verdade, com a data;
  - no diagrama, trocar o card do ponto fraco por um card de operação "Volume criptografado".
- [ ] **Step 5:** 7 dias depois da migração, `docker volume rm` dos volumes antigos e `sudo fstrim -v /`, para os blocos livres não guardarem o banco antigo. Snapshots do VPS feitos antes da migração ainda têm os dados sem criptografia: listar com o Paulo e apagar os que ele aprovar. Anotar a data na memória.
