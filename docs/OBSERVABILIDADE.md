# Observabilidade

## O que existe

- **Logs:** em JSON, uma linha por evento, com `correlation_id` e `update_id` para seguir uma mensagem do início ao fim. Nunca levam o que o cliente escreveu, só ids. Os do bot e do OCR vão para o journald do servidor e ficam **7 dias**, inclusive entre deploys.
- **Saúde:**
  - `/health` é público e diz se o processo responde;
  - `/ready` é interno e diz se banco, Redis e agenda estão prontos. O Docker usa o `/ready` para marcar o bot como `healthy`.
- **Métricas Prometheus:** ficam em `/metrics`, só dentro do servidor (o proxy bloqueia de fora). Mostram as requisições HTTP por rota e status, a fila do Redis e os tempos de processamento. O Prometheus coleta essas métricas quando o overlay de observabilidade está ligado (abaixo).
- **No banco:**
  - **Mensagens:** cada mensagem fica gravada com a intenção que a triagem deu (`sessions.intent`).
  - **Auditoria:** a tabela `audit` guarda as ações na agenda e no Google.
  - **Agendamento:** as ofertas de horário (`offers`) e as reuniões (`appointments`) guardam todo o caminho do agendamento.
- **Backup:** diário, para o S3 (restic), e uma cópia antes de cada deploy.

## Avisos de erro

Todo erro do bot (nível ERROR no log) vira um aviso para os admins:

- **Por tipo:** o mesmo erro no mesmo lugar é um tipo só, com a mensagem como está no código (sem os valores) e o tipo da exceção. Nunca leva dado de cliente.
- **Sem enxurrada:** no máximo um aviso por tipo a cada `ERROR_ALERT_MINUTES` (15 por padrão), dizendo quantas vezes aconteceu e desde que horas.
- **Entrega:** o aviso entra na sala de administração do painel e o monitor (`cli/monitor.py`) manda por notificação do app, como o aviso de bot fora do ar. Sem o monitor ativo, fica só na sala.
- **Telegram:** só se `ADMIN_ALERT_CHAT_ID` (um chat só dos admins) estiver definido; nunca no grupo da equipe. O aviso de bot fora do ar continua indo para esse chat ou, sem ele, para o grupo da equipe.

## Grafana e Prometheus

- **Onde:** `https://<host do agente>/painel/grafana/`, com o login do painel (duas etapas) e só para quem está na sala de administração. O proxy (`deploy/edge/Caddyfile`) pergunta ao painel (`/painel/api/observability/auth`) antes de cada acesso e apaga qualquer cabeçalho de login vindo de fora.
- **O que coleta:** a cada 30 segundos, as métricas do bot (`/metrics`), do servidor (node-exporter: CPU, memória, disco, rede) e do próprio Prometheus. Guarda 30 dias, até 2 GB.
- **Painel:** "Converge · Agente", na pasta Converge: bot no ar, erros por lugar, fila, tempos de resposta, chamadas e tokens de IA, serviços externos com falha e o uso do servidor.
- **Rede:** o Grafana fica numa rede só com o proxy (`converge-grafana`); nenhum outro container do servidor fala com ele.

### Ligar no servidor (uma vez)

1. `docker network create converge-grafana`
2. No `/opt/edge`: o `docker-compose.yml` e o `Caddyfile` deste repositório (`deploy/edge`), depois `docker compose up -d`.
3. No `.env` do agente: `GRAFANA_ADMIN_PASSWORD` com um valor aleatório (o login por senha fica desligado; ela só protege a conta interna) e `docker-compose.observability.yml` no fim do `COMPOSE_FILE`.
4. `docker compose up -d prometheus node-exporter grafana`. Os próximos deploys já mandam a configuração.

## Ler os logs no servidor

```bash
journalctl -t converge-agent-bot --since "2 hours ago"          # o bot
journalctl -t converge-agent-bot --since "08:00" --until "09:00"
journalctl -t converge-agent-bot -o cat | grep '"level":"ERROR"' # só erros
journalctl -t converge-agent-ocr --since today                  # o OCR
```

`docker logs converge-agent-bot` continua funcionando, mas só mostra o container atual.

## O que ainda falta

- **Decisão da triagem:** a intenção entendida, o dia e o período não ficam guardados por turno. Só a intenção vai junto da mensagem.
