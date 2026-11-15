# App da equipe — incremento 1: instalar e manter conectado

Em 26/10, Paulo confirmou que a agenda está funcionando e priorizou o uso no
celular. A agenda para no incremento 2; vínculo e ações no painel ficam na fila.

Base: `main` após PR #30 (`150e3e3`). Branch: `feat/app-celular`.
Desenho original: `docs/superpowers/specs/2026-10-13-app-da-equipe-design.md`.
Atualizações locais do contrato: `.docs/APP_CELULAR_INCREMENTO_1.md`.

## Entrega

- PWA com identidade estável e início em `/painel/#inbox`, ícones do logo
  existente, instruções para iPhone e Android e instalação quando suportada.
- Opção explícita de manter o aparelho conectado, marcada por padrão no app
  instalado. Senha e autenticador continuam obrigatórios.
- Sessão confiável vence após 90 dias sem uso, com cookie renovado no máximo
  uma vez por dia. Sessões normais e a etapa entre senha e código mantêm seus
  limites anteriores.
- Tela de aparelhos: qualquer membro autenticado pode desconectar um aparelho.
  A sessão some imediatamente, o próximo uso é recusado e a ação é auditada.
- CLI `python -m cli.panel_users disconnect <email>`.
- Aviso de conexão, página offline e atualização oferecida sem recarregar
  uma resposta que está sendo escrita.

## Decisões de implementação

- Migração aditiva e repetível **0014**, pois 0011–0013 já foram usadas.
- Um id numérico identifica a sessão na tela, sem expor token ou hash.
- A preferência de sessão fica na etapa de senha e é carregada pelo servidor
  até a conclusão do segundo fator. Marcar a opção não abre acesso antes dele.
- O service worker só armazena uma lista fechada de arquivos públicos e a
  página offline. HTML autenticado, APIs, mídias e URLs com query ficam fora
  do cache. Os dados das conversas continuam dependentes de conexão.
- A versão do cache inclui a impressão dos arquivos do painel; uma mudança
  nesses arquivos altera o worker mesmo se APP_VERSION ficar no padrão.
- O manifesto e a navegação usam `/painel/` para ficar dentro do mesmo escopo.
- Notificações push e respostas prontas são os incrementos seguintes.

## Validação e publicação

1. Regressões HTTP de MFA, cookies, revogação, origem e rotas públicas.
2. PostgreSQL isolado: 89/90/91 dias, sessão curta, renovação diária, migração
   repetida, listagem sem tokens e desconexão.
3. Chromium: login em viewport móvel, Inbox, aparelhos, service worker,
   offline, cache sem API e atualização.
4. Suíte completa com PostgreSQL e Redis descartáveis, Ruff e Pyright.
5. Commit somente do escopo, push e PR; autoria do Paulo.
6. Merge só após confirmação explícita do Paulo, pois dispara a implantação.
7. Após deploy: instalação real no iPhone/Android, fechar e reabrir pela tela
   inicial, confirmar a sessão e desconectar pelo outro aparelho.
