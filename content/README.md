# The organization's own words

Everything in this directory belongs to the client, not to the code. Point
`CONTENT_DIR` somewhere else to run a second organization from the same image.

| file | what it is | who reads it |
|---|---|---|
| `faq.md` | the only facts the desk may state | `cli/load_content.py` → `knowledge_chunks` |
| `messages.toml` | every sentence the desk says, and the four things it must never say | `core/content.py`, at every reply |
| `staff.json` | the team, their calendars and who covers for whom — **not in git**: real people's ids; copy `staff.example.json` | `cli/load_content.py` → `staff` |
| `email_templates.toml` | the inbox categories and the drafts for each | mail triage; the drafting half is roadmap 3.2 |
| `pricing.toml` | the price table and the negotiation limits — **optional**: without it the desk does not quote | `core/services/pricing.py`; checked by `cli/load_content.py` at every deploy |

The contents here are **Converge's own**, taken from the website
(`../website`, getconverge.me). When the site changes — a plan price, a product,
the contact details — change `faq.md` to match and reload it.

Each product has its own `## <Produto> — …` sections (Atendimento com IA,
Assistente Jurídico, Construction Manager); `tests/test_product_knowledge.py`
holds their shape. The Construction Manager is described as **in development,
with no client in production** — rewrite those sections, and that test, the day
it goes live.

Not yet filled, because the website does not say:

- `facts.endereco` is empty and the confirmation does not name a place.
- Office hours are `OFFICE_*` in `.env`; nothing here states them to a person.

## faq.md

One `##` heading per question, the answer directly below it. Each heading and
its answer become one retrievable chunk, so anything that must be read together
— a price and the service it belongs to — has to sit under the same heading.

The desk states nothing that is not in this file. A question it does not cover
gets `reply.unknown` and a person, every time. That is the whole safety property
of the phase, and it only holds if the facts in here are true: a price invented
by an AI writing assistant will be repeated to a patient with total confidence.

In production, put this document in the client's own drive and set
`KNOWLEDGE_DOCUMENT_ID` — that copy wins over this file, because it is the one
the organization actually edits.

After changing it:

```bash
python cli/load_content.py --faq-only
```

## messages.toml

Placeholders are checked at startup against what the code can supply. A message
that names `{profissional}` when nothing knows who that is refuses the
deployment rather than reaching a patient with the braces still in. The list
lives in `core/content.py` as `_PLACEHOLDERS`; adding a placeholder to a message
means adding it there first.

`voice.never` is not style. For a clinic it is the difference between a
receptionist and a liability, and an empty list refuses to start.
