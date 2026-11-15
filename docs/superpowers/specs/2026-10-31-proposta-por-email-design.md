# Proposta por e-mail, sem reunião — design

Data: 2026-10-31 · Status: aguardando revisão

## Objetivo

Muita gente desiste da conversa de 30 minutos porque não quer marcar uma
reunião. O atendimento passa a oferecer duas opções: a conversa com um
especialista (consultoria, diagnóstico melhor) ou responder algumas perguntas
ali mesmo e receber a proposta por e-mail, preparada pela equipe, em até 2 dias
úteis.

Sucesso:
- quem não quer reunião sai da conversa com o pedido de proposta registrado, e
  a equipe recebe um resumo pronto para montar a proposta, sem precisar
  perguntar de novo;
- nada no resumo é algo que a pessoa não disse ou não conferiu.

## Fora de escopo

- O agente não dá preço, não negocia e não envia a proposta: quem envia é a
  equipe. A venda autônoma continua no `docs/ROADMAP.md`.
- Faixa de orçamento: não é perguntada (decisão do dono: a resposta ancora a
  proposta para baixo). No lugar, o custo do problema hoje.
- Revisão da planilha de custo (`content/pricing.toml`): próximo projeto, depois
  deste. As respostas coletadas aqui já servem de entrada para ela.

## Decisões tomadas

1. **Roteiro fixo conduzido pelo sistema** (não conversa livre pela IA): ordem,
   término e resumo decididos por código; respostas guardadas como escritas.
2. **Uma leitura da IA no início** preenche o que a pessoa já contou (nome,
   empresa, ramo, cargo, o que quer resolver); o resto fica em branco.
3. **Conferência pela pessoa** antes de enviar: o resumo é mostrado com
   "Está certo" e "Corrigir".
4. **E-mail obrigatório**, telefone opcional.
5. **Prazo prometido:** "em até 2 dias úteis".
6. **Funil:** o card fica em "respondido" com a marca "proposta solicitada";
   mover para "proposta" continua sendo decisão da equipe.
7. **Abandono:** respostas parciais guardadas; após 24 h sem resposta, a equipe
   recebe o resumo parcial marcado "incompleto", uma vez.
8. **Site** é oferecido como serviço complementar **com custo próprio**
   (design e desenvolvimento), cotado como item da proposta. O agente não
   informa valor.
9. **App do painel** não é cobrado à parte: está incluso.

## Fluxo da conversa

### Entrada

- Quando a pessoa pede proposta, pergunta preço ou mostra interesse em
  contratar, o agente oferece dois botões: "Conversar 30 min com um
  especialista" e "Receber proposta por e-mail".
- A triagem ganha a intenção `proposal` ("me manda uma proposta", "quanto
  custa", "quero contratar"). Uma pergunta de preço que hoje cai em FAQ passa a
  oferecer as duas opções.
- "Conversar 30 min" segue o fluxo de agendamento que já existe.

### Questionário

Uma pergunta por mensagem, texto em `messages.toml` (`[proposal]`), cada uma
com três botões:

- "Pular" (`prop:skip`): avança sem resposta;
- "Prefiro conversar" (`prop:stop`): encerra o questionário (status cancelado) e
  segue para os horários;
- "Falar com a equipe" (`prop:team`, texto `proposal.button_team`): pausa o
  questionário (respostas guardadas, `step` vazio) e começa o mesmo
  encaminhamento à equipe do botão "Falar com a equipe" que já existe (pede
  e-mail e telefone antes, se faltarem e nunca foram pedidos).

Uma mensagem digitada enquanto há pergunta pendente é a resposta, guardada como
escrita, exceto:

- termina em "?": é uma dúvida (ver Interações);
- há horários oferecidos ainda valendo (`offers` pendente): a mensagem vai para
  a oferta, não para o questionário;
- é um pedido de pessoa por frase fixa, lido por código (sem IA), sem
  acentos e sem diferença de maiúsculas — "falar com um atendente / uma pessoa
  / alguém / a equipe", "quero um atendente / humano", "pessoa de verdade",
  "atendimento humano": pausa o questionário e encaminha à equipe como o botão.
  As frases têm forma de pedido de propósito: "uma atendente o dia todo" é
  resposta, não pedido.

| # | chave | pergunta (sentido) |
|---|---|---|
| 1 | `quem` | nome, empresa, ramo e cargo |
| 2 | `desafio` | o que mais toma tempo hoje ou o que quer resolver — com exemplos: atendimento e agenda, documentos, cobrança, relatórios, obra, jurídico, site |
| 3 | `custo_hoje` | quanto isso custa hoje: pessoas ou horas por semana |
| 4 | `canais` | por onde os clientes chegam e quantas conversas por mês, mais ou menos |
| 5 | `ferramentas` | ferramentas que já usam: Google ou Microsoft, CRM, ERP, planilhas |
| 6 | `usuarios` | quantas pessoas da equipe usariam |
| 7 | `prazo` | urgência ou prazo |

O e-mail (8) é pedido pelo pedido de contato que já existe
(`_ask_details`), só se faltar.

### Preenchimento prévio

Ao começar, uma chamada de IA lê a conversa até ali e devolve JSON com apenas
estas chaves, cada uma texto ou `null`: `nome`, `empresa`, `ramo`, `cargo`,
`desafio`. Só o que foi dito; na dúvida, `null`. Valores que só dizem "não sei"
("null", "none", "não informado", "n/a", "-", vazio; sem diferença de
acentos e maiúsculas) são descartados como se fossem `null`. Uma pergunta cujos campos
estão todos preenchidos é pulada. Falha da IA = nada preenchido, o
questionário segue normal.

### Conferência e envio

- No fim, o agente mostra o resumo ("Vou enviar isto para a equipe: …"),
  com o que veio do preenchimento prévio e as respostas.
- "Está certo": status enviado; mensagem final: "Pronto! A equipe vai preparar
  sua proposta e enviar para o seu e-mail em até 2 dias úteis."
- "Corrigir": o agente pergunta qual item corrigir e repete só aquela pergunta.

### O que a equipe recebe

- Aviso no grupo e push no app, pelo mesmo caminho de hoje
  (`inbox.notify_team`, `inbox.push_to_team`): "📝 {nome} pediu proposta por
  e-mail · {empresa}", com o botão que abre a conversa na Inbox. Sem empresa
  conhecida, o trecho " · {empresa}" sai inteiro.
- No painel, o card do contato ganha a seção "Proposta solicitada" com as
  respostas como escritas, a data e o status (enviado ou incompleto), e o selo
  "proposta solicitada" na lista e no funil.

### Aba "Pedidos de proposta" no painel e no app

Uma aba nova no painel (e no app, que é o mesmo painel), ao lado das
existentes, com a lista dos pedidos:

- Cada linha: nome, empresa, ramo, data do pedido, prazo (data do pedido + 2
  dias úteis, pelos dias de expediente configurados) e status.
- Status: **novo** (enviado pela pessoa, proposta ainda não enviada),
  **incompleto** (abandono após 24 h), **proposta enviada**.
- Filtros por status; os novos e incompletos aparecem primeiro, ordenados pelo
  prazo; prazo vencido ou que vence hoje fica destacado.
- Tocar numa linha abre o pedido: as respostas como escritas, o que veio do
  preenchimento prévio, e-mail e telefone, e o botão para abrir a conversa na
  Inbox.
- Botão **"Marcar proposta como enviada"**: registra quem marcou e quando, e
  move o card do funil para "proposta" (é a equipe que decide, como hoje).
- Questionários ainda em andamento não aparecem na aba; aparecem quando são
  enviados ou viram incompletos.
- Mesmo acesso do painel: quem entra no painel vê a aba.

### Abandono

Uma varredura periódica, no mesmo padrão do worker de devolução de conversas
(`run_handoff_timeout_worker`), envia à equipe, uma vez, os questionários em
andamento sem resposta há 24 h, com o status incompleto. Se a pessoa voltar a
responder depois, o questionário continua de onde parou.

### Interações com o que já existe

- Uma mensagem fora do roteiro durante o questionário (uma dúvida, "quanto
  custa?") é respondida pelo caminho normal e o agente retoma a pergunta
  pendente.
- Pedido de pessoa ou de agendamento no meio do questionário: o questionário
  fica pausado (dados guardados, `step` vazio) e segue o pedido. Pausam: o
  toque em "Conversar 30 min" (`prop:meet`), em "Remarcar"/agendar
  (`book:start`), em "Falar com a equipe" (o botão de sempre ou `prop:team`),
  a frase fixa de pedido de pessoa, e um pedido de agendamento ou de pessoa
  feito como pergunta. Pausado, nada do que a pessoa digita vira resposta;
  pedir a proposta de novo (botão "Proposta por e-mail" ou a intenção
  `proposal`) retoma de onde parou e descarta os horários oferecidos que
  ainda valiam.
- Um questionário pausado por um agendamento continua `open`: se ficar 24 h
  sem resposta, a equipe recebe o resumo parcial como incompleto, como
  qualquer outro abandono, mesmo que a pessoa tenha marcado a conversa.
- Contato em modo teste da equipe: aviso marcado como teste, como hoje.

## Dados

- **Andamento:** o passo do questionário fica em `proposal_briefings.step`
  (não em `contacts.pending_action`).
- **Respostas:** tabela nova `proposal_briefings` (migração 0023; o nome
  `briefings` já é do resumo da manhã): `id`, `contact_id` (apaga junto com o
  contato), `status` (`open`, `sent`, `incomplete_sent`, `cancelled`,
  `proposal_sent`), `step`, `answers` (JSON cifrado como campo selado
  obrigatório: coluna antiga `''`, valor em `answers_sealed`), `prefilled`
  (lista das chaves vindas do preenchimento prévio), `created_at`,
  `updated_at`, `submitted_at`, dois avisos à equipe — `notified_at` ("pedido
  enviado") e `incomplete_notified_at` ("abandonado") —, `proposal_sent_at`,
  `proposal_sent_by`. Um questionário ativo (`open` ou `incomplete_sent`) por
  contato.
- A marca "proposta solicitada" vem da tabela (`status in (sent,
  incomplete_sent, proposal_sent)`), sem coluna nova em `contacts`.

## Código

- `core/services/proposal_briefing.py` (novo): `start`, `answer`, `skip`,
  `stop`, `correct`, `submit`, `summary`, `prefill`, `wants_a_person`,
  `team_notice`, `sweep_abandoned` e o worker `run_proposal_worker`.
- `core/repositories/proposal_briefings.py` (novo): SQL da tabela
  `proposal_briefings` (`open_for`, `create`, `save`, `set_status`,
  `mark_notified`, `abandoned`, `close_abandoned`, `requests`,
  `mark_proposal_sent`). `requests` deixa de fora as conversas da equipe
  (como o funil e a Inbox), salvo em `/teste`.
- `core/services/proposal_requests.py` (novo): a aba "Pedidos de proposta"
  (lista, detalhe, prazo, "Marcar proposta como enviada").
- `core/models.py`: intenção `proposal`; modelo `ProposalBriefing`.
- `core/services/graph.py`: `proposal` na triagem.
- `core/handlers/message.py`: encaminha `proposal` e os botões `prop:*`
  (`prop:start`, `prop:meet`, `prop:skip`, `prop:stop`, `prop:team`, `prop:ok`,
  `prop:fix`, `prop:fix:<passo>`) para o serviço; pausa o questionário nos
  toques e frases descritos acima.
- `core/content.py`: valida `[proposal]` (perguntas, exemplos, textos dos
  botões até 20 caracteres, itens do "Corrigir" até 24, conferência, final,
  avisos à equipe com " · {empresa}") no startup.
- `core/web/panel.py` e `core/web/static/painel/`: aba "Pedidos de proposta"
  (lista, detalhe, "Marcar proposta como enviada"), seção "Proposta
  solicitada" no card e selo na lista.
- `core/web/app.py`: liga o worker de abandono.
- `content/messages.toml`: `[proposal]`; regra de condução para oferecer as
  duas opções.
- `content/faq.md`: seções "Posso receber a proposta por e-mail, sem reunião?",
  "A Converge também faz site?", "O aplicativo do painel tem custo adicional?".

## Testes

- Roteiro: cada passo leva ao próximo; "Pular" avança sem resposta; "Prefiro
  marcar a conversa" cancela e oferece horários.
- Preenchimento prévio: só as chaves permitidas são aceitas; chave a mais e
  valor que não é texto são descartados; JSON inválido ou falha da IA = nada
  preenchido; perguntas preenchidas são puladas.
- Resumo: igual, caractere a caractere, ao que a pessoa escreveu; mostra o que
  veio do preenchimento prévio.
- Conferência: "Corrigir" repete só a pergunta escolhida.
- E-mail: pedido só se faltar; sem e-mail, não envia à equipe.
- Equipe: aviso enviado uma vez por envio; respostas cifradas no banco.
- Abandono: 24 h sem resposta envia uma vez como incompleto; voltar depois
  continua de onde parou.
- Aba: lista só pedidos enviados ou incompletos, ordenados pelo prazo; prazo
  de 2 dias úteis respeita fim de semana e dias sem expediente; "Marcar
  proposta como enviada" registra quem e quando, move o funil para
  "proposta" e só funciona para quem está logado no painel.
- Conteúdo: `[proposal]` incompleto impede a subida; seções novas do FAQ.

## Riscos

- O questionário pode parecer formulário: textos curtos e humanos, e até 7
  perguntas, das quais algumas já vêm puladas.
- A promessa de 2 dias úteis passa a ser compromisso da equipe; o aviso deixa
  o prazo visível.
- WhatsApp: fora da janela de 24 h, a equipe responde pela proposta por e-mail,
  não pelo canal; o aviso de abandono não manda nada ao cliente.
