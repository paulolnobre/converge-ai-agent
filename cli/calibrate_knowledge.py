"""How well the FAQ search separates what it covers from what it does not.

Runs each question in content/calibration.toml through the same embedding and
cosine as the desk, and reports the best match and its score. Then it looks for
the KNOWLEDGE_MIN_SIMILARITY that keeps every covered question and drops the
most uncovered ones.

Only embeddings are called — a few cents at most. The floor only decides
whether the answering model is called at all; the model is separately told to
refuse when the retrieved text does not answer, and --answers checks that it
does, by running every question through the desk's real triage and answer.

    python -m cli.calibrate_knowledge
    python -m cli.calibrate_knowledge --answers
"""
from __future__ import annotations

import asyncio
import sys
import tomllib

from core import content
from core.config import settings
from core.content import _root
from core.integrations.llm import embed
from core.repositories import knowledge as knowledge_repo
from core.repositories.connection import connect_async
from core.services import knowledge
from core.services.graph import route
from core.services.knowledge import _cosine


def _load() -> list[dict]:
    with (_root() / "calibration.toml").open("rb") as handle:
        return tomllib.load(handle).get("question", [])


async def main() -> int:
    questions = _load()
    if not questions:
        print("No questions in content/calibration.toml.")
        return 1
    conn = await connect_async()
    try:
        chunks = await knowledge_repo.all_chunks(conn)
    finally:
        await conn.close()
    if not chunks:
        print("The knowledge base is empty. Run: python -m cli.load_content")
        return 1

    vectors = await embed([q["text"] for q in questions])
    if not vectors:
        print("Could not embed the questions — is the embedding provider configured?")
        return 1
    covered: list[float] = []
    uncovered: list[float] = []
    wrong_top = 0
    print(f"{'score':>6}  {'ok':<3} question  →  best match")
    for question, vector in zip(questions, vectors, strict=True):
        scored = sorted(
            ((_cosine(vector, chunk["embedding"]), chunk["heading"]) for chunk in chunks),
            reverse=True,
        )
        score, heading = scored[0]
        expect = question.get("expect", "")
        if expect:
            covered.append(score)
            # Top-k goes to the model, so "found" means in the top k, not first.
            found = any(h.startswith(expect) for _, h in scored[: settings.knowledge_top_k])
            wrong_top += not found
            mark = "✓" if found else "✗"
        else:
            uncovered.append(score)
            mark = "—"
        print(f"{score:6.3f}  {mark:<3} {question['text']}  →  {heading}")

    print()
    print(f"Covered questions:   {len(covered)}, lowest best score {min(covered):.3f}")
    if uncovered:
        print(f"Uncovered questions: {len(uncovered)}, highest best score {max(uncovered):.3f}")
    print(f"Covered but the right entry not in the top {settings.knowledge_top_k}: {wrong_top}")
    print(f"Current KNOWLEDGE_MIN_SIMILARITY: {settings.knowledge_min_similarity}")

    floor = min(covered)
    # The highest floor that still keeps every covered question, with a margin:
    # a real phrasing will score a little lower than any test set's worst.
    suggested = round(max(floor - 0.05, 0.0), 2)
    dropped = sum(1 for score in uncovered if score < suggested)
    lost = sum(1 for score in covered if score < settings.knowledge_min_similarity)
    print(f"Covered questions the current floor would drop: {lost}")
    print(f"Suggested floor: {suggested} (drops {dropped} of {len(uncovered)} uncovered "
          "before the model is called; the model's own refusal handles the rest)")
    ok = wrong_top == 0 and lost == 0
    if "--answers" in sys.argv:
        ok = await _check_answers(questions) and ok
    return 0 if ok else 2


async def _check_answers(questions: list[dict]) -> bool:
    """Every uncovered question must end in the desk's "I don't know" or a
    handoff, never an answer. Covered ones are printed for a person to read:
    whether an answer is right is not something this script can judge."""
    unknown = content.text("reply.unknown")
    conn = await connect_async()
    improvised = 0
    try:
        print("\nAnswers:")
        for question in questions:
            triage, reply = await route(
                question["text"], "calibration",
                retrieve=lambda q: knowledge.retrieve(conn, q),
            )
            declined = reply.text == unknown or reply.needs_human
            if not question.get("expect") and not declined:
                improvised += 1
                mark = "✗ answered what the FAQ does not cover"
            else:
                mark = "declined" if declined else "answered"
            print(f"\n[{triage.intent}] {question['text']}  ({mark})")
            print(f"  {reply.text[:300]}")
    finally:
        await conn.close()
    print(f"\nUncovered questions answered anyway: {improvised}")
    return improvised == 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
