# Escolher o dia, depois o horário — especificação

Data: 15/10/2026 · Status: aprovada pelo Codex (5/5, rodada 9); aguardando merge do Paulo · Revisada depois da
rodadas 1 a 8 do Codex (`.docs/REVISAO_SPEC_ESCOLHER_DIA_RODADA_{1..8}.md`)

## Objetivo

Hoje o bot oferece 3 horários livres, espaçados, e o cliente escolhe um. Quando
nenhum serve, ele precisa adivinhar outro dia. Com este recurso ligado, o
cliente vê a disponibilidade e escolhe:

1. primeiro o dia, numa lista só com os dias que têm horário livre;
2. depois o horário, numa lista com os horários livres daquele dia.

Ele **nunca vê a agenda**: só horários livres, sem nome, título ou link de
ninguém.

Junto vem um **limite de agendamentos por cliente**: no máximo N conversas
futuras marcadas ao mesmo tempo, com N configurável.

Os dois ficam desligados por padrão. Com eles desligados, nada muda.

Pronto quando:

- com `BOOKING_MODE=choose_day`, o cliente agenda escolhendo dia e horário, no
  WhatsApp e no Telegram, por toque ou por texto;
- com `MAX_UPCOMING_APPOINTMENTS=1`, um segundo agendamento é recusado, com a
  oferta de remarcar o primeiro, e nenhum caminho (botão velho, remarcação
  antiga, dois toques seguidos) passa do limite;
- com os dois desligados, os testes de agendamento de hoje passam sem mudança.

## Decisões já tomadas com o Paulo (15/10/2026)

1. **Disponibilidade, não a agenda.** Compartilhar a agenda da clínica, mesmo
   só para leitura, mostraria os outros pacientes.
2. **Dentro da conversa (opção 1)**, com listas do WhatsApp e botões do
   Telegram. Uma página de agendamento com link próprio fica para depois, se
   precisar.
3. **Dia, depois horário** (sugestão do Paulo), em vez de uma lista única de
   horários ou de um botão "ver mais".
4. **A primeira mensagem já traz os dias (opção A).** Sem sugestão do horário
   mais próximo junto.
5. **O limite vale para todo agendamento (opção A)**, nos dois modos, e não só
   no fluxo novo.
6. **Por empresa, desligado por padrão.**
7. **O limite é por contato, ou seja, por canal** (aprovado pelo Paulo em
   15/10/2026). O mesmo cliente no Telegram e no WhatsApp tem um limite em cada.

## O que já existe (conferido no código em 15/10/2026)

- **Quem cria agendamento:** só o bot, por `scheduling.book`
  (`core/services/scheduling.py:217`). A equipe não cria agendamentos no
  sistema: o que ela marca direto na agenda não entra na tabela.
- **`booked_via`:** existe no banco (`init.sql:108`, padrão `'telegram'`), mas o
  código não grava. Um agendamento pelo WhatsApp fica como `telegram`.
- **As ofertas** (`core/repositories/offers.py`):
  - `save` grava uma `Offer` e **substitui** a pendente anterior do mesmo chat
    (`superseded`). Só a última vale.
  - `pending_for_chat` devolve a pendente; `claim`/`release` marcam `taken` e
    devolvem; `expire` encerra.
- **O toque num horário** é `slot:<offer_id>:<índice>` (`message.py:848`). O
  caminho de agendamento já cuida de:
  - pegar um botão de uma oferta velha;
  - o "esse horário acabou de ser ocupado";
  - a trava do banco contra dois clientes no mesmo horário;
  - a confirmação e o link da reunião.
- **Remarcar** (`take_offer` → `_replace`, `scheduling.py:338–380`): marca o
  novo e só depois cancela o antigo. Se o cancelamento falhar na agenda, hoje o
  erro vai para o log e o cliente fica com duas conversas.
- **Os pontos de entrada que pedem horário:**
  - `_handle_booking` (`message.py:264`) e `_resume` (`:244`) perguntam o
    período antes de chamar `_offer`, se o cliente não disse;
  - `_handle_reschedule` (`:271`): com mais de uma conversa, pergunta qual; com
    uma, pergunta o período;
  - os toques `book:start` e `appt:move:<id>` (`:832`, `:836`): perguntam o
    período;
  - o toque `period:<período>:<dia>:<id>` (`:813`) chama `_offer`;
  - `_book_picked_slot` (`:320`), num horário ocupado, chama `_offer` de novo,
    **sem** o dia, o período nem a conversa sendo remarcada.
- **A espera por um dia escrito:** `pending_action` em `day:<id>` ou
  `day2:<id>` (`_handle_day_reply`, `message.py:197`), gravada com `set_waiting`,
  que não marca os dados de contato como pedidos.
- **A leitura de um horário escrito** (`slot_choice.interpret`,
  `slot_choice.py:257`): "10h" sem minutos casa com 10h e 10h30 e vira escolha
  ambígua.
- **Os horários:**
  - `offer_times` (`scheduling.py:55`) junta poucos candidatos e espalha 3; ele
    não lista o horizonte inteiro;
  - `free_slots` (`slots.py:49`) dá os inícios livres numa grade de 30 min,
    dentro do expediente;
  - configurações (valores padrão): `booking_horizon_days = 30`,
    `slot_step_minutes = 30`, `appointment_duration_minutes = 30`,
    `minimum_notice_minutes = 120`, o expediente e os dias úteis;
  - **as agendas consultadas** vêm de `calendars_in_scope`
    (`scheduling.py:32`), que prioriza as agendas da equipe
    (`STAFF_CALENDAR_IDS`) e junta os ocupados de todas. **A agenda onde o
    evento é criado** é outra coisa: a da `Offer` (`reception_calendar_id`, se
    não houver outra).
- **No WhatsApp:**
  - até 3 opções viram botões, e mais de 3 viram lista (`channels.py:118`);
  - o título é cortado em 24 caracteres na lista e em 20 no botão;
  - a lista não tem descrição por linha, e o botão é sempre "Escolher".

## Configuração

No `.env` de cada empresa:

| Variável | Valores | Padrão |
|---|---|---|
| `BOOKING_MODE` | `suggest` (3 sugestões, como hoje) ou `choose_day` | `suggest` |
| `MAX_UPCOMING_APPOINTMENTS` | `0` (sem limite) ou um número de 1 a 5 | `0` |

Um valor inválido impede o bot de subir, com mensagem clara, como as outras
configurações.

## A disponibilidade

Uma função nova, `available_starts(start_day, end_day, period, calendars)`, lista todos os
inícios livres entre dois dias, sem espalhar nem cortar em 3. Ela usa
**exatamente** as regras de `offer_times`, lidas da configuração:

- **as mesmas agendas consultadas** (`calendars_in_scope`) e a **união** dos
  ocupados de todas. Um horário só aparece se todas estão livres, que é o que o
  `is_free` confere de novo na confirmação;
- a duração (`appointment_duration_minutes`), a grade (`slot_step_minutes`), o
  expediente e `is_workday`;
- a antecedência mínima: nada antes de agora + `minimum_notice_minutes`.

**O horizonte tem origem fixa em hoje:** do dia de hoje até hoje +
`booking_horizon_days`, inclusive, no fuso do escritório. Ele **nunca anda** com
a página nem com o dia pedido. Um horário só entra se começa e termina dentro do
expediente daquele dia.

Os números citados nesta spec (30 dias, 30 min, 120 min) são os valores padrão
dessas configurações, não constantes do recurso.

## O fluxo com `choose_day`

### Pontos de entrada

Com `choose_day`, **a pergunta do período sai**. A lista de dias já diz quando
um dia só tem manhã ou só tarde. Se o cliente disse um período ("de manhã"),
ele filtra a lista.

| Ponto de entrada | Hoje | Com `choose_day` |
|---|---|---|
| `_handle_booking`, `_resume` | período, depois `_offer` | lista de dias (com o período dito, se houver) |
| `_handle_reschedule` com uma conversa | período | lista de dias, remarcando essa conversa |
| `_handle_reschedule` com várias | "qual delas?" | igual: "qual delas?", e depois a lista de dias |
| toque `book:start` | período | lista de dias |
| toque `appt:move:<id>` | período | lista de dias, remarcando `<id>` |
| toque `period:` (de uma mensagem antiga) | `_offer` | lista de dias com aquele período |
| o cliente escreve um dia ("quinta de manhã") | `_offer` daquele dia | **direto a lista de horários de quinta**, só manhã |
| horário ocupado, recusado ou não entendido | `_offer` sem contexto | lista de dias **com** o período e a remarcação de antes |

### O estado da navegação

O que viaja em cada toque, e o que fica guardado para o texto:

- **Nos toques:**
  - `day:<AAAA-MM-DD>:<p>:<r>`: os horários de um dia;
  - `days:<AAAA-MM-DD>:<p>:<r>`: a lista de dias a partir de uma data;
  - `more:<AAAA-MM-DD>:<HHMM>:<p>:<r>`: os horários do dia depois de HHMM.

  `<p>` é `m`, `t` ou `-` (manhã, tarde, sem filtro). `<r>` é o id da conversa
  sendo remarcada, ou `-`. A hora vai como `HHMM`, sem `:`, que é o separador.
  O pior caso cabe nos 64 bytes do Telegram.
- **Para o texto:** enquanto a lista de dias está aberta, o contato fica com
  `pending_action = pickday:<p>:<r>` (via `set_waiting`).
  - **A resposta escrita é lida por um intérprete só,
    `slot_choice.read_day_reply(text) -> DayReply`**, usado aqui e depois da
    triagem. Ele **não tenta entender frases mistas**. Cada regra que tentava
    (rodadas 3 a 6 do Codex) abria outro caso. Em vez de adivinhar, ele separa o
    que é claro do que não é:
    0. **O texto passa pela mesma normalização de `slot_choice`** (acentos,
       caixa e a palavra isolada `n` virando `nao`, como em `requested_day` e
       `interpret` hoje), guardando antes a posição dos dias.
    1. **Ausência de preferência sai primeiro.** "sem preferência", "não tenho
       preferência" (de horário, de período), "qualquer horário" e "tanto faz"
       viram `period = any` e são retirados do texto.
    2. **Cancelar ou sair** ("cancelar", "cancela", "desmarcar", "desistir",
       "sair", "parar", "esquece"): `kind = cancel_or_exit`.
    3. **Com algum dia ou período e com negação ("não", "nao", "nem", "n") ou
       "sim"** no que sobrou: `kind = mixed`, sem dias nem período. O intérprete
       não decide qual dia nem qual período o cliente quis. Isso vale para "não
       posso de manhã" tanto quanto para "quinta não posso".
    4. **Com algum dia, sem negação nem "sim":** `kind = clear`, e os dias
       citados vão para `days`. Um período dito ("de manhã") vai para `period`.
    5. **Sem dia, mas com um período, e sem negação nem "sim"** (o `any` do
       passo 1, ou "de manhã", "de tarde", "à tarde"): `kind = period_only`.
    6. **O resto:** `kind = none`.

    A ordem decide: cada mensagem cai na primeira regra que serve.

    | Frase | `kind` | `days` | `period` |
    |---|---|---|---|
    | "quinta" | `clear` | quinta | — |
    | "quinta de manhã" | `clear` | quinta | manhã |
    | "quinta ou sexta" | `clear` | quinta, sexta | — |
    | "quinta, não tenho preferência de horário" | `clear` | quinta | `any` |
    | "na quinta não tenho preferência de horário" | `clear` | quinta | `any` |
    | "quinta, qualquer horário" | `clear` | quinta | `any` |
    | "quinta não posso" | `mixed` | — | — |
    | "não posso quinta e sexta" | `mixed` | — | — |
    | "quinta não, sexta sim" | `mixed` | — | — |
    | "quinta não e sexta sim" | `mixed` | — | — |
    | "não posso quinta, mas sexta" | `mixed` | — | — |
    | "quinta n posso" | `mixed` | — | — |
    | "quinta n e sexta sim" | `mixed` | — | — |
    | "sem preferência de horário" | `period_only` | — | `any` |
    | "de tarde" | `period_only` | — | tarde |
    | "não posso de manhã" | `mixed` | — | — |
    | "n posso de tarde" | `mixed` | — | — |
    | "quero cancelar a de quinta" | `cancel_or_exit` | — | — |
    | "obrigado" | `none` | — | — |
  - **O que acontece com a lista de dias aberta:**
    - `clear` com um dia: os horários daquele dia, com o `<r>` da espera e o
      período de `period` (ou o `<p>` da espera). **A remarcação nunca se perde
      aqui;**
    - `clear` com mais de um dia: a lista de dias **só com os citados** que têm
      horário livre, com "Qual desses dias?" e a mesma espera. Se só um deles
      está livre, vai direto aos horários dele;
    - `mixed`: "Para não errar, toque no dia na lista." e a lista de dias de
      novo, com a mesma espera (o mesmo `<p>` e `<r>`). Custa um toque a mais
      num caso raro, e nunca agenda o dia que o cliente recusou;
    - `period_only`: a lista de dias de novo, com o período novo e o mesmo
      `<r>`. A espera passa a `pickday:<período novo>:<r>`;
    - `cancel_or_exit` e `none`: a espera é limpa e a mensagem segue a triagem,
      que já trata cancelar com confirmação.
  - **Depois da triagem**, quando ela devolve `booking` ou `reschedule`, o dia
    preferido sai de `read_day_reply`, e não mais de `requested_day(body)`
    (`message.py:696`):
    - **só um `clear` com um dia vira dia preferido;**
    - `clear` com mais de um dia: a entrada mostra a lista restrita;
    - `mixed`: nenhum dia preferido; a entrada mostra a lista de dias inteira.

    Assim, um dia recusado nunca vira o dia preferido, em nenhum dos dois modos.
  - A espera tem precedência sobre a coleta de dados de contato, como a espera
    `day:` de hoje.
- **A lista de horários** é uma `Offer`, como hoje, com o `replaces_id` e o
  período guardados nela. O período vai num campo novo, `offers.period`.

### A validade de cada lista

- **Uma lista de dias nunca é "velha".** Ela é recalculada a cada toque, sem
  estado guardado. Um toque num dia de uma lista antiga:
  - recalcula os horários daquele dia, se ele ainda está no horizonte e não
    passou;
  - senão, manda a lista de dias de hoje.
- **Uma lista de horários vale só enquanto for a última `Offer` pendente do
  chat.**
  - Tocar num dia, em "Outro dia" ou em "Próximos dias" **encerra a `Offer`
    pendente** (`expire`).
  - Por isso, um horário de uma lista anterior nunca agenda. Cai no
    `scheduling.slot_taken` de hoje.
  - Abrir os horários de um dia grava uma `Offer` nova, que substitui a
    anterior (`save`).

### A lista de dias

- **Quais dias:** os dias do horizonte com pelo menos um início livre, dentro do
  período `<p>`. Um dia cheio não aparece.
- **Até 9 dias por lista.** Cada linha tem:
  - **título:** "Terça, 21/10";
  - **descrição:** "6 horários", "1 horário", "só de manhã" ou "só à tarde".
- **A 10ª linha, "Próximos dias"**, aparece só se houver dias livres depois do
  último mostrado, dentro do horizonte.
- **Mensagem:** "Escolha o dia da conversa:", com o botão "Escolher dia".
- **Sem nenhum dia livre no horizonte:** `scheduling.no_slots`, como hoje.

### A lista de horários do dia

- **Todos os inícios livres do dia**, dentro de `<p>`. O espaçamento de 2 h
  **não vale** aqui.
- **Cada página tem as duas saídas**, para o cliente nunca precisar percorrer o
  dia inteiro:
  - quando o dia tem mais horários do que cabem: **8 horários**, "Mais horários
    neste dia" e "Outro dia";
  - quando cabem todos: até **9 horários** e "Outro dia".
- **Título da linha:** o horário ("9h", "9h30").
- **Mensagem:** "Horários de terça, 21/10:", com o botão "Escolher horário".
- **O dia lotou entre a lista de dias e o toque:** "Esse dia acabou de lotar.",
  e a lista de dias de novo.

### Escrever um horário

Com uma lista de horários aberta, `slot_choice.interpret` lê o texto **só contra
a página atual** (a `Offer` pendente).

- **Uma hora sem minutos ("às 10h") vale 10h00**, se 10h00 está na página.
- Se só 10h30 está, é uma escolha ambígua, e o bot pergunta, como hoje.
- A mudança vale nos dois modos. No modo `suggest`, as sugestões ficam a 2 h
  uma da outra, então "10h" nunca casa com duas delas.

## O limite de agendamentos

Vale nos dois modos.

### O que conta

- As conversas **do contato** com `status = 'confirmed'` e `starts_at > agora`.
- Como só o bot cria agendamentos, isso é tudo o que o cliente marcou pelo bot.
- **O limite é por contato, ou seja, por conversa num canal.** O sistema
  identifica o contato pelo `chat_id`: o mesmo cliente no Telegram e no WhatsApp
  são dois contatos, e cada um tem o seu limite. Unir as duas identidades
  exigiria uma vinculação verificada, que fica fora do escopo. Na prática, os
  clientes estão quase todos no WhatsApp.
- `booked_via` passa a ser gravado com o canal (`telegram` ou `whatsapp`), para
  relatório. As linhas antigas ficam como estão.
- Se um dia a equipe criar agendamentos pelo sistema, eles gravam
  `booked_via = 'team'` e ficam fora da conta. Isso está fora do escopo agora.

### Antes de oferecer

Em qualquer ponto de entrada de **agendamento novo** (não de remarcação), se o
contato já atingiu o limite:

- **com uma conversa marcada:**
  - mensagem: "Você já tem uma conversa marcada para {quando}. Quer remarcar
    essa?";
  - botões "Remarcar" (`appt:move:<id>`, o caminho que já existe) e "Manter"
    (`limit:keep`, que responde "Combinado, fica mantida.");
- **com duas ou mais:**
  - mensagem: "Você já tem {n} conversas marcadas: {lista}. Quer remarcar uma
    delas?";
  - os botões do "qual delas?" de hoje (`_choose_keyboard(..., "move")`), mais
    "Manter".

### Na hora de agendar

Em `take_offer`, sob uma **trava por contato** (`pg_advisory_xact_lock` no id do
contato), pega antes de qualquer leitura decisiva e mantida até a gravação local
do agendamento:

1. **Se a `Offer` é uma remarcação**, a conversa substituída precisa ser deste
   contato, estar `confirmed` e ser futura.
   - Se não for (já remarcada, cancelada ou passada), o agendamento é recusado
     com o resultado `stale_reschedule`: "Essa remarcação já foi feita." e a
     `Offer` é encerrada.
   - Isso também fecha um toque `period:` ou `appt:move:` antigo, que carrega
     uma conversa já remarcada.
2. **A conta:** as conversas futuras confirmadas, **menos a substituída**,
   quando ela é válida. Se a conta já está no limite, o agendamento é recusado
   com o resultado `limit`:
   - a mensagem e os botões de "Antes de oferecer";
   - a `Offer` encerrada;
   - nada é marcado na agenda.
3. Só então o caminho de hoje: `claim`, marcar na agenda, gravar.

A trava cobre a validação, a conta e a gravação local, e é solta no fim da
transação. Dois toques seguidos, em listas diferentes, esperam um pelo outro.

**`BookingResult`** ganha os resultados `limit` e `stale_reschedule`, ao lado de
`booked`, `taken` e `failed`.

### Quando o cancelamento da conversa antiga falha

O cliente já recebeu a confirmação da nova, e a ordem "nova primeiro, antiga
depois" continua. Se a agenda recusar o cancelamento da antiga:

- **a linha local da antiga passa a `cancelled` mesmo assim**:
  - o cliente não recebe lembrete dela;
  - ela não conta no limite;
  - ela some da lista de "suas conversas";
- **a equipe recebe um aviso** no grupo (o mesmo canal dos outros avisos): "Não
  consegui cancelar na agenda a conversa de {quando} de {cliente}, que foi
  remarcada para {quando_nova}. Apague o evento à mão.";
- a auditoria já registra a falha, como hoje.

Assim, o limite e o que o cliente vê ficam certos, e o que sobra na agenda é um
evento que a equipe apaga.

### O resto

- **Remarcar não conta como agendamento novo.**
- **O modo teste segue a mesma regra**, para dar para testar com `/teste`.
- **`MAX_UPCOMING_APPOINTMENTS=0`:** nenhuma conta, nenhuma trava a mais, como
  hoje. As validações de remarcação do passo 1 valem sempre, porque fecham um
  defeito que existe hoje.

## Os canais

O teclado que os handlers montam passa a ser uma estrutura do próprio projeto,
renderizada por canal:

- **`Choice(label, data, description=None)`**, e um **`Menu(choices,
  list_button=None)`**.
- **No WhatsApp:**
  - uma lista de dias ou de horários **sempre vira lista**, mesmo com 1 a 3
    opções, para mostrar a descrição e manter a mesma cara;
  - `description` vira a `description` da linha;
  - `list_button` vira o botão da lista;
  - os outros teclados (até 3 opções, sem `list_button`) continuam virando
    botões, como hoje.
- **No Telegram:** um botão por opção, com o texto `label · description` quando
  há descrição.
- **A validação de conteúdo** confere os rótulos **como saem renderizados**:
  - título da linha: até 24 caracteres;
  - descrição: até 72;
  - botão da lista: até 20;
  - botões comuns: até 20.

  Com a data mais longa ("Quarta, 31/12") e a descrição mais longa.

## Textos

Em `content/messages.toml`, numa seção nova, para cada empresa ajustar:

- `days_prompt` = "Escolha o dia da conversa:"
- `days_button` = "Escolher dia"
- `days_count` = "{n} horários" e `days_one` = "1 horário"
- `days_only_morning` = "só de manhã" e `days_only_afternoon` = "só à tarde"
- `days_next` = "Próximos dias"
- `times_prompt` = "Horários de {dia_semana}, {data}:"
- `times_button` = "Escolher horário"
- `times_more` = "Mais horários neste dia"
- `times_other_day` = "Outro dia"
- `day_full` = "Esse dia acabou de lotar."
- `limit_one` = "Você já tem uma conversa marcada para {quando}. Quer remarcar essa?"
- `limit_many` = "Você já tem {n} conversas marcadas: {lista}. Quer remarcar uma delas?"
- `limit_reschedule` = "Remarcar", `limit_keep` = "Manter", `limit_kept` = "Combinado, fica mantida."
- `stale_reschedule` = "Essa remarcação já foi feita."
- `team_cancel_failed` = "Não consegui cancelar na agenda a conversa de {quando} de {cliente}, que foi remarcada para {quando_nova}. Apague o evento à mão."

## Erros e casos de borda

| Situação | O que acontece |
|---|---|
| Nenhum dia livre em 30 dias | `scheduling.no_slots`, como hoje. |
| O dia lotou entre a lista e o toque | "Esse dia acabou de lotar." e a lista de dias. |
| Toque num horário de uma lista que não é a última | `scheduling.slot_taken`, como hoje. |
| Toque num dia de uma lista antiga, já fora do horizonte ou no passado | A lista de dias de hoje. |
| Toque antigo que carrega uma conversa já remarcada | `stale_reschedule`; nada é marcado. |
| Limite atingido entre a oferta e o toque | `limit`; nada é marcado. |
| A agenda recusa cancelar a conversa antiga | A antiga fica `cancelled` no sistema, e a equipe é avisada. |
| A agenda (Google ou Microsoft) não responde ao listar | A mensagem de hoje para falha da agenda. |

## Testes

1. **Disponibilidade:**
   - `available_starts` usa `calendars_in_scope`: com `STAFF_CALENDAR_IDS`, um
     horário em que a recepção está livre mas alguém da equipe está ocupado não
     aparece;
   - respeita a duração, a grade (`slot_step_minutes`, inclusive com 60), o
     expediente, os dias úteis e a antecedência configurada;
   - o horizonte vai de hoje a hoje + `booking_horizon_days` (testado com 7 e
     com 30), inclusive, no fuso do escritório, e não anda com a página nem com
     o dia pedido.
2. **Dias:**
   - só dias com horário livre;
   - até 9;
   - "Próximos dias" só quando há mais, e para no horizonte;
   - o período filtra, e a descrição diz "só de manhã" quando é o caso.
3. **Horários:**
   - o dia inteiro, sem o espaçamento de 2 h;
   - com mais de 9: 8, "Mais horários" e "Outro dia" em toda página, e a
     segunda começa depois do último mostrado;
   - com até 9: todos e "Outro dia";
   - calculados no toque;
   - um dia que lotou volta aos dias.
4. **Pontos de entrada:** cada linha da tabela "Pontos de entrada" com
   `choose_day`, incluindo o horário ocupado, recusado e não entendido, que
   voltam aos dias com o período e a remarcação.
5. **Validade:**
   - depois de "Outro dia", um horário da lista anterior não agenda;
   - um dia de uma lista antiga recalcula;
   - um toque `period:` antigo, de uma remarcação já feita, dá
     `stale_reschedule`.
6. **Texto:**
   - "quinta" com a lista de dias aberta mostra os horários de quinta, com o
     período e a remarcação;
   - `read_day_reply` com cada linha da tabela da spec;
   - com a lista de dias aberta:
     - "quinta, não tenho preferência de horário" abre quinta sem filtro, com a
       remarcação preservada (com duas conversas futuras, não pergunta de novo
       qual);
     - "quinta ou sexta" com as duas livres mostra a lista só com elas; com só
       sexta livre, vai direto a sexta;
     - "quinta não posso" e "quinta não, sexta sim" respondem "Para não errar,
       toque no dia na lista." e mandam os dias de novo, com a remarcação
       preservada;
     - "quinta n posso" responde como "quinta não posso";
     - "não posso de manhã" e "n posso de tarde" respondem "Para não errar,
       toque no dia na lista." e mandam os dias de novo, com o mesmo período e a
       remarcação de antes (nunca filtrando pelo período recusado);
     - "de tarde" e "sem preferência de horário" mandam os dias de novo com o
       período novo, sem perder a remarcação (com duas conversas futuras, não
       pergunta de novo qual);
     - "obrigado" e "quero cancelar a de quinta" limpam a espera e seguem a
       triagem;
   - "quero remarcar, mas quinta não posso", com a triagem devolvendo
     `reschedule` ou `booking`: nenhum dia preferido, a lista inteira;
   - "às 10h" com 10h e 10h30 na página agenda 10h;
   - com só 10h30, é ambíguo;
   - um horário de outra página não casa.
7. **Limite:**
   - com 1, uma nova oferta vira `limit_one` com "Remarcar" e "Manter";
   - com 2 e duas marcadas, `limit_many`;
   - dois agendamentos simultâneos em Postgres real, em listas diferentes, não
     passam do limite;
   - remarcar não conta;
   - com 0, nada muda;
   - vale no modo `suggest` também;
   - `booked_via` grava o canal.
8. **Cancelamento que falha:** a antiga fica `cancelled`, a equipe recebe o aviso,
   e a conta do limite não a inclui.
9. **Canais:**
   - o WhatsApp monta lista válida mesmo com 1 opção, com descrição e o botão
     certo, e os rótulos renderizados cabem nos limites;
   - os outros teclados continuam como hoje;
   - o Telegram monta os mesmos botões com "label · descrição";
   - todo `callback_data` cabe em 64 bytes no pior caso.
10. **Configuração:** valor inválido em qualquer das duas variáveis impede a
    subida.
11. **Modo desligado:** com `suggest` e limite 0, os testes de agendamento de
    hoje passam sem mudança, exceto os que cobrem os dois defeitos de hoje que
    esta spec fecha:
    - remarcação antiga que agenda de novo;
    - o "10h" ambíguo.

## Dados

Uma migração aditiva (o número é o próximo livre quando o plano for escrito):

- `offers.period VARCHAR(10)`: o período da lista de horários.

`booked_via` já existe.

## Fora do escopo

- Uma página de agendamento com link (opção 2). Fica para depois, se a lista
  não bastar.
- **Escolher especialidade e profissional antes do dia** (pedido do Paulo para
  o futuro, por exemplo numa clínica: especialidade → médico → a agenda dele).
  Agora o fluxo usa as agendas de `calendars_in_scope`, como hoje. **O desenho
  deixa a porta aberta:**
  - `available_starts` e as listas recebem o conjunto de agendas como
    parâmetro, e não leem uma configuração global por dentro;
  - a `Offer` já guarda `calendar_id` e `staff_id`;
  - a tabela `staff` já tem `specialties` e `calendar_id`.

  Um passo "especialidade → profissional" na frente só estreita as agendas e
  carrega a escolha nos toques.
- Mudar o espaçamento das 3 sugestões do modo `suggest`.
- Agendamentos criados pela equipe no sistema (`booked_via = 'team'`).
