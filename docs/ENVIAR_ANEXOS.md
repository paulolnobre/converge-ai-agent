# Enviar anexos pelo app

O Inbox e o grupo Administração compartilham o editor de anexos. A ativação
é explícita: `PANEL_UPLOADS_ENABLED=true`, após a migração 0018 e a imagem
com FFmpeg. Com a flag desligada, os controles ficam ocultos e o POST retorna
503; leitura e limpeza do que já existe continuam funcionando.

No Inbox, primeiro assuma a conversa. Use **Anexar** para selecionar uma foto,
um arquivo ou um áudio já gravado; **Gravar áudio** pede acesso ao microfone
somente ao clicar. Pare para ouvir a prévia, depois envie ou descarte. Escolher
um arquivo ou gravar nunca envia automaticamente. A legenda pode ter até 500
caracteres; áudio é enviado sem legenda, com texto separado se necessário.
Na Administração, apenas membros explícitos podem enviar ou baixar arquivos.
O nome de quem enviou fica no histórico. Uma legenda não executa comandos do bot.

## Limites e formatos

- Um arquivo por envio, até 20 MB de entrada, com prazo e no máximo dois
  processamentos simultâneos por processo. Limite por conta: 12 envios/minuto.
- Fotos JPEG/PNG/WebP: até 20 megapixels; o servidor decodifica, remove
  metadados e converte para JPEG ou PNG, até 5 MB de saída.
- Áudios: até 3 minutos. WebM/Opus do Chrome e MP4/AAC do Safari são
  convertidos localmente para Ogg/Opus mono. Sem IA ou transcrição automática.
  Um navegador sem gravação pode anexar um áudio existente. HEIC não é aceito
  nesta versão; salve como JPEG/PNG.
- WhatsApp: PDF, TXT, Word, Excel e PowerPoint, além de fotos e áudios.
  Outros tipos podem ser compartilhados no grupo e no Telegram como download.
- Nenhum documento é executado ou aberto inline. Conteúdo HTML permanece texto.
  O servidor verifica a mídia exibida pelos bytes, não pelo MIME do navegador.

## Entrega e falhas

O Inbox verifica o dono e a janela WhatsApp antes de receber os bytes e outra
vez depois de preparar o arquivo. A sessão é revalidada antes de guardar e
antes de enviar. A transação reserva uma tentativa antes de falar com o canal;
o dono permanece bloqueado durante o envio limitado a 55 segundos, como na
resposta de texto. Nenhum bloqueio de conversa dura durante upload/conversão.

Cada seleção tem um UUID. Repetir a requisição não envia novamente; o UUID
fica ligado à conta, ao destino e ao conteúdo com HMAC. Se o canal aceitou o
arquivo mas a resposta/commit se perdeu, o app mostra **envio não confirmado**.
Confira com o destinatário antes de escolher o arquivo para outro envio.
Não há repetição automática em caso incerto. O histórico só diz confirmado
quando o canal confirmou aceitação; não promete leitura no aparelho.

Os arquivos e seus nomes ficam cifrados no mesmo volume da mídia recebida.
Áudios enviados: 48 horas. Fotos e documentos: 90 dias. O prazo é verificado
na leitura mesmo antes da limpeza. Cota total e piso de espaço livre existentes
incluem esses arquivos; limpeza, purge e troca de chave reutilizam o fluxo de
mídia. O grupo recebe push sem nome, legenda ou conteúdo do arquivo.
Não há cópia persistente do rascunho no navegador. O FFmpeg recebe ambiente
sem as credenciais do app, protocolos de entrada restritos e prazos limitados.

## Implantação

1. Publicar a imagem com FFmpeg e aplicar a migração 0018 via `cli.migrate`.
2. Conferir que MEDIA_DIR está gravável no volume cifrado e há espaço.
3. Habilitar `PANEL_UPLOADS_ENABLED=true` no ambiente do bot e recriá-lo.
4. Conferir prontidão e acesso negado sem sessão. Testar gravação e reprodução
   em um iPhone/Android real; os testes automatizados usam Chromium e arquivos
   sintéticos, sem enviar nada para clientes.

Referências dos contratos de canal e gravação:
[Meta: upload de áudio](https://www.postman.com/meta/whatsapp-business-platform/request/2fuw5le/upload-audio),
[Meta: envio por ID](https://www.postman.com/meta/whatsapp-business-platform/request/goznbg0/send-audio-message-by-id),
[Telegram Bot API](https://core.telegram.org/bots/api),
[MediaRecorder](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/isTypeSupported_static).
