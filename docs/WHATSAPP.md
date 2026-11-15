# WhatsApp — guia de operação

O agente atende pelo WhatsApp pela Cloud API da Meta, sem intermediário. A
conversa é a mesma do Telegram: saudação, FAQ, agendar com botões, remarcar,
cancelar, áudio. A equipe responde pela Inbox do painel. O que é da equipe
(grupo, aviso privado, `/resumo`) continua no Telegram.

Especificação: `docs/superpowers/specs/2026-10-05-whatsapp-design.md`.

## 1. Variáveis na VPS (`/opt/converge-agent/.env`)

| Variável | De onde vem |
|---|---|
| `WHATSAPP_APP_SECRET` | Meta for Developers → app → Configurações do app → Básico → Chave secreta do app |
| `WHATSAPP_PHONE_NUMBER_ID` | app → WhatsApp → Configuração da API → "ID do número de telefone" |
| `WHATSAPP_ACCESS_TOKEN` | para teste, o token temporário da mesma tela (vale 24h); para produção, um token permanente de **usuário do sistema**, em Configurações do negócio → Usuários do sistema, com as permissões `whatsapp_business_messaging` e `whatsapp_business_management` |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | você inventa: uma string longa e aleatória (`openssl rand -hex 24`) |
| `WHATSAPP_REMINDER_TEMPLATE` | o nome do modelo aprovado (seção 3); vazio, o lembrete de contato do WhatsApp não sai |
| `WHATSAPP_APP_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID` | informativos |

Os quatro primeiros ligam o canal. Faltando qualquer um, o webhook responde
503 e o Telegram segue igual. Depois de editar o `.env`:

```bash
cd /opt/converge-agent && docker compose up -d reception-bot
```

Nunca cole esses valores numa conversa ou num commit. Escreva direto no
arquivo da VPS.

## 2. Teste com o número de teste da Meta

O app já vem com um número de teste, que manda mensagem para até 5 telefones
cadastrados. Nada aqui toca no número do CRM.

1. Em WhatsApp → Configuração da API, cadastre os telefones que vão testar
   ("Para"), e confirme cada um pelo código que chega no WhatsApp deles.
2. Use o **ID do número de teste** em `WHATSAPP_PHONE_NUMBER_ID` e o token
   temporário em `WHATSAPP_ACCESS_TOKEN`.
3. Em WhatsApp → Configuração → Webhook:
   - URL de retorno: `https://bot.getconverge.me/whatsapp/webhook`
   - Token de verificação: o mesmo `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
   - Clique em **Verificar e salvar**. A Meta chama a URL uma vez, e o bot
     devolve o desafio só se o token bater.
   - Em "Campos do webhook", **assine `messages`**.
4. Mande "oi" de um telefone cadastrado para o número de teste. Siga as
   seções 1 (conversa), 3 (agendar) e 5 (cancelar) do roteiro de testes.

Para ver o que chegou:

```bash
cd /opt/converge-agent && docker compose logs --since 10m reception-bot | grep -i whatsapp
```

## 3. Modelo do lembrete (WhatsApp Manager → Modelos de mensagem)

A Meta só aceita mensagem que a empresa inicia por modelo aprovado. O lembrete
sai 24h antes (ou 1h antes, se marcado em cima da hora), quase sempre fora da
janela de 24h da conversa.

- **Nome:** `lembrete_conversa`
- **Categoria:** Utilidade
- **Idioma:** Português (BR)
- **Corpo:**

  ```
  Olá, {{1}}! Passando para lembrar da sua conversa com a equipe Converge {{2}} às {{3}}. Link da reunião: {{4}}

  Consegue confirmar pelo botão abaixo? Se precisar, dá para remarcar ou cancelar por ali também.
  ```

  Exemplos para a Meta: `{{1}}` Ana, `{{2}}` quinta-feira, 09/10,
  `{{3}}` 10:00, `{{4}}` https://meet.google.com/abc-defg-hij

- **Botões** (Resposta rápida, nesta ordem): `Confirmo`, `Remarcar`,
  `Cancelar`

Os botões levam a mesma ação dos botões do Telegram. Por isso a ordem importa.
Enquanto o modelo não estiver aprovado, deixe `WHATSAPP_REMINDER_TEMPLATE`
vazio: o lembrete de quem é do WhatsApp não sai e fica um aviso no log.

### Modelo de retomada (automação "Parou no meio")

Retoma a conversa de quem parou de responder no meio do atendimento ou depois
do preço. Hoje as automações pulam quem é do WhatsApp; com este modelo, a
"Parou no meio" passa a alcançá-los.

- **Nome:** `retomada_de_conversa` · **Categoria:** Marketing · **Idioma:** Português (BR)
- **Corpo** (uma variável, o nome): `Olá, {{1}}! Aqui é a equipe da Converge. Nossa
  conversa ficou pela metade. Quer continuar de onde paramos? É só responder por aqui.`
- **Botões** (resposta rápida, nesta ordem): `Quero continuar`, `Agora não`
- **No servidor:** `WHATSAPP_RESUME_TEMPLATE=retomada_de_conversa` e
  `WHATSAPP_RESUME_LANGUAGE=pt_BR` (o idioma tem de ser o mostrado no WhatsApp
  Manager; um idioma errado faz o envio falhar). Sem o nome, nada muda.
- **No painel (Automações):** "Parou no meio" com **1 dia** sem resposta (24h).
  Comece em "aprovar": a equipe vê a fila antes de sair.
- **Quem NÃO recebe:** quem respondeu SAIR, está em Perdido/Fechado, tem conversa
  com a equipe aberta, tem consulta marcada ou recebeu outra automação nos
  últimos 30 dias, e quem **disse que não quer**: o agente lê as últimas
  mensagens e classifica em *recusou* (não manda), *em andamento* (manda) ou
  *dúvida* (vai para a fila da equipe, mesmo no modo automático).
- **"Quero continuar"** vira uma mensagem da pessoa e o agente retoma pelo
  histórico. **"Agora não"** é anotado e a pessoa é deixada em paz.

### Modelos de cobrança, pagamento e resposta da equipe

Sem estes três, o agente falha em silêncio quando a pessoa não escreve há mais
de 24h: a cobrança do painel é recusada ("janela fechada"), o agradecimento de
pagamento não sai e a equipe não consegue responder pela Inbox. Todos em
Português (BR). Cada um só é usado depois de 24h sem a pessoa escrever;
dentro da janela a mensagem sai em texto livre, como antes. Com o nome no
`.env` da VPS, o agente usa o modelo; vazio, nada muda:

| Situação | Variável | Sem o modelo |
|---|---|---|
| Cobrança criada ou reenviada no painel | `WHATSAPP_CHARGE_TEMPLATE=cobranca_enviada` | o painel recusa ("janela fechada") |
| Pagamento confirmado pelo Asaas | `WHATSAPP_PAID_TEMPLATE=pagamento_recebido` | só a equipe e o e-mail ficam sabendo |
| Equipe quer responder na Inbox | `WHATSAPP_TEAM_TEMPLATE=resposta_da_equipe` | a caixa de resposta fica bloqueada |

Se o de pagamento ou o da equipe foi aprovado em outro idioma (o WhatsApp
Manager mostra "English", por exemplo), informe o idioma em
`WHATSAPP_PAID_LANGUAGE` ou `WHATSAPP_TEAM_LANGUAGE` (`en` para English). O
texto continua em português; só a etiqueta precisa bater com a da Meta.

Com o da equipe, a Inbox mostra **Reabrir conversa** no lugar da caixa
bloqueada. Os botões "Tenho uma dúvida" e "Ver resposta" chegam como se a
pessoa tivesse escrito, e a janela abre.

**Escolher o modelo na Inbox.** Depois de 24h, quem assumiu a conversa escolhe
qual modelo enviar entre os que só pedem o nome e estão no `.env`: resposta da
equipe, retomada de conversa, contato por interesse e contato por e-mail. Cada
um sai com os botões do próprio fluxo. Cobrança, pagamento, proposta e
lembrete não aparecem na lista: dependem dos dados do fluxo e saem pelos
botões de cada um. A lista fica em `core/services/wa_templates.py`, e a aba
**Automações** mostra quais estão ativos neste servidor (o nome no `.env`; a
aprovação da Meta continua sendo conferida no WhatsApp Manager).

A Meta recusa corpo que começa ou termina com variável, e duas variáveis
coladas. Por isso cada texto abre e fecha com palavras.

**1. Cobrança enviada**

- **Nome:** `cobranca_enviada` · **Categoria:** Utilidade
- **Corpo:**

  ```
  Olá, {{1}}! Segue a cobrança referente a {{2}}, no valor de {{3}}, com vencimento em {{4}}. Para pagar, acesse: {{5}}

  Qualquer dúvida, é só responder por aqui.
  ```

  Exemplos: `{{1}}` Ana, `{{2}}` Plano Atendimento com IA, `{{3}}` R$ 497,00,
  `{{4}}` 15/11/2026, `{{5}}` https://www.asaas.com/i/abc123
- **Botão** (resposta rápida): `Tenho uma dúvida`. Ao tocar, a janela reabre e
  a equipe responde normalmente.

**2. Pagamento recebido**

- **Nome:** `pagamento_recebido` · **Categoria:** Utilidade
- **Corpo:**

  ```
  Olá, {{1}}! Recebemos o seu pagamento de {{2}} referente a {{3}}. Obrigado pela confiança na Converge!
  ```

  Exemplos: `{{1}}` Ana, `{{2}}` R$ 497,00, `{{3}}` Plano Atendimento com IA
- **Sem botões.**

**3. Resposta da equipe (reabre a conversa)**

- **Nome:** `resposta_da_equipe` · **Categoria:** Utilidade
- **Corpo:**

  ```
  Olá, {{1}}! Aqui é a equipe da Converge. Temos uma resposta para a sua conversa. Toque no botão abaixo para recebê-la por aqui.
  ```

  Exemplo: `{{1}}` Ana
- **Botão** (resposta rápida): `Ver resposta`. O toque abre a janela de 24h e a
  equipe escreve livremente, sem novo modelo.

**4. Visita perdida (modelo pronto da Meta)**

- **Nome:** `missed_appointment` · **Categoria:** Utilidade · **Idioma:** Português (BR)
- **Corpo:** `Olá, {{1}}, sentimos a sua falta na sua consulta agendada {{2}} para {{3}}. Responda para reagendar ou entre em contato com {{4}} para marcar um novo horário.`
- Uso previsto: a automação "Não compareceu". `{{1}}` nome, `{{2}}` e `{{3}}`
  dia e horário (confira a ordem no exemplo do WhatsApp Manager), `{{4}}` o
  contato da Converge. **Ainda não ligado no código**: hoje essa automação
  pula quem é do WhatsApp.

**5. Primeiro contato com lead que veio por e-mail**

Usado pelo botão **Novo lead** da aba Leads, quando a origem é **E-mail**: a equipe cadastra nome, WhatsApp,
e-mail e origem, e o painel manda esta mensagem. Quando a pessoa responde, o
assistente continua a conversa pelo histórico.

- **Nome:** `contato_por_email` · **Categoria:** Marketing (a Meta classifica
  assim o primeiro contato da empresa) · **Idioma:** Português (BR)
- **Corpo:**

  ```
  Olá, {{1}}! Aqui é a equipe da Converge. Você nos deixou o seu contato por e-mail e estamos passando para explicar melhor como funcionam os nossos serviços. Podemos conversar por aqui?
  ```

  Exemplo: `{{1}}` Ana
- **Botões** (resposta rápida, nesta ordem): `Quero saber mais`, `Agora não`
- **No servidor:** `WHATSAPP_LEAD_TEMPLATE=contato_por_email`. Vazio, o lead é
  cadastrado e nada é enviado.
- Só cadastre quem deixou o contato pedindo retorno: a Meta exige que a pessoa
  tenha aceitado receber mensagens, e denúncias derrubam a qualidade do número.

**6. Primeiro contato com lead de qualquer outra origem**

O mesmo botão **Novo lead**, para quem não veio por e-mail: indicação,
Instagram, Facebook, site, Google ou outro. Também vale para um lead de e-mail
enquanto o modelo 5 não estiver aprovado.

- **Nome:** `contato_interesse` · **Categoria:** Marketing · **Idioma:**
  Português (BR)
- **Corpo:**

  ```
  Olá, {{1}}, tudo bem? Aqui é a equipe da Converge. Vimos que você demonstrou interesse nas nossas soluções de inteligência artificial para empresas. Gostaria de conhecer melhor como elas podem ajudar no seu negócio?
  ```

  Exemplo: `{{1}}` Bruno
- **Botões** (resposta rápida, nesta ordem): `Quero saber mais`, `Agora não`
- **No servidor:** `WHATSAPP_LEAD_GENERAL_TEMPLATE=contato_interesse`. Vazio,
  leads dessas origens são cadastrados e nada é enviado.
- A mesma regra do modelo 5: só para quem demonstrou interesse e aceitou ser
  contatado.

**7. Proposta em PDF**

Usado pelo botão **Enviar proposta (PDF)** da aba Propostas. Dentro das 24h o
painel manda o PDF com o mesmo texto e os mesmos botões, sem modelo; depois
delas, este modelo. O pedido passa para "proposta enviada" sozinho.

- **Nome:** `proposta_converge` · **Categoria:** Utilidade · **Idioma:**
  Português (BR)
- **Amostra de mídia:** Documento, com um PDF de exemplo (só para a
  aprovação; cada envio leva o PDF real). O campo **Cabeçalho** de texto fica
  vazio.
- **Corpo:**

  ```
  Olá, {{1}}! Segue em anexo a proposta da Converge para {{2}}, preparada a partir do que você nos contou. Se estiver tudo certo, é só tocar em Aceitar proposta. Qualquer dúvida, responda por aqui ou toque em Tenho dúvidas.
  ```

  Exemplo: `{{1}}` Maria, `{{2}}` Clínica Exemplo
- **Botões** (resposta rápida, nesta ordem): `Aceitar proposta`,
  `Tenho dúvidas`, `Não tenho interesse`
- **No servidor:** `WHATSAPP_PROPOSAL_TEMPLATE=proposta_converge`. Vazio, a
  proposta só sai dentro das 24h.
- O que cada botão faz: **Aceitar** responde e avisa a equipe (o card não se
  move sozinho: fechar é decisão da equipe); **Tenho dúvidas** vira uma
  mensagem para o assistente; **Não tenho interesse** agradece, pergunta o
  motivo uma vez, sem insistir, e avisa a equipe.

**8. Pedido de mais informações para a proposta**

Usado pelo botão **Pedir mais informações** da aba Propostas, quando a equipe
precisa de mais dados para montar a proposta. Dentro das 24h vai uma mensagem
com os dois botões, sem modelo; depois delas, este modelo.

- **Nome:** `pedir_informacoes` · **Categoria:** Utilidade · **Idioma:**
  Português (BR)
- **Corpo:**

  ```
  Olá, {{1}}! Para montar a sua proposta, a nossa equipe precisa de mais algumas informações. Você prefere marcar uma conversa de 30 minutos ou falar com a nossa equipe por aqui?
  ```

  Exemplo: `{{1}}` Maria
- **Botões** (resposta rápida, nesta ordem): `Conversar por aqui`, `Agendar conversa`
  (os payloads são do código; quem cadastra só dá os textos)
- **No servidor:** `WHATSAPP_INFO_TEMPLATE=pedir_informacoes`. Vazio, o pedido
  só sai dentro das 24h.
- O que cada botão faz: **Conversar por aqui** continua com quem assumiu a
  conversa, que recebe o aviso; **Agendar conversa** devolve a conversa ao bot
  e abre os horários (o Flow de agendamento), avisando quem pediu.

Se a Meta reclassificar algum como Marketing, aceite: muda só o preço por
mensagem. Nome e idioma têm de ficar idênticos ao que aparece no WhatsApp
Manager.

## 4. Virada do número do CRM

Situação confirmada em 06/10/2026: o número está na Cloud API, a conta do
WhatsApp Business e o app do agente ficam no mesmo Business Suite, e o CRM é
outro app. A virada é trocar qual app recebe, sem migrar o número.

Antes de virar:
- todo o roteiro passou no número de teste;
- o modelo do lembrete está aprovado;
- o token permanente e o ID do número **real** estão no `.env` da VPS.

Na virada, no mesmo momento:
1. No Gerenciador de Negócios, dê ao app do agente acesso à conta do WhatsApp
   Business e inscreva o app nela (WhatsApp → Configuração → Webhook do app,
   com a URL e o token da seção 2, e o campo `messages` assinado).
2. **Desligue o robô do CRM e a inscrição do app do CRM nesse número.**
   Enquanto os dois apps estiverem inscritos, cada mensagem chega aos dois e o
   cliente recebe duas respostas.
3. Mande "oi" de um celular de fora da equipe e confira a resposta e a
   conversa na Inbox.

Volta atrás: reinscreva o app do CRM e religue o robô dele, e desinscreva o app
do agente. O número, o nome de exibição e os modelos aprovados nunca saem da
conta. O histórico do CRM não vem para o painel. Ele continua no CRM.

## 5. O que a v1 não faz

- Automações não saem pelo WhatsApp: cada uma precisaria de um modelo de
  marketing aprovado. Contatos do WhatsApp ficam fora delas.
- Passadas 24h da última mensagem do cliente, a Inbox não deixa responder.
  Reabrir a conversa com um modelo fica para depois.
- A mesma pessoa no Telegram e no WhatsApp são dois contatos.
