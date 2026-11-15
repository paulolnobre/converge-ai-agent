# Revisão — modo teste da equipe (`/teste`)

Branch `feat/modo-teste`, diff `git diff 7a448b4..HEAD`. Spec:
`docs/superpowers/specs/2026-10-08-modo-teste-design.md`. Plano:
`docs/superpowers/plans/2026-10-08-modo-teste.md`.

Alguém da equipe manda `/teste` (ou `/teste antigo`) e é atendido como cliente,
pelo caminho real, com tudo marcado TESTE. `/sair`, ou 2h sem mensagem, cancela
os eventos futuros no Google e apaga os dados do teste.

## Onde olhar com mais cuidado

1. **Limpeza (`core/repositories/test_mode.py:delete`)** — pode apagar cliente
   real? O escopo está todo no `target` do SQL. Confira o `staff_own`, a
   cascata e o que fica para trás (`sessions` com `contact_id` nulo, `tasks`,
   `mail_threads`).
2. **Google antes do banco (`core/services/test_mode.py:cleanup`)** — uma falha
   do Google não pode impedir a limpeza nem sumir em silêncio.
3. **Lugar do comando no turno (`core/handlers/message.py:process_message`)** —
   depois de `_note_inbound`, antes do limite de mensagens, do opt-out e da
   triagem. `/teste` de quem não é da equipe segue como mensagem comum. `SAIR`
   sem barra continua opt-out.
4. **Janela de 24h do WhatsApp** — o contato é apagado e recriado no `/teste`;
   o novo precisa nascer com `last_inbound_at`, senão o bot não responde.
5. **Expiração (`sweep_in_pool`)** — lock do chat e reconferência sob o lock;
   uma mensagem que chega no meio mantém o teste vivo. Falha no aviso não
   desfaz a limpeza.
6. **Predicados (`hidden_from_customer_lists_sql`, `left_out_of_metrics_sql`)**
   — o teste aparece na Inbox e no funil e fica fora do resumo, das automações e
   dos números do painel.
7. **Marca TESTE** — todos os avisos ao grupo e ao privado de quem assumiu, e o
   título do evento.

## Como rodar

- `.venv/bin/python -m pytest -p no:warnings -o addopts="" tests/test_test_mode.py`
  (a suíte inteira trava no sandbox; rode arquivos específicos).
- Os testes em Postgres real (`tests/test_test_mode_pg.py`) ficam pulados sem
  `TEST_DATABASE_URL`; eles já passaram num Postgres descartável.

## Formato da resposta

Em português. Para cada achado: arquivo:linha, gravidade (P1 bloqueia o merge,
P2 corrigir antes do merge, P3 pode esperar), o cenário concreto que quebra e a
correção sugerida. No fim, uma nota de 1 a 5 e se recomenda o merge.
