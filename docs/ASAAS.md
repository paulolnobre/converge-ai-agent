# Asaas — cobrança da Converge

O agente cobra os clientes da Converge pela conta Asaas da Converge. Desenho e
incrementos: spec local de 29/10/2026. A equipe cria cobranças no painel;
o webhook atualiza o status quando o Asaas informa um pagamento.

## Chave

1. Asaas → Integrações → Chaves de API → gerar. Sandbox em sandbox.asaas.com
   (comece por ele); produção em asaas.com.
2. No `.env`, **entre aspas simples**:

   ```
   ASAAS_API_KEY='$aact_hmlg_...'
   ASAAS_ENV=sandbox
   ```

   Sem as aspas, o Docker Compose lê `$aact_…` como variável, a chave chega
   cortada ao container e o Asaas responde `invalid_access_token`.
3. Conferir por tamanho, nunca imprimindo o valor.

Sem `ASAAS_API_KEY`, pagamentos ficam desligados.

## Smoke

```
docker compose -p ai-agents-reception run --rm reception-bot python -m cli.smoke_payments
```

- Sandbox: confere a conta, cria um cliente de teste e uma cobrança de R$ 5,00,
  lê de volta e cancela.
- Produção: só confere a conta. `--producao` cria e cancela de verdade.

Nunca mostra chave, CPF/CNPJ ou link de pagamento.

## Webhook

URL: `https://bot.getconverge.me/asaas/webhook` (ou o host da instalação).

1. Faça o deploy da versão que contém `POST /asaas/webhook` antes de ativar
   os envios no Asaas. Sem `ASAAS_WEBHOOK_TOKEN`, a rota responde 404.
2. No servidor, gere um token exclusivo com `openssl rand -hex 32` e coloque
   em `ASAAS_WEBHOOK_TOKEN` no `.env`, numa única linha. Use o mesmo valor
   no campo **Token de autenticação** do webhook no Asaas. Não use a chave de
   API como token e não envie nenhum dos dois pelo chat. Confira a configuração
   sem imprimir os valores. Recrie o serviço para carregar a variável.
3. No ambiente correto (comece pelo sandbox), cadastre a URL completa,
   **API v3**, envio **Sequencial** e somente estes cinco eventos:

   - `PAYMENT_RECEIVED` — pagamento recebido;
   - `PAYMENT_CONFIRMED` — pagamento confirmado;
   - `PAYMENT_OVERDUE` — vencido;
   - `PAYMENT_DELETED` — excluído;
   - `PAYMENT_REFUNDED` — estornado.
4. Só depois do deploy e do token carregado, ative o webhook e a fila de
   sincronização. Se já o cadastrou antes, mantenha ambos desligados até então.
5. Crie uma cobrança de teste pelo painel no sandbox, simule o pagamento no
   Asaas e confira **Pago** no painel, o agradecimento no canal do cliente e
   o aviso no grupo da equipe e por push (quando configurados).

O cabeçalho `asaas-access-token` é comparado com o token configurado. O endpoint
grava o evento na fila durável e criptografada, responde **200** e o worker
processa sem esperar o horário comercial. Sem conseguir gravar na fila, responde
503 para o Asaas tentar novamente. Não há confirmação de pagamento por URL de
retorno do navegador.

Eventos repetidos não repetem a alteração nem os avisos. Um vencimento que
chega depois do pagamento não desfaz **Pago**; pago só pode mudar para
estornado. Cancelado e estornado são terminais. Cobranças feitas diretamente
no Asaas, sem registro local, são ignoradas. Uma resposta perdida na criação
é recuperada por `externalReference=charge:<id>`, somente se a cobrança local
ainda não estiver vinculada a outro pagamento.

`PAYMENT_CONFIRMED` já marca **Pago**, embora o saldo ainda possa não estar
disponível na conta Asaas. `PAYMENT_RECEIVED` posterior não envia outro aviso.
Os avisos são tentados depois de persistir status, evento e auditoria; uma
falha de entrega não desfaz o pagamento nem provoca reenvio automático. Uma
interrupção entre essa gravação e o envio pode deixar o aviso sem entrega;
o status continua consultável no painel.

No WhatsApp, o agradecimento só é tentado com a janela de 24h aberta. A recusa
do provedor também é respeitada: sem entrega, nada é escrito no histórico.
Mesmo com a janela fechada, ou com uma pessoa atendendo a conversa, o status
e o aviso à equipe continuam independentes.

Referências: [recebimento e autenticação](https://docs.asaas.com/docs/receive-asaas-events-at-your-webhook-endpoint),
[eventos e idempotência](https://docs.asaas.com/docs/webhooks-events),
[eventos de cobrança](https://docs.asaas.com/docs/webhook-para-cobrancas).
