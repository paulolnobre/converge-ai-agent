# Conhecimento profundo dos produtos — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O atendimento explica em profundidade o Atendimento com IA, o Assistente Jurídico e o Construction Manager, só com o que está na base.

**Architecture:** Conteúdo novo em `content/faq.md` (um `##` por pergunta, nome do produto no título, formato "Produto — pergunta?"), mais `knowledge_top_k` de 4 para 6. Um teste novo trava a estrutura, o anonimato e o status do Construction Manager. Nenhum outro código muda.

**Tech Stack:** Python, pytest, `core.services.knowledge.split`, texto em português.

**Spec:** `docs/superpowers/specs/2026-10-31-conhecimento-produtos-design.md`

## Global Constraints

- Português do Brasil, tom do `faq.md` atual (frases curtas, sem marketing exagerado).
- Título de seção: `## <Produto> — <pergunta>?` com `<Produto>` exatamente `Atendimento com IA`, `Assistente Jurídico` ou `Construction Manager` (travessão `—`).
- Anonimato: nenhum nome de cliente (`Acme`), nenhum `PJe`, nenhum detalhe interno que o site não diga.
- Só afirmar o que está nas fontes: `converge-landing-page/atendimento-ia/index.html`, `converge-landing-page/ai-legal-assistent/index.html`, `nobre-silva-legal-agent/README.md` (referência, sem copiar nomes) e `ai-agents-construction/README.md`, mais o que o autor disse sobre o Construction Manager (spec).
- Construction Manager: **em desenvolvimento, nenhum cliente em produção**; fases 1–2 testadas só localmente; fases 3–4 e a visão são futuras; o agente nunca compra nem fecha nada sozinho.
- Cada corpo de seção com no máximo 1500 caracteres. Não usar `###` dentro do `faq.md`.
- Commits na branch `feat/conhecimento-produtos`, com as linhas de atribuição pedidas.

## Review Focus

- "O que o Construction Manager faz hoje?" não pode soar como produto em uso → Task 1 trava frases proibidas e exige "em desenvolvimento" e "nenhum cliente".
- Vazamento do nome do cliente em qualquer seção, inclusive as antigas → Task 1 varre o `faq.md` inteiro.
- Títulos repetidos quebram `knowledge_repo.hashes` (indexa por título) → Task 1 exige títulos únicos.
- Seção longa dilui a busca → Task 1 limita o corpo a 1500 caracteres.
- Itens futuros escritos como se existissem (compra automática, estoque) → Task 4 exige "nunca compra" no Construction Manager e marca cada item como planejado.

---

### Task 1: Teste guarda e top_k

**Files:**
- Create: `tests/test_product_knowledge.py`
- Modify: `core/config.py:243`

**Interfaces:**
- Consumes: `core.content._root() -> Path` (raiz do conteúdo), `core.services.knowledge.split(document: str) -> list[dict]` (cada item tem `heading`, `body`).
- Produces: `PRODUCTS: dict[str, int]` no teste — mínimo de seções por produto — e as funções `_chunks()` e `_product_chunks(name)`, usadas só no arquivo.

- [ ] **Step 1: Escrever os testes que falham**

Em `tests/test_product_knowledge.py`: `PRODUCTS = {"Atendimento com IA": 7, "Assistente Jurídico": 8, "Construction Manager": 5}`; `_chunks()` lê `core.content._root() / "faq.md"` e chama `knowledge.split`. Testes:
  - `test_each_product_has_its_minimum_sections`: para cada produto, o número de chunks cujo `heading` começa com `f"{name} — "` é ≥ o mínimo.
  - `test_headings_are_unique`: `len(set(headings)) == len(headings)`.
  - `test_no_section_body_exceeds_1500_characters`.
  - `test_the_faq_never_names_the_reference_client`: regex `\b(Acme|PJe)\b` não casa em `faq.md` inteiro (títulos e corpos).
  - `test_construction_manager_is_described_as_in_development`: o texto das seções do Construction Manager contém `"em desenvolvimento"`, `"nenhum cliente"` e `"nunca compra"`, e não contém nenhuma de `["já está em produção", "já em produção", "em uso por"]`.
  - `test_top_k_is_wide_enough_for_broad_questions`: `settings.knowledge_top_k == 6`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_product_knowledge.py -v`
Expected: FAIL nos testes de seções por produto, do Construction Manager e de `top_k`. Os demais passam ou falham só se o `faq.md` atual violar a regra; se `test_the_faq_never_names_the_reference_client` falhar, remover o termo do `faq.md` agora.

- [ ] **Step 3: Ajustar `knowledge_top_k` para 6 em `core/config.py:243`**, mantendo o comentário e acrescentando uma linha dizendo que perguntas amplas sobre um produto precisam de várias seções.

- [ ] **Step 4: Ajustar a redação do spec**

No spec, trocar a linha sobre "não contém 'em produção'" pela regra de frases proibidas do teste (o agente pode escrever "não está em produção").

- [ ] **Step 5: Commit** (`test: guarda de estrutura e anonimato dos produtos`).

### Task 2: Atendimento com IA

**Files:**
- Modify: `content/faq.md` (acrescentar ao fim, antes da seção final de encaminhamento, ou logo após "O que é o Atendimento com IA da Converge?")

**Interfaces:**
- Produces: ≥ 7 seções `Atendimento com IA — …`, cobrindo exatamente estes títulos:
  - `Atendimento com IA — para quem serve e em que situações ele ajuda?`
  - `Atendimento com IA — como uma mensagem é tratada, do começo ao fim?`
  - `Atendimento com IA — o que ele responde e o que nunca diz?`
  - `Atendimento com IA — como funcionam agenda, lembrete, remarcação e cancelamento?`
  - `Atendimento com IA — como funcionam o painel de funil e o cadastro do cliente?`
  - `Atendimento com IA — entende áudio e atende fora do horário?`
  - `Atendimento com IA — o que ainda não está disponível?`

- [ ] **Step 1: Escrever as 7 seções** a partir de `atendimento-ia/index.html` (seções "Como funciona", "O que já funciona", "Roteiro", "Por que confiar"). Cenários concretos (ex.: clínica que quer agendar avaliação) saem do exemplo do próprio site. "Não disponível" lista só o roteiro do site (WhatsApp/Instagram sob demanda, encaminhamento para a pessoa certa, caixa de e-mail, acesso por pessoa no painel, Microsoft 365 em cliente real).
- [ ] **Step 2: Rodar** `pytest tests/test_product_knowledge.py::test_each_product_has_its_minimum_sections -v` — o caso do Atendimento deve passar (os demais ainda falham, pois dependem das tasks seguintes; rodar sem `-k` só no fim).
- [ ] **Step 3: Commit** (`docs(faq): Atendimento com IA em profundidade`).

### Task 3: Assistente Jurídico

**Files:**
- Modify: `content/faq.md` (após "O que é o Assistente Jurídico da Converge?")

**Interfaces:**
- Produces: ≥ 8 seções `Assistente Jurídico — …`, com estes títulos:
  - `Assistente Jurídico — para quem serve e em que situações ele ajuda?`
  - `Assistente Jurídico — como uma pergunta é respondida, do começo ao fim?`
  - `Assistente Jurídico — o que ele faz com o acervo do escritório?`
  - `Assistente Jurídico — como pesquisa legislação e jurisprudência?`
  - `Assistente Jurídico — como acompanha processos e intimações?`
  - `Assistente Jurídico — redige, revisa e exporta documentos?`
  - `Assistente Jurídico — como organiza pastas e quem pode ver o quê?`
  - `Assistente Jurídico — como garante que cita o que realmente leu?`

- [ ] **Step 1: Escrever as 8 seções** a partir de `ai-legal-assistent/index.html`, usando o README do repo de referência só para confirmar o que existe (nenhum nome de cliente, nada de PJe, nada de infraestrutura). Manter as 3 seções antigas (visão, proteção de dados, o que não existe) e conferir que o roteiro delas não contradiz as novas.
- [ ] **Step 2: Rodar** `pytest tests/test_product_knowledge.py -v`. Esperado: seções do Jurídico passam; `test_the_faq_never_names_the_reference_client` e `test_headings_are_unique` continuam verdes.
- [ ] **Step 3: Commit** (`docs(faq): Assistente Jurídico em profundidade`).

### Task 4: Construction Manager e coerência final

**Files:**
- Modify: `content/faq.md` (resposta "Quais aplicações de agentes de IA…" e novas seções), `content/README.md` (uma linha avisando que o status do Construction Manager deve ser atualizado ao ir para produção).

**Interfaces:**
- Produces: ≥ 5 seções `Construction Manager — …`:
  - `Construction Manager — o que é e em que fase está?`
  - `Construction Manager — para quem é e que problemas resolve?`
  - `Construction Manager — o que já foi desenvolvido e testado?`
  - `Construction Manager — o que vem a seguir?`
  - `Construction Manager — como serão compras e estoque?`

- [ ] **Step 1: Escrever as 5 seções** com o conteúdo do spec: em desenvolvimento, nenhum cliente em produção, fases 1–2 testadas só em ambiente interno local, para construtoras de qualquer porte com obra/orçamento/estoque; planejado (rotular como planejado): cotação por e-mail, painéis, orçamentação automatizada, planejamento, estoque por consumo informado em campo com projeção de compra, agente secretário; o agente **nunca compra nem fecha nada sozinho**.
- [ ] **Step 2: Ajustar** o item do Construction Manager na resposta "Quais aplicações…" para dizer "em desenvolvimento, sem clientes em produção ainda" e apontar para as novas seções.
- [ ] **Step 3: Rodar a suíte inteira** `pytest -q`. Esperado: tudo verde, incluindo `tests/test_content.py` e `tests/test_knowledge.py`.
- [ ] **Step 4: Rodar a carga em modo seco**, se o CLI aceitar (`python -m cli.load_content --help`); se exigir banco e chave de embedding, pular e registrar que a carga real acontece no deploy.
- [ ] **Step 5: Commit** (`docs(faq): Construction Manager e coerência de status`), push da branch.

---

## Self-review

- Spec: objetivo, regras de conteúdo (Task 1 testa), três produtos (Tasks 2–4), `top_k` (Task 1), seções existentes (Tasks 3–4), testes (Task 1), entrega no Drive/servidor (fora do código, na spec).
- Tipos e nomes: títulos e mínimos do teste batem com as listas das tasks (7, 8, 5).
- Proporção: o plano lista decisões e títulos; o texto das seções é escrito na execução a partir das fontes.
