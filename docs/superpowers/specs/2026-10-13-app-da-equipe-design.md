# App da equipe (PWA) — especificação

Data: 13/10/2026 · Status: aguardando revisão do Paulo

## Objetivo

A equipe atende pelo celular num app de verdade:
- **Instalação:** fica na tela inicial e abre em tela cheia, direto na Inbox.
- **Sessão:** continua conectado.
- **Notificações:** avisam quem espera uma pessoa.
- **Respostas prontas:** permitem responder rápido.

O app substitui aos poucos o papel do grupo do Telegram para a equipe. Os dois rodam em paralelo até o Telegram ser desligado por configuração.

**É a validação do caminho 1:** um PWA em cima do painel atual. Se der certo, o caminho 2 (o painel reescrito em React) reaproveita tudo o que é do servidor. Isso inclui sessões por aparelho, notificações e respostas prontas.

**Pronto quando:**
- os três instalaram o app (Android e iPhone);
- recebem as notificações da tabela da parte 3;
- tocar numa notificação abre a conversa já logado;
- as respostas prontas funcionam;
- dá para desconectar um aparelho de outro aparelho.

## Decisões já tomadas com o Paulo (10–13/10/2026)

1. **Caminho 1 (PWA)** para validar, e caminho 2 (React) depois, se der certo. App nativo nas lojas, não.
2. **Prioridades:**
   - entra agora: sessão sem login repetido (B), cara de app (C), respostas prontas (D);
   - foto, documento e áudio ficam para a próxima etapa.
3. **O app substitui o Telegram para a equipe, de forma gradual.** Os dois avisam em paralelo, e o Telegram é desligado por configuração.
4. **Sessão no estilo do WhatsApp:**
   - o aparelho fica conectado até 90 dias sem uso ou até ser desconectado;
   - a trava por biometria (passkey) é opcional, ligada por cada pessoa, numa etapa seguinte;
   - qualquer pessoa da equipe pode desconectar qualquer aparelho.
5. **Notificações dizem quem e o quê, nunca o conteúdo da mensagem.**
6. **O cookie Lax** já está feito à parte (PR #16): o link do Telegram abre já logado.

---

## Parte 1 — Instalação e cara de app

### Arquivos novos
Ficam em `core/web/static/painel/` e são servidos pela lista fechada de `core/web/panel.py`:

| Caminho | Conteúdo |
|---|---|
| `/painel/manifest.webmanifest` | `name` "Converge", `short_name` "Converge", `start_url` `/painel/#inbox`, `scope` `/painel/`, `display` `standalone`, `background_color` e `theme_color` `#0A0C0F`, ícones |
| `/painel/sw.js` | o service worker, com escopo `/painel/` (servido da própria pasta para ter esse escopo) |
| `/painel/assets/icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `apple-touch-icon.png` | o logo da Converge |
| `/painel/instalar` | página com o passo a passo, para Android e iPhone |

### No `index.html` (e nas páginas de login)
- `<link rel="manifest">`, `apple-touch-icon`, `theme-color` e `apple-mobile-web-app-capable`.

### Instalação
- **Android/Chrome:** o evento `beforeinstallprompt` mostra um botão **"Instalar app"** no menu lateral.
- **iPhone/Safari:** fora do modo instalado (`display-mode` não `standalone`), aparece um aviso **uma vez**, guardado no `localStorage`, que leva a `/painel/instalar`.

### Service worker
- **Casca em cache:** HTML, CSS, JS, ícones e fontes. **Nunca** guarda respostas de `/painel/api/*`.
- **Sem rede:** as chamadas à API mostram "Sem conexão — tentando de novo".
- **Versão:** cada deploy muda a versão do cache (o `app_version`). O app mostra **"Nova versão disponível — atualizar"**, que recarrega.
- **Ao sair ou ser desconectado:** a página e o service worker apagam os caches.

### Selo no ícone
- `navigator.setAppBadge(n)`, onde `n` é o `waiting_total` da Inbox, o mesmo número do menu.
- Atualizado pelo polling da página e por cada notificação recebida.

### CSP do painel
- Entram só `worker-src 'self'` e `manifest-src 'self'`.

---

## Parte 2 — Sessão por aparelho

### Dados — migração aditiva `0011_device_sessions.sql`
```sql
ALTER TABLE panel_sessions
  ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'browser'
    CHECK (kind IN ('browser', 'device')),
  ADD COLUMN IF NOT EXISTS device_name VARCHAR(80);
```

### Login
- **A opção:** o login ganha **"Manter conectado neste aparelho"**.
  - **Marcada por padrão** no app instalado (`display-mode: standalone`).
  - **Desmarcada** no navegador.
- **Marcada:**
  - a sessão nasce como `kind='device'`;
  - o `device_name` vem do `User-Agent` (ex.: "iPhone · Safari", "Android · Chrome");
  - o cookie dura 90 dias e é renovado (novo `max_age`) no máximo uma vez por dia de uso.
- **Desmarcada:** como hoje (`PANEL_IDLE_MINUTES`, `PANEL_SESSION_HOURS`).
- **Validade de uma sessão `device`:** só `last_seen_at` nos últimos `PANEL_DEVICE_IDLE_DAYS` (padrão 90). Não há limite absoluto.

### Aparelhos conectados
- **A tela:** nova tela no painel. Lista todas as sessões `device` ativas da equipe:
  - pessoa, aparelho, último uso;
  - se o aparelho tem **notificações ativas**;
  - o botão **Desconectar**.
- **Quem desconecta:** qualquer usuário do painel pode desconectar qualquer aparelho.
- **O que desconectar faz:**
  1. apaga a sessão e as inscrições de notificação dela;
  2. registra `device_revoked` em `panel_auth_events`, com quem desconectou.
- **No próximo uso:** o aparelho recebe 401, volta ao login e apaga os caches.
- **No terminal:** `python -m cli.panel_users disconnect <email>` desconecta todos os aparelhos de alguém.

### Registro
- `panel_auth_events` passa a gravar também `device_signed_in` e `device_revoked`.

---

## Parte 3 — Notificações

### Chaves VAPID
- Geradas no servidor e guardadas só no `.env`: `PUSH_VAPID_PUBLIC_KEY`, `PUSH_VAPID_PRIVATE_KEY`, `PUSH_VAPID_SUBJECT=mailto:team@getconverge.me`.
- Sem elas, as notificações ficam desligadas e o resto funciona.
- Envio com `pywebpush` (dependência nova).

### Dados — na mesma migração `0011`
```sql
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id             BIGSERIAL PRIMARY KEY,
    session_hash   CHAR(64) NOT NULL REFERENCES panel_sessions(token_hash) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    endpoint       TEXT NOT NULL UNIQUE,
    p256dh         TEXT NOT NULL,
    auth           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ok_at     TIMESTAMPTZ,
    failures       INTEGER NOT NULL DEFAULT 0
);
```
- **Desconectar o aparelho** (apagar a sessão) leva a inscrição junto, em cascata.

### Ativar
- O app instalado mostra **"Ativar notificações"**. O iPhone exige um toque.
- O toque pede a permissão, cria a inscrição com a chave pública e a envia para `POST /painel/api/push/subscribe`.

### O que vira notificação

| Evento | Destino | Texto (exemplo) | Ao tocar |
|---|---|---|---|
| Cliente pediu uma pessoa | toda a equipe, prioridade alta | "🙋 Ana quer falar com uma pessoa · WhatsApp" | a conversa |
| Nova conversa | toda a equipe | "💬 Nova conversa: Ana · WhatsApp" | a conversa |
| Cliente escreveu numa conversa que você assumiu | só quem assumiu | "Ana escreveu na conversa que está com você" | a conversa |
| Reunião marcada, remarcada ou cancelada | toda a equipe | "📅 Ana marcou uma conversa para 15/10 às 15:30" | a reunião no painel |
| "Aconteceu?" | toda a equipe | "A conversa com Ana (15/10 15:30) aconteceu?" | Android: botões *Aconteceu* e *Não compareceu* na notificação; iPhone: abre a pergunta no app |
| Resumo da manhã | toda a equipe | "☀️ Resumo do dia" | a tela "Resumo do dia" |
| Cliente **não confirmou** a reunião | toda a equipe | "⚠️ Ana ainda não confirmou a conversa de hoje às 15:30" | a reunião no painel |

- **Ficam em silêncio**, sem notificação, só no painel: alguém assumiu ou devolveu, atendimento encerrado por inatividade e cliente **confirmou** presença pelo lembrete.

### Reunião não confirmada (pedido do Paulo, 13/10/2026)
- **Regra:** a equipe só fica sabendo da confirmação quando ela **não** acontece.
- **Quando sai:** **2h antes do início**, para cada reunião confirmada na agenda cujo lembrete já foi enviado (`reminder_sent_at` preenchido) e que continua sem confirmação (`attendance_confirmed_at` nulo), sai um aviso **uma vez só**.
- **Por onde:** pelo `team_notices`, então vai também ao Telegram enquanto ele estiver ligado.
- **Sem lembrete enviado, não há aviso:** sem lembrete, o cliente nem teve como confirmar.
- **Onde roda:** no mesmo laço do worker de lembretes.
- **Dados:** a coluna `appointments.unconfirmed_noticed_at TIMESTAMPTZ` entra na migração `0011` e garante o "uma vez só".
- **Antecedência:** `UNCONFIRMED_NOTICE_HOURS` (padrão 2).

### Textos
- **Iguais aos do Telegram:** os textos vêm de `content/messages.toml`, os mesmos que o Telegram usa. **Nunca levam o conteúdo da mensagem do cliente.**
- **Modo teste:** contatos em teste saem com "🧪 TESTE ·", pela mesma `team_text`.

### Um ponto de saída para avisos à equipe
- **O serviço novo:** `core/services/team_notices.py` passa a ser o caminho de todos os avisos da tabela. Hoje eles saem por `inbox.notify_team`, `handoff`, `attendance`, `inbox.ping_owner` e `secretarial`.
- **O que ele faz:** manda para o Telegram (se `TEAM_TELEGRAM_NOTICES` estiver ligado) e para as notificações push (se houver chaves e inscrições).
- **O envio push:**
  - roda **em segundo plano**, depois da resposta ao cliente, com prazo curto;
  - **nunca atrasa nem derruba o turno**;
  - quando o serviço de push responde **404/410**, a inscrição é apagada;
  - as outras falhas contam em `failures` e vão para o log.

### Ações na notificação e telas novas
- **Aconteceu / Não compareceu:**
  - `POST /painel/api/appointments/{id}/outcome` (`done` | `no_show`), chamado pelo service worker na ação da notificação, ou pela tela do app;
  - reaproveita `appointments_repo.set_outcome`, em que a primeira resposta vale (inclusive se veio do Telegram).
- **Resumo do dia:**
  - `GET /painel/api/briefing` devolve o texto de `secretarial.daily_briefing`;
  - uma tela "Resumo do dia" mostra esse texto.

---

## Parte 4 — Respostas prontas

### Dados — na migração `0011`
```sql
CREATE TABLE IF NOT EXISTS quick_replies (
    id          SERIAL PRIMARY KEY,
    title       VARCHAR(40) NOT NULL,
    body        VARCHAR(4000) NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```
- Começa com três sugestões: "Boas-vindas", "Link da reunião" e "Encerrar".

### API
- `GET`, `POST`, `PATCH` e `DELETE` em `/painel/api/quick-replies`.
- Compartilhadas por toda a equipe.

### Tela e uso
- **Tela "Respostas prontas":** criar, editar, apagar e reordenar.
- **Na Inbox:**
  - um botão **⚡** ao lado do campo de resposta abre a lista, com busca pelo título;
  - **`/` no começo do campo** abre a mesma lista, filtrada.
- **O toque coloca o texto no campo, não envia.**
- **Marcadores preenchidos na hora de inserir:**
  - `{nome}`: o primeiro nome do cliente;
  - `{atendente}`: o primeiro nome de quem está logado;
  - um marcador desconhecido fica como está.
- **Mesmas regras da resposta normal:** só quem assumiu responde, e no WhatsApp vale a janela de 24h.

---

## Parte 5 — Telegram em paralelo e desligamento

- **`TEAM_TELEGRAM_NOTICES`** (`on` | `off`, padrão `on`).
  - Com `off`, os avisos da tabela da parte 3 param de ir para o grupo e para o privado do Telegram.
  - Muda-se no `.env` e reiniciando o bot, sem deploy.
- **Na transição:** os dois avisam. O "aconteceu?" respondido em um vale para os dois.
- **Critério sugerido para desligar:** 2 semanas com os três com notificações ativas, sem aviso perdido. A tela "Aparelhos conectados" mostra quem está com notificações ativas.
- **Continua no Telegram mesmo com `off`:**
  - o Telegram como **canal de clientes**;
  - o `/teste` por lá;
  - os comandos de grupo `/id` e `/painel`.
- **Resumo da manhã com `off`:** só no app.

---

## Testes

- **Manifest e service worker:**
  - servidos pela lista fechada;
  - o SW tem escopo `/painel/`;
  - o SW nunca coloca `/painel/api/` em cache (teste do próprio arquivo JS lido como texto).
- **Sessão `device`:**
  - criada com a opção marcada;
  - válida com 89 dias sem uso e vencida com 91;
  - o cookie renovado no máximo uma vez por dia;
  - a sessão de navegador inalterada.
- **Aparelhos:**
  - qualquer usuário desconecta qualquer aparelho;
  - desconectar apaga a inscrição;
  - o aparelho desconectado recebe 401;
  - o evento é registrado;
  - o comando `disconnect` da CLI.
- **Notificações:**
  - cada evento da tabela gera notificação para o destino certo;
  - a reunião sem confirmação avisa uma vez, 2h antes; quem confirmou não gera aviso; sem lembrete enviado, também não;
  - os silenciosos não geram;
  - o texto nunca contém a mensagem do cliente;
  - o modo teste leva o prefixo;
  - 404/410 apaga a inscrição;
  - uma falha de push não afeta o turno;
  - sem chaves VAPID, nada quebra.
- **`TEAM_TELEGRAM_NOTICES=off`:** nenhum aviso vai ao Telegram, e os push continuam.
- **Outcome:**
  - o endpoint registra;
  - uma segunda resposta (app ou Telegram) não muda nada;
  - vale a checagem de origem.
- **Respostas prontas:** CRUD, os dois marcadores, o desconhecido intacto, o texto inserido e não enviado.
- **No navegador (Playwright):**
  - o painel instalável (manifest válido);
  - o botão ⚡ e o atalho `/`;
  - a tela de aparelhos, nos temas claro e escuro e no celular.
- **Num celular de verdade:** instalação no Android e no iPhone, e notificação recebida com a tela travada. Feito por vocês, com o roteiro do PR.
- **Revisão:** o Codex revisa no fim.

## Fora desta etapa

- Foto, documento e áudio pela Inbox (mandar e ver).
- A trava por biometria (passkey) ao abrir o app.
- Papéis no painel (admin e atendente).
- Preferências de notificação por pessoa (ex.: silenciar "nova conversa").
- Modelos aprovados pela Meta para escrever depois de 24h.
- O caminho 2 (React).
