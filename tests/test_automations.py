"""Automations: nothing reaches a client unapproved (in approve mode), out of
hours, twice, or after SAIR — and the team-only one never reaches a client."""
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import content
from core.config import settings
from core.integrations import telegram
from core.models import Contact
from core.repositories import automations as repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.services import automations

ZONE = ZoneInfo(settings.office_timezone)
TUESDAY_10H = datetime(2026, 9, 29, 10, 0, tzinfo=ZONE)
TUESDAY_20H = datetime(2026, 9, 29, 20, 0, tzinfo=ZONE)
SATURDAY_10H = datetime(2026, 10, 3, 10, 0, tzinfo=ZONE)
TEAM = "-1001234567890"
CONN = object()


async def _value(value):
    return value


# ── The message itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize(("message", "problem"), [
    ("", "vazia"),
    ("   ", "vazia"),
    ("x" * 1001, "1000"),
    ("Olá {cliente}!", "{nome}"),
    ("Olá {nome!", "sem fechar"),
])
def test_a_bad_message_is_refused_with_the_reason(message, problem):
    assert problem in (automations.template_problem(message) or "")


def test_a_message_with_the_name_is_accepted():
    assert automations.template_problem("Olá, {nome}! Tudo bem?") is None


def test_the_message_uses_the_first_name_and_always_offers_the_way_out():
    text = automations.render("Olá, {nome}!", "Ana Maria Souza")
    assert text.startswith("Olá, Ana!")
    assert text.endswith(content.text("automation.opt_out_footer"))
    assert automations.render("Olá, {nome}!", None).startswith("Olá, tudo bem!")


def test_the_buttons_per_kind():
    stalled = automations.keyboard("stalled", 7)
    no_show = automations.keyboard("no_show", 7)
    assert stalled is not None and no_show is not None
    data = [b["callback_data"] for row in stalled["inline_keyboard"] for b in row]
    assert data == ["why:7:preco", "why:7:avaliando", "why:7:momento", "why:7:outro"]
    assert no_show["inline_keyboard"][0][0]["callback_data"] == "book:start"
    assert automations.keyboard("reactivate", 7) is None


@pytest.mark.parametrize(("moment", "open_"), [
    (TUESDAY_10H, True), (TUESDAY_20H, False), (SATURDAY_10H, False),
])
def test_office_hours_are_the_real_ones(monkeypatch: pytest.MonkeyPatch, moment, open_):
    """DESK_ALWAYS_OPEN keeps the desk answering at night; nothing is started
    with a client at night."""
    monkeypatch.setattr(settings, "desk_always_open", True)
    assert automations.in_office_hours(moment) is open_


# ── One sweep ────────────────────────────────────────────────────────────────

class World:
    """core.repositories.automations, in memory."""

    def __init__(self):
        self.automations: list[dict] = []
        self.candidates: dict[str, list[dict]] = {}
        self.queue: dict[int, dict] = {}
        self.sent: list[tuple] = []
        self.posted: list[tuple] = []
        self.touched: list[int] = []
        self.active: set[str] = set()   # chats a person was called to, or holds
        self.events: list[str] = []     # lock / handoff reads, in order
        self.chats: dict[int, str] = {}  # contact id -> chat, when not the default
        self.finder_kwargs: dict[str, dict] = {}  # what each finder was asked besides the basics
        self.wrote: set[int] = set()  # contacts who wrote after their item was queued

    def automation(self, kind, *, enabled=True, mode="approve", days=60, message="Olá, {nome}!"):
        self.automations.append({"id": len(self.automations) + 1, "kind": kind, "enabled": enabled,
                                 "mode": mode, "days": days, "message": message})

    def pick(self, kind, contact_id, name="Ana", appointment_id=None):
        self.candidates.setdefault(kind, []).append({
            "contact_id": contact_id, "name": name, "chat_id": str(100 + contact_id),
            "appointment_id": appointment_id, "since": TUESDAY_10H, "stage": "respondido",
        })


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(settings, "team_chat_id", TEAM)

    async def list_automations(conn):
        return list(w.automations)

    def finder(kind):
        async def find(conn, automation_id, days, limit=200, **kwargs):
            w.finder_kwargs[kind] = kwargs
            return list(w.candidates.get(kind, []))
        return find

    async def enqueue(conn, automation_id, contact_id, appointment_id, status, reason):
        if any(q["automation_id"] == automation_id and q["contact_id"] == contact_id
               and q["status"] in ("pending", "approved") for q in w.queue.values()):
            return None
        qid = len(w.queue) + 1
        kind = next(a["kind"] for a in w.automations if a["id"] == automation_id)
        w.queue[qid] = {"id": qid, "automation_id": automation_id, "contact_id": contact_id,
                        "kind": kind, "status": status, "reason": reason, "opt_out": False}
        return qid

    async def count_pending(conn):
        return sum(q["status"] == "pending" for q in w.queue.values())

    async def approved(conn, limit=20):
        rows = []
        for q in w.queue.values():
            if q["status"] == "approved":
                message = next(a["message"] for a in w.automations if a["id"] == q["automation_id"])
                rows.append({"id": q["id"], "contact_id": q["contact_id"], "appointment_id": None,
                             "queued_at": TUESDAY_10H,
                             "kind": q["kind"], "message": message, "name": "Ana",
                             "chat_id": w.chats.get(q["contact_id"], str(100 + q["contact_id"])),
                             "automation_opt_out": q["opt_out"]})
        return rows

    async def approved_for_send(conn, queue_id):
        return next((row for row in await approved(conn) if row["id"] == queue_id), None)

    async def mark(conn, queue_id, status):
        w.queue[queue_id]["status"] = status

    async def wrote_since(conn, contact_id, since):
        return contact_id in w.wrote

    async def touch_contact(conn, contact_id):
        w.touched.append(contact_id)

    async def send_message(chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        w.sent.append((chat_id, text, reply_markup))
        return True

    async def post_message(chat_id, text, reply_markup=None, **kwargs):
        w.posted.append((str(chat_id), text))
        return 1

    for name, value in {
        "list_automations": list_automations, "enqueue": enqueue, "count_pending": count_pending,
        "approved": approved, "approved_for_send": approved_for_send,
        "mark": mark, "touch_contact": touch_contact, "wrote_since": wrote_since,
        "reactivate_candidates": finder("reactivate"), "stalled_candidates": finder("stalled"),
        "no_show_candidates": finder("no_show"), "no_proposal_candidates": finder("no_proposal"),
    }.items():
        monkeypatch.setattr(repo, name, value)
    monkeypatch.setattr(telegram, "send_message", send_message)
    monkeypatch.setattr(telegram, "post_message", post_message)
    monkeypatch.setattr(contacts_repo, "record_message", lambda *a, **k: _value(None))
    # Nobody has refused unless a test says so (the real one reads the history).
    monkeypatch.setattr(automations, "classify_silence",
                        lambda conn, pick: _value("em_andamento"))

    async def lock_chat(conn, chat_id):
        w.events.append(f"lock {chat_id}")

    async def active_session(conn, chat_id):
        w.events.append(f"read {chat_id}")
        return object() if str(chat_id) in w.active else None

    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)
    monkeypatch.setattr(handoffs_repo, "active_session", active_session)
    return w


async def test_in_approve_mode_nothing_goes_out_and_the_team_is_told(world):
    world.automation("reactivate")
    world.pick("reactivate", 1)
    world.pick("reactivate", 2, name="Bruno")

    result = await automations.run(CONN, TUESDAY_10H)

    assert result == {"queued": 2, "sent": 0, "alerts": 0}
    assert world.sent == [], "nothing reaches a client before a person approves"
    assert world.posted == [(TEAM, content.render("automation.awaiting_approval", total=2))]


async def test_a_second_sweep_does_not_queue_or_announce_again(world):
    world.automation("reactivate")
    world.pick("reactivate", 1)

    await automations.run(CONN, TUESDAY_10H)
    await automations.run(CONN, TUESDAY_10H)

    assert len(world.queue) == 1 and len(world.posted) == 1


async def test_approved_messages_go_out_in_office_hours_with_the_way_out(world):
    world.automation("stalled", mode="auto", message="Oi, {nome}, parou por quê?")
    world.pick("stalled", 1)

    result = await automations.run(CONN, TUESDAY_10H)

    assert result["sent"] == 1
    chat, text, markup = world.sent[0]
    assert chat == "101" and text.startswith("Oi, Ana, parou por quê?")
    assert text.endswith(content.text("automation.opt_out_footer"))
    assert markup["inline_keyboard"][0][0]["callback_data"] == "why:1:preco"
    assert world.queue[1]["status"] == "sent" and world.touched == [1]


@pytest.mark.parametrize("moment", [TUESDAY_20H, SATURDAY_10H])
async def test_nothing_goes_out_after_hours_even_when_approved(world, moment):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)

    await automations.run(CONN, moment)

    assert world.sent == [] and world.queue[1]["status"] == "approved"


async def test_a_disabled_automation_picks_nobody(world):
    world.automation("reactivate", enabled=False)
    world.pick("reactivate", 1)

    assert await automations.run(CONN, TUESDAY_10H) == {"queued": 0, "sent": 0, "alerts": 0}
    assert world.queue == {}


async def test_someone_who_said_sair_after_approval_is_not_sent(world):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)
    await automations.run(CONN, TUESDAY_20H)          # queued, not yet sent
    world.queue[1]["opt_out"] = True

    await automations.run(CONN, TUESDAY_10H)

    assert world.sent == [] and world.queue[1]["status"] == "discarded"


async def test_an_approved_message_is_dropped_once_a_person_holds_the_conversation(world):
    """Approved yesterday, sent this morning — but Paulo took Ana's
    conversation in the Inbox meanwhile. Nothing automated reaches her."""
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)
    await automations.run(CONN, TUESDAY_20H)          # approved, waiting for office hours
    world.active.add("101")

    await automations.run(CONN, TUESDAY_10H)

    assert world.sent == [] and world.queue[1]["status"] == "discarded"


async def test_whether_a_person_holds_it_is_read_under_the_conversation_lock(world):
    """Checked under the same lock "Assumir" takes, so a claim cannot land
    between the check and the send."""
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert world.events == ["lock 101", "read 101"] and len(world.sent) == 1


async def test_dispatch_commits_each_approved_message_separately(world):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)
    world.pick("reactivate", 2)
    await automations.run(CONN, TUESDAY_20H)

    class Pool:
        def __init__(self):
            self.connections = []

        @asynccontextmanager
        async def connection(self):
            conn = object()
            self.connections.append(conn)
            yield conn

    pool = Pool()
    assert await automations.dispatch_approved(pool, TUESDAY_10H) == 2
    assert len(pool.connections) == 3  # list, then one transaction per send
    assert len({id(conn) for conn in pool.connections}) == 3


async def test_a_claim_between_dispatch_selection_and_send_discards_the_message(world):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)
    await automations.run(CONN, TUESDAY_20H)

    class Pool:
        def __init__(self):
            self.opens = 0

        @asynccontextmanager
        async def connection(self):
            self.opens += 1
            if self.opens == 2:  # after the list, before this item's send
                world.active.add("101")
            yield object()

    assert await automations.dispatch_approved(Pool(), TUESDAY_10H) == 0
    assert world.queue[1]["status"] == "discarded"
    assert world.sent == []


async def test_no_proposal_only_ever_tells_the_team(world):
    world.automation("no_proposal", mode="auto", days=3, message="")
    world.pick("no_proposal", 1, appointment_id=9)

    result = await automations.run(CONN, TUESDAY_10H)

    assert result["alerts"] == 1 and world.sent == []
    assert world.posted[0][0] == TEAM and "ainda está em" in world.posted[0][1]


async def test_a_failed_send_is_marked_and_not_counted(world, monkeypatch: pytest.MonkeyPatch):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)
    monkeypatch.setattr(telegram, "send_message", lambda *a, **k: _value(False))

    result = await automations.run(CONN, TUESDAY_10H)

    assert result["sent"] == 0 and world.queue[1]["status"] == "failed"


# ── The replies ──────────────────────────────────────────────────────────────

async def test_a_reason_is_recorded_and_the_team_told(world, monkeypatch: pytest.MonkeyPatch):
    recorded: list[tuple] = []

    async def record_reason(conn, queue_id, contact_id, reason):
        recorded.append((queue_id, contact_id, reason))
        return True

    monkeypatch.setattr(repo, "record_reason", record_reason)

    chose, reply = await automations.handle_reason_tap(
        CONN, Contact(id=7, name="Ana"), "why:5:momento"
    )

    assert recorded == [(5, 7, "momento")]
    assert chose == content.text("automation.reason_momento")
    assert reply == content.text("automation.reason_thanks")
    assert world.posted == [(TEAM, content.render(
        "automation.reason_notice", nome="Ana", motivo=content.text("automation.reason_momento")
    ))]


async def test_a_forged_reason_records_nothing(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(repo, "record_reason",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not record")))
    for data in ("why:5:hackeado", "why:abc:preco", "why:"):
        await automations.handle_reason_tap(CONN, Contact(id=7, name="Ana"), data)
    assert world.posted == []


@pytest.mark.parametrize(("body", "stops"), [
    ("SAIR", True), ("sair", True), ("Parar.", True), ("stop", True),
    ("quero sair do plano", False), ("sair?", False),
])
def test_only_a_plain_sair_is_an_opt_out(body, stops):
    assert automations.is_opt_out(body) is stops


async def test_an_automation_never_goes_to_whatsapp(world):
    """No message the company starts reaches WhatsApp without an approved
    template — and automations have none yet."""
    world.chats[1] = "wa:5591999990000"
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert world.sent == [] and world.queue[1]["status"] == "discarded"


def test_candidates_exclude_whatsapp():
    assert "c.source <> 'whatsapp'" in repo._CLIENT_OK
    assert repo._CLIENT_OK == repo.client_ok() == repo.client_ok(False)


def test_whatsapp_is_included_only_when_asked_for():
    asked = repo.client_ok(True)

    assert "c.source <> 'whatsapp'" not in asked
    # Every other exclusion stays: opt-out, 30-day rule, a person holding the chat.
    for kept in ("automation_opt_out", "last_automation_at", "handoff_sessions", "appointments up"):
        assert kept in asked


# ── Resuming a conversation that went quiet, on WhatsApp ────────────────────

from core.integrations import whatsapp  # noqa: E402


@pytest.fixture
def resume(world, monkeypatch: pytest.MonkeyPatch):
    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v",
                        "whatsapp_resume_template": "retomada_de_conversa",
                        "whatsapp_resume_language": "pt_BR"}.items():
        monkeypatch.setattr(settings, name, value)
    state: dict = {"templates": [], "result": "ok", "recorded": [], "label": "em_andamento",
                   "classified": []}

    async def send_template(to, name, language, body_params, button_payloads):
        state["templates"].append((to, name, language, body_params, button_payloads))
        return state["result"]

    async def record_message(conn, contact_id, chat_id, role, message, intent=None, **kwargs):
        state["recorded"].append((chat_id, message, intent))

    async def classify(conn, pick):
        state["classified"].append(pick["contact_id"])
        return state["label"]

    monkeypatch.setattr(whatsapp, "send_template", send_template)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(automations, "classify_silence", classify)
    world.chats[1] = "wa:5591999990000"
    return state


async def test_a_quiet_whatsapp_conversation_gets_the_resume_template(world, resume):
    world.automation("stalled", mode="auto")
    world.pick("stalled", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["templates"] == [("5591999990000", "retomada_de_conversa", "pt_BR", ["Ana"],
                                    ["resume:yes:1", "resume:no:1"])]
    assert world.sent == []
    assert world.queue[1]["status"] == "sent" and world.touched == [1]
    assert resume["recorded"] == [("wa:5591999990000",
                                   content.render("automation.resume_whatsapp_text", nome="Ana"),
                                   "automation")]


async def test_without_the_template_a_whatsapp_contact_is_still_left_out(
    world, resume, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "whatsapp_resume_template", None)
    world.automation("stalled", mode="auto")
    world.pick("stalled", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["templates"] == [] and world.queue[1]["status"] == "discarded"


async def test_only_the_quiet_conversation_uses_the_resume_template(world, resume):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["templates"] == [] and world.queue[1]["status"] == "discarded"


async def test_a_template_the_channel_refused_is_marked_failed(world, resume):
    resume["result"] = "failed"
    world.automation("stalled", mode="auto")
    world.pick("stalled", 1)

    assert await automations.run(CONN, TUESDAY_10H) == {"queued": 0, "sent": 0, "alerts": 0}

    assert world.queue[1]["status"] == "failed" and world.touched == []
    assert resume["recorded"] == []


@pytest.mark.parametrize("template, expected", [("retomada_de_conversa", True), (None, False)])
async def test_the_finder_is_asked_for_whatsapp_only_when_it_can_be_answered(
    world, resume, monkeypatch: pytest.MonkeyPatch, template, expected
):
    monkeypatch.setattr(settings, "whatsapp_resume_template", template)
    world.automation("stalled")
    world.automation("reactivate")

    await automations.run(CONN, TUESDAY_10H)

    assert world.finder_kwargs["stalled"].get("whatsapp", False) is expected
    assert world.finder_kwargs["reactivate"].get("whatsapp", False) is False


async def test_someone_who_said_no_is_left_alone_and_never_picked_again(world, resume):
    resume["label"] = "recusou"
    world.automation("stalled", mode="auto")
    world.pick("stalled", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["templates"] == [] and world.sent == []
    assert world.queue[1]["status"] == "discarded"
    assert "não quer" in world.queue[1]["reason"]


async def test_a_doubt_goes_to_the_team_even_when_the_rule_sends_by_itself(world, resume):
    resume["label"] = "duvida"
    world.automation("stalled", mode="auto")
    world.pick("stalled", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["templates"] == [] and world.sent == []
    assert world.queue[1]["status"] == "pending"
    assert "confira" in world.queue[1]["reason"]
    assert world.posted, "the team is told something is waiting for approval"


async def test_only_the_quiet_kind_is_read_for_refusals(world, resume):
    world.automation("reactivate", mode="auto")
    world.pick("reactivate", 1)

    await automations.run(CONN, TUESDAY_10H)

    assert resume["classified"] == []


async def test_only_so_many_are_read_per_sweep(world, resume):
    world.automation("stalled")
    for n in range(1, 26):
        world.pick("stalled", n)

    await automations.run(CONN, TUESDAY_10H)

    assert len(resume["classified"]) == automations._CLASSIFY_PER_RUN == 20
    assert len(world.queue) == 20


# ── Reading why they went quiet ─────────────────────────────────────────────

@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch):
    state: dict = {"history": [("user", "Quanto custa?"), ("assistant", "Depende do caso."),
                               ("user", "Hoje não quero, obrigado")],
                   "payload": {"label": "recusou"}, "calls": [], "raise": None}

    async def recent_history(conn, chat_id, limit=10):
        state["limit"] = limit
        return state["history"]

    async def complete_json(system, user, **kwargs):
        state["calls"].append((system, user, kwargs))
        if state["raise"]:
            raise state["raise"]
        return state["payload"]

    monkeypatch.setattr(contacts_repo, "recent_history", recent_history)
    monkeypatch.setattr(automations, "complete_json", complete_json)
    return state


@pytest.mark.parametrize("label", ["recusou", "em_andamento", "duvida"])
async def test_each_label_is_taken_as_said(reading, label):
    reading["payload"] = {"label": label}

    assert await automations.classify_silence(CONN, {"chat_id": "42"}) == label


@pytest.mark.parametrize("payload", [None, {}, {"label": "talvez"}, {"label": 3}, ["recusou"]])
async def test_anything_unclear_is_a_doubt(reading, payload):
    reading["payload"] = payload

    assert await automations.classify_silence(CONN, {"chat_id": "42"}) == "duvida"


async def test_a_model_that_fails_is_a_doubt(reading):
    reading["raise"] = RuntimeError("provider down")

    assert await automations.classify_silence(CONN, {"chat_id": "42"}) == "duvida"


async def test_with_nothing_to_read_it_is_a_doubt(reading):
    reading["history"] = []

    assert await automations.classify_silence(CONN, {"chat_id": "42"}) == "duvida"
    assert reading["calls"] == []


async def test_the_model_reads_the_last_messages_as_data(reading):
    await automations.classify_silence(CONN, {"chat_id": "42"})

    ((system, user, kwargs),) = reading["calls"]
    assert "Hoje não quero, obrigado" in user and "Quanto custa?" in user
    assert "DADOS" in system and "duvida" in system
    assert kwargs["model"] == settings.router_model and kwargs["task"] == "json"
    assert reading["limit"] == automations._CLASSIFY_HISTORY


# ── The two buttons of the template ─────────────────────────────────────────

async def test_not_now_is_recorded_and_answered(monkeypatch: pytest.MonkeyPatch):
    saved: list[tuple] = []

    async def record_reason(conn, queue_id, contact_id, reason):
        saved.append((queue_id, contact_id, reason))
        return True

    monkeypatch.setattr(repo, "record_reason", record_reason)

    chose, reply = await automations.handle_resume_no(
        CONN, Contact(id=7, chat_id="wa:55"), "resume:no:12")

    assert saved == [(12, 7, "agora_nao")]
    assert chose == content.text("automation.resume_no")
    assert reply == content.text("automation.resume_no_reply")


async def test_a_forged_not_now_records_nothing(monkeypatch: pytest.MonkeyPatch):
    async def record_reason(*args):
        raise AssertionError("must not record")

    monkeypatch.setattr(repo, "record_reason", record_reason)

    _, reply = await automations.handle_resume_no(
        CONN, Contact(id=7, chat_id="wa:55"), "resume:no:abc")

    assert reply == content.text("automation.resume_no_reply")


# ── Review: what could slip past the reading ───────────────────────────────

async def test_a_long_message_is_read_whole_not_cut(reading):
    reading["history"] = [("user", "x" * 3000 + " não tenho interesse, não me procurem")]

    await automations.classify_silence(CONN, {"chat_id": "42"})

    ((_, user, _),) = reading["calls"]
    assert user.endswith("não tenho interesse, não me procurem")


@pytest.mark.parametrize("kind", ["stalled", "reactivate"])
async def test_someone_who_wrote_after_being_queued_is_not_sent(world, kind):
    world.automation(kind, mode="auto")
    world.pick(kind, 1)
    await automations.run(CONN, TUESDAY_20H)       # queued after hours: nothing goes out yet
    assert world.queue[1]["status"] == "approved" and world.sent == []

    world.wrote.add(1)                             # "não quero mais", before the office opens
    await automations.run(CONN, TUESDAY_10H)

    assert world.sent == [] and world.queue[1]["status"] == "discarded"
