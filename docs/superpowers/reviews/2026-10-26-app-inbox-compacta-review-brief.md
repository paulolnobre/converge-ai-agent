# Revisão — app compacto e OCR manual no celular e computador

Branch `feat/app-inbox-compacta`, base `def7a64` (PR #31). O escopo inclui
instalação no computador, cores sólidas e os ajustes de controles e OCR
solicitados pelo Paulo. Guia de uso em `docs/APP_CELULAR.md` e `docs/OCR_INBOX.md`.

## Comportamento

A Inbox usa toda a janela: lista e conversa lado a lado acima de 860 px e
conversa em tela cheia em janelas menores. Cores verdes e cinzas opacas nos
temas claro e escuro, incluindo entrada e instalação. Busca local sem acentos,
linhas compactas e navegação lateral recolhida enquanto atende.

Setas sem texto/fundo para voltar e enviar. Assumir somente sem atendente
(incluindo pedidos aguardando pessoa), Devolver somente com o usuário atual;
nenhuma dessas ações se outra pessoa estiver com a conversa. O servidor
recusa tomada de conversa de outra pessoa e devolução sem ser responsável,
com a trava de chat e a checagem de estado para cliques antigos preservadas.
Origem e dados do contato ficam no menu ⋮.

A janela tem altura fixa, com rolagem dentro da lista, mensagens e campo de
resposta. Digitar linhas ou trocar status não altera a altura do campo ou da
conversa. No celular, o teclado desloca apenas a barra de resposta, sem
redimensionar o restante; zoom continua disponível. O manifesto conserva
identidade, escopo, início e modo standalone do PWA existente.

OCR manual em imagens/PDFs disponíveis, em português/inglês, acionado por
Extrair texto. O resultado é exibido para conferência/cópia em uma janela
sobre a conversa; não grava cadastro, não manda mensagem e não vai para IA.
`PANEL_OCR_ENABLED=false` desativa botão e endpoint. O piloto do servidor usa
true, preparado para o próximo deploy. Não há migração nova.

## Limites e pontos de revisão

- Disponibilidade e contato do anexo verificados antes e depois da extração;
  endpoint exige MFA/origem e responde no-store. Sem cache ou texto em logs.
- Processo filho com ambiente mínimo sem variáveis de credenciais, limites
  de memória/CPU/arquivo e grupo encerrado no timeout/cancelamento. Temporários
  apenas em pasta privada de /dev/shm, removida pelo processo pai.
- Um trabalho por vez/processo, 10 MB, 20 milhões de pixels, até três páginas,
  20 mil caracteres e 45 segundos. Limitação parcial sinalizada na tela.
- PDF textual usa a camada existente; escaneado usa Poppler/Tesseract. PDF
  protegido, formato inválido e tipos sem suporte são recusados. Confirmar o
  resultado visualmente, sobretudo fotos borradas e manuscritos.
- Dependências OCR incluídas na imagem; Compose reserva até 128 MB de memória
  compartilhada para temporários. .superpowers fica fora do contexto Docker.

## Evidência

- Validação final: **1338 testes passaram em 35,49s**, com PostgreSQL/Redis
  descartáveis. Ruff, Pyright, sintaxe JS e diff sem erros.

- Testes de OCR: sessão/origem, flag, vínculo/expiração do anexo, ausência,
  tipos, limite, concorrência, timeout/cancelamento e remoção de temporários.
- A imagem executa OCR real sem rede e com raiz somente leitura: imagem, PDF
  textual, escaneado, protegido e inválido, todos sintéticos. O CI repete este
  teste na imagem construída.
- Navegador Chromium, login/MFA e PostgreSQL isolados: o botão lê um original
  sintético cifrado através da rota real, roda OCR real no container e mostra
  o resultado no computador e celular; fecha e limpa o texto.
- Desktop 1280×900 e celular 320×568, 390×844, paisagem 844×390: busca, origem,
  envio, responsabilidade, janela de 24h, documento, menus, setas e altura
  fixa ao digitar. Viewport do teclado simulado: só a barra sobe, conversa
  conserva altura. Sem erros JS ou rolagem horizontal da página.
- Manifesto sem erros e sem impedimento de instalação no Chromium. Safari,
  instalação pelo sistema e teclado físico permanecem no roteiro pós-deploy.

## Publicação

Push/PR executam CI sem implantação. Merge dispara deploy e fica com Paulo.
O OCR pode ser retirado por configuração, sem apagar anexos ou alterar a
retenção existente. A captura de imagens/áudios do WhatsApp já está ativa
em produção e foi confirmada pelo Paulo; ela é independente deste PR.
