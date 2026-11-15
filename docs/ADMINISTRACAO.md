# Administração no app

O grupo reúne contas escolhidas do painel e o bot. Os membros conversam no
app, recebem aviso de nova mensagem e executam os comandos descritos abaixo.
O monitor publica
queda e retorno no histórico e envia push para os aparelhos dos membros. O
Telegram pode continuar recebendo os alertas durante a transição.

## Acesso

O acesso ao painel não concede acesso ao grupo. Após as migrações 0016 e 0017, adicione
as contas já cadastradas de Paulo, Victor e Rodrigo pelos e-mails corretos:

```sh
python -m cli.admin_room add <email-da-conta>
python -m cli.admin_room list
python -m cli.admin_room remove <email-da-conta>
```

A conta precisa estar ativa. A remoção vale no próximo pedido, inclusive numa
tela já aberta, e retira os próximos avisos. Convites, senha, segundo fator e
revogação de aparelhos continuam usando o acesso existente do painel. As
mensagens são cifradas com DATA_KEYS, também cobertas por `cli.seal_data` na
checagem e na troca de chave. O histórico nunca vai para o armazenamento do
navegador. O push não carrega texto da conversa nem o nome de quem escreveu.

## Monitor separado do bot

A imagem inclui `python -m cli.monitor`. O overlay
`deploy/docker-compose.monitor.yml` inicia esse processo em outro container,
sem depender do container do bot, sem Docker socket e sem chaves de IA.
Use a mesma imagem testada da versão do bot. Copie o overlay para o diretório
do Compose e, **depois de aplicar as migrações**, suba somente o monitor:

```sh
docker compose -f docker-compose.yml -f docker-compose.monitor.yml up -d monitor
```

O CI envia o overlay e o auxiliar `admin-monitor.sh`. Após a ativação, mantenha
o overlay na lista COMPOSE_FILE do host: cada deploy atualiza também o monitor,
depois das migrações e da verificação de prontidão do bot.
A ativação exige as DATA_KEYS atuais, o acesso ao banco e as chaves VAPID já
configuradas; não gere outra chave push. Confira que o mesmo ambiente define
PANEL_IDLE_MINUTES e PANEL_DEVICE_IDLE_DAYS, se foram personalizados.
TELEGRAM_BOT_TOKEN e TEAM_CHAT_ID são opcionais para a cópia no Telegram.

O monitor consulta `/ready` a cada minuto, com timeout, sem seguir redirects.
Depois de **3 minutos consecutivos observados** sem prontidão, registra e
envia um alerta. O primeiro resultado pronto registra o retorno. Um intervalo
sem observação não é apresentado como tempo de queda confirmado. O incidente
fica no banco, preservando a deduplicação após reinícios. O envio ocorre após
o commit; cada aparelho confirmado é marcado para não repetir em tentativas
parciais. Falhas de envio tentam de novo por até 15 minutos. O aparelho precisa
ter ativado as notificações no app e manter uma sessão válida.

O monitor cobre a parada do bot, falha de prontidão e a morte do worker
reportada por `/ready`. Não cobre servidor inteiro ou banco fora do ar: o
primeiro exige monitor externo; o segundo impede consultar assinaturas. Essa
limitação também aparece em `/status` quando não há observação recente.
Alertas por volume de erros dos logs ainda não estão incluídos.

## Escopo desta primeira versão

- Um grupo com associação explícita, mensagens de até 4.000 caracteres,
  páginas de 50 mensagens e atualização a cada 5 segundos enquanto aberto.
- Reenvio de mensagem com o mesmo identificador não duplica o histórico.
- O bot publica eventos e executa comandos administrativos explícitos: `/status`,
  `/resumo`, `/agenda [AAAA-MM-DD]`, `/pendencias`, `/assumir ID`, `/devolver ID`,
  `/sincronizar`, `/painel` e `/ajuda` (também `/comandos`).
- `/resumo` usa o mesmo serviço do grupo do Telegram. `/agenda` mostra os
  agendamentos confirmados do bot no dia; a aba Agenda mantém a visão completa.
- Assumir, devolver e sincronizar geram um pedido, sem executá-lo imediatamente.
  A mesma conta confirma com `/confirmar CÓDIGO` ou desiste com `/cancelar CÓDIGO`.
  O pedido vale por 5 minutos, é de uso único e registra autor, ação e resultado.
- Cada confirmação revalida a associação ao grupo e o estado da conversa.
  As regras do Inbox continuam valendo: outra pessoa não perde a conversa,
  e só o dono pode devolvê-la. O aviso ao cliente respeita a janela do canal.
- As mensagens continuam sendo conversa normal. Só comandos explícitos são
  despachados. Não há execução de código nem chamadas ao modelo a partir delas.
- O template `retomar_conversa` informado por Paulo segue aguardando o review
  da Meta. Nenhum envio desse template é feito por este incremento.

Validação local deve usar Postgres isolado, contas sintéticas e transporte
push simulado. A ativação em produção e a entrega em iPhone/Android reais
precisam ser verificadas após a publicação.
