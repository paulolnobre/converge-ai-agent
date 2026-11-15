# Criptografia em repouso do agente

O banco (Postgres) e a fila (Redis) do agente ficam num volume criptografado.
Especificação: `docs/superpowers/specs/2026-10-10-criptografia-em-repouso-design.md`.

## Como é

| | |
|---|---|
| Volume | `/var/lib/converge-agent.img` (10 GB), LUKS2 `aes-xts-plain64`, chave de 512 bits, `argon2id` |
| Montado em | `/srv/converge-agent` (`postgres/` e `redis/`) |
| Chave | AWS SSM Parameter Store, `/converge/agent/luks-key`, região `sa-east-1`, `SecureString` |
| Credencial de leitura | `/etc/converge-luks-aws.env` (`root:root`, `600`), usuário IAM `converge-agent-vps-luks` |
| Quem pode ler a chave | só esse usuário, e só a partir do IP do VPS (`203.0.113.10`) |
| Destrava no boot | `converge-agent-volume.service`, que roda `/usr/local/sbin/converge-agent-unlock` |
| Liga no Compose | `COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml` no `.env` do stack |

**A chave nunca fica no servidor.** A cada boot, ela vem da AWS e vai direto
para o `cryptsetup`, por `stdin`. Uma cópia do disco ou um snapshot do VPS não
abre o volume.

**Sem o volume montado, o Postgres não sobe.** O override aponta para pastas que
só existem com o volume aberto, e nunca cria um banco vazio no lugar. Com o
volume fechado, `/srv/converge-agent` fica vazia e imutável (`chattr +i`): nada
consegue criar essas pastas no disco do servidor.

## Destravar à mão

Quando a AWS não respondeu no boot, ou o IP do VPS mudou:

```bash
sudo /usr/local/sbin/converge-agent-unlock
journalctl -u converge-agent-volume -n 50
```

O script tenta por até 10 minutos e explica no log por que desistiu. Ele nunca
imprime a chave. Ele também se recusa a montar, sem tocar em nada, quando:

- **"há arquivos em /srv/converge-agent fora do volume"**: alguém criou algo na
  pasta com o volume fechado. Confira o que é, mova para outro lugar e rode de
  novo;
- **"... está montado em /srv/converge-agent, e não é o volume"**: outro disco
  foi montado ali. Desmonte e rode de novo.

## Se o IP do VPS mudar

No console da AWS: **IAM → Policies → `converge-agent-ler-chave-luks`**. Troque o
IP em `aws:SourceIp` pelo novo (`curl -4 https://checkip.amazonaws.com`, rodado
no VPS). Depois, destrave à mão.

## Revogar o acesso (suspeita de vazamento)

1. **Desative a chave de acesso:** **IAM → Users → `converge-agent-vps-luks` →
   Security credentials →** desative (ou apague) a chave de acesso. A partir daí,
   ninguém lê a chave do volume, inclusive o próprio servidor no próximo boot.
2. **Gere uma nova e ponha no servidor** com o mesmo comando usado na instalação,
   que lê os valores do terminal e grava em `/etc/converge-luks-aws.env`.

## A chave do volume

- **Guarde uma cópia** no gerenciador de senhas. Se o parâmetro for apagado, o
  volume não abre mais. Os backups do restic continuam valendo de forma
  independente.
- **Trocar a chave** (`cryptsetup luksChangeKey`) exige a chave atual e a nova.
  Faça numa janela combinada.

## Instalação (uma vez)

1. Depois de um deploy, os arquivos estão em `/opt/converge-agent/luks/`.
2. `sudo bash /opt/converge-agent/luks/install.sh`: instala o script e a unit,
   e habilita a unit.
3. `sudo bash /opt/converge-agent/luks/create-volume.sh`: cria, formata e monta
   o volume. Recusa rodar se o arquivo do volume já existir.
4. A migração dos dados segue o plano
   `docs/superpowers/plans/2026-10-13-criptografia-em-repouso.md` (Task 5).

# Criptografia na aplicação

O volume LUKS (acima) protege cópias do disco. Por cima dele, o próprio app
cifra os campos de cliente antes de gravar. Quem lê o banco **sem as chaves**
só vê bytes: uma senha do banco vazada, uma injeção de SQL ou um `pg_dump` fora
do lugar não expõem conversas nem nomes.

Especificação: `docs/superpowers/specs/2026-10-15-criptografia-campos-design.md`.

## O que é cifrado

- **`contacts`:** `name`, `email`, `phone`, `organization`, `notes`,
  `origin_detail` e `interest`. Telefone e e-mail também ganham uma impressão
  digital (`phone_fp`, `email_fp`), para a busca.
- **`sessions.message`:** a conversa inteira.
- **`appointments`:** `title` e `meeting_url`.
- **`automation_queue.reason`**, **`mail_threads.subject`** e
  **`mail_drafts.body_text`**.
- **As filas do Redis**, inclusive a quarentena.
- **Os arquivos do Inbox** (fotos, áudios, vídeos e documentos que o cliente
  manda): cada um num envelope com a mesma chave, preso ao próprio nome no
  disco, e o nome original do arquivo (`media.filename`) como campo cifrado.
  Veja "Os arquivos do Inbox", abaixo.

Cada campo tem duas colunas: a antiga, que fica vazia, e `<campo>_sealed`, com o
envelope (AES-256-GCM, com a versão da chave).

**Fica de fora:** o `chat_id` (no WhatsApp, `wa:` + telefone), as tabelas da
equipe e o FAQ. A auditoria (`workspace_actions`) não é cifrada, mas não recebe
mais conteúdo: só ids e o tipo do erro.

**O que a criptografia não protege:** um erro do painel que mostre o registro
errado. O app decifra normalmente; isso é controle de acesso.

## As chaves

No `.env` do stack, ao lado das outras:

| Variável | O que é |
|---|---|
| `DATA_KEYS` | `1:<base64 de 32 bytes>`; numa rotação, `2:...,1:...`. A primeira cifra; todas decifram. |
| `DATA_INDEX_KEY` | base64 de 32 bytes, só para as impressões digitais. |

- **Sem as duas, o bot não sobe em produção.**
- **Perder as chaves é perder os dados.** Guarde uma cópia no gerenciador de
  senhas.
- **O backup do restic leva o `.env` junto com o banco**: é assim que um backup
  restaurado abre. Uma chave retirada continua no gerenciador enquanto existir
  backup feito com ela, ou seja, 30 dias.

**Gerar (uma vez), no VPS, como `deploy`:**

```bash
bash /opt/converge-agent/data-keys.sh /opt/converge-agent/.env
```

O script não imprime nada, e recusa um `.env` que já tem as chaves. Para copiar
para o gerenciador, abra o `.env` no seu terminal.

**Conferir, sem mostrar as chaves:**

```bash
cd /opt/converge-agent && docker compose exec -T reception-bot python -m cli.data_keys check
```

## `seal_data`: converter, conferir, travar

O deploy roda o comando depois de cada release, já com a tag gravada:

```bash
docker compose exec -T reception-bot python -m cli.seal_data
```

Ele faz, nesta ordem:

1. marca os agendamentos de teste antigos;
2. limpa nomes de arquivo da auditoria antiga;
3. cifra o que ainda está em claro, no banco e nas filas;
4. confere;
5. se estiver limpo, instala as **travas**: restrições que fazem o banco recusar
   texto claro nas colunas antigas.

Pode ser rodado de novo quando quiser.

Se ele falhar no deploy, o CI fica vermelho e a imagem nova continua de pé, porque
ela lê dados meio convertidos. Leia o que o comando listou, corrija e rode de
novo.

**Só conferir:** `python -m cli.seal_data --check`. A saída diz tabela, coluna e
id, nunca o conteúdo.

## Trocar a chave de cifra

1. Acrescente `2:<chave nova>` **no fim** de `DATA_KEYS` e reinicie o bot. A
   partir daí, ele já decifra com as duas.
2. Mova a `2:` para **a frente** e reinicie. Tudo o que for gravado sai com a 2.
3. `python -m cli.seal_data --rekey`: recifra o que estava na 1, no banco, nas
   filas e nos arquivos do Inbox. Pode rodar com o bot no ar.
4. `python -m cli.seal_data --check --key-version 2`: tem que dar limpo. E
   `python -m cli.media status` não pode mostrar arquivo na versão 1 (órfãos e
   temporários entram na conta; a limpeza apaga os velhos em até 15 minutos,
   ou rode `python -m cli.media cleanup`), nem ilegíveis.
5. Se a quarentena guarda itens de antes, decida cada um antes (veja abaixo).
   Depois de tirar a chave, eles não abrem mais.
6. Tire a `1:` e reinicie. Guarde a chave antiga no gerenciador por mais 30 dias.

## Trocar a chave de índice

Com o bot parado:

```bash
docker compose stop reception-bot
# DATA_INDEX_KEY nova no .env
docker compose run --rm reception-bot python -m cli.seal_data --reindex
docker compose up -d reception-bot
```

## A quarentena das filas

Um item da fila que não abre (adulterado, com uma chave que não está mais
carregada, ou com JSON quebrado) vai para a quarentena, cifrado, e o worker
segue. Se a gravação na quarentena falhar, o `/ready` responde "quarentena
pendente" até ela acontecer.

```bash
docker compose exec -T reception-bot python -m cli.queue_quarantine list      # motivo, hora e tamanho
docker compose exec -T reception-bot python -m cli.queue_quarantine drop <id> <id>  # apaga os escolhidos
```

## Os arquivos do Inbox

Com `MEDIA_ENABLED=true`, o bot guarda o que o cliente manda para a equipe ver
no Inbox: áudio por 48 h (até 168 h com o atendimento aberto), o resto por 90
dias. Ficam em `/data/media` no container, que no VPS é
`/srv/converge-agent/media`, **no volume cifrado** (o overlay
`docker-compose.encrypted.yml` monta a pasta). O caminho é só um uuid; o
conteúdo, um envelope que só abre com aquele nome. A mídia **não tem backup**.

**A pasta, uma vez, antes do primeiro deploy desta versão.** Dona é o usuário
`app` da imagem (sem a pasta, o `deploy.sh` para sem mudar nada):

```bash
cd /opt/converge-agent
COMPOSE_FILE=docker-compose.yml docker compose run --rm --no-deps --entrypoint id reception-bot
# uid=<U>(app) gid=<G>(app)
install -d -m 700 -o <U> -g <G> /srv/converge-agent/media
```

A limpeza roda sempre, com a captura ligada ou não, a cada 15 minutos, e apaga
o que venceu (com o nome), órfãos, temporários e marcas velhas. No `/metrics`:
`media_cleanup_runs_total{result}` e `media_cleanup_last_success`; 24 h sem uma
passada boa viram log `CRITICAL`.

```bash
docker compose exec -T reception-bot python -m cli.media status    # registros, arquivos por versão de chave, filas
docker compose exec -T reception-bot python -m cli.media cleanup   # uma passada agora
```

Com o bot parado, troque `exec -T` por `run --rm --no-deps`.

`python -m cli.media purge` apaga **todos** os arquivos e nomes e marca os
registros como expirados. É para o restore (`docs/BACKUP.md`) e para voltar ao
formato 1 (abaixo); com o bot parado.

## O formato dos dados, e voltar para uma imagem antiga

A imagem declara o formato que lê (label `org.converge.data-format`). O
Postgres (tabela `data_format`) e o Redis (chave `data_format`) guardam o maior
formato já gravado. As imagens sem o label são do 0; as da cifra na aplicação,
do **formato 1**; desta (arquivos do Inbox) em diante, do **formato 2**, que
lê o 1 e o 2.

Os marcadores só vão a 2 quando o bot sobe com `MEDIA_ENABLED=true` (só a
captura cria dado de mídia). Antes disso, um deploy desta versão ainda pode
voltar sozinho para a anterior; depois de ligar a captura, não. Desligar a
captura não baixa o marcador.

**O `deploy.sh` nunca sobe uma imagem abaixo do formato guardado:**

- **num deploy novo:** recusa sem mudar nada, e o bot que está rodando continua;
- **numa volta automática:** para o bot em vez de subir a imagem antiga.

### Voltar para uma imagem anterior à agenda no painel

O espelho da Agenda do painel (`calendar_events`, com título, participantes e link cifrados) se refaz sozinho a
partir do Google. Uma imagem anterior não o conhece: não o limpa nem o recifra. Antes de voltar a ela, com o bot
parado:

```bash
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM calendar_events; DELETE FROM calendar_ignores; DELETE FROM calendar_sync_state;"'
```

### Voltar do formato 2 para o 1

Nesta ordem, para que nenhum arquivo, nome ou mensagem com mídia fique para trás
numa imagem que não tem a limpeza:

1. `MEDIA_ENABLED=false` no `.env` e reinicie o bot (esta imagem ainda): ele
   para de capturar e de converter mídia.
2. **Espere as filas que andam sozinhas esvaziarem de mídia:**
   `python -m cli.media status` mostra `queued_media_updates`, os payloads com
   mídia em todas as listas (ativa, adiada, em processamento, dead-letter e
   quarentena; um envelope que não abre conta). Enquanto houver algum na
   ativa, na adiada ou em processamento, o bot os processa normalmente. Itens
   adiados para o próximo expediente podem levar horas ou dias.
3. **Dead-letter e quarentena não andam sozinhas:**
   `python -m cli.media normalize-queues`. Cada item com mídia nelas sai (se a
   mensagem tem mais de 90 dias, ou se já foi tratada) ou perde a mídia e
   fica onde estava. Um envelope que não abre fica e continua contando:
   resolva com `cli.queue_quarantine`.
4. **Pare o bot** (`docker compose stop reception-bot`) e confira de novo,
   com `run --rm --no-deps`, que `queued_media_updates` é 0. Antes de parar,
   o `/ready` não pode estar dizendo "quarentena pendente".
5. Com o bot ainda parado:
   `docker compose run --rm --no-deps reception-bot python -m cli.media purge`.
   Ele termina mostrando o `status`, que tem que estar zerado.
6. Só então baixe os dois marcadores à mão e suba a imagem do formato 1:

   ```bash
   docker compose exec -T postgres sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE data_format SET version = 1"'
   docker compose exec -T redis redis-cli SET data_format 1
   ```

   A tabela `media_events` fica no banco, sem uso.

### Voltar ao formato 0

**Voltar ao formato 0 é voltar a guardar em claro.** De um formato 2, volte
antes ao 1 (acima). Depois, só desse jeito:

1. `docker compose stop reception-bot`.
2. Restaurar o Postgres do backup `pre-deploy-<tag>` da primeira imagem do
   formato 1 (`docs/BACKUP.md`). Isso baixa o marcador do Postgres.
3. Com a imagem do formato 1, que ainda decifra:

   ```bash
   IMAGE_TAG=<tag de uma imagem do formato 1> docker compose run --rm reception-bot \
     python -m cli.queue_unseal
   ```

   O `IMAGE_TAG` explícito importa: depois de uma volta recusada, o `.env` pode
   nomear outra imagem, e só uma do formato 1 tem esse comando e as chaves de
   abrir as filas.

   O comando reescreve as filas em JSON e só então apaga o marcador do Redis. Ele
   recusa rodar se o bot ainda deu sinal de vida nos últimos 60 s.
4. Só então o `deploy.sh` aceita a imagem antiga.
