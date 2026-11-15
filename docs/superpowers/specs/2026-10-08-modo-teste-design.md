# Modo teste da equipe (`/teste`) — especificação

Data: 08/10/2026 · Status: aguardando revisão do Paulo

## Objetivo

Alguém da equipe testa o atendimento como se fosse um cliente, de ponta a ponta,
pelo próprio WhatsApp ou Telegram, sem deixar dados de teste misturados aos
clientes reais. O teste roda pelo caminho real: o bot, a agenda do Google, a
Inbox e o grupo da equipe. Tudo sai marcado como TESTE e é apagado no fim.

Pronto quando: com `/teste`, a pessoa da equipe conversa, marca uma reunião,
pede uma pessoa e é atendida pela Inbox. Com `/sair`, ou depois de 2h sem
mensagem, nada do teste sobra no banco, e os eventos futuros criados no teste
estão cancelados no Google.

## Decisões já tomadas com o Paulo (08/10/2026)

1. **Alcance: ponta a ponta, marcado.** Agenda de verdade, com convite por
   e-mail, conversa na Inbox e avisos no grupo, tudo marcado como TESTE.
2. **Fim: `/sair` ou 2h sem mensagem.** Os dois apagam os dados e cancelam os
   eventos.
3. **Perfil escolhido no comando.** `/teste` começa como cliente novo, e
   `/teste antigo` começa como cliente que já conversou antes.
4. **Abordagem: marcar o próprio contato.** O teste usa o contato do chat real
   de quem testa, marcado como teste. Nada é traduzido para outro chat. O
   histórico normal dessa pessoa com o bot é apagado a cada teste.
5. **Só equipe reconhecida.** Só quem o sistema reconhece como equipe, pelo
   Telegram ou pelo WhatsApp (PR #10), pode ligar o teste.

## Comandos

Os três comandos são lidos antes de qualquer outra coisa no turno: antes da
triagem, do limite de mensagens e do opt-out. Vêm escritos com a barra, sem
diferenciar maiúsculas.

| Comando | Quem | O que faz |
|---|---|---|
| `/teste` | equipe | Limpa o contato (ver Limpeza) e o marca como teste "novo". O bot responde `teste.started_new`. |
| `/teste antigo` | equipe | O mesmo, mas o contato começa com nome e e-mail da pessoa na equipe e uma troca curta de mensagens datada de ontem. O bot responde `teste.started_returning`. |
| `/sair` | quem está em teste | Faz a limpeza e responde `teste.ended`. |

- **Quem não é da equipe:** `/teste` é uma mensagem comum, segue para a
  triagem.
- **`/sair` fora de um teste:** de alguém da equipe, recebe `teste.not_in_test`.
  De um cliente, é uma mensagem comum.
- **`SAIR` sem barra:** continua sendo o opt-out das mensagens automáticas,
  dentro e fora do teste.
- **`/teste` com um teste em andamento:** recomeça do zero, com limpeza e nova
  marca.

## Dados — migração `0008_test_mode.sql` (aditiva)

```sql
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS test_mode VARCHAR(10)
    CHECK (test_mode IN ('novo', 'antigo')),
  ADD COLUMN IF NOT EXISTS test_started_at TIMESTAMPTZ;
```

- `test_mode` nulo significa contato normal.
- A inatividade é medida por `contacts.last_inbound_at`, que já existe.

## Ligar o teste

Tudo numa transação, sob o lock do chat (`handoffs_repo.lock_chat`):

1. Limpa o contato do chat (ver Limpeza). Se não existe, nada a limpar.
2. Cria o contato do chat de novo, com `test_mode` e `test_started_at = now()`.
   - **No "novo":** sem nome, e-mail nem telefone. O bot pede os dados antes de
     agendar, como para alguém que nunca falou com a Converge.
   - **No "antigo":**
     - nome e e-mail vêm da linha da pessoa em `staff`;
     - o telefone é a primeira forma do WhatsApp, quando o teste é pelo WhatsApp;
     - duas linhas em `sessions`, criadas ontem: `user "oi"` e a resposta
       `reply.welcome`. Assim o "oi" seguinte recebe o `reply.welcome_back`.

## Onde o teste aparece

O contato em teste deixa de ser escondido como equipe nas listas de clientes, e
continua fora de tudo que conta como cliente. São dois predicados SQL em
`core/repositories/contacts.py`, substituindo o uso direto de `is_staff_sql`:

- **`hidden_from_customer_lists_sql(c)`**: é da equipe e não está em teste.
  - Usado nas consultas da Inbox (lista, contagem de quem espera, aviso de nova
    conversa) e no funil e na lista de contatos (`list_leads`).
- **`left_out_of_metrics_sql(c)`**: é da equipe ou está em teste.
  - Usado no resumo diário e nas automações.

`is_staff_sql` continua sendo o que decide se o chat é da equipe.

No painel:

- **Na API:** as respostas de leads, da lista da Inbox e da conversa trazem
  `is_test`.
- **Na tela:** etiqueta **TESTE** na linha da Inbox, no cabeçalho da conversa,
  no card do funil, na tabela de contatos e no painel lateral.
- **Nos números do Painel:** as contagens por etapa, "De onde vêm os contatos"
  e os demais números ignoram contatos com `is_test`.

## Marca nas mensagens à equipe e na agenda

- **Prefixo:** toda mensagem ao grupo da equipe, ou ao privado de quem assumiu,
  sobre um contato em teste começa com `teste.team_prefix` ("🧪 TESTE · ").
  - Isso vale para nova conversa, pedido de pessoa, marcou, remarcou,
    cancelou, confirmou presença, "aconteceu?", assumiu, devolveu e
    encerramento por inatividade de atendimento.
  - Um helper só, `team_text(text, contact)`, aplicado nos pontos que hoje
    montam esses textos (`inbox.notify_team`, `handoff`, `attendance`).
- **Agenda:** o título do evento criado para um contato em teste começa com
  `[TESTE] `. O convite segue para o e-mail do contato.
- **Lembrete:** sai normalmente.

## Limpeza

Usada por `/sair`, por `/teste` (antes de marcar de novo) e pela expiração.

1. **Escopo:** só contatos com `test_mode` não nulo. A condição está no próprio
   SQL de cada passo, então um erro no código não apaga um cliente real. A
   exceção é o passo de limpar antes de ligar, que vale para o contato do
   próprio chat de quem é da equipe.
2. **Agenda:** cancela no Google os compromissos futuros do contato que não
   estão cancelados, com `sendUpdates=all`.
   - Se o Google falhar num evento, a limpeza continua, e o aviso final lista o
     evento a apagar à mão (`teste.cancel_failed`, com data e hora).
3. **Banco, numa transação:**
   - apaga `workspace_actions` e `appointments` do contato;
   - apaga o contato, o que leva junto, em cascata, `sessions`, `offers`,
     `handoff_sessions` (e com elas `handoff_messages`) e `automation_queue`.
4. **O que não é apagado:** as mensagens já enviadas ao grupo do Telegram.

## Expiração em 2h

O loop de `run_handoff_timeout_worker`, que roda a cada minuto, chama também
`sweep_tests(conn)`:

- encontra os contatos com `test_mode` não nulo e `last_inbound_at` (ou
  `test_started_at`, se não houver mensagem) há mais de
  `TEST_IDLE_MINUTES` (padrão 120);
- faz a limpeza de cada um e manda `teste.expired` para o chat de quem
  testava.
  - No WhatsApp a janela de 24h está aberta, porque a última mensagem tem
    pouco mais de 2h.
  - Se o envio falhar, a limpeza vale assim mesmo.

## Textos (`content/messages.toml`, seção `[teste]`)

- `started_new`: "Modo teste ligado: agora te trato como cliente novo. Tudo fica
  marcado como TESTE e é apagado quando você mandar /sair, ou depois de 2h sem
  mensagem."
- `started_returning`: o mesmo, com "cliente antigo".
- `ended`: "Teste encerrado e apagado."
- `expired`: "Teste encerrado por inatividade e apagado."
- `not_in_test`: "Você não está em modo teste. Mande /teste para começar."
- `cancel_failed`: "Não consegui cancelar na agenda o evento de {quando}. Apague
  à mão."
- `team_prefix`: "🧪 TESTE · "

## Testes

- **Ligar:**
  - `/teste` marca "novo" e limpa o histórico;
  - `/teste antigo` traz nome, e-mail e o histórico de ontem, e o "oi" recebe o
    "de novo";
  - quem não é da equipe recebe a triagem normal.
- **Durante:**
  - o contato aparece na lista da Inbox e no `list_leads` com `is_test`;
  - fica fora do resumo e das automações;
  - as mensagens ao grupo trazem o prefixo;
  - o título do evento traz `[TESTE] `.
- **Sair:**
  - `/sair` cancela os eventos futuros e apaga tudo;
  - com o Google falhando, os dados ainda são apagados e o aviso lista o
    evento;
  - `/sair` fora de teste recebe a resposta certa;
  - `SAIR` sem barra continua sendo opt-out.
- **Expirar:** depois de 120 min sem mensagem, a limpeza acontece e o aviso
  sai. Antes disso, nada acontece.
- **Segurança:** a limpeza nunca apaga um contato sem `test_mode`. Um contato
  real com o mesmo formato de chat continua intacto.
- **No Postgres real:** a migração, os dois predicados e a limpeza em cascata,
  numa transação desfeita.
- **No navegador:** a etiqueta TESTE na Inbox, no funil e na lista de contatos,
  nos temas claro e escuro.
- **Revisão:** o Codex revisa no fim.

## Fora desta etapa

- Apagar do Telegram as mensagens de teste enviadas ao grupo.
- Testar como cliente de um canal diferente do que a pessoa está usando.
- Simular datas (testar o lembrete de 24h exigiria esperar 24h).
