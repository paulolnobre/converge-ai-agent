# Criptografia em repouso do agente — especificação

Data: 10/10/2026 · Status: aguardando revisão do Paulo

## Objetivo

O banco do agente (Postgres) e a fila (Redis), onde ficam conversas, contatos e
mensagens de clientes, passam a ficar num volume criptografado. A chave fica
**fora do servidor**: ele a busca na AWS a cada boot. Assim, uma cópia do disco
do VPS, um snapshot ou um disco descartado não expõem os dados.

Pronto quando:
- os dados do agente estão no volume criptografado;
- um reboot do VPS sobe tudo sozinho;
- o site e o FAQ dizem "criptografado em repouso", e isso é verdade.

## Decisões já tomadas com o Paulo (09–10/10/2026)

1. **Chave fora do servidor (opção B).** A chave fica no AWS SSM Parameter
   Store (`SecureString`, chave KMS gerenciada `aws/ssm`), região `sa-east-1`.
   Custo: US$ 0.
2. **Acesso mínimo.**
   - **Usuário IAM:** `converge-agent-vps-luks`, com a policy
     `converge-agent-ler-chave-luks`, que permite só o `ssm:GetParameter` de
     `/converge/agent/luks-key`, só a partir do IP do VPS (`203.0.113.10`).
   - **Proteção:** uma cópia do disco leva a credencial, mas ela não funciona
     de outro IP. Pode ser revogada a qualquer momento.
3. **Já feito e conferido em 10/10/2026:**
   - o parâmetro existe (`SecureString`, 64 caracteres);
   - a credencial está em `/etc/converge-luks-aws.env` (`root:root`, `600`);
   - a leitura a partir do VPS funciona.

## O volume

| | |
|---|---|
| Arquivo | `/var/lib/converge-agent.img`, 10 GB |
| Formato | LUKS2 (`aes-xts-plain64`, chave de 512 bits, `argon2id`) |
| Mapeamento | `/dev/mapper/converge-agent`, ext4 |
| Montagem | `/srv/converge-agent` |
| Conteúdo | `postgres/` (dados do Postgres) e `redis/` (AOF da fila) |

- **A chave nunca toca o disco.** Ela vai da resposta da AWS direto para
  o `cryptsetup --key-file=-`, por `stdin`. Não é gravada, impressa nem logada.
- **O volume é independente do da construtora.** Ele não muda nada no volume
  `/srv/construction` nem no `/etc/crypttab`.

## Destravar no boot

Scripts versionados em `deploy/luks/` e instalados por
`deploy/luks/install.sh`:

- **`converge-agent-unlock`** (`/usr/local/sbin/`):
  1. lê `/etc/converge-luks-aws.env`;
  2. busca o parâmetro com `curl --aws-sigv4`, sem instalar a AWS CLI;
  3. abre o LUKS por `stdin` e monta o volume;
  4. sobe a stack do agente (`docker compose up -d`) se ela não estiver de pé.

  Ele tenta de novo com espera crescente por até 10 minutos, para cobrir a rede
  ou a AWS demorando no boot. Se tudo falhar, sai com erro, e a falha fica no
  `journalctl`.
- **`converge-agent-volume.service`** (systemd):
  - `Type=oneshot` com `RemainAfterExit=yes`;
  - roda depois de `network-online.target` e `docker.service`;
  - é **só ordem, sem dependência:** o Docker e a construtora sobem
    normalmente, mesmo se o agente não destravar.

## A trava contra banco vazio

Se o volume não estiver montado, o Postgres **não pode subir** com um banco
novo e vazio no lugar do real.

- **O override:** o `deploy/docker-compose.encrypted.yml` troca os volumes do
  `postgres` e do `redis` por *bind mounts* de `/srv/converge-agent/postgres`
  e `/srv/converge-agent/redis`, com `create_host_path: false`.
- **Sem o volume montado:** essas pastas não existem, o container não inicia,
  e o bot fica fora do ar em vez de atender sem histórico.
- **Ativado só no VPS:** o override só vale depois da migração, por
  `COMPOSE_FILE=docker-compose.yml:docker-compose.encrypted.yml` no `.env` do
  stack. O `deploy.sh` e o CI não mudam.
- **Antes da migração:** nada muda. Um deploy no meio do caminho continua
  usando os volumes atuais.

## Migração (janela de 1–2 min, num horário combinado)

1. Fora da janela:
   - instalar os scripts;
   - criar e formatar o volume (`luksFormat` com a chave vinda da AWS);
   - montar e conferir.
2. Snapshot do restic (`backup.sh … pre-criptografia`).
3. Parar `reception-bot`, `postgres` e `redis` do agente.
4. Copiar com `rsync -aHAX` o conteúdo dos volumes `converge-agent_postgres_data`
   e `converge-agent_redis_data` para `/srv/converge-agent/{postgres,redis}`,
   mantendo dono e permissões.
5. Pôr o `COMPOSE_FILE` no `.env` e subir a stack.
6. Conferir:
   - contagens iguais às de antes da parada (`contacts`, `sessions`,
     `appointments`, `knowledge_chunks`, `staff`);
   - `/ready` respondendo 200;
   - o painel abrindo;
   - uma mensagem de teste pelo WhatsApp respondida.
7. **Voltar atrás**, se algo falhar: tirar a linha do `COMPOSE_FILE` e subir.
   Os volumes antigos estão intactos.
8. Os volumes antigos ficam 7 dias e depois são removidos (`docker volume rm`).

## Teste de reboot

Num horário combinado, `reboot` do VPS. Conferir que, sem ninguém mexer:
- o volume destravou;
- o agente e a construtora subiram;
- o bot respondeu.

## Riscos e como ficam

| Risco | Efeito | O que evita ou resolve |
|---|---|---|
| A AWS fora no momento de um boot | o agente não sobe | tentativas por 10 min; depois, rodar `converge-agent-unlock` à mão |
| O IP do VPS muda | o agente não sobe no próximo boot | atualizar o IP na policy |
| Parâmetro apagado por engano | dados do volume ilegíveis | cópia da chave no gerenciador de senhas do Paulo; backups do restic independentes |
| Credencial vazada | lê a chave, mas só do IP do VPS | revogar a chave de acesso na AWS |

## Depois de pronto

- **Site:** o bloco de segurança passa a dizer "banco criptografado em
  repouso (AES-256), com a chave fora do servidor".
- **FAQ:** a resposta "Como a Converge protege os dados" passa a dizer o mesmo.
- **Memória e diagrama:** `security-claims` e `.docs/arquitetura.html` são
  atualizados.

## Testes

- **`tests/test_deploy.py`, no padrão atual do arquivo:**
  - o override usa `create_host_path: false` nos dois serviços;
  - o `unlock` usa `--key-file=-` e não redireciona a chave para arquivo nem
    para log;
  - a unit é só de ordem (sem `Requires=docker.service`).
- **No VPS:** a migração e o reboot conferidos como descrito acima.

## Fora desta etapa

- **Os segredos de configuração** (`.env`, `google_credentials.json`): já
  protegidos por permissão de arquivo, e os backups são criptografados.
- **O volume da construtora**, cuja chave ainda está no disco: é outro
  projeto, mas o mesmo desenho serviria para ele.
- **Criptografia de campos dentro do banco** (`pgcrypto`).
