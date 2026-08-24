# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
EscrowJury — escrow primitive with AI adjudication.

Deposit GEN against a set of written terms. If both sides agree the terms
were met, the depositor releases the funds to the recipient. If they can't
agree, a quorum of GenLayer validators reads the committed terms, inspects
the structured evidence each side stored on-chain, and rules a partial or
full payout.

Two-axis judgment
    Validators score delivery (did the work actually happen?) and quality
    (was it acceptable?) on 0-100 scales. The payout ratio is the product
    of the two divided by 100, so a zero on either axis zeroes the payout.
    Two verdicts count as equal when their payout products land in the same
    thousand-bucket, which means validators who weigh delivery and quality
    differently still reach consensus as long as the resulting split is the
    same.

Evidence that can't be faked
    Every evidence submission stores the raw details in the dispute record,
    hashes them with Keccak-256 on-chain, and reverts unless the claimed
    hash matches those exact bytes. The hash is the canonical locator, so
    there is no way to point validators at bytes the contract hasn't seen.

No live-URL adjudication
    Terms are committed at escrow creation and never re-fetched. Validators
    judge the stored snapshot, the complaint, and the authenticated evidence
    — nothing either party can rewrite after the fact.
"""
from genlayer import *
from dataclasses import dataclass
import datetime
import json
import typing

# ── constants ────────────────────────────────────────────────────────────────

MIN_TERMS_CHARS: int = 20
MAX_TERMS_CHARS: int = 2000

MIN_EVIDENCE_CHARS: int = 20
MAX_EVIDENCE_CHARS: int = 3000

VALID_EVIDENCE_KINDS: tuple[str, ...] = (
    "EXECUTION_LOG",
    "ERROR_REPORT",
    "TRANSACTION_RECEIPT",
    "SCREENSHOT",
    "OTHER",
)

SECONDS_PER_DAY: int = 86400
DEFAULT_EVIDENCE_WINDOW_SECONDS: int = 1 * SECONDS_PER_DAY   # 24 hours
DEFAULT_AUTO_RELEASE_SECONDS: int = 7 * SECONDS_PER_DAY      # 7 days
DEFAULT_STALE_DISPUTE_SECONDS: int = 7 * SECONDS_PER_DAY     # 7 days
DEFAULT_ADJUDICATION_THROTTLE_SECONDS: int = 60

# Escrow statuses
ACTIVE = "ACTIVE"
RELEASED = "RELEASED"
REFUNDED = "REFUNDED"
DISPUTED = "DISPUTED"
# Dispute statuses
PENDING_EVIDENCE = "PENDING_EVIDENCE"
OPEN = "OPEN"
RESOLVED = "RESOLVED"


# ── native transfer interface ────────────────────────────────────────────────


@gl.evm.contract_interface
class _NativeRecipient:
    """A plain address we send native GEN to.

    This must be the EVM interface, not ``gl.get_contract_at``: the GenVM
    proxy posts an intelligent-contract message that fails on a wallet with
    no contract. The EVM interface emits an ``EthSend`` with empty calldata,
    which is the native-value transfer an ordinary address can receive.
    """

    class View:
        pass

    class Write:
        pass


# ── helpers ──────────────────────────────────────────────────────────────────


def _is_hex_digest(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _canonical_evidence_reference(evidence_hash: str) -> str:
    return "onchain://evidence/" + evidence_hash


# ── storage ──────────────────────────────────────────────────────────────────


@allow_storage
@dataclass
class Escrow:
    id: u256
    depositor: Address
    recipient: Address
    amount: u256  # GEN deposited into escrow
    terms_hash: str
    terms_snapshot: str
    status: str  # ACTIVE | RELEASED | REFUNDED | DISPUTED
    created_at: u256
    auto_release_deadline: u256
    dispute_id: u256


@allow_storage
@dataclass
class Dispute:
    id: u256
    escrow_id: u256
    reason: str
    status: str  # PENDING_EVIDENCE | OPEN | RESOLVED
    evidence_deadline: u256
    depositor_evidence: str
    depositor_evidence_kind: str
    depositor_evidence_hash: str
    recipient_evidence: str
    recipient_evidence_kind: str
    recipient_evidence_hash: str
    delivery_pct: u8
    quality_pct: u8
    resolution_reason: str
    attempts: u8
    last_adjudicated_at: u256


# ── contract ─────────────────────────────────────────────────────────────────


class EscrowJury(gl.Contract):
    escrows: TreeMap[u256, Escrow]
    disputes: TreeMap[u256, Dispute]
    depositor_index: TreeMap[Address, DynArray[u256]]
    recipient_index: TreeMap[Address, DynArray[u256]]

    next_escrow_id: u256
    next_dispute_id: u256
    escrow_locked: u256  # total GEN held in {ACTIVE, DISPUTED} escrows

    def __init__(self):
        self.next_escrow_id = u256(1)
        self.next_dispute_id = u256(1)
        self.escrow_locked = u256(0)

    # ── helpers ─────────────────────────────────────────────────────────

    def _now(self) -> int:
        raw = gl.message_raw.get("datetime")
        if not raw:
            raise gl.vm.UserError("no timestamp available in this message")
        try:
            return int(
                datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            )
        except (ValueError, TypeError):
            raise gl.vm.UserError("malformed timestamp in this message")

    def _escrow_or_revert(self, escrow_id: u256) -> Escrow:
        e = self.escrows.get(escrow_id)
        if e is None:
            raise gl.vm.UserError("escrow not found")
        return e

    def _dispute_or_revert(self, dispute_id: u256) -> Dispute:
        d = self.disputes.get(dispute_id)
        if d is None:
            raise gl.vm.UserError("dispute not found")
        return d

    # ── escrow lifecycle ────────────────────────────────────────────────

    @gl.public.write.payable
    def create_escrow(
        self,
        recipient: Address,
        terms: str,
        evidence_window_days: u256,
        auto_release_days: u256,
    ) -> u256:
        # The calldata roundtrip in direct tests may deliver Address as
        # raw bytes; convert back so storage serialization works.
        if isinstance(recipient, bytes):
            recipient = Address(recipient)

        value = int(gl.message.value)
        if value <= 0:
            raise gl.vm.UserError("must deposit GEN with the escrow")
        if recipient == gl.message.sender_address:
            raise gl.vm.UserError("cannot escrow to yourself")
        if not (MIN_TERMS_CHARS <= len(terms) <= MAX_TERMS_CHARS):
            raise gl.vm.UserError("terms must be between 20 and 2000 characters")
        if int(auto_release_days) < 1:
            raise gl.vm.UserError("auto-release delay must be at least 1 day")
        if int(evidence_window_days) < 1:
            raise gl.vm.UserError("evidence window must be at least 1 day")

        h = Keccak256()
        h.update(terms.encode("utf-8"))
        terms_hash = h.hexdigest()

        now = self._now()
        eid = self.next_escrow_id
        self.next_escrow_id = u256(int(eid) + 1)

        escrow = Escrow(
            id=eid,
            depositor=gl.message.sender_address,
            recipient=recipient,
            amount=u256(value),
            terms_hash=terms_hash,
            terms_snapshot=terms,
            status=ACTIVE,
            created_at=u256(now),
            auto_release_deadline=u256(now + int(auto_release_days) * SECONDS_PER_DAY),
            dispute_id=u256(0),
        )
        self.escrows[eid] = escrow

        self.depositor_index.get_or_insert_default(gl.message.sender_address).append(eid)
        self.recipient_index.get_or_insert_default(recipient).append(eid)

        self.escrow_locked = u256(int(self.escrow_locked) + value)

        return eid

    @gl.public.write
    def release_escrow(self, escrow_id: u256):
        e = self._escrow_or_revert(escrow_id)
        if e.status != ACTIVE:
            raise gl.vm.UserError("escrow is not active")
        sender = gl.message.sender_address
        if sender != e.depositor:
            if self._now() < int(e.auto_release_deadline):
                raise gl.vm.UserError("only the depositor can release before the deadline")

        e.status = RELEASED
        self.escrows[escrow_id] = e
        self.escrow_locked = u256(int(self.escrow_locked) - int(e.amount))
        _NativeRecipient(e.recipient).emit_transfer(value=u256(e.amount))

    @gl.public.write
    def refund_escrow(self, escrow_id: u256):
        e = self._escrow_or_revert(escrow_id)
        if e.status != ACTIVE:
            raise gl.vm.UserError("escrow is not active")
        if self._now() < int(e.auto_release_deadline):
            raise gl.vm.UserError("escrow cannot be refunded before the auto-release deadline")

        e.status = REFUNDED
        self.escrows[escrow_id] = e
        self.escrow_locked = u256(int(self.escrow_locked) - int(e.amount))
        _NativeRecipient(e.depositor).emit_transfer(value=u256(e.amount))

    # ── dispute ─────────────────────────────────────────────────────────

    @gl.public.write
    def file_dispute(self, escrow_id: u256, reason: str) -> u256:
        e = self._escrow_or_revert(escrow_id)
        if int(e.dispute_id) != 0:
            raise gl.vm.UserError("this escrow already has an open dispute")
        if e.status != ACTIVE:
            raise gl.vm.UserError("escrow is not active")
        sender = gl.message.sender_address
        if sender != e.depositor and sender != e.recipient:
            raise gl.vm.UserError("only the depositor or recipient can file a dispute")
        if len(reason) < 20:
            raise gl.vm.UserError("dispute reason must be at least 20 characters")

        now = self._now()
        did = self.next_dispute_id
        self.next_dispute_id = u256(int(did) + 1)

        d = Dispute(
            id=did,
            escrow_id=escrow_id,
            reason=reason,
            status=PENDING_EVIDENCE,
            evidence_deadline=u256(now + DEFAULT_EVIDENCE_WINDOW_SECONDS),
            depositor_evidence="",
            depositor_evidence_kind="",
            depositor_evidence_hash="",
            recipient_evidence="",
            recipient_evidence_kind="",
            recipient_evidence_hash="",
            delivery_pct=u8(0),
            quality_pct=u8(0),
            resolution_reason="",
            attempts=u8(0),
            last_adjudicated_at=u256(0),
        )
        self.disputes[did] = d

        e.dispute_id = did
        e.status = DISPUTED
        self.escrows[escrow_id] = e

        return did

    @gl.public.write
    def submit_dispute_evidence(
        self,
        dispute_id: u256,
        evidence_kind: str,
        evidence_hash: str,
        evidence_details: str,
    ):
        d = self._dispute_or_revert(dispute_id)
        if d.status not in (PENDING_EVIDENCE, OPEN):
            raise gl.vm.UserError("only an open dispute accepts evidence")

        e = self._escrow_or_revert(d.escrow_id)
        sender = gl.message.sender_address

        if sender == e.depositor:
            slot = "depositor"
        elif sender == e.recipient:
            slot = "recipient"
        else:
            raise gl.vm.UserError("only the depositor or recipient can submit evidence")

        if evidence_kind not in VALID_EVIDENCE_KINDS:
            raise gl.vm.UserError("evidence kind must be one of: " + ", ".join(VALID_EVIDENCE_KINDS))
        if not _is_hex_digest(evidence_hash):
            raise gl.vm.UserError("evidence hash must be a 64-character hex string")
        if not (MIN_EVIDENCE_CHARS <= len(evidence_details) <= MAX_EVIDENCE_CHARS):
            raise gl.vm.UserError("evidence details must be 20-3000 characters")

        # Bind the claimed hash to the exact bytes being stored. The details
        # become the retrievable artifact in the dispute record; the hash is
        # the canonical locator (onchain://evidence/<hash>).
        computed = Keccak256()
        computed.update(evidence_details.encode("utf-8"))
        if computed.hexdigest() != evidence_hash:
            raise gl.vm.UserError("evidence hash does not match evidence details")

        if slot == "depositor":
            if d.depositor_evidence != "":
                raise gl.vm.UserError("depositor has already submitted evidence")
            d.depositor_evidence = evidence_details
            d.depositor_evidence_kind = evidence_kind
            d.depositor_evidence_hash = evidence_hash
        else:
            if d.recipient_evidence != "":
                raise gl.vm.UserError("recipient has already submitted evidence")
            d.recipient_evidence = evidence_details
            d.recipient_evidence_kind = evidence_kind
            d.recipient_evidence_hash = evidence_hash

        self.disputes[dispute_id] = d

    @gl.public.write
    def finalize_dispute(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        if d.status not in (PENDING_EVIDENCE, OPEN):
            raise gl.vm.UserError("dispute is not open")

        now = self._now()
        both_submitted = d.depositor_evidence != "" and d.recipient_evidence != ""
        deadline_passed = now >= int(d.evidence_deadline)

        if not both_submitted and not deadline_passed:
            raise gl.vm.UserError(
                "evidence window is still open; wait for both sides to submit or the deadline to pass"
            )

        if int(d.last_adjudicated_at) > 0:
            elapsed = now - int(d.last_adjudicated_at)
            if elapsed < DEFAULT_ADJUDICATION_THROTTLE_SECONDS:
                raise gl.vm.UserError("adjudication is throttled; wait before retrying")

        d.status = OPEN
        d.last_adjudicated_at = u256(now)
        d.attempts = u8(int(d.attempts) + 1)
        self.disputes[dispute_id] = d

        self._run_adjudication(dispute_id)

    @gl.public.write
    def retry_dispute(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        if d.status != OPEN:
            raise gl.vm.UserError("dispute must be in OPEN state to retry")

        now = self._now()
        elapsed = now - int(d.last_adjudicated_at)
        if elapsed < DEFAULT_ADJUDICATION_THROTTLE_SECONDS:
            raise gl.vm.UserError("adjudication is throttled; wait before retrying")

        d.last_adjudicated_at = u256(now)
        d.attempts = u8(int(d.attempts) + 1)
        self.disputes[dispute_id] = d

        self._run_adjudication(dispute_id)

    @gl.public.write
    def settle_dispute(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        if d.status != RESOLVED:
            raise gl.vm.UserError("dispute is not resolved")

        e = self._escrow_or_revert(d.escrow_id)
        product = int(d.delivery_pct) * int(d.quality_pct)  # 0..10000
        to_recipient = (int(e.amount) * product) // 10000
        to_depositor = int(e.amount) - to_recipient

        self.escrow_locked = u256(int(self.escrow_locked) - int(e.amount))

        if to_recipient > 0:
            _NativeRecipient(e.recipient).emit_transfer(value=u256(to_recipient))
        if to_depositor > 0:
            _NativeRecipient(e.depositor).emit_transfer(value=u256(to_depositor))

    @gl.public.write
    def close_stale_dispute(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        if d.status == RESOLVED:
            raise gl.vm.UserError("dispute is already resolved")

        now = self._now()
        if now < int(d.evidence_deadline) + DEFAULT_STALE_DISPUTE_SECONDS:
            raise gl.vm.UserError("dispute is not yet stale")

        e = self._escrow_or_revert(d.escrow_id)
        d.delivery_pct = u8(0)
        d.quality_pct = u8(0)
        d.resolution_reason = "stale dispute closed; full refund"
        d.status = RESOLVED
        self.disputes[dispute_id] = d

        self.escrow_locked = u256(int(self.escrow_locked) - int(e.amount))
        _NativeRecipient(e.depositor).emit_transfer(value=u256(e.amount))

    # ── adjudication ────────────────────────────────────────────────────

    def _run_adjudication(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        e = self._escrow_or_revert(d.escrow_id)

        def do_adjudicate() -> str:
            evidence_text = ""
            if d.depositor_evidence:
                evidence_text += f"Depositor evidence ({d.depositor_evidence_kind}):\n{d.depositor_evidence}\n\n"
            if d.recipient_evidence:
                evidence_text += f"Recipient evidence ({d.recipient_evidence_kind}):\n{d.recipient_evidence}\n"

            prompt = f"""You are a dispute arbitrator. Judge whether the terms were met.

TERMS OF THE ESCROW:
{e.terms_snapshot}

COMPLAINT:
{d.reason}

EVIDENCE:
{evidence_text if evidence_text else "Neither side submitted evidence."}

Scoring rules:
- delivery_pct: 0-100. 0 = nothing delivered. 100 = fully delivered.
- quality_pct: 0-100. 0 = unacceptable. 100 = exactly as agreed.
- The payout ratio is delivery_pct * quality_pct / 100. A zero on either axis zeroes the payout.

Return ONLY a JSON object with exactly these three keys:
{{"delivery_pct": <int 0-100>, "quality_pct": <int 0-100>, "reason": "<one sentence>"}}

Do not include markdown or extra text before or after the JSON."""
            try:
                data = gl.nondet.exec_prompt(prompt, response_format="json")
                delivery_pct = int(data.get("delivery_pct", -1))
                quality_pct = int(data.get("quality_pct", -1))
                reason = str(data.get("reason", ""))[:600]
            except Exception:
                return json.dumps({"error": "unparseable verdict"})
            if not (0 <= delivery_pct <= 100) or not (0 <= quality_pct <= 100):
                return json.dumps({"error": "invalid verdict"})
            return json.dumps(
                {"delivery_pct": delivery_pct, "quality_pct": quality_pct, "reason": reason},
                sort_keys=True,
            )

        principle = """Both answers are JSON arbitration verdicts. They are equivalent if and only if
the product (delivery_pct * quality_pct) falls in the same thousand-bucket:
0-999, 1000-1999, 2000-2999, ..., 9000-9999, 10000. The reason text may differ
in wording as long as it supports the same outcome. If either answer contains
an "error" key, they are equivalent only if both contain an "error" key."""

        try:
            result_raw = gl.eq_principle.prompt_comparative(do_adjudicate, principle)
            verdict = json.loads(result_raw)
            if "error" in verdict:
                d.status = OPEN
                self.disputes[dispute_id] = d
                return
            delivery_pct = int(verdict["delivery_pct"])
            quality_pct = int(verdict["quality_pct"])
            reason = str(verdict.get("reason", ""))[:600]
            if not (0 <= delivery_pct <= 100) or not (0 <= quality_pct <= 100):
                d.status = OPEN
                self.disputes[dispute_id] = d
                return
        except Exception:
            d.status = OPEN
            self.disputes[dispute_id] = d
            return

        d.delivery_pct = u8(delivery_pct)
        d.quality_pct = u8(quality_pct)
        d.resolution_reason = reason
        d.status = RESOLVED
        self.disputes[dispute_id] = d

    # ── views ───────────────────────────────────────────────────────────

    @gl.public.view
    def get_config(self) -> typing.Any:
        return {
            "escrow_count": int(self.next_escrow_id) - 1,
            "dispute_count": int(self.next_dispute_id) - 1,
            "escrow_locked": int(self.escrow_locked),
            "evidence_window_seconds": DEFAULT_EVIDENCE_WINDOW_SECONDS,
            "auto_release_seconds": DEFAULT_AUTO_RELEASE_SECONDS,
            "stale_dispute_seconds": DEFAULT_STALE_DISPUTE_SECONDS,
        }

    @gl.public.view
    def get_escrow(self, escrow_id: u256) -> typing.Any:
        e = self.escrows.get(escrow_id)
        if e is None:
            return None
        return {
            "id": int(e.id),
            "depositor": str(e.depositor),
            "recipient": str(e.recipient),
            "amount": int(e.amount),
            "terms_hash": e.terms_hash,
            "status": e.status,
            "created_at": int(e.created_at),
            "auto_release_deadline": int(e.auto_release_deadline),
            "dispute_id": int(e.dispute_id),
        }

    @gl.public.view
    def get_escrow_terms(self, escrow_id: u256) -> typing.Any:
        e = self.escrows.get(escrow_id)
        if e is None:
            return None
        return {
            "id": int(e.id),
            "terms_hash": e.terms_hash,
            "terms_snapshot": e.terms_snapshot,
        }

    @gl.public.view
    def get_dispute(self, dispute_id: u256) -> typing.Any:
        d = self.disputes.get(dispute_id)
        if d is None:
            return None
        return {
            "id": int(d.id),
            "escrow_id": int(d.escrow_id),
            "reason": d.reason,
            "status": d.status,
            "evidence_deadline": int(d.evidence_deadline),
            "depositor_evidence": d.depositor_evidence,
            "depositor_evidence_kind": d.depositor_evidence_kind,
            "depositor_evidence_hash": d.depositor_evidence_hash,
            "recipient_evidence": d.recipient_evidence,
            "recipient_evidence_kind": d.recipient_evidence_kind,
            "recipient_evidence_hash": d.recipient_evidence_hash,
            "delivery_pct": int(d.delivery_pct),
            "quality_pct": int(d.quality_pct),
            "resolution_reason": d.resolution_reason,
            "attempts": int(d.attempts),
        }

    @gl.public.view
    def list_escrows(self, offset: u256, limit: u256) -> typing.Any:
        results: list = []
        total = int(self.next_escrow_id) - 1
        for i in range(int(offset), min(int(offset) + int(limit), total)):
            e = self.escrows.get(u256(i + 1))
            if e is not None:
                results.append({
                    "id": int(e.id),
                    "depositor": str(e.depositor),
                    "recipient": str(e.recipient),
                    "amount": int(e.amount),
                    "terms_hash": e.terms_hash,
                    "status": e.status,
                    "created_at": int(e.created_at),
                    "auto_release_deadline": int(e.auto_release_deadline),
                    "dispute_id": int(e.dispute_id),
                })
        return results

    @gl.public.view
    def list_depositor_escrows(self, address: Address, offset: u256, limit: u256) -> typing.Any:
        if isinstance(address, bytes):
            address = Address(address)
        ids = self.depositor_index.get_or_insert_default(address)
        results: list = []
        for idx in range(int(offset), min(int(offset) + int(limit), len(ids))):
            e = self.escrows.get(ids[idx])
            if e is not None:
                results.append({
                    "id": int(e.id),
                    "depositor": str(e.depositor),
                    "recipient": str(e.recipient),
                    "amount": int(e.amount),
                    "terms_hash": e.terms_hash,
                    "status": e.status,
                    "created_at": int(e.created_at),
                    "auto_release_deadline": int(e.auto_release_deadline),
                    "dispute_id": int(e.dispute_id),
                })
        return results

    @gl.public.view
    def list_recipient_escrows(self, address: Address, offset: u256, limit: u256) -> typing.Any:
        if isinstance(address, bytes):
            address = Address(address)
        ids = self.recipient_index.get_or_insert_default(address)
        results: list = []
        for idx in range(int(offset), min(int(offset) + int(limit), len(ids))):
            e = self.escrows.get(ids[idx])
            if e is not None:
                results.append({
                    "id": int(e.id),
                    "depositor": str(e.depositor),
                    "recipient": str(e.recipient),
                    "amount": int(e.amount),
                    "terms_hash": e.terms_hash,
                    "status": e.status,
                    "created_at": int(e.created_at),
                    "auto_release_deadline": int(e.auto_release_deadline),
                    "dispute_id": int(e.dispute_id),
                })
        return results
