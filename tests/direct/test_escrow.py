"""Escrow lifecycle: create, release, refund, validation."""

from tests.direct.conftest import TERMS, create_escrow, set_time, to_hex


def test_create_escrow_stores_terms(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob, amount=500)

    e = contract.get_escrow(eid)
    assert e is not None
    assert e["id"] == eid
    assert e["recipient"] == to_hex(direct_bob)
    assert e["amount"] == 500
    assert len(e["terms_hash"]) == 64
    assert e["status"] == "ACTIVE"
    assert e["dispute_id"] == 0

    t = contract.get_escrow_terms(eid)
    assert t["terms_hash"] == e["terms_hash"]
    assert t["terms_snapshot"] == TERMS


def test_create_escrow_indexes(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    create_escrow(contract, direct_vm, direct_alice, direct_bob)

    depositor_escrows = contract.list_depositor_escrows(direct_alice, 0, 50)
    assert len(depositor_escrows) == 1

    recipient_escrows = contract.list_recipient_escrows(direct_bob, 0, 50)
    assert len(recipient_escrows) == 1


def test_cannot_escrow_to_yourself(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/escrow_jury.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    with direct_vm.expect_revert("cannot escrow to yourself"):
        contract.create_escrow(direct_alice, TERMS, 1, 7)


def test_cannot_escrow_zero(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("must deposit GEN"):
        contract.create_escrow(direct_bob, TERMS, 1, 7)


def test_terms_length_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    with direct_vm.expect_revert("between 20 and 2000"):
        contract.create_escrow(direct_bob, "too short", 1, 7)


def test_release_escrow_by_depositor(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_alice
    contract.release_escrow(eid)

    e = contract.get_escrow(eid)
    assert e["status"] == "RELEASED"


def test_release_escrow_after_deadline(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    e = contract.get_escrow(eid)
    set_time("2030-01-09T00:00:00Z")  # past the 7-day auto-release deadline
    direct_vm.sender = direct_charlie
    contract.release_escrow(eid)

    e = contract.get_escrow(eid)
    assert e["status"] == "RELEASED"


def test_cannot_release_before_deadline_by_third_party(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("only the depositor"):
        contract.release_escrow(eid)


def test_refund_after_deadline(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    set_time("2030-01-09T00:00:00Z")
    direct_vm.sender = direct_charlie
    contract.refund_escrow(eid)

    e = contract.get_escrow(eid)
    assert e["status"] == "REFUNDED"


def test_cannot_refund_before_deadline(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    with direct_vm.expect_revert("cannot be refunded before"):
        contract.refund_escrow(eid)


def test_cannot_release_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    eid = create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_alice
    contract.release_escrow(eid)

    with direct_vm.expect_revert("escrow is not active"):
        contract.release_escrow(eid)


def test_list_escrows_paginates(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    create_escrow(contract, direct_vm, direct_alice, direct_bob)
    create_escrow(contract, direct_vm, direct_alice, direct_bob)

    page = contract.list_escrows(0, 1)
    assert len(page) == 1

    page2 = contract.list_escrows(0, 50)
    assert len(page2) == 2


def test_get_escrow_missing_returns_none(direct_deploy):
    contract = direct_deploy("contracts/escrow_jury.py")
    assert contract.get_escrow(999) is None
    assert contract.get_escrow_terms(999) is None


def test_config_tracks_escrow(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/escrow_jury.py")
    cfg = contract.get_config()
    assert cfg["escrow_count"] == 0
    assert cfg["escrow_locked"] == 0

    create_escrow(contract, direct_vm, direct_alice, direct_bob, amount=300)
    cfg = contract.get_config()
    assert cfg["escrow_count"] == 1
    assert cfg["escrow_locked"] == 300