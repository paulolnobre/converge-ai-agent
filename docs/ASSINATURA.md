# Assinatura do contrato (Autentique)

O contrato que o card mostra em **Conferir contrato** vai para a Autentique
pelo botão **Enviar para assinatura** (aba Pedidos de proposta). Código:
`core/services/contract_sign.py` e `core/integrations/autentique.py`.

## Como funciona

- Quem aperta precisa ter assumido a conversa, como em todo envio do painel.
- Dois signatários: quem assina pelo cliente (dados do Flow `dados_contrato`,
  com o CPF) e o representante da Converge (`CONTRACT_SIGNER_*`).
- A Autentique manda o link por e-mail para os dois. O link do cliente também
  vai na conversa quando a janela de 24h do WhatsApp está aberta.
- Um envio por pedido. Se a Autentique recusar, o pedido pode ser enviado de
  novo. Se ela não confirmar (tempo esgotado), o painel pede para conferir no
  painel da Autentique antes de tentar de novo, para não criar dois documentos.
- Fica gravado em `contract_signatures` (migração 0029) o id do documento,
  quem enviou, quando e se foi sandbox. O link de assinatura não é guardado.

## Configuração (.env)

```
AUTENTIQUE_API_KEY=            # painel.autentique.com.br/perfil/api
AUTENTIQUE_SANDBOX=true        # false só quando for para valer
CONTRACT_SIGNER_NAME=          # quem assina pela Converge
CONTRACT_SIGNER_EMAIL=
```

Sem a chave ou sem o representante, o botão não aparece.

Em sandbox o documento não gasta crédito, não tem valor jurídico e some em
poucos dias. Contato em `/teste` vai sempre para o sandbox, mesmo com
`AUTENTIQUE_SANDBOX=false`.

## Assinado (webhook)

No Painel do desenvolvedor da Autentique, cadastre o endpoint
`https://bot.getconverge.me/autentique/webhook` com os eventos
`signature.accepted` e `document.finished`, e ponha o segredo no `.env`:

```
AUTENTIQUE_WEBHOOK_SECRET=
```

Sem o segredo, o endereço responde 404. Com ele, só entra o corpo assinado
(HMAC-SHA256 em `X-Autentique-Signature`). A fila guarda só o evento, o
documento e a assinatura, nunca CPF ou e-mail. Código:
`core/services/contract_signed.py`, migração 0030.

- `signature.accepted`: o card mostra quem já assinou (cliente ou Converge).
- `document.finished`: o card passa a "Assinado", com o botão **Baixar
  contrato assinado** (abre na Autentique), e a equipe recebe um aviso uma vez.
- Eventos repetidos ou fora de ordem não mudam nada. Documento que não foi
  enviado pelo painel é ignorado.

## Cobrança no assinado

No `document.finished`, o bot cria no Asaas a **primeira cobrança da
implantação** e manda o link ao cliente na conversa (fora da janela de 24h,
pelo modelo `WHATSAPP_CHARGE_TEMPLATE`) e por e-mail. Código:
`core/services/contract_charge.py`, migração 0032.

- **Valor:** o que o contrato enviado diz. Ao enviar para assinatura, as
  parcelas vão para `contract_signatures.setup_terms`; editar o orçamento
  depois não muda a cobrança. "Metade": 50%. "Parcelado": a primeira parcela.
- **Forma:** a que o cliente escolheu no questionário (cartão, boleto ou
  Pix); sem escolha, o link em que ele escolhe. No cartão parcelado, soma a
  taxa do cartão (`taxa_cartao_pct` e `taxa_cartao_fixa` em
  `content/pricing.toml`), para chegar à Converge o valor da proposta.
- **Cliente no Asaas:** criado com a razão social e o CNPJ/CPF dos dados do
  contrato.
- **Uma vez só:** uma cobrança por pedido e parte. Um evento repetido retoma
  a cobrança de onde parou (acha no Asaas pela referência, entrega se ainda
  não entregou), com um lock por contrato entre workers.
- **A equipe sempre sabe:** aviso de enviada, de criada mas não entregue
  (usar Reenviar) ou do motivo de não ter saído: cobrança desligada, contrato
  de teste com Asaas em produção, contrato enviado antes desta versão, CNPJ/CPF
  inválido, erro do Asaas ou cobrança já paga ou cancelada.

## Ainda não

- O resto da implantação (segunda metade no aceite, demais parcelas) e a
  mensalidade: próximos PRs.
- Reenviar ou cancelar um contrato enviado: hoje, pelo painel da Autentique.
