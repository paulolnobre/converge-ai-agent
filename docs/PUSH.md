# Notificações push no app do painel

Quem ativou as notificações recebe:

- **"💬 Nova conversa: Ana · WhatsApp"** — toda a equipe, na primeira mensagem de alguém (ou na primeira depois de 24h de silêncio), no máximo uma vez por dia por contato. As respostas seguintes do bot não avisam.
- **"🙋 Ana quer falar com uma pessoa · …"** — toda a equipe, quando o cliente pede uma pessoa ou manda um arquivo que abre um pedido.
- **"Nova mensagem de Ana."** — só quem assumiu a conversa, quando o cliente escreve, no máximo a cada 5 minutos.

São os mesmos avisos do Telegram, que continua em paralelo. A notificação nunca leva o que o cliente escreveu, e tocar nela abre a conversa.

## Ligar (uma vez, no servidor)

```bash
sudo -u deploy bash /opt/converge-agent/push-keys.sh /opt/converge-agent/.env
cd /opt/converge-agent && sudo -u deploy docker compose up -d reception-bot
```

- O script gera o par de chaves VAPID e grava no `.env` sem mostrar nada. Ele se recusa a rodar se as chaves já existirem, porque trocar as chaves desliga todos os aparelhos inscritos.
- Guarde `PUSH_VAPID_PRIVATE_KEY` no gerenciador de senhas, junto com as `DATA_KEYS`.

## Ativar em cada aparelho

- **iPhone ou iPad (iOS 16.4 ou mais novo):** primeiro instale o app pela página "Instalar", com Compartilhar e depois "Adicionar à Tela de Início". Abra pelo ícone, toque em **Ativar notificações** no menu e permita.
- **Android:** no Chrome, instalado ou não, toque em **Ativar notificações** no menu e permita.
- **Computador:** no app instalado ou no navegador, toque em **Ativar notificações** no menu e permita.
- Se aparecer **"Atualize o app para ativar notificações"**, use o aviso "Nova versão disponível · Atualizar".
- Em **Aparelhos conectados** aparece "notificações ativas" em cada aparelho inscrito.

## Roteiro de teste

1. Ative as notificações no aparelho.
2. Assuma uma conversa de teste no painel (`/teste` no Telegram).
3. Trave a tela e escreva nessa conversa pelo Telegram de teste.
4. A notificação "🧪 TESTE · Nova mensagem de …" deve chegar.
5. Toque nela: o app abre direto na conversa.

## Desligar

- **Num aparelho:** use "Notificações ativas · desativar" no menu.
- **Em todos:** tire as três linhas `PUSH_VAPID_*` do `.env` e reinicie o bot. O item some e nada é enviado.

## Como funciona

- **Envio:** cifra para o aparelho e assina com a chave VAPID antes de postar no serviço de push (Apple, Google, Mozilla ou Microsoft), que não consegue ler o conteúdo.
- **Sem atrasar o atendimento:** roda em segundo plano, com prazo de 10 s, no máximo 4 envios ao mesmo tempo, e nunca atrasa a resposta ao cliente.
- **Inscrição apagada:**
  - quando o serviço responde que ela não existe mais;
  - quando o aparelho é desconectado ou a sessão é apagada.
- **Sessão vencida:** se a sessão venceu mas ainda não foi apagada, o aparelho deixa de receber.
- **Código:**
  - envio: `core/services/push.py`;
  - validação do endereço: `core/services/push_endpoint.py`;
  - rotas: `core/web/panel.py`;
  - app: `core/web/static/painel/sw.js` e `pwa.js`.
