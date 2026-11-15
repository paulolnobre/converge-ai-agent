"""The per-contract charge lock against a real Postgres (TEST_DATABASE_URL):
it outlives commits, and a second worker is turned away until it is freed."""
import os

import psycopg
import pytest

from core.services import contract_charge

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


async def test_one_worker_at_a_time_per_contract():
    one = await psycopg.AsyncConnection.connect(URL or "")
    two = await psycopg.AsyncConnection.connect(URL or "")
    try:
        assert await contract_charge._lock(one, 987654)
        await one.commit()
        assert not await contract_charge._lock(two, 987654)
        assert await contract_charge._lock(two, 987655)
        await contract_charge._unlock(two, 987655)
        await contract_charge._unlock(one, 987654)
        assert await contract_charge._lock(two, 987654)
        await contract_charge._unlock(two, 987654)
    finally:
        await one.close()
        await two.close()
