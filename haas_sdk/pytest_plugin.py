"""pytest plugin for haas_sdk (SDK-TEST-05): `haas_fake` and `async_haas_fake`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from haas_sdk.testing import FakeHaas


@pytest.fixture
def haas_fake() -> Iterator[FakeHaas]:
    """A `FakeHaas` whose `client()` is a real sync `Haas` over the fake."""
    fake = FakeHaas(tenant="t1", strict=True)
    yield fake


@pytest.fixture
async def async_haas_fake() -> AsyncIterator[FakeHaas]:
    """A `FakeHaas` for async tests (`fake.async_client()`)."""
    fake = FakeHaas(tenant="t1", strict=True)
    yield fake
