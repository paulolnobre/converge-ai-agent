# Revisão da Inbox — guia para o revisor

## Rodada 6 — o teardown que travava, explicado e removido

A rodada 5 deu **3/5**: Inbox e backup corretos, mas
`tests/test_graph.py` travava no teardown do pytest-asyncio, dentro de
`asyncio.runners.close()`, na `.venv` local.

**Causa.** A aresta condicional `_needs_scheduling` era uma função comum
(`def`). O LangGraph roda funções comuns numa thread do *default executor*
do loop. Com um probe em `run_in_executor`, esse foi o único trabalho em
thread que o teste criou. Ao fechar o loop, `Runner.close()` precisa
encerrar esse executor e esperar a thread. Foi ali que o traceback parou.
Na mesma `.venv` e no mesmo Python 3.12.15, o encerramento termina
instantaneamente fora do sandbox da revisão. Não sei dizer o que, no
sandbox, impede esse encerramento de concluir. Por isso a correção não
depende disso: elimina a thread.

**Correção.** `_needs_scheduling` virou `async def`. Agora o grafo inteiro
roda como corrotina no próprio loop, sem *default executor* para encerrar.
Em produção, isso também tira um salto de thread por mensagem.
Regressão: `test_routing_never_leaves_the_event_loop` falha se o
roteamento chamar `run_in_executor`. Ela falhou antes da correção e passa
depois. Com o mesmo probe na suíte inteira, **nenhum** dos 501 testes usa
mais o default executor, então esse mecanismo de travamento não existe em
outro ponto da suíte.

**Melhoria menor da rodada 5.** O caso de falha do backup em
`tests/test_deploy.py` agora exige a mensagem `Pre-deploy backup failed`
na saída. Uma mutação da mensagem em `deploy.sh` faz o teste falhar.

**Verificação:** 501 testes passaram em ~8 s na `.venv` local, e Ruff e
Pyright não reportaram erros nem avisos. O CI deste commit fica para depois
do push.

**Foco da revisão:** rodar a suíte completa no mesmo sandbox em que o
travamento apareceu. Se ainda travar, peço o traceback completo de todas as
threads (`faulthandler.dump_traceback_later`), porque o default executor
deixou de existir nos testes.

---

## Rodada 5 — backup fora da VPS provado; union com a main corrigida

A revisão da rodada 5 deu **2/5**, com um bloqueio: a união local de
`feat/inbox` com `main` (que já trazia o backup fechado e mergeado,
PR #1) quebrava os seis casos de `tests/test_deploy.py`, porque esse teste
só copiava `deploy.sh` para a pasta temporária e a `main` passou a chamar
`./backup.sh` antes da migração. Corrigido: o teste agora cria um `backup.sh`
simulado na mesma pasta, e um sétimo caso cobre o backup falhando —
confirma que a migração não inicia e que não é preciso rollback, porque
nada mudou ainda (o `reception-bot` anterior segue rodando). `deploy.sh`
também passou a reportar essa falha com `::error::`, no mesmo padrão dos
outros passos, em vez de parar silenciosamente por `set -e`.

A revisão também apontou `tests/test_graph.py::test_a_booking_is_left_for_the_caller_to_schedule`
como impresso "PASSED" sem o processo encerrar (exit 124 após 20s). Esse
arquivo não foi tocado por nenhuma mudança da Inbox ou do backup. Não
reproduzi em três execuções independentes: na `.venv` local (Postgres real,
500 testes, ~9s), num container `python:3.12-slim` com Postgres real, e no
mesmo container **sem rede** (`--network none`), isolando qualquer tentativa
de alcançar AWS/Telegram/Google. Em nenhuma delas o processo deixou de
encerrar. Não há `create_task`, `AsyncClient` ou outro recurso em aberto no
caminho de `graph.route` que esse teste exercita — os clientes HTTP
preguiçosos (`telegram`, `llm`, `workspace/*`) não são tocados pelos mocks
desse arquivo. Registro isto como não reproduzido localmente, não como
corrigido; se recorrer, preciso de um traceback (`faulthandler`) ou de saber
se outro processo competia por recursos no sandbox da revisão.

**Verificação após as correções do fixture de deploy:** suíte completa com
**500 testes** (485 da rodada 4 + os de backup trazidos pela `main` + o novo
caso de falha de backup), Ruff e Pyright sem erros nem avisos,
`bash -n deploy/deploy.sh` passou. Confirmado também sem `.venv`/`.env`
locais vazando credenciais de AWS/restic para a subprocess do teste de
backup — a primeira tentativa falhou exatamente por isso (variáveis
exportadas no shell mascarando o caso "configuração ausente"), não por um
defeito no código.

**Backup fora da VPS:** desde a rodada 4, o backup saiu de "código pronto"
para "rodando de verdade". Bucket S3 com Object Lock, IAM com permissão
mínima, restic instalado na VPS, `/etc/converge-backup.env` preenchido,
repositório inicializado, os dois timers (`converge-agent`,
`ai-agents-construction`) ativos, primeiro backup de cada stack concluído (4
snapshots), chave pessoal do Paulo adicionada ao repositório, e
`restore-check.sh converge-agent` restaurou dados reais (agendamentos,
contatos, sessões, usuários do painel) num Postgres descartável. O
bloqueio operacional da rodada 4 está, portanto, resolvido.

---

## Preparação da rodada 5 — correções locais da revisão 4

Os quatro achados de código da revisão 4 foram corrigidos na branch `feat/inbox`.
Esta seção registra a implementação local; o CI e a revisão independente desta
versão devem ser conferidos após o push. Nenhuma mudança foi aplicada na VPS.

| Achado | Correção | Regressão |
|---|---|---|
| Mensagem ou botão acima do limite some da Inbox assumida | Depois do lock, o texto/caption ou um marcador de mídia ilegível é registrado e o responsável é avisado. Áudio limitado não é baixado/transcrito; o bot segue calado | `test_the_message_limit_does_not_speak_for_the_bot_while_a_person_holds_it`, `test_a_limited_button_still_reaches_the_owner`, `test_limited_voice_is_recorded_without_download` |
| “Devolver ao bot” retorna sucesso quando o canal recusa a mensagem | `inbox.release` propaga erro 502; a rota deixa a transação falhar e desfaz o fechamento da sessão. Um canal ainda não integrado devolve 501 | `test_giving_back_refused_by_channel_reports_error_without_team_notice` e testes de rota existentes |
| Falha ao gravar metadados após subir a imagem | `.env` e `.deployed-tag` são preparados em diretório temporário privado. Se a troca atômica de um arquivo falhar, o script restaura o marcador anterior e a imagem anterior | `test_release_records_only_a_fully_configured_image` inclui falha de gravação dos dois marcadores |
| Bot sinaliza “digitando” durante atendimento humano | `send_typing` fica após a decisão de repasse | `test_the_bot_does_not_type_during_a_human_handoff` |

Os seis cenários novos/estendidos falharam antes da correção. Depois dela, a
suíte local passou com **485 testes**, Ruff sem falhas, Pyright sem erros nem
avisos, e `bash -n deploy/deploy.sh` passou. A falha de envio do Telegram pode
ser ambígua após timeout: um envio aceito e não confirmado ainda pode gerar
uma segunda mensagem após nova tentativa. Esse risco também existe em
`inbox.reply`; verificar a entrega no Telegram antes de reenviar.

**Bloqueio operacional:** a captura do hPanel mostra backups semanais
agendados, mas nenhum backup ainda criado. Antes do merge, confirmar um ponto
de restauração real e um `pg_dump` do Postgres guardado fora da VPS, com
restauração em banco isolado. A compra opcional de backup diário do provedor
reduz a janela de perda, mas não substitui a cópia lógica nem esse ensaio.

---

## Rodada 4 — confira as correções da terceira rodada

A terceira rodada deu **3/5**: os dois bloqueios da rodada 2 foram resolvidos,
mas encontrou dois novos bloqueios e dois riscos de concorrência. Estas correções
estão em `feat/inbox`; o CI desta versão depende do push. Revise o diff
da rodada 4, inclusive os testes que já estavam no checkout antes das
alterações de produção desta rodada.

| # | Achado da rodada 3 | Correção | Verificação nesta rodada |
|---|---|---|---|
| 1 | “Devolver ao bot” fecha a sessão durante um turno já lido como assumido | `inbox.release` toma `handoffs.lock_chat` antes de consultar e fechar a sessão | `test_giving_back_waits_for_the_bot_turn_in_progress`; no Postgres local, a devolução esperou o turno confirmar a atividade antes de fechar |
| 2 | Automação aprovada antes de “Assumir” ainda fala com o cliente | Cada envio toma o lock da conversa, relê a aprovação/opt-out e a sessão ativa, e descarta o item se alguém foi chamado ou assumiu. O worker confirma a fila antes dos envios e usa uma transação por item | `test_an_approved_message_is_dropped_once_a_person_holds_the_conversation`, `test_whether_a_person_holds_it_is_read_under_the_conversation_lock`, `test_dispatch_commits_each_approved_message_separately`, `test_a_claim_between_dispatch_selection_and_send_discards_the_message`; as duas consultas SQL foram executadas no Postgres local |
| 3 | O sweep fecha uma sessão reativada depois de selecioná-la como inativa | `close_if_idle` exige que a sessão ainda esteja ativa e inativa no momento do UPDATE; o sweep usa o lock por conversa e confirma cada fechamento separadamente | `test_a_session_that_came_back_to_life_is_not_closed`, `test_timeout_sweep_releases_each_chat_lock_before_the_next`; no Postgres local, uma sessão reativada após a seleção permaneceu assumida |
| 4 | O aviso de limite sai após alguém assumir | `process_message` toma o lock também no ramo de limite, antes de decidir se o bot pode avisar | `test_the_message_limit_does_not_speak_for_the_bot_while_a_person_holds_it` agora confere a ordem `lock → read` |

**Verificação:** os testes que já estavam no checkout falharam em sete casos
antes das alterações de produção. Os testes de transações curtas foram incluídos
na refatoração. A suite completa passou com **479 testes**, Ruff passou e Pyright
reportou zero erros e avisos. Os ensaios no Postgres local usaram linhas
temporárias removidas ao final, sem envio ao Telegram. Nesta rodada, a interface
real passou no Chromium em desktop e celular com API local simulada: login com
retorno ao link, assumir, responder, trocar origem, devolver, abrir conversa
por hash e exibir HTML recebido como texto. Esse smoke test visual não substitui
um teste de ponta a ponta com o backend real. O CI só poderá validar esta
versão depois do push.

**Release:** o healthcheck de produção agora exige `/ready` (Postgres e Redis).
Falhas ao subir a imagem, carregar conteúdo ou configurar o webhook restauram
a imagem anterior. Quatro cenários do script de deploy foram testados com Docker
simulado, sem acessar a VPS.

**Risco aceito de envio confirmado seguido de rollback:** se o Telegram aceitar
uma resposta e a transação falhar, a mensagem **não aparece** no histórico da
Inbox; o operador verá um erro e poderá reenviar, duplicando a resposta. Para
este volume, fica como risco operacional consciente. Antes de reenviar após
erro ambíguo, é preciso conferir a entrega fora da Inbox. Uma reconciliação de
envio persistida pode ser considerada depois.

**Foco da revisão:** confirme que o lock das automações vale até o envio, mas é
liberado ao fim de cada item; que `approved_for_send` não reenvia um item cujo
estado mudou; e que `close_if_idle` não fecha uma sessão cuja atividade mudou
entre a seleção e o UPDATE. Por decisão do Paulo, lembretes de compromissos
já marcados continuam saindo durante um atendimento assumido. A supressão
acima se aplica às mensagens de automação aprovadas pela equipe, não a esses
avisos transacionais.

---

## Rodada 3 — confira as correções da segunda rodada

A segunda rodada deu 3/5, com dois bloqueios de merge. As correções estão no
commit `3e26595`:

| # | Achado da rodada 2 | Correção | Verificação |
|---|---|---|---|
| 1 | O bot age depois de uma tomada de posse concorrente | `handoffs.lock_chat` (`pg_advisory_xact_lock` por `chat_id`). O bot o toma no começo de cada turno (`process_message` e `process_callback`, depois do limite de mensagens), e `inbox.claim` o toma antes de ler quem está com a conversa. O worker usa uma transação por mensagem (`core/agent.py`), então quem assume no meio de um turno espera o turno inteiro, com a resposta, terminar. É um lock consultivo porque uma conversa que ninguém pediu não tem linha de sessão para travar | `test_the_conversation_is_locked_before_the_bot_decides_anything` (mensagem e botão), `test_taking_a_conversation_waits_for_the_bot_turn_in_progress`; no Postgres real, "bot locked → bot replied → panel claimed" |
| 2 | Sessões assumidas pelo Telegram antes do deploy ficam caladas | A `0005_inbox.sql` passa `status='claimed' AND claimed_by_user_id IS NULL` para `open` e limpa o dono antigo: a conversa aparece em "Esperando pessoa" e o bot volta a responder. Depois do deploy, nenhum código cria sessão assumida sem `claimed_by_user_id`. Em produção hoje não há nenhuma: só 1 sessão, fechada | No Postgres real, uma sessão da era Telegram ficou `open` e sem dono. A migração inteira rodou de novo sem erro |
| 4 | A janela de 50 não garante zero buracos | O painel relê as últimas 200 mensagens a cada 8 consultas, uns 32 s, e junta pelo id. Uma gravação atrasada atrás de mais de 50 aparece no máximo nesse intervalo | Revisão do código. O E2E do navegador passou os 7 passos |
| pool | Três respostas lentas mais o worker esgotam o pool de 4 | `db_pool_max` padrão 10 (produção não define `DB_POOL_MAX`; o Postgres tem `max_connections=100`) | — |

**Onde o autor discordou, de novo com justificativa:**
- **Item 3, aviso duplicado quando o turno falha depois de postar.** Aceito. O
  worker já tem entrega pelo menos uma vez: um turno que falha é refeito do
  zero, e os outros efeitos externos (resposta do bot, evento na agenda) têm o
  mesmo perfil. Um "Nova conversa" repetido no grupo, raro, custa menos do que
  uma reserva fora da transação ou uma fila de saída.
- **Item 5, envio confirmado com falha no commit.** Aceito. Para acontecer, o
  banco precisa cair entre o envio e o commit da mesma requisição. O painel
  mostra o erro, mas a próxima releitura não mostrará uma mensagem cujo registro
  sofreu rollback. É preciso conferir a entrega fora da Inbox antes de reenviar
  para evitar duplicidade. Registrar uma "intenção" antes do envio exigiria uma
  reconciliação que o volume atual não justifica.

**Números:** 468 testes, ruff e pyright limpos, e o E2E do navegador passou
nos 7 passos depois destas correções.

---

## Rodada 2 — confira as correções da sua revisão

Sua primeira revisão está em `.docs/REVISAO_INBOX_2026-09-30.md`, com
veredito 2/5. As correções estão no commit `f367cc4`. Para cada achado, confirme
que o cenário descrito não acontece mais, e aponte qualquer regressão:

| # | Achado | Correção | Teste |
|---|---|---|---|
| 1 | Resposta sai depois de perder a posse | `handoffs.lock_for_reply` (`SELECT … FOR UPDATE` com dono e estado) antes do envio; o lock vale até o commit, e `claim_by_user`/`close_by` na mesma linha esperam | `test_a_reply_goes_out_only_while_the_conversation_is_locked_as_yours`; no Postgres real, uma tomada de posse espera a resposta em andamento terminar |
| 2 | Clique antigo em "Assumir" vira "Assumir no lugar" | A rota exige `{state, owner_user_id}` que a tela mostrou (`Claim`, obrigatório); o serviço compara com o estado atual e devolve 409 se mudou; sessão antiga (só `claimed_by_id`) só é tomada com `state=claimed, owner_user_id=null` | `test_a_click_on_a_screen_that_is_out_of_date_takes_nothing`, `test_a_claim_made_the_old_way_can_be_taken_over_explicitly`, `test_claiming_must_say_what_the_screen_showed` |
| 3 | Botões antigos acionam o bot | `process_callback` checa `_held()` antes de qualquer efeito: o toque vira `(tocou no botão) <rótulo>` na conversa, e quem está com ela recebe o aviso | `test_an_old_button_tapped_while_a_person_holds_it_does_nothing`, com horário, período, cancelar, remarcar e equipe |
| 4 | Qualquer um devolve conversa alheia | `handoffs.close_by`: devolve se o pedido não tem dono ou se o dono é quem pede; no serviço, 409 com o nome do dono; na tela, o botão só aparece para o dono ou para pedido sem dono | `test_only_whoever_holds_it_can_give_it_back`, `test_a_request_nobody_has_taken_can_be_closed_by_anyone`, `test_giving_back_loses_to_someone_who_just_took_it` |
| 5 | Áudio ilegível e limite de mensagens | Com a conversa assumida: sem aviso de limite, e a mídia ilegível entra como `inbox.unreadable` e avisa o dono | `test_something_the_desk_cannot_read_goes_to_whoever_holds_it`, `test_the_message_limit_does_not_speak_for_the_bot_while_a_person_holds_it` |
| 6 | Polling esconde mensagens novas | Duas consultas: primeiro `id > after`, depois até 50 das mais recentes com `id <= after` dos últimos 10 min | No Postgres real, com 250 mensagens recentes na tela, a nova volta; uma gravada com id 3 abaixo também volta |
| 7 | Aviso duplicado ou perdido | **Parcial, com justificativa abaixo.** Saiu a segunda tentativa sem botão, que duplicava em timeout ambíguo; o botão só entra com endereço https (o Telegram recusa o resto); uma tentativa por aviso | `test_an_address_telegram_would_refuse_gets_no_button`, `test_a_notice_telegram_did_not_take_is_posted_once` |
| 8 | `needs_person` em duas linhas | Uma linha: `🙋 {nome} quer falar com uma pessoa · {canal} · {motivo}` | `test_the_request_for_a_person_is_one_line` |
| 9 | `hashchange` na mesma conversa com outra aba aberta | Só ignora se a conversa já está aberta **e** a aba Inbox está ativa | Revisão manual. O E2E do navegador passou os 7 passos depois das correções |
| — | Limite de 4096 com o nome na frente | A mensagem assinada é medida (`CHANNEL_LIMIT`) | `test_the_signed_message_must_fit_the_channel` |
| — | Comentários do repasse antigo | `core/models.py` e `core/repositories/handoffs.py` atualizados (`infra/init.sql` é a base congelada e não muda) | — |

**Onde o autor discordou.** Julgue se a justificativa se sustenta:
- **Item 7, fila persistente (outbox) para os avisos do grupo.** Não foi feita.
  Os avisos ao grupo são um atalho: a conversa aparece na Inbox de qualquer
  forma, ordenada pela última mensagem. Perder um aviso de vez em quando custa
  pouco, e uma fila com retry e estado de entrega é uma estrutura nova. O
  risco que custava caro, o aviso duplicado pela segunda tentativa, foi
  eliminado.
- **`seen` sem limite e reconstrução do DOM.** Não mudou. Hoje as conversas têm
  poucas dezenas de mensagens, e a carga inicial já é limitada a 200. Fica
  anotado para quando pesar.
- **Chamada HTTP com a transação aberta.** Isso agora é intencional na
  resposta: é o lock que impede a troca de dono durante o envio (item 1). São
  3 pessoas da equipe e o pool tem 4 conexões. Diga se enxerga um cenário em
  que isso trave o atendimento.

**Números:** 465 testes, ruff e pyright limpos, e o CI da branch roda no push.

---

**Branch:** `feat/inbox`. Revise a partir de `origin/main`, ou seja,
`git diff origin/main...feat/inbox`. A spec e o plano também entram no diff.

**Leia antes:**
- **Spec:** `docs/superpowers/specs/2026-09-29-inbox-design.md`. É a autoridade.
- **Plano:** `docs/superpowers/plans/2026-09-29-inbox.md`. Explica o como.
  Onde a implementação diverge do plano, a decisão está listada abaixo.

## O que mudou, em uma frase

A equipe deixa de responder clientes pelo Telegram, seja no grupo ou no
privado com o bot, e passa a responder por uma aba **Inbox** no `/painel`.
O grupo só recebe avisos de uma linha com o botão "Abrir no painel", e cada
contato ganha **canal** e **origem**.

## Onde está cada coisa

| Parte | Arquivos |
|---|---|
| Banco | `infra/migrations/0005_inbox.sql` |
| Leituras e reservas atômicas | `core/repositories/inbox.py`, `core/repositories/handoffs.py` (`claim_by_user`) |
| Regras (assumir, responder, devolver, avisos, envio por canal) | `core/services/inbox.py` |
| O bot deixa de repassar | `core/services/handoff.py`, `core/handlers/message.py` |
| API | `core/web/panel.py` (seção "Inbox") |
| Tela | `core/web/static/painel/inbox.js`, `index.html`, `painel.css`, `painel.js`, `auth.js` |
| Textos | `content/messages.toml` (`[inbox]`), `core/content.py` |
| Testes | `tests/test_inbox.py`, `tests/test_desk_flow.py`, `tests/test_panel.py` |

## Prioridades da revisão

Procure primeiro **o que pode chegar errado a um cliente ou vazar dado**.
Estilo e nomes vêm por último.

1. **Nada sai para o cliente sem ser de quem assumiu.** Confira em
   `inbox.reply` e nas rotas: um usuário logado consegue enviar para uma
   conversa que não é dele? E um pedido vindo de outro site (a CSRF está em
   `_same_origin`, com cookie `SameSite=Strict`)?
2. **Corrida ao assumir.** `claim_by_user` só troca de dono se o dono ainda for
   o que o painel mostrou (`IS NOT DISTINCT FROM`). Isso cobre dois cliques
   simultâneos, e também uma sessão assumida pelo fluxo antigo (só com
   `claimed_by_id`) no momento do deploy?
3. **O bot calado quando alguém assumiu.** `handle_customer_message` devolve
   `"claimed"` e o handler sai antes da triagem. Há algum caminho em que o bot
   ainda responde numa conversa assumida? Olhe em especial `process_callback`
   (botões antigos tocados pelo cliente) e mensagens de voz.
4. **Aviso de conversa nova uma vez só.** `claim_conversation_notice` roda
   antes de gravar a mensagem e depende do lock de linha do `UPDATE`. Isso
   aguenta duas mensagens simultâneas e um reprocessamento da fila (retry do
   worker)? Os chats da equipe ficam de fora?
5. **Mensagens na tela sem buracos.** `inbox_repo.messages(after_id)` devolve
   também os últimos 10 minutos, porque um id menor pode ser gravado depois,
   numa transação lenta. A tela junta por id. Existe cenário em que uma
   mensagem nunca aparece, ou aparece duplicada?
6. **XSS e vazamento.**
   - `inbox.js` usa só `textContent`?
   - A API nunca devolve `chat_id`?
   - O redirecionamento pós-login só aceita `#inbox/<dígitos>` (em `auth.js`,
     `painel.js` e `inbox.js`)?
7. **Migração segura em produção,** que já tem dados:
   - ela roda duas vezes sem erro?
   - o `CHECK` novo em `contacts.source` aceita todas as linhas existentes (hoje
     todas são `telegram`)?
   - os `REFERENCES panel_users ON DELETE SET NULL` estão corretos?
8. **O que saiu não deixou órfãos.** O repasse pelo Telegram saiu (`/assumir`,
   `/encerrar`, `handle_team_reply`, `handle_private_reply`, `echo_bot_reply` e
   os textos `handoff.card`/`dm_*`). Sobrou alguma referência, texto ou
   comentário que descreve o comportamento antigo?

## Riscos que os testes não cobrem

Estes riscos estão no plano, na seção "Review Focus". Confira cada um de
propósito:
- mensagem fora de ordem na tela (item 5 acima);
- duas pessoas clicando "Assumir" ao mesmo tempo (item 2);
- um botão com endereço que o Telegram recusa: o aviso sai sem botão em vez de
  se perder (`notify_team`);
- login expirado ao tocar no botão: a pessoa volta para a conversa;
- mensagem do cliente contada duas vezes enquanto espera uma pessoa: no estado
  "aguardando", o handoff não grava mais a mensagem, porque o fluxo normal já
  grava.

## Decisões tomadas durante a implementação

Julgue se estão certas:
- **`STAGE_LABELS` mantido.** A reescrita do `handoff.py` no plano apagava
  `STAGE_LABELS`, que `core/services/secretarial.py` importa. Foi mantido.
- **Listener de `hashchange` no `inbox.js`.** Abrir `/painel#inbox/<id>` numa
  aba em que o painel já está carregado só muda o fragmento e não abria nada.
- **Layout:** os filtros da lista quebram linha, e no celular os botões da
  conversa ficam numa linha própria. Veio das capturas de tela do teste no
  navegador.
- **Constante `_S_COLUMNS` em `handoffs.py`,** em vez de repetir a lista de
  colunas com prefixo em cada query.
- **O teste de intake de pedido de pessoa** agora confere o aviso de uma linha
  e os dados salvos no contato, não um card com o telefone.

## Fora do escopo

Não aponte como falta:
- anexos, áudio e imagem enviados pelo painel;
- "não lida" por pessoa;
- notificação do navegador;
- a janela de 24h do WhatsApp;
- a origem pelo `referral` da Meta.

Tudo isso está listado como fora desta etapa na spec.

## Como rodar

```bash
.venv/bin/python -m pytest -q -p no:warnings   # 441 testes
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright
```

**Postgres local:** a porta 5432 do host é de **outro** projeto
(`ai-agents-postgres`). Não use essa porta. O Postgres deste projeto é o
container `ai-agents-reception-postgres-1`, sem porta publicada. Para rodar
algo contra ele, use o IP do container:

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ai-agents-reception-postgres-1
DB_HOST=<ip> .venv/bin/python -m cli.migrate
```

O container `reception-bot` local fica **parado** de propósito, porque a
produção roda na VPS. Não o suba, e não registre o webhook do Telegram
localmente.

## Como entregar

Uma lista de achados, do mais grave para o menos grave. Cada achado traz:
- arquivo e linha;
- o que acontece de errado, com um cenário concreto (entrada → resultado);
- a correção sugerida.

Separe o que é bug ou risco de segurança do que é sugestão de melhoria. Não
altere o código. O autor corrige com um teste que falhe primeiro.
