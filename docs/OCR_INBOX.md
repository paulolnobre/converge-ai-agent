# Testar leitura de texto na Inbox

O botão **Extrair texto** de uma imagem ou PDF recebido abre uma janela com
o texto e, para imagens, o original ao lado (acima no celular). Confira antes
de copiar. A extração é manual: não envia resposta ao cliente, não alimenta
o assistente e não preenche cadastros. A janela mantém o tamanho durante a
leitura. Ao fechar, seu resultado é descartado.

O piloto usa `INBOX_OCR_ENABLED=true` na configuração do servidor. O padrão
é `false`: para retirar o recurso, defina `false` e recrie apenas o serviço
do bot pelo fluxo normal. O botão some e a API recusa novas extrações. Os
anexos originais seguem a captura/limpeza de mídia já existente.

## Limites do piloto

- Imagens JPEG, PNG, WebP e GIF (primeiro quadro), até 20 milhões de pixels.
- Imagens e PDFs de até 10 MB; no máximo as três primeiras páginas de PDF.
- Até 20 mil caracteres, com aviso quando a leitura for parcial.
- Português e inglês. PDF com texto usa a camada existente; páginas
  escaneadas usam OCR. PDFs protegidos são recusados.
- Um trabalho por vez por processo do servidor; os demais recebem um aviso
  para tentar em instantes. Tempo total de até 45 segundos.

Fotos borradas, inclinadas e manuscritos podem produzir leitura incorreta.
O piloto oferece texto bruto para a equipe conferir; campos estruturados e
interpretação clínica exigem outro escopo.

## Processamento e privacidade

Tesseract, Poppler, Pillow e pypdf rodam no próprio servidor. Não há provedor
externo ou chave de API. O processo de leitura recebe só o arquivo e um
ambiente mínimo, sem variáveis de credenciais do app, com limites de memória, CPU e
arquivos. Originais em claro e páginas renderizadas ficam apenas em uma
pasta privada de `/dev/shm` (memória; limite de 128 MB no Compose), removida inclusive em timeout ou
cancelamento normal da requisição. Não há texto extraído no banco, cache ou
logs. A resposta HTTP é `no-store`.

Em produção a leitura roda num container próprio (`ocr` em
`deploy/docker-compose.prod.yml`, `core/services/ocr_server.py`): mesma imagem,
mas sem rede nenhuma, sem `.env`, sem credenciais do Google, sem arquivos de
clientes, sem banco, disco somente leitura e sem capacidades. O bot fala com
ele por um socket num volume que o bot monta só para leitura; o container não
consegue abrir conexão com nada. Um arquivo feito para explorar o Tesseract ou
o Poppler não encontra chaves nem tem para onde mandar nada. O bot confia na
resposta só como texto, número de páginas e um sinal, cortados aos mesmos
limites, com um prazo único para conectar, enviar e receber
(`core/services/ocr_remote.py`). Se o bot desiste, a leitura para e os
arquivos em memória somem.

Em produção, sem `PANEL_OCR_SOCKET`, o recurso fica desligado: nunca roda
dentro do bot. Uma imagem anterior ao container não conhece
`INBOX_OCR_ENABLED`; num rollback, a leitura simplesmente some. Só em
desenvolvimento, sem socket, ela roda num processo filho do servidor.

Só uma sessão com os dois fatores concluídos pode pedir a leitura. A API
confere se o anexo pertence ao contato solicitado e ainda está disponível;
confere novamente depois do OCR. Arquivo vencido ou removido não gera um
resultado. Os prazos do original continuam os da mídia.

## Validação reproduzível

`tests/test_panel_ocr.py` verifica sessão/origem, feature flag, vínculo ao
contato, expiração durante a leitura, arquivo ausente, tipos recusados,
concorrência, tamanho, timeout e cancelamento com limpeza.
`tests/test_ocr_remote.py` cobre o protocolo pelo socket, respostas
malformadas ou grandes demais, container que para de ler, desistência no meio
da leitura, o bloqueio em produção sem socket e as restrições do serviço `ocr`
no Compose.

O job **Build image** do CI executa `tests/ocr_image_smoke.py` dentro da
imagem construída, sem rede e com sistema de arquivos somente leitura,
usando imagens e PDFs sintéticos. O teste cobre imagem, PDF textual,
escaneado, protegido, inválido e ausência de arquivos temporários ao final.

Fontes: [Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html),
[qualidade de imagens](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html) e
[Pillow](https://pillow.readthedocs.io/en/stable/reference/Image.html).
