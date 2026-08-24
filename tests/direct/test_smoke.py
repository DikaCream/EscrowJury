"""Full-path smoke test: escrow -> dispute -> evidence -> adjudication."""

from tests.direct.conftest import (
    TERMS,
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


def test_smoke_full_path(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")

    # 1. Create
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob, amount=1000)
    cfg = contract.get_config()
    assert cfg["escrow_count"] == 1
    assert cfg["escrow_locked"] == 1000

    e = contract.get_escrow(eid)
    assert e["status"] == "ACTIVE"
    assert e["amount"] == 1000
    assert len(e["terms_hash"]) == 64

    # 2. Terms are retrievable and immutable
    t = contract.get_escrow_terms(eid)
    assert t["terms_snapshot"] == TERMS
    assert t["terms_hash"] == e["terms_hash"]

    # 3. File dispute
    direct_vm.sender = direct_alice
    did = int(contract.file_dispute(eid, SHORT_REASON))

    d = contract.get_dispute(did)
    assert d["status"] == "PENDING_EVIDENCE"

    # 4. Submit evidence on both sides
    submit_evidence(direct_vm, contract, did, "EXECUTION_LOG", DEPOSITOR_EVIDENCE_HASH, DEPOSITOR_EVIDENCE, direct_alice)
    submit_evidence(direct_vm, contract, did, "ERROR_REPORT", RECIPIENT_EVIDENCE_HASH, RECIPIENT_EVIDENCE, direct_bob)

    d = contract.get_dispute(did)
    assert d["depositor_evidence"] == DEPOSITOR_EVIDENCE
    assert d["recipient_evidence"] == RECIPIENT_EVIDENCE

    # 5. Adjudicate and settle
    set_time("2030-01-02T00:00:00Z")
    mock_adjudication(direct_vm, delivery_pct=100, quality_pct=100)

    contract.finalize_dispute(did)
    d = contract.get_dispute(did)
    assert d["status"] == "RESOLVED"
    assert d["delivery_pct"] == 100
    assert d["quality_pct"] == 100

    contract.settle_dispute(did)
    cfg = contract.get_config()
    assert cfg["escrow_locked"] == 0

    # 6. Indexes are intact
    assert len(contract.list_depositor_escrows(direct_alice, 0, 50)) == 1
    assert len(contract.list_recipient_escrows(direct_bob, 0, 50)) == 1