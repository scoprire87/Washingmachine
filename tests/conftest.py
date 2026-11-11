"""Shared test helpers."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "dishwasher"
# Program logs of a real dishwasher (seconds from the start, watts)
PROGRAMS = [
    "auto", "complete", "eco", "eco_quick", "glasses", "intensive",
    "intensive_extra_rinse", "quick", "quick_shine",
]


def load_trace(name: str, offset: float = 0.0) -> list[tuple[float, float]]:
    with open(FIXTURES / f"{name}.csv", newline="") as handle:
        return [
            (float(row["seconds"]) + offset, float(row["power"]))
            for row in csv.DictReader(handle)
        ]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
