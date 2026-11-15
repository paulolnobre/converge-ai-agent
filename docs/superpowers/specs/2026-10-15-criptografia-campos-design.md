# Criptografia dos campos sensíveis na aplicação — especificação

Data: 15/10/2026 · Status: aguardando revisão do Paulo · Revisada depois das
rodadas 1 a 5 do Codex (`.docs/REVISAO_SPEC_CAMPOS_RODADA_{1..5}.md`)

## Objetivo

O Postgres e a fila do Redis passam a guardar só texto cifrado nos campos que
identificam o cliente ou registram o que foi dito a ele ou sobre ele. A
criptografia é feita pela aplicação, e a chave nunca passa pelo banco.

O volume criptografado (LUKS, desde 13/10/2026) protege cópias do disco. Esta
camada protege contra quem **lê o banco sem ter as chaves**:

- uma senha do banco vazada;
- uma injeção de SQL;
- um `pg_dump` fora do lugar.

Nesses casos, saem bytes, não conversas nem nomes.

**O que ela não protege:** um erro do painel que entregue o registro errado. O
repositório decifra normalmente, e o painel mostra em claro. Isso é controle de
acesso, tratado no painel, não aqui.

Pronto quando:

- todos os campos da tabela abaixo estão cifrados em produção;
- não sobrou texto claro neles, e o banco recusa texto claro novo;
- o bot, o painel e a Inbox funcionam como antes;
- os logs e a auditoria não carregam conteúdo de cliente;
- o FAQ e o site podem dizer que as conversas e os dados dos clientes ficam
  cifrados pela aplicação.

## Decisões já tomadas com o Paulo (13–15/10/2026)

1. **Escopo A.** Conteúdo e dados de contato cifrados. O `chat_id` (no WhatsApp,
   `wa:` + o telefone) fica legível: o Paulo não o considera sensível, e ele
   está em 333 pontos do código, nos joins e no endereço de envio.
2. **Na aplicação, nos repositórios.** Um módulo pequeno cifra na escrita e
   decifra na leitura. Fora: `pgcrypto`, que põe a chave dentro de cada consulta
   (visível em `pg_stat_activity` e nos logs do Postgres); e um adaptador
   automático do psycopg, que esconde metade do mecanismo.
3. **Fora do escopo:**
   - **por decisão:** `chat_id`, as tabelas da equipe (`staff`, `panel_users`),
     `knowledge_chunks` (o FAQ público), `offers.slots` (só horários) e
     `workspace_actions`;
   - **por natureza:** os códigos (`stage`, `source`, `origin`, `reply_reason`,
     `pending_action`, `status`) e `automations.message`, o modelo de mensagem
     da empresa, que não é sobre uma pessoa.
4. **`workspace_actions` (aprovado pelo Paulo em 15/10/2026, desde que não guarde dado sensível).** O Paulo deixou a tabela de
   fora porque eu disse que ela não guardava conteúdo, e isso estava errado. Em
   caso de falha, `target` recebe o nome do arquivo (por exemplo,
   `Contrato — Maria Silva.pdf`) e `detail` recebe o texto da exceção
   (`documents.py:75–103`). A proposta continua sem cifrar a tabela, para a
   auditoria seguir legível, mas ela **deixa de receber conteúdo**: ids e
   códigos, nunca nomes. A seção "Logs e auditoria" diz como.
5. **Antes do app da equipe (PWA).** Com 2 contatos e 8 sessões, converter é
   barato agora. A migração `0009` passa a ser desta spec; a do app vira `0010`.

## O que já foi conferido (15/10/2026)

- **LangSmith:** desligado em produção (`LANGSMITH_TRACING=false`, sem chave).
- **Busca:** nenhum SQL procura texto nos campos cifrados, com duas exceções:
  - `inbox.py:27`, `left(last.message, 120)`: a prévia passa a ser cortada em
    Python;
  - `test_mode.py:116` e `:130`, `title LIKE '[TESTE] %'`: o modo teste acha os
    agendamentos de teste pelo título. Isso vira uma coluna (abaixo).
- **`COALESCE`** sobre `mail_threads.subject` (`mailbox.py:32`) continua válido
  sobre os bytes.
- **Restrições:** nenhum trigger, nenhum `UNIQUE` em telefone ou e-mail de
  cliente.
- **Tabelas sem código:** nenhum código escreve em `tasks` nem em
  `meeting_notes`. Ficam fora. Quem passar a usá-las cifra os campos de texto, e
  o `--check` ganha as colunas.

## Os campos

| Tabela | Campos cifrados | Impressão digital (busca) |
|---|---|---|
| `contacts` | `name`, `email`, `phone`, `organization`, `notes`, `origin_detail`, `interest` | `phone`, `email` |
| `sessions` | `message` | — |
| `appointments` | `title` (leva o nome do cliente), `meeting_url` (quem tem o link entra) | — |
| `automation_queue` | `reason` | — |
| `mail_threads` | `subject` | — |
| `mail_drafts` | `body_text` | — |
| fila do Redis | o payload inteiro de cada mensagem enfileirada | — |

O inventário de quem escreve e quem lê cada campo está no Anexo.

## O módulo `core/crypto.py`

### Envelope

`seal(context: str, text: str | None) -> bytes | None`:

- AES-256-GCM, nonce aleatório de 96 bits (`os.urandom`), tag de 128 bits;
- o resultado é `versão do formato (1 byte, = 1) ‖ versão da chave (1 byte) ‖
  nonce (12) ‖ texto cifrado ‖ tag (16)`;
- `None` continua `None`, para que `IS NULL` siga funcionando. `''` é cifrado
  como qualquer texto.

`open_sealed(context: str, data: bytes | None) -> str | None`:

- recusa envelope com menos de 30 bytes, versão de formato desconhecida ou
  versão de chave que não está carregada;
- uma tag que não confere levanta `SealedDataError`;
- a exceção nunca leva o conteúdo nem encadeia a causa original
  (`raise ... from None`).

### Contexto autenticado (AAD)

O `context` é `"<tabela>.<coluna>"` (por exemplo, `"contacts.phone"`), ou
`"queue"` para a fila. Ele entra como dado associado do GCM. Assim, um telefone
copiado para o campo de nome, ou uma nota copiada para uma mensagem, falha ao
abrir.

**Risco aceito:** o contexto não inclui o id da linha. Quem tem escrita no banco
poderia trocar o telefone cifrado de um contato pelo de outro. Mas quem tem
escrita já pode mudar o `chat_id` e desviar a conversa, que é pior. Amarrar ao
id exigiria reservar o id antes de cada `INSERT`, em todos os repositórios, sem
fechar esse risco maior.

### Impressão digital

`fingerprint(kind: str, value: str | None) -> bytes | None`: HMAC-SHA256 com a
chave de índice sobre `b"converge-fp-v1\x00" + kind + b"\x00" + canônico`
(UTF-8).

- **`phone`:** o canônico é `55` + DDD + número, só dígitos, a partir do
  `_normalise_phone` que o bot já usa (`contact_details.py:43`). Um número sem
  DDI ganha o `55`. Um número que não é brasileiro válido usa só os dígitos.
- **`email`:** `strip()`, e o endereço inteiro em minúsculas. Um e-mail com
  espaço no meio não é e-mail: a impressão é `None`, e o painel recusa salvar.

O mesmo canônico vale na gravação, na busca e na conversão.

## As chaves

- **`DATA_KEYS`** no `.env` do VPS: uma lista versionada, `1:<32 bytes em
  base64>,2:<...>`. A primeira cifra, e todas decifram. As versões vão de 1 a
  255, sem repetição.
- **`DATA_INDEX_KEY`:** 32 bytes em base64, só para as impressões digitais.
- **O bot e os comandos não sobem sem as duas**, nem com uma mal formada. A
  mensagem diz qual chave e por quê, sem mostrar o valor, e como gerar:
  `python -m cli.data_keys new`, que grava direto no `.env` sem imprimir.
- **Cópia no gerenciador de senhas do Paulo.** Perder as chaves é perder os
  dados. O restic leva o `.env` junto com o banco, e é isso que permite
  restaurar. Uma chave retirada continua no gerenciador enquanto existir backup
  feito com ela: 30 dias, a retenção do restic.

### Trocar a chave de cifra

O bot é um container só. Os comandos rodam como processos curtos na mesma
imagem.

1. Acrescentar `2:` **no fim** da lista e reiniciar o bot: todos já decifram com
   a 2.
2. Mover a `2:` para a frente e reiniciar: tudo o que é gravado sai com a 2.
3. `python -m cli.seal_data --rekey`: recifra o que ainda está na 1, no
   Postgres e nas filas do Redis. No Postgres, cada linha é atualizada só se o
   envelope ainda for o que foi lido:

   ```sql
   UPDATE contacts SET phone_sealed = %s WHERE id = %s AND phone_sealed = %s
   ```

   Se o painel gravou um valor novo nesse meio-tempo, a linha não é tocada: o
   valor novo já saiu com a 2. As impressões digitais não mudam, porque usam
   outra chave. Pode ser interrompido e rodado de novo.
4. `seal_data --check --key-version 2`: nenhum envelope na 1, nem no Postgres
   nem no Redis.
5. Se a quarentena guarda itens com o payload original na 1, decidir cada um
   antes: reprocessar ou apagar (`cli.queue_quarantine`). Depois de tirar a
   chave, eles não abrem mais.
6. Só então tirar a `1:` e reiniciar.

### Trocar a chave de índice

Sem escritores: parar o bot, rodar `seal_data --reindex` com a chave nova e
subir de novo. Com o volume atual de dados, são segundos.

## O banco: migração `0009`, só aditiva

- Para cada campo, uma coluna `BYTEA` ao lado: `name_sealed`, `message_sealed`
  e assim por diante.
- Em `contacts`, `phone_fp` e `email_fp` (`BYTEA`), com índice.
- **`appointments.test_mode BOOLEAN NOT NULL DEFAULT FALSE`.** As duas
  consultas do modo teste passam a usar a coluna, e quem cria um agendamento de
  teste a marca.
- **`data_format (version SMALLINT NOT NULL)`**, uma tabela de uma linha: o maior
  formato de dados que já pode ter sido gravado neste banco (veja "A fronteira
  da volta").
- Nada é apagado nem renomeado.

A imagem antiga continua atendendo enquanto a migração roda. Por isso, marcar os
agendamentos de teste antigos e limpar a auditoria antiga ficam no `seal_data`,
que roda depois que a imagem nova sobe (abaixo), e não na migração.

## Compatibilidade e conversão

### Estados de cada campo

A imagem nova nunca escreve texto na coluna antiga. Ao gravar, ela preenche
`<campo>_sealed` e, **no mesmo `UPDATE` ou `INSERT`**, esvazia a coluna antiga.
O que "esvaziar" quer dizer depende do tipo da coluna, e a leitura segue disso:

| Tipo da coluna antiga | A imagem nova grava | Linha não convertida | Linha convertida | Leitura |
|---|---|---|---|---|
| `NOT NULL` (`message`, `title`, `body_text`) | `_sealed` preenchido, antiga `''` | `_sealed IS NULL` (a antiga pode ser `''`, um valor válido) | `_sealed IS NOT NULL`, antiga `''` | `_sealed` se preenchido; senão, a antiga |
| opcional (os demais) | `_sealed` (ou `NULL`), antiga `NULL` | antiga `IS NOT NULL` (inclusive `''`) | antiga `NULL` | a antiga se não for `NULL`; senão, `_sealed` |

Assim, `''` gravado antes da mudança é convertido como qualquer valor, e o `''`
que a imagem nova usa como sentinela numa coluna `NOT NULL` nunca é tomado por
dado antigo. Cada campo é independente: uma linha pode ter o nome convertido e o
telefone ainda não.

### A conversão (`cli.seal_data`)

Em lotes de 500, cada linha numa atualização condicional ao estado lido:

```sql
-- coluna NOT NULL
UPDATE sessions SET message_sealed = %s, message = ''
 WHERE id = %s AND message_sealed IS NULL AND message = %s
-- coluna opcional
UPDATE contacts SET name_sealed = %s, name = NULL
 WHERE id = %s AND name = %s
```

- Se a linha mudou entre a leitura e a escrita, a atualização não pega nada. A
  imagem nova já gravou `_sealed`, então o valor novo vale.
- Em `contacts`, a mesma atualização grava `phone_fp` e `email_fp`.
- **Idempotente e retomável:** rodar de novo só pega linhas não convertidas.
- **A cada rodada, antes de converter os títulos**, o `seal_data` também:
  - marca `appointments.test_mode` nos agendamentos de teste cujo título ainda
    não foi convertido, nos dois casos que existem:
    - **órfãos:** `contact_id IS NULL AND title LIKE '[TESTE] %'` (o contato do
      teste já foi apagado; é o predicado de hoje);
    - **ainda ligados ao contato:** `title LIKE '[TESTE] %'` e o contato com
      `test_mode IS NOT NULL`.

    Um agendamento de cliente nunca entra: um contato real não tem
    `test_mode`, e um título de cliente não começa com `[TESTE] `, que só o modo
    teste escreve;
  - limpa a auditoria: `target` vira `NULL` nas ações `upload` com falha, e
    `detail` vira só a classe do erro.

  Assim, nada que a imagem antiga gravou enquanto a migração rodava fica de
  fora.
- **Redis:** converte a fila adiada e a das mensagens que esgotaram as
  tentativas (veja "A fila do Redis").

### A trava no banco

Quando o `--check` dá limpo, o `seal_data` instala uma restrição `CHECK` em cada
coluna antiga: `= ''` nas `NOT NULL`, `IS NULL` nas opcionais. Ela é instalada
como `NOT VALID` e depois validada, e é idempotente (consulta `pg_constraint`).

A partir daí, **o banco recusa texto claro novo**, venha de onde vier:

- um caminho de código esquecido;
- uma imagem antiga.

A falha é alta, um erro de banco, e não um vazamento silencioso.

### A fronteira da volta: a versão do formato dos dados

A imagem nova pode escrever dados cifrados assim que sobe, antes de o deploy
terminar. Por isso, **uma imagem de formato menor deixa de poder subir no
momento em que uma de formato maior sobe**, e não só depois da conversão. A
regra vale para qualquer subida: a volta automática, um deploy manual com uma
tag antiga, ou alguém rodando o `deploy.sh` à mão.

- **A imagem declara o formato** num label: `org.converge.data-format=1`. Uma
  imagem sem o label é formato 0, como a de hoje.
- **Os dois armazenamentos guardam o formato**, porque os dois recebem dados
  cifrados e o backup só leva o Postgres:
  - no Postgres, a tabela `data_format`;
  - no Redis, a chave `data_format` (persistida pelo AOF, como as filas).

  A imagem de formato 1, ao iniciar e **antes de ligar os workers e o webhook**,
  grava `max(atual, 1)` nos dois. Sem a tabela, sem a linha ou sem a chave, vale
  0.
- **O formato exigido é o maior dos dois.**
- **O `deploy.sh` confere antes de subir qualquer imagem**, inclusive na volta:
  se o formato da imagem é menor que o exigido, ele **não sobe a imagem e para o
  bot**:
  - o CI fica vermelho;
  - o log diz "a imagem <tag> é do formato X, e o Postgres (ou o Redis) já tem
    dados do formato Y";
  - os dados ficam a salvo, e o atendimento para até a correção.
- **Cada marcador mora junto com os seus dados.** Restaurar o backup pré-deploy
  baixa só o do Postgres. O Redis continua no formato 1, com mensagens cifradas
  nas filas, e a imagem antiga segue bloqueada.
- **Voltar ao formato 0 é um procedimento explícito**, escrito no guia, e quer
  dizer voltar a guardar em claro:
  1. parar o bot;
  2. restaurar o Postgres do backup pré-deploy;
  3. com a imagem de formato 1, que ainda decifra, rodar
     `python -m cli.queue_unseal`: reescreve as filas em JSON puro, com a mesma
     transação otimista da conversão, e só no fim apaga a chave `data_format` do
     Redis. Itens que não abrem ficam na quarentena, que a imagem antiga ignora;
  4. só então o `deploy.sh` aceita a imagem antiga.

  Não existe outro jeito de baixar o formato.
- O formato só cresce. Toda imagem a partir desta é formato 1, e voltas entre
  elas seguem automáticas.

### Ordem no deploy

O `deploy.sh` de hoje faz backup, migra, sobe a imagem, roda `load_content` e
`set_webhook`, e grava a tag. Se algo falhar, ele volta para a imagem anterior,
agora com a regra do formato acima.

1. **O backup pré-deploy** (restic) já existe. Ele é o ponto de restauração
   anterior à mudança.
2. **A conversão roda depois da tag gravada:** `seal_data`, depois
   `seal_data --check`, depois a trava.
3. **Se a conversão ou o `--check` falhar:**
   - o deploy termina com erro e o CI fica vermelho;
   - a imagem nova continua: ela lê dados meio convertidos (a tabela de estados
     acima);
   - corrige-se e roda-se de novo.
4. **Voltar à imagem de formato 0** depois disso exige restaurar o backup
   pré-deploy. Isso fica escrito no guia de operação.

### O que o `--check` confere

1. Nenhuma linha não convertida, pela tabela de estados.
2. Toda coluna `NOT NULL` tem `_sealed` preenchido em todas as linhas.
3. Todo envelope abre com as chaves carregadas e com o contexto da sua coluna.
   Com `--key-version N`, todos estão na versão N.
4. `phone_fp` e `email_fp` batem com o valor decifrado recalculado.
5. Todo agendamento cujo título (decifrado) começa com `[TESTE] ` tem a marca
   `test_mode`, e nenhuma falha de upload tem `target` preenchido na auditoria.
6. As quatro filas do Redis só têm envelopes que abrem, e nenhum JSON puro. A
   fila ativa e a de processamento esvaziam sozinhas em segundos; o `--check`
   espera até 60 s por elas antes de acusar.
7. A quarentena só tem envelopes que abrem. O `--check` informa quantos itens
   ela tem, por motivo, e, com `--key-version N`, quantos guardam um payload
   original de outra versão de chave.
8. As travas estão instaladas (depois da primeira conversão limpa).

A saída diz quantas linhas falham em cada item, com tabela, coluna e id (ou
fila e posição), nunca o conteúdo.

## A fila do Redis

O payload de cada mensagem passa por `seal("queue", json)` ao entrar na fila e
por `open_sealed` ao sair, em base64, porque o cliente usa
`decode_responses=True`. Isso cobre a fila adiada da noite e a fila das mensagens
que esgotaram as tentativas, que guardam o payload sem prazo, num Redis com AOF.

- **Transição:** a leitura aceita um payload antigo em JSON puro (começa com
  `{`).
- **Conversão das filas que guardam:** a fila adiada e a das mensagens que
  esgotaram as tentativas são convertidas pelo `seal_data` com transação
  otimista do Redis:
  1. `WATCH` na chave;
  2. ler a lista;
  3. cifrar o que estiver em JSON puro (ou recifrar, no `--rekey`);
  4. `MULTI`, substituir a lista, `EXEC`.

  Se outra operação mexeu na lista, o `EXEC` falha e a rodada repete.
- **A fila ativa e a de processamento** não são reescritas. O worker remove o
  item da lista de processamento pelo valor exato, e reescrever um item em uso
  quebraria isso. Elas esvaziam sozinhas em segundos, e o `--check` confere.
- **Um item que não abre não derruba o worker.** Hoje, a decodificação acontece
  fora do `try` de cada item (`agent.py:31`, `:75`), e um erro encerra o loop.
  Agora:
  - abrir e decodificar ficam dentro do tratamento de cada item;
  - um item que não abre (adulterado, chave ausente, JSON inválido) vai para
    `queue:quarantine` **cifrado de novo**:
    `seal("queue.quarantine", payload original)`, junto com o motivo e a hora.
    Um JSON antigo inválido pode ter texto de cliente, e a quarentena não guarda
    nada em claro;
  - **a passagem é atômica e nunca perde o item:**
    1. o envelope da quarentena é preparado antes, em Python;
    2. um script Lua faz, sem ninguém no meio, primeiro o `LPUSH` na quarentena
       e depois o `LREM` do valor original na fila de processamento;
    3. se o `LPUSH` falhar (por exemplo, o Redis sem memória, com
       `noeviction`), nada é removido;
    4. se a cifragem falhar, o script nem roda.

    Nos dois casos, o item continua na fila de processamento. A recuperação de
    hoje (`recover_processing`) só roda quando o worker inicia, e não conta
    tentativas. Por isso:
    - o worker guarda o payload exato numa lista em memória de "quarentena
      pendente" e tenta a passagem de novo a cada 30 s, enquanto estiver vivo.
      Não há limite de tentativas: o item não é processado, só movido, e nunca
      sai da fila de processamento antes de estar gravado na quarentena;
    - enquanto a lista não estiver vazia, o `/ready` responde que não está
      pronto ("quarentena pendente"), e o log diz quantos itens esperam, sem o
      conteúdo;
    - se o processo reiniciar, a recuperação de hoje devolve o item à fila
      ativa, ele falha de novo ao abrir, e a passagem é tentada outra vez;
  - o log diz só a fila e o motivo;
  - o worker segue para o próximo.
- **O `/ready` passa a conferir se o worker está vivo.** Hoje ele vê só o Redis
  e o Postgres, e responderia "pronto" com o worker parado.
- **A quarentena entra na conversão, no `--rekey` e no `--check`** como as
  outras filas que guardam. O `--rekey` recifra o envelope de fora; o payload
  original de dentro fica como chegou.
- **`python -m cli.queue_quarantine`:** `list` mostra motivo, hora e tamanho de
  cada item, nunca o conteúdo; `drop` apaga os itens escolhidos.
- A retenção das filas não muda nesta spec.

## Logs e auditoria

- **`documents.py`:** o log e o `target` usam o id do arquivo quando existe;
  quando não existe (falha no upload), o log diz só "upload falhou" e o
  `target` fica `NULL`.
- **`detail` da auditoria:** passa a guardar só a classe do erro e o status
  HTTP, nunca `str(exc)`.
- **`WorkspaceError` (Google e Microsoft):** a mensagem leva método, host e
  status, nunca o caminho nem a query (onde aparecem e-mails e ids de agenda).
  Hoje, `google.py:121` e `:128` põem a URL inteira, e `google.py:122` e
  `microsoft.py:144` encadeiam a exceção do `httpx` (`from exc`), que leva a URL
  para o traceback de quem chama `logger.exception`. Passa a ser `from None`.
- **Erros de criptografia:** o log diz tabela, coluna e id da linha. O conteúdo
  e o envelope nunca aparecem, e o traceback não encadeia a causa.
- **Uma linha ilegível numa lista não derruba a lista:** o item mostra
  "[ilegível]" naquele campo, e o erro vai para o log.

## Erros

| Situação | O que acontece |
|---|---|
| Falta `DATA_KEYS` ou `DATA_INDEX_KEY`, ou uma delas está mal formada | O bot e os comandos não sobem; o log diz qual chave e por quê, sem o valor. |
| Envelope adulterado, com contexto errado ou com versão de chave desconhecida | `SealedDataError`. Numa lista, o campo vira "[ilegível]"; numa leitura única, o erro sobe, e o log tem tabela, coluna e id. |
| Uma imagem ou um caminho grava texto numa coluna antiga, depois da trava | O banco recusa; erro alto no log. |
| `seal_data` ou `--check` falha no deploy | O CI fica vermelho; a imagem nova continua; corrige-se e roda-se de novo. |
| Qualquer subida (volta automática ou deploy manual) de uma imagem de formato menor que o do banco | O `deploy.sh` não sobe a imagem e para o bot; o CI fica vermelho. |
| Um item da fila não abre | Vai para `queue:quarantine`; o worker segue. |

## Testes

1. **Módulo:**
   - `open_sealed(c, seal(c, x)) == x`, inclusive `''`, acentos e emoji;
   - dois `seal` do mesmo texto dão resultados diferentes;
   - um byte trocado, um envelope truncado e um contexto diferente geram
     `SealedDataError`, e a mensagem do erro não contém o texto;
   - depois da rotação, a chave antiga ainda decifra;
   - `fingerprint("phone", "+55 (91) 98888-7777") ==
     fingerprint("phone", "91988887777") ==
     fingerprint("phone", "5591988887777")`;
   - `fingerprint("email", " Ana@X.com ") == fingerprint("email", "ana@x.com")`,
     e `fingerprint("email", "a b@x.com") is None`;
   - `fingerprint("email", x) != fingerprint("phone", x)`.
2. **Cada repositório, em Postgres real** (o padrão de `tests/test_test_mode_pg.py`,
   com `TEST_DATABASE_URL`):
   - depois de gravar, a linha crua não contém o texto nem o nome, e a coluna
     antiga está vazia;
   - ler pelo repositório devolve o original;
   - buscar o contato por telefone ou e-mail, em outro formato, acha a linha;
   - uma linha com a coluna antiga em texto (não convertida) é lida pelo texto.
3. **Inicialização:** sem cada uma das chaves, ou com uma mal formada, a config
   recusa, e a mensagem não contém o valor.
4. **Conversão:**
   - converte linhas antigas, esvazia o texto e grava as impressões;
   - converte um `''` antigo numa coluna `NOT NULL` e numa opcional, e nenhum
     dos dois vira `None` na leitura;
   - rodar duas vezes não muda nada;
   - uma linha alterada entre a leitura e a escrita não é sobrescrita;
   - no `--rekey`, uma linha que o painel regravou depois da leitura não é
     sobrescrita;
   - um agendamento `[TESTE]` criado sem a marca é marcado na rodada seguinte,
     tanto órfão quanto ainda ligado ao contato em teste; um agendamento de
     cliente nunca é marcado;
   - o `--check` acusa um agendamento com título decifrado `[TESTE] ...` sem a
     marca;
   - `--check` acusa: texto que sobrou; `_sealed` faltando numa coluna
     `NOT NULL`; envelope que não abre; impressão que não bate;
   - depois da trava, um `UPDATE` com texto na coluna antiga é recusado;
   - `--rekey` recifra para a chave nova, e `--check --key-version` confirma.
5. **Modo teste:** a recuperação acha o agendamento de teste pela coluna, com o
   título cifrado, e nunca apaga um agendamento de cliente.
6. **Fila:**
   - o payload no Redis não contém o texto;
   - um payload antigo em JSON puro ainda é lido, e a conversão o cifra na fila
     adiada e na das tentativas esgotadas;
   - uma lista que muda durante a conversão é relida, não sobrescrita;
   - um item adulterado vai para `queue:quarantine` cifrado, sem texto claro, e
     o worker processa o seguinte;
   - um JSON antigo inválido com texto de cliente não aparece em claro na
     quarentena;
   - se a gravação na quarentena falhar, o item continua na fila de
     processamento e o `/ready` acusa "quarentena pendente"; quando o Redis
     volta a aceitar gravações, o item vai para a quarentena sem reiniciar o
     processo, e o `/ready` volta a responder pronto;
   - com o worker parado, `/ready` responde que não está pronto.
7. **Logs e auditoria:** uma falha de upload não grava nem loga o nome do
   arquivo; o log completo de um `WorkspaceError` registrado com
   `logger.exception`, traceback incluído, não contém o caminho da URL.
8. **Formato:** a imagem nova grava `data_format = 1` no Postgres e no Redis
   antes de ligar os workers; nenhum dos dois diminui sozinho.
9. **Inbox:** a prévia é cortada em 120 caracteres em Python; uma mensagem
   ilegível vira "[ilegível]" sem derrubar a lista.
10. **Deploy** (padrão de `tests/test_deploy.py`):
   - a conversão roda depois da tag gravada; uma falha ali deixa o CI vermelho
     e não volta para a imagem anterior;
   - uma falha antes da tag, com a imagem anterior de formato menor que o do
     banco, para o bot em vez de subir a anterior;
   - um deploy manual de uma tag de formato 0, com o banco em formato 1, é
     recusado antes de subir qualquer coisa;
   - com formatos iguais, a volta continua como hoje;
   - um banco sem a tabela `data_format` conta como formato 0;
   - com o Postgres restaurado para formato 0 e o Redis ainda em formato 1, a
     imagem antiga é recusada;
   - depois do `queue_unseal`, as filas estão em JSON puro, a chave do Redis
     sumiu, e a imagem antiga é aceita.

## Implantação

1. **Gerar as chaves no VPS** (`python -m cli.data_keys new`), gravar direto no
   `.env` sem imprimir, e guardar a cópia no gerenciador de senhas.
2. **Merge e deploy.** O backup pré-deploy, a migração `0009`, a imagem nova e,
   com a tag gravada, a conversão e a trava.
3. **Conferir:**
   - `seal_data --check` limpo;
   - uma consulta crua em `sessions` e `contacts` mostra só bytes;
   - `redis-cli` mostra os payloads cifrados;
   - o bot responde, e a Inbox mostra as conversas;
   - um `/teste` e um `/sair` funcionam.
4. **Atualizar o FAQ e o site:** as conversas e os dados de contato ficam
   cifrados também pela aplicação.
5. **Guia de operação** (`docs/CRIPTOGRAFIA.md`, nova seção): chaves, rotação,
   o `--check`, e o fato de a imagem anterior à `0009` não ser alvo de volta.

## Fora do escopo

- Pseudonimizar o `chat_id` (opção B). Pode virar um passo próprio, se um
  cliente ou uma exigência de conformidade pedir.
- Busca por texto nas conversas. Se vier, precisa de um desenho próprio.
- Gerenciar a chave por KMS a cada pedido, ou num HSM.
- Prazo de retenção das filas do Redis.

## Anexo — inventário inicial (15/10/2026)

Quem toca cada campo hoje, levantado por busca no código. O plano confirma linha
a linha. As escritas dinâmicas do painel (`funnel.py:40`, `EDITABLE`) e os
`RETURNING` contam como escrita e leitura.

| Campo | Escreve | Lê no SQL |
|---|---|---|
| `contacts.name`, `email`, `phone` | `contacts.py`, `test_mode.py`, `funnel.py` (painel) | `contacts.py`, `inbox.py`, `funnel.py`, `briefing.py`, `automations.py`, `handoffs.py`, `test_mode.py`, `cli/load_content.py` |
| `contacts.organization`, `notes`, `interest` | `contacts.py`, `funnel.py` (painel) | `contacts.py`, `funnel.py` |
| `contacts.origin_detail` | `inbox.py:93` | `funnel.py`, `inbox.py` (`conversation`) |
| `sessions.message` | `contacts.py:229`, `test_mode.py:21` | `contacts.py:241` (`recent_history`), `inbox.py:27` (prévia), `funnel.py`, `automations.py`, `handoff.py` (serviço) |
| `appointments.title`, `meeting_url` | `appointments.py:19` | `appointments.py`, `funnel.py:86` e `:117`, `test_mode.py:111` |
| `automation_queue.reason` | `automations.py:197` | `automations.py` (painel) |
| `mail_threads.subject` | `mailbox.py:14` | `mailbox.py` |
| `mail_drafts.body_text` | `mailbox.py:64` | `mailbox.py:83` (`RETURNING`), serviço `mailbox.py:70` e `:131` |
| fila (e quarentena) | `update_queue.py:50`, `:141` | `update_queue.py:73`, `:89`, `:95`, `:109`; `agent.py:31` e `:75` (decodificação) |
