"""Media payloads in the Redis queues, for going back to an image without
media: counted in every list (an item being processed included, an unreadable
one counted), and the lists that do not drain on their own normalized.
Against a real Redis (TEST_REDIS_URL)."""
import json
from datetime import UTC, datetime, timedelta

from core.config import settings
from core.crypto import open_text
from core.integrations import update_queue
from tests.test_queue_sealing import REDIS, needs_redis, redis_queue  # noqa: F401

pytestmark = needs_redis
NOW = int(datetime.now(UTC).timestamp())


def _wa(update_id: str, *, date: int = NOW) -> dict:
    return {"update_id": update_id, "message": {
        "chat": {"id": "wa:5591988887777", "type": "private"}, "date": date,
        "caption": "meu exame", "_wa_type": "image",
        "_media": {"kind": "image", "file_id": "wa-media:M", "mime_type": "image/jpeg",
                   "filename": None, "declared_bytes": None, "source_id": f"{update_id}:M"}}}


TEXT = {"update_id": "wa:T", "message": {"chat": {"id": "wa:5591988887777"}, "text": "oi"}}


async def test_counts_cover_every_list_and_unreadable_counts(redis_queue):  # noqa: F811
    client = redis_queue
    await client.rpush(settings.queue_active_name, update_queue.encode_update(_wa("wa:A")),
                       update_queue.encode_update(TEXT))
    await client.rpush(settings.queue_deferred_name, update_queue.encode_update(_wa("wa:D")))
    await client.rpush(settings.queue_dead_letter_name, update_queue.encode_update(_wa("wa:X")))
    await client.rpush(settings.queue_dead_letter_name, "not-an-envelope")
    await update_queue.quarantine(update_queue.encode_update(_wa("wa:Q")), "teste")
    await client.rpush(settings.queue_active_name, update_queue.encode_update(_wa("wa:P")))
    reserved = await update_queue.reserve_update(deferred=False)  # now in processing
    assert reserved is not None
    counts = await update_queue.media_payload_counts()
    assert counts == {"active": 1, "deferred": 1, "processing": 1, "dead_letter": 2,
                      "quarantine": 1}


async def test_normalize_drops_done_and_old_and_strips_the_rest(redis_queue):  # noqa: F811
    client = redis_queue
    old = NOW - int(timedelta(days=91).total_seconds())
    for update in (_wa("wa:DONE"), _wa("wa:OLD", date=old), _wa("wa:KEEP"), TEXT):
        await client.rpush(settings.queue_dead_letter_name, update_queue.encode_update(update))
    await update_queue.quarantine(update_queue.encode_update(_wa("wa:QKEEP")), "teste")
    await client.rpush(settings.queue_dead_letter_name, "not-an-envelope")

    async def seen(update_id: str) -> bool:
        return update_id == "wa:DONE"

    result = await update_queue.normalize_media_payloads(seen)
    assert result == {"dropped": 2, "stripped": 2, "unreadable": 1}
    left = [str(i) for i in await client.lrange(settings.queue_dead_letter_name, 0, -1)]
    updates = [update_queue.decode_update(i) for i in left if i != "not-an-envelope"]
    assert [u["update_id"] for u in updates] == ["wa:KEEP", "wa:T"]
    kept = updates[0]["message"]
    assert "_media" not in kept and "caption" not in kept
    raw, = [str(i) for i in await client.lrange(settings.queue_quarantine_name, 0, -1)]
    envelope = json.loads(open_text(update_queue._QUARANTINE_CONTEXT, raw))
    assert envelope["reason"] == "teste"
    inner = update_queue.decode_update(envelope["payload"])
    assert "_media" not in inner["message"] and "caption" not in inner["message"]
    assert json.dumps(updates)  # still plain updates
    assert (await update_queue.media_payload_counts())["dead_letter"] == 1  # the unreadable one


async def test_a_list_that_changes_mid_rewrite_is_counted_once(redis_queue, monkeypatch):  # noqa: F811
    """A push between the read and the write makes the rewrite read again;
    what the first attempt counted must not add to the second."""
    import redis as sync_redis

    client = redis_queue
    await client.rpush(settings.queue_dead_letter_name, update_queue.encode_update(_wa("wa:KEEP")))
    real_unpack, reads = update_queue._unpack, []

    def unpack_and_push_once(name, item):
        if name == settings.queue_dead_letter_name:
            reads.append(item)
        if len(reads) == 2:  # the rewrite's first read (the first is the scan for `seen`)
            sync_redis.Redis.from_url(REDIS or "").rpush(
                settings.queue_dead_letter_name, update_queue.encode_update(TEXT))
        return real_unpack(name, item)

    monkeypatch.setattr(update_queue, "_unpack", unpack_and_push_once)

    async def seen(update_id: str) -> bool:
        return False

    assert await update_queue.normalize_media_payloads(seen) == {
        "dropped": 0, "stripped": 1, "unreadable": 0}
    assert await client.llen(settings.queue_dead_letter_name) == 2
