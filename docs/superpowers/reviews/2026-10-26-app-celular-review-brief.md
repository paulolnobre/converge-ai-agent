# Revisão — app para celular, incremento 1

Branch `feat/app-celular`, base `150e3e3` (PR #30). Plano:
`docs/superpowers/plans/2026-10-26-app-celular-incremento-1.md`.

## Comportamento

Instala o painel como Converge e abre na Inbox. No app instalado, a opção de
manter o aparelho conectado começa marcada; no navegador, desmarcada. Os
dois fatores continuam obrigatórios. A sessão confiável vence por inatividade
(90 dias, configurável) ou revogação, e qualquer membro autenticado pode
desconectar um aparelho pelo painel. A CLI também pode revogar por pessoa.

O service worker guarda apenas arquivos públicos de uma lista fechada e uma
página offline. APIs, mídias, páginas autenticadas e URLs com query ficam
fora do cache. A versão combina APP_VERSION com a impressão dos arquivos.
Uma atualização é oferecida à pessoa, com confirmação antes de descartar uma
resposta escrita. O botão offline força uma navegação de rede, inclusive
quando a página offline foi devolvida no endereço original da Inbox.

## Onde conferir

1. `core/repositories/panel_auth.py`: a duração longa só para `kind=device`
   **e** MFA concluído; preferência na sessão curta entre senha e código;
   renovação diária; filtros da lista e limpeza de sessões.
2. `core/web/panel.py`: cookie Secure/HttpOnly/Lax em `/painel`, origem nas
   ações, revogação atual sem renovar cookie apagado, auditoria do ator.
3. `core/web/static/painel/sw.js`: nenhuma resposta privada armazenada, nem
   quando alguém navega diretamente para uma URL de API.
4. `pwa.js`: recuperação offline, limpeza de cache ao sair, atualização por
   ação explícita, abertura após restauração de histórico.
5. `0014_device_sessions.sql`: aditiva, repetível, sessões existentes ficam
   de navegador. A tela usa ids numéricos, sem token, hash ou User-Agent bruto.

## Evidência

- Validação final: **1327 testes passaram em 34,90s**, com PostgreSQL/Redis
  descartáveis; Ruff e Pyright sem erros e `git diff --check` limpo.
- Suíte com PostgreSQL/Redis descartáveis; regressões HTTP de login/MFA,
  revogação, origem e cookie; SQL real em schema isolado para os limites
  89/90/91 dias, renovação, limpeza e migração repetida.
- Regressões JS executam o worker para testar o bloqueio de cache em APIs,
  mídia e HTML autenticado; executam o botão que recupera a navegação offline.
- Chromium com perfil persistente: manifesto sem erro e sem impedimentos de
  instalação; viewport móvel, login real com dois fatores e banco isolado,
  cookie de 90 dias, fechar/reabrir, offline/reconectar, atualização do worker,
  tela de aparelhos e revogação por outro usuário. Console sem erros JS.
- As listas de atendimento no cenário de navegador são sintéticas; o login,
  as sessões e a revogação usam as rotas reais e PostgreSQL. Nenhum worker de
  atendimento ou provedor de calendário/mensagens foi iniciado.
- Capturas móveis verificadas em claro e escuro. A simulação móvel usa
  Chromium; instalação no Safari/iPhone e Chrome/Android reais fica para o
  roteiro pós-deploy em `docs/APP_CELULAR.md`.

## Publicação

Não exige novas chaves. `PANEL_DEVICE_IDLE_DAYS=90` é o padrão. A migração
0014 roda no deploy normal. Push e respostas prontas pertencem aos próximos
incrementos. Merge depende da confirmação explícita do Paulo, pois aciona a
implantação automática.
