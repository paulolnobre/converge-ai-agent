# Revisão — escolher o dia, depois o horário, e o limite de agendamentos

Branch `feat/escolher-dia`, diff `git diff origin/main..HEAD`. Spec:
`docs/superpowers/specs/2026-10-15-escolher-dia-design.md`. Plano:
`docs/superpowers/plans/2026-10-21-escolher-dia.md`.

Com `BOOKING_MODE=choose_day`, o cliente vê uma lista de dias com horário livre
e depois a lista de horários do dia (listas do WhatsApp, botões do Telegram),
por toque ou por texto. Com `MAX_UPCOMING_APPOINTMENTS`, um contato não passa
do limite de conversas futuras. Os dois ficam desligados por padrão.

## Onde olhar com mais cuidado

1. **O fluxo nos handlers** (`core/handlers/message.py`: `_offer`,
   `_offer_days`, `_offer_day_times`, `_handle_pickday`, os toques `day:`,
   `days:`, `more:`, `limit:keep`): cada ponto de entrada da tabela da spec,
   listas velhas, toques malformados, a espera `pickday`.
2. **O limite e a remarcação** (`scheduling.take_offer`): a trava por contato
   antes de qualquer leitura decisiva, `stale_reschedule`, a conta sem a
   substituída, e o que acontece com a `Offer`.
3. **A disponibilidade** (`scheduling.available_starts`,
   `core/services/booking_lists.py`): horizonte no fuso do escritório,
   antecedência, grade, agendas em escopo, páginas.
4. **A leitura do texto** (`slot_choice.read_day_reply`, e "10h" = 10h00 em
   `interpret`): a tabela da spec, e se a mudança quebra algo no modo `suggest`.
5. **Os canais** (`telegram.menu`, `channels.telegram_markup`, `send_list`):
   chaves `_*` nunca chegam ao Telegram; limites do WhatsApp.
6. **O modo desligado**: com `suggest` e limite 0, nada muda para o cliente.

## Como rodar

`.venv/bin/python -m pytest -q -p no:warnings -o addopts="" tests/test_booking_lists.py tests/test_slot_choice.py tests/test_channels.py tests/test_booking.py tests/test_choose_day.py tests/test_desk_flow.py`
(a suíte inteira trava no sandbox; rode arquivos específicos). Os testes em
Postgres (`tests/test_booking_pg.py`) ficam pulados sem `TEST_DATABASE_URL`;
eles passaram num banco descartável (932 passed).

## Formato

Em português. Para cada achado: arquivo:linha, gravidade (P1 bloqueia, P2
corrigir antes do merge, P3 pode esperar), cenário concreto e correção. No fim,
nota de 1 a 5 e se recomenda o merge.
