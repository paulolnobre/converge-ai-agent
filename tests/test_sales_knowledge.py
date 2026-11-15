"""What the desk can say when it sells: deployment, infrastructure, technology,
security, objections — and what it must keep to itself.

The desk talks to anyone who writes, and a prospect asking "where does it run?"
may be someone mapping the server. These sections are written at the level of
the public site: what exists and why it is safe, never a provider, a host, a
path or a component an attacker could look up. The tests hold that line, and
hold the status of the two things that are not built yet — payments and a
broader clinic solution — so the desk never sells them as ready.
"""
import re

from core import content
from core.services import knowledge

REQUIRED_HEADINGS = [
    # Implantação
    "O que a minha empresa precisa ter para implantar um agente da Converge?",
    "Quem instala e configura o agente? Preciso de equipe de TI?",
    "Como a equipe aprende a usar o agente?",
    "Tem aplicativo para a equipe acompanhar pelo celular?",
    # Infraestrutura e operação
    "Onde o agente fica hospedado e quem cuida do servidor?",
    "Como são feitas as atualizações do agente?",
    "O que acontece se a IA ou o sistema ficar fora do ar?",
    "Como a Converge acompanha se o agente está funcionando?",
    # Tecnologia
    "Com quais sistemas e ferramentas o agente se integra?",
    "Como o agente evita inventar respostas?",
    "O agente entende áudio, fotos e documentos?",
    "Como a Converge garante a qualidade do que entrega?",
    # Segurança e dados
    "Quem tem acesso aos dados das conversas?",
    "Os dados continuam sendo da minha empresa? E se eu cancelar?",
    "Dá para controlar o que cada pessoa da equipe pode ver e fazer?",
    # Objeções
    "A IA vai substituir minha equipe?",
    "E se o agente errar?",
    "Qual a diferença entre um agente da Converge e um chatbot comum?",
    "Por que não usar o ChatGPT direto?",
    "A Converge atende empresas pequenas?",
    "Por onde começar? Preciso saber exatamente o que quero automatizar?",
    "A Converge faz consultoria ou só entrega a solução?",
    # Pagamentos e clínicas
    "A Converge integra pagamentos, cobrança e nota fiscal?",
    "A Converge tem uma solução para clínicas e consultórios?",
    # Produtos: por que confiar
    "Assistente Jurídico — o que acontece se a IA errar ou ficar fora do ar?",
    "Construction Manager — por que dá para confiar nos números?",
    # Proposta por e-mail, site e aplicativo
    "Posso receber a proposta por e-mail, sem reunião?",
    "A Converge também faz site?",
    "O aplicativo do painel tem custo adicional?",
    "Qual é o passo a passo da implantação e o que eu preciso preparar?",
    "Vocês usam API não oficial do WhatsApp?",
    "Quais são os requisitos mínimos para conectar o WhatsApp oficial (Meta) ao agente?",
    "O que é preciso para criar e verificar o Meta Business da empresa?",
    "O atendimento funciona em vários canais ao mesmo tempo, num painel só?",
    "A solução aguenta picos de mensagens e cresce com a empresa?",
    "O que a Converge consegue automatizar na minha empresa?",
]

# Nothing an attacker could look up: hosts, paths, components, regions.
INTERNAL_TERMS = re.compile(
    r"(?i)\b(VPS|LUKS|docker|postgres(ql)?|redis|journald|prometheus|"
    r"sa-east-1|S3|nginx|caddy|ubuntu|debian)\b|/opt/|bot\.getconverge\.me"
)


def _document() -> str:
    return (content._root() / "faq.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _body(heading: str) -> str:
    chunk = next(c for c in knowledge.split(_document()) if c["heading"] == heading)
    return _flat(chunk["body"])


def test_every_sales_section_exists():
    headings = {c["heading"] for c in knowledge.split(_document())}
    missing = [h for h in REQUIRED_HEADINGS if h not in headings]
    assert not missing


def test_the_faq_names_no_internal_infrastructure():
    assert INTERNAL_TERMS.findall(_document()) == []


def test_payments_are_in_implementation_not_ready():
    body = _body("A Converge integra pagamentos, cobrança e nota fiscal?")
    assert "Asaas" in body and "em implantação" in body
    assert "parceria" not in body.lower()


def test_the_clinic_solution_is_a_possibility_not_a_product():
    body = _body("A Converge tem uma solução para clínicas e consultórios?")
    assert "ainda não" in body and "sob medida" in body
    assert "profissional" in body  # triage organizes; a person evaluates


def test_construction_trust_section_keeps_its_status():
    body = _body("Construction Manager — por que dá para confiar nos números?")
    assert "em desenvolvimento" in body


def test_the_desk_asks_about_the_business_it_was_already_told():
    rules = content.voice_rules()
    assert "já disse o ramo" in rules
    assert "objeção" in rules


def test_consulting_is_part_of_the_offer():
    body = _body("A Converge faz consultoria ou só entrega a solução?")
    assert "consultoria" in body and "especialista" in body
    assert "consultoria" in content.voice_rules()


def test_site_has_its_own_cost_and_the_app_does_not():
    site = _body("A Converge também faz site?")
    assert "custo" in site and "proposta" in site and "R$" not in site
    app = _body("O aplicativo do painel tem custo adicional?")
    assert "sem custo adicional" in app and "R$" not in app
    proposal = _body("Posso receber a proposta por e-mail, sem reunião?")
    assert "2 dias úteis" in proposal and "sem reunião" in proposal


def test_after_a_product_the_desk_offers_the_call_or_a_proposal_here():
    rules = content.voice_rules()
    after_product = next(line for line in rules.splitlines()
                         if line.startswith("- Depois de explicar um produto"))
    assert "30 minutos" in after_product
    assert "proposta" in after_product and "algumas perguntas" in after_product


def test_the_solutions_answer_lists_them_in_one_sentence():
    body = _body("Quais soluções e produtos a Converge oferece?")
    assert "duas frentes" not in body
    assert "agentes de IA, automações e integrações" in body


def test_the_invitation_is_offered_once_as_a_choice():
    rules = content.voice_rules()
    assert "uma vez só" in rules
    assert "no máximo duas vezes" not in rules


def test_the_goal_is_closing_a_meeting_or_a_proposal_request():
    rules = content.voice_rules()
    assert "reunião online" in rules and "pedido de proposta" in rules
    assert "Informar é o meio" in rules


def test_the_implementation_steps_say_who_prepares_what():
    body = _body("Qual é o passo a passo da implantação e o que eu preciso preparar?")
    for needed in ("Meta", "Google", "site", "serviço adicional", "custo", "CRM"):
        assert needed in body, needed
    assert "R$" not in body


def test_only_the_official_whatsapp_api_is_offered():
    body = _body("Vocês usam API não oficial do WhatsApp?")
    assert body.startswith("Não.") and "API oficial da Meta" in body
    assert "bloqueado" in body
    for unofficial in ("Z-API", "Baileys", "Evolution", "WPPConnect"):
        assert unofficial not in _document()
    assert "API não oficial" in content.voice_rules()


def test_the_meta_requirements_are_a_concrete_list():
    body = _body("Quais são os requisitos mínimos para conectar o WhatsApp oficial (Meta) ao agente?")
    for needed in ("portfólio", "número", "pagamento", "verificação", "CNPJ", "site",
                   "política de privacidade", "domínio", "Instagram", "Facebook", "painel"):
        assert needed in body, needed
    assert "R$" not in body
    verify = _body("O que é preciso para criar e verificar o Meta Business da empresa?")
    assert "razão social" in verify and "verificação" in verify


def test_a_question_about_requirements_gets_a_list_not_a_generic_answer():
    rules = content.voice_rules()
    assert "requisitos" in rules and "lista" in rules


def test_instagram_and_facebook_are_not_required_but_can_be_added():
    body = _body("Quais são os requisitos mínimos para conectar o WhatsApp oficial (Meta) ao agente?")
    assert "não são exigidos" in body and "integra" in body


def test_several_channels_one_panel_and_an_installable_app():
    channels = _body("O atendimento funciona em vários canais ao mesmo tempo, num painel só?")
    for needed in ("WhatsApp", "Telegram", "Instagram", "painel"):
        assert needed in channels, needed
    app = _body("Tem aplicativo para a equipe acompanhar pelo celular?")
    assert "PWA" in app and "computador" in app


def test_scaling_is_explained_by_the_queue_and_promises_no_autoscaling():
    body = _body("A solução aguenta picos de mensagens e cresce com a empresa?")
    assert "fila" in body and "nuvem" in body
    assert "automaticamente" not in body and "R$" not in body


# The only answers that talk about the 30-minute call: it is their subject, or
# they offer it next to the proposal. Anywhere else the invitation is the
# rules' to make, once — a line inside every answer is how it got repeated.
MAY_MENTION_THE_CALL = {
    "Quanto custa um agente de IA da Converge?",
    "Por onde começar? Preciso saber exatamente o que quero automatizar?",
    "A Converge faz consultoria ou só entrega a solução?",
    "Como falar com a Converge ou agendar uma conversa?",
    "Posso receber a proposta por e-mail, sem reunião?",
}


def test_only_the_answers_about_the_call_mention_it():
    pushing = [c["heading"] for c in knowledge.split(_document())
               if c["heading"] not in MAY_MENTION_THE_CALL
               and re.search(r"30 minutos|marcar (?:uma |a )?conversa", c["body"])]
    assert pushing == []


def test_the_price_answer_offers_the_ai_the_proposal_and_the_call():
    body = _body("Quanto custa um agente de IA da Converge?")
    assert "O melhor caminho é marcar" not in body
    assert "proposta" in body and "por aqui" in body and "30 minutos" in body
    assert "R$" not in body


def test_once_the_person_chooses_the_desk_stops_offering():
    rules = content.voice_rules()
    assert "não ofereça mais" in rules


def test_what_can_be_automated_starts_with_a_diagnosis_and_examples_by_area():
    body = _body("O que a Converge consegue automatizar na minha empresa?")
    assert "diagnóstico" in body and "lista fechada" in body
    for area in ("advocacia", "Construtoras", "Clínicas", "Atendimento e vendas",
                 "Rotinas administrativas", "Financeiro"):
        assert area in body, area
    assert "em desenvolvimento" in body and "R$" not in body
    assert "Asaas" in body and "em implantação" in body
    assert "diagnóstico" in _body("Quais soluções e produtos a Converge oferece?")
    rules = content.voice_rules()
    assert "não é um produto fechado" in rules and "exemplos concretos" in rules
