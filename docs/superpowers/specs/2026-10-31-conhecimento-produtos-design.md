# Conhecimento profundo dos produtos Converge — design

Data: 2026-10-31 · Status: aguardando revisão

## Objetivo

O atendimento da Converge deve conseguir explicar, com profundidade, cada
produto listado no site: o que é, para quem serve, funcionalidades, cenários de
uso, como funciona e o que ainda não existe. Hoje cada produto tem 1 a 3
parágrafos em `content/faq.md`.

Sucesso = um prospect pergunta "explica o Assistente Jurídico a fundo" ou "o que
o Construction Manager vai fazer?" e recebe uma resposta rica, fiel à base e sem
promessas falsas.

## Fora de escopo

- Mudar a arquitetura de recuperação (tags, filtros por produto, vários
  documentos). Base de dezenas de trechos não justifica.
- Sincronizar automaticamente a partir dos outros repositórios.
- Preço, prazo e pagamento: já cobertos por seções existentes.

## Regras de conteúdo

1. **Só afirmar o que está escrito.** Fontes: páginas do site
   (`atendimento-ia`, `ai-legal-assistent`) e os READMEs dos repos de
   referência. O agente continua sem inventar; pergunta fora da base vai para a
   equipe.
2. **Anonimato total.** Nenhum nome de cliente (nem o escritório do Assistente
   Jurídico). Nenhum detalhe interno que o site não diga (infraestrutura,
   segurança além do já público, PJe, nomes de módulos).
3. **Status honesto e explícito.** Cada produto declara o que está em produção,
   o que está no roteiro e o que não existe.
4. **Título com o nome do produto**, ex.: "Assistente Jurídico — o que ele faz
   com o acervo?", para a busca por similaridade não confundir produtos.

## Produtos e seções

### Atendimento com IA (em produção, usado no próprio atendimento Converge)
Visão geral · Para quem serve e cenários · Fluxo de uma mensagem · Capacidades
em produção · Agenda, lembrete, remarcação e cancelamento · Painel de funil e
cadastro · Limites e roteiro (WhatsApp/Instagram sob demanda, encaminhamento
para a pessoa certa, caixa de e-mail, acesso por pessoa, Microsoft 365).

### Assistente Jurídico (em produção em escritório real)
Visão geral · Para quem serve e cenários · Fluxo de uma pergunta · Acervo
(formatos, OCR, importação em lote, busca) · Pesquisa externa e acompanhamento de
processos (fontes oficiais, DataJud, DJEN, OAB) · Redação, revisão e exportação ·
Pastas e permissões por posto · Citação conferida e avaliação das respostas ·
Limites e roteiro (já em `faq.md`, será mantido e ampliado).

### Construction Manager (**em desenvolvimento, sem cliente em produção**)
- Status: em desenvolvimento; as fases 1 e 2 foram testadas apenas em ambiente
  local interno. O agente deve dizer exatamente isso e nunca falar como se
  clientes já usassem.
- Para quem: construtoras de qualquer porte que trabalhem com obra, orçamento e
  estoque.
- Visão: solução completa para a obra — orçamento, planejamento, controle,
  acompanhamento em tempo real e execução —, com arquivos (notas fiscais,
  contratos) organizados sem sobrecarregar o engenheiro.
- Desenvolvido e testado localmente (fases 1–2): diário de obra a partir de
  texto, voz ou foto do mestre de obras, publicado em PDF na pasta da obra;
  leitura de nota fiscal fotografada e lançamento no comparativo
  orçado × realizado, com conferência humana quando algo não é certo.
- Futuro (fases 3–4 e visão): cotação com fornecedores por e-mail e comparação
  das respostas; painéis; orçamentação automatizada como alternativa às
  plataformas caras; planejamento automatizado; controle de estoque por consumo
  informado em campo, com projeção de quanto e quando comprar guiada pelo
  planejamento; "agente secretário" que mantém planejamento, orçamento e estoque
  atualizados.
- Regra de confiança: o agente nunca compra nem fecha nada sozinho.
Seções: Visão geral e status · Para quem e cenários · O que já foi testado ·
O que vem a seguir · Como compra e estoque funcionarão (planejado).

## Mudanças de código

- `core/config.py`: `knowledge_top_k` de 4 para 6, para perguntas amplas
  trazerem mais seções do mesmo produto. `knowledge_min_similarity` fica.
- Nenhuma outra mudança de código.

## Seções existentes

As respostas atuais sobre produtos em `faq.md` são mantidas e coerentes com as
novas (sem contradição de status). A resposta "Construction Manager … ainda em
desenvolvimento" é ampliada para apontar a nova descrição.

## Testes

Novo teste em `tests/test_knowledge.py` (ou `tests/test_content.py`) sobre o
`faq.md` real:
- Cada produto tem o conjunto mínimo de seções com o nome no título.
- O texto do Construction Manager contém "em desenvolvimento", "nenhum cliente"
  e "nunca compra", e nenhuma das frases "já está em produção", "já em
  produção", "em uso por" (ele pode dizer que "não está em produção").
- Nenhum termo proibido (nome do cliente, "PJe") aparece.
- Todo chunk é gerado por `split` sem corpo vazio.
Suíte existente continua verde.

## Entrega e operação (passos que o autor executa)

O código e o `faq.md` ficam no GitHub (branch `feat/conhecimento-produtos`).
Depois do merge:
1. Atualizar a cópia do FAQ no Drive do cliente (ela vence o arquivo local
   quando `KNOWLEDGE_DOCUMENT_ID` está definido), com o conteúdo novo.
2. No servidor: atualizar a imagem e rodar
   `python cli/load_content.py --faq-only`.
3. Testar por mensagem: perguntar sobre cada produto e sobre algo fora da base
   (deve encaminhar para a equipe).

## Riscos

- Texto longo demais por seção dilui a busca: manter cada seção curta e focada.
- Drive desatualizado: o agente continua respondendo com a versão antiga.
- Status do Construction Manager envelhece: lembrar de atualizar ao ir para
  produção.
