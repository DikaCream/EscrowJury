"""Shared helpers for EscrowJury direct tests.

The ``direct_vm``, ``direct_deploy``, ``direct_alice``, ``direct_bob`` and
``direct_charlie`` fixtures come from the genlayer-test plugin. This module
only adds time travel, the evidence fixtures, and a create helper.
"""

import json
import sys
from datetime import datetime, timezone

import pytest

BASE_ISO = "2030-01-01T00:00:00Z"


def set_time(iso_str: str) -> None:
    """Advance the contract's view of block time.

    The direct VM's ``warp()`` does not refresh ``message_raw['datetime']``,
    which is what the contract's ``_now()`` reads, so we mutate it directly.
    """
    import genlayer.gl as gl

    gl.message_raw["datetime"] = iso_str


@pytest.fixture(autouse=True)
def _reset_block_time():
    """Keep block time deterministic across tests.

    ``genlayer.gl`` is imported once per session, so ``message_raw['datetime']``
    leaks between tests. Reset it to a fixed base before and after each test.
    """
    _reset()
    yield
    _reset()


def _reset():
    if "genlayer.gl" in sys.modules:
        gl = sys.modules["genlayer.gl"]
        if getattr(gl, "message_raw", None) is not None:
            gl.message_raw["datetime"] = BASE_ISO


TERMS = (
    "Design a landing page with three sections: hero, features grid, pricing "
    "table. Delivery within five business days. Two rounds of revisions included."
)
SHORT_REASON = (
    "The work was never started and there was no communication from the "
    "recipient after the escrow was funded."
)

# The artifact validators independently acquire for the recipient's evidence.
ARTIFACT_URL = "https://deliverable.example/landing"
ARTIFACT_BODY = (
    "Acme Landing Page\n"
    "Hero section: Ship your ideas.\n"
    "Features grid: instant deploy, edge rendering, team analytics.\n"
    "Pricing table: Free tier, Pro $20 per month, Enterprise custom.\n"
    "Footer: contact@acme.example\n"
)

DEPOSITOR_EVIDENCE = (
    "The recipient never delivered a landing page. The repository at "
    "https://deliverable.example/landing returns nothing related to the "
    "agreed hero, features grid, or pricing table from the terms."
)

RECIPIENT_EVIDENCE = (
    "The landing page is live at https://deliverable.example/landing with the "
    "hero section, features grid, and pricing table described in the terms. "
    "The artifact was published on 2026-08-02."
)


def to_hex(addr_bytes):
    """Convert address bytes to checksummed hex matching contract output."""
    if hasattr(addr_bytes, "as_hex"):
        return addr_bytes.as_hex
    from genlayer.py.types import Address
    return Address(addr_bytes).as_hex


def create_escrow(contract, vm, depositor, recipient, amount=100, terms=TERMS):
    """Deposit GEN into escrow and return the escrow id."""
    vm.sender = depositor
    vm.value = amount
    return int(contract.create_escrow(recipient, terms, 1, 7))


def mock_artifact(vm, url_pattern=ARTIFACT_URL, body=ARTIFACT_BODY):
    """Mock web render so evidence acquisition fetches this artifact."""
    vm.mock_web(url_pattern, {"status": 200, "body": body})


def submit_evidence(vm, contract, dispute_id, kind, details, sender, url=""):
    """Submit one evidence record as the given sender."""
    vm.sender = sender
    contract.submit_dispute_evidence(dispute_id, kind, url, details)


def mock_adjudication(vm, delivery_pct=100, quality_pct=100, reason="Terms were met."):
    """Mock the LLM call so adjudication returns a fixed two-axis verdict."""
    vm.mock_llm(
        r".*arbitrator.*",
        json.dumps({
            "delivery_pct": delivery_pct,
            "quality_pct": quality_pct,
            "reason": reason,
        }),
    )
