from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def sept() -> date:
    return date(2026, 9, 1)


@pytest.fixture
def feb() -> date:
    return date(2027, 2, 1)
