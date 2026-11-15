# Revisão — reuniões do bot acompanham o Google

Branch `feat/agenda-acompanhar`, base `226f1d8` (`main`). Plano:
`docs/superpowers/plans/2026-10-26-agenda-incremento-2-acompanhar.md`.
Spec local: `docs/superpowers/specs/2026-10-22-agenda-no-painel-design.md`,
seção Incremento 2. Ledger local:
`.superpowers/sdd/2026-10-26-agenda-incremento-2-acompanhar/progress.md`.

Reuniões confirmadas do bot passam a acompanhar o Google: início movido
atualiza horário e lembrete; cancelamento exige Gone ou NotFound em duas
passadas com listagem bem-sucedida; problemas de organização, dia inteiro e
colisão suspendem lembretes e aparecem no painel. Botões novos carregam o
início da reunião; ofertas guardam o início que substituem. Não há mensagem
ao contato por uma mudança vinda do Google.

## Revisão independente e correções

A revisão do Opus sobre `226f1d8..aa4987e` aprovou os cinco cenários de foco
do plano, com três correções antes do merge. Corrigidas nesta passada:

1. **Alterar só a duração:** `apply_duration` atualiza o fim e a versão sem
   apagar lembrete, confirmação, pergunta ou validade dos botões. A mudança
   de início continua usando `apply_move` e sua comparação de fotografia.
2. **Transações do worker:** `send_due` encerra a transação implícita da
   consulta antes do lote, inclusive vazio. Assim, cada `ask_outcomes`
   confirma sua reivindicação e libera suas travas antes das automações.
3. **Histórico limitado:** alvos e descoberta de agendas usam
   `ends_at > NOW() - INTERVAL '14 days'`. Não há teto para reuniões futuras;
   reuniões movidas para além da listagem continuam acompanhadas.

## Validação

- 1300 testes passando com PostgreSQL e Redis descartáveis, incluindo sete
  regressões novas. APIs de calendário e envios ficam falsos nos testes.
- Ruff, Pyright e `git diff --check` limpos.
- Regressões de transação usam conexão real sem autocommit, outra conexão
  verifica as duas travas e a persistência da pergunta após rollback posterior.
- Consulta somente leitura em produção em 26/10: zero reuniões confirmadas
  com evento de provedor, tanto dentro quanto fora da janela. Portanto, não
  há impacto existente para medir; o fluxo real ainda precisa de `/teste`.

## Observações menores da revisão, fora desta passada

- Evitar regravar problemas iguais e tomar travas quando nada mudou.
- Preservar a fotografia original ao encadear ofertas de remarcação.
- Troca de horários entre duas reuniões continua exigindo resolver a colisão
  no Google, identificada no painel como `start_collision`.
- Tratar corpo JSON não objeto na classificação do erro 403 do Google.

Merge somente após autorização explícita do Paulo. Depois da implantação,
testar com `/teste`: marcar pelo bot, mover no Google e verificar o horário
novo e o lembrete. Não habilitar captura de mídia como parte deste PR.
