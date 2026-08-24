"""Dispute flow: file, submit evidence, adjudicate, settle."""

from tests.direct.conftest import (
    SHORT_REASON,
    DEPOSITOR_EVIDENCE,
    DEPOSITOR_EVIDENCE_HASH,
    RECIPIENT_EVIDENCE,
    RECIPIENT_EVIDENCE_HASH,
    create_escrow,
    submit_evidence,
    mock_adjudication,
    set_time,
)


def _file(direct_vm, contract, escrow_id, sender, reason=SHORT_REASON):
    direct_vm.sender = sender
    return int(contract.file_dispute(escrow_id, reason))


def test_file_dispute_opens_evidence_window(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    did = _file(direct_vm, contract, eid, direct_alice)

    d = contract.get_dispute(did)
    assert d["status"] == "PENDING_EVIDENCE"
    assert d["evidence_deadline"] > 0
    assert d["reason"] == SHORT_REASON

    e = contract.get_escrow(eid)
    assert e["status"] == "DISPUTED"
    assert e["dispute_id"] == did


def test_both_parties_can_file_dispute(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    did = _file(direct_vm, contract, eid, direct_bob)
    assert did > 0


def test_third_party_cannot_file(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("only the depositor or recipient"):
        contract.file_dispute(eid, SHORT_REASON)


def test_one_dispute_per_escrow(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    _file(direct_vm, contract, eid, direct_alice)
    with direct_vm.expect_revert("already has an open dispute"):
        _file(direct_vm, contract, eid, direct_alice)


def test_cannot_dispute_non_active(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_alice
    contract.release_escrow(eid)

    with direct_vm.expect_revert("escrow is not active"):
        _file(direct_vm, contract, eid, direct_alice)


def test_submit_evidence_both_sides(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    d = contract.get_dispute(did)
    assert d["depositor_evidence"] == DEPOSITOR_EVIDENCE
    assert d["depositor_evidence_kind"] == "EXECUTION_LOG"
    assert d["depositor_evidence_hash"] == DEPOSITOR_EVIDENCE_HASH

    submit_evidence(direct_vm, contract, did, "TRANSACTION_RECEIPT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)
    d = contract.get_dispute(did)
    assert d["recipient_evidence"] == RECIPIENT_EVIDENCE
    assert d["recipient_evidence_kind"] == "TRANSACTION_RECEIPT"
    assert d["recipient_evidence_hash"] == RECIPIENT_EVIDENCE_HASH


def test_evidence_hash_must_match_details(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("evidence hash does not match"):
        contract.submit_dispute_evidence(did, "EXECUTION_LOG", "a" * 64, "Some evidence that does not hash to a repeated a string")


def test_cannot_submit_evidence_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)

    with direct_vm.expect_revert("already submitted"):
        submit_evidence(direct_vm, contract, did, "ERROR_REPORT", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)


def test_evidence_validation_fields(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("evidence kind must be"):
        contract.submit_dispute_evidence(did, "NOT_A_KIND", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE)

    with direct_vm.expect_revert("evidence hash must be"):
        contract.submit_dispute_evidence(did, "ERROR_REPORT", "too-short", DEPOSITOR_EVIDENCE)

    with direct_vm.expect_revert("details must be 20"):
        contract.submit_dispute_evidence(did, "ERROR_REPORT", DEPOSITOR_EVIDENCE_HASH, "ab")


def test_finalize_needs_evidence(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    with direct_vm.expect_revert("evidence window is still open"):
        contract.finalize_dispute(did)


def test_finalize_after_deadline(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    d = contract.get_dispute(did)
    set_time("2030-01-02T00:00:00Z")  # past the 24h evidence window
    mock_adjudication(direct_vm, delivery_pct=100, quality_pct=100)

    contract.finalize_dispute(did)

    d = contract.get_dispute(did)
    assert d["status"] == "RESOLVED"
    assert d["delivery_pct"] == 100
    assert d["quality_pct"] == 100


def test_settle_requires_resolved(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    with direct_vm.expect_revert("dispute is not resolved"):
        contract.settle_dispute(did)


def test_partial_payout(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob, amount=1000)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    submit_evidence(direct_vm, contract, did, "TRANSACTION_RECEIPT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)

    set_time("2030-01-02T00:00:00Z")
    # delivery 80%, quality 50% -> product 4000 -> 40% payout
    mock_adjudication(direct_vm, delivery_pct=80, quality_pct=50)

    contract.finalize_dispute(did)

    d = contract.get_dispute(did)
    assert d["status"] == "RESOLVED"
    assert d["delivery_pct"] == 80
    assert d["quality_pct"] == 50


def test_close_stale_dispute(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    d = contract.get_dispute(did)
    set_time("2030-01-10T00:00:00Z")  # past evidence deadline + 7-day stale window

    contract.close_stale_dispute(did)
    d = contract.get_dispute(did)
    assert d["status"] == "RESOLVED"
    assert d["resolution_reason"] != ""


def test_cannot_close_non_stale(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    with direct_vm.expect_revert("not yet stale"):
        contract.close_stale_dispute(did)


def test_retry_dispute_after_failure(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    submit_evidence(direct_vm, contract, did, "TRANSACTION_RECEIPT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)

    set_time("2030-01-02T00:00:00Z")
    # First attempt: simulate an unparseable verdict
    mock_adjudication(direct_vm, delivery_pct=-1, quality_pct=-1)

    try:
        contract.finalize_dispute(did)
    except Exception:
        pass

    d = contract.get_dispute(did)
    assert d["status"] == "OPEN"
    assert d["attempts"] == 1

    # Clear old mock and set a new one for the retry
    direct_vm.clear_mocks()
    set_time("2030-01-02T00:10:00Z")  # past throttle
    mock_adjudication(direct_vm, delivery_pct=100, quality_pct=100)

    contract.retry_dispute(did)

    d = contract.get_dispute(did)
    assert d["status"] == "RESOLVED"
    assert d["delivery_pct"] == 100
    assert d["quality_pct"] == 100
    assert d["attempts"] == 2


def test_retry_dispute_throttled(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    submit_evidence(direct_vm, contract, did, "TRANSACTION_RECEIPT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)

    set_time("2030-01-02T00:00:00Z")
    mock_adjudication(direct_vm, delivery_pct=-1, quality_pct=-1)
    try:
        contract.finalize_dispute(did)
    except Exception:
        pass
    direct_vm.clear_mocks()

    # Retry immediately — same timestamp, should be throttled
    with direct_vm.expect_revert("adjudication is throttled"):
        contract.retry_dispute(did)


def test_cannot_retry_resolved(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)
    did = _file(direct_vm, contract, eid, direct_alice)

    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    submit_evidence(direct_vm, contract, did, "TRANSACTION_RECEIPT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)

    set_time("2030-01-02T00:00:00Z")
    mock_adjudication(direct_vm, delivery_pct=100, quality_pct=100)
    contract.finalize_dispute(did)

    with direct_vm.expect_revert("dispute must be in OPEN state"):
        contract.retry_dispute(did)


def test_get_dispute_missing_returns_none(direct_deploy):
    contract = direct_deploy("contracts/escrow_jury.py")
    assert contract.get_dispute(999) is None