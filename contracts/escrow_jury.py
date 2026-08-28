# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
EscrowJury — escrow primitive with AI adjudication.

Deposit GEN against a set of written terms. If both sides agree the terms
were met, the depositor releases the funds to the recipient. If they can't
agree, a quorum of GenLayer validators reads the committed terms and the
independently acquired evidence, and rules a partial or full payout.

Two-axis judgment, exact payout
    Validators score delivery (did the work actually happen?) and quality
    (was it acceptable?) on 0-100 scales. The payout ratio is the product
    of the two divided by 100. Consensus is bound to the EXACT canonical
    payout (floor(amount * delivery_pct * quality_pct / 10000) in whole
    GEN): two verdicts that imply different payouts are not equivalent,
    no matter how close the scores are. A zero on either axis zeroes the
    payout.

Evidence the contract acquires itself
    Evidence is an artifact URL plus a short description. At submission,
    validators independently fetch the artifact (gl.nondet.web.render),
    normalize it deterministically, and commit the exact bytes and their
    Keccak-256 hash to the dispute record through consensus. Adjudication
    judges those acquired bytes, not the parties' retellings. If an
    artifact cannot be acquired, that evidence is marked unverified and
    carries minimal weight.

Immutable commitments
    Terms are committed at escrow creation and never re-fetched. Evidence
    bytes are committed at submission and never re-fetched. Validators
    judge stored snapshots — nothing either party can rewrite after the
    fact.
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
MAX_EVIDENCE_URL_CHARS: int = 512
MAX_EVIDENCE_SNAPSHOT_CHARS: int = 4000

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
SETTLED = "SETTLED"
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


def _normalize_content(text: str) -> str:
    """Deterministic normalization so dynamic pages stay comparable.

    Collapses whitespace, drops blank lines, and removes consecutive
    duplicate lines (common boilerplate) so the snapshot validators commit
    is stable across fetches.
    """
    lines: list = []
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if not line:
            continue
        if lines and line == lines[-1]:
            continue
        lines.append(line)
    return "\n".join(lines)


def _hash_text(text: str) -> str:
    h = Keccak256()
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _is_valid_evidence_url(url: str) -> bool:
    if not url:
        return True
    if len(url) > MAX_EVIDENCE_URL_CHARS:
        return False
    if not (url.startswith("https://") or url.startswith("http://")):
        return False
    return " " not in url and "\n" not in url and "\t" not in url


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
    status: str  # ACTIVE | RELEASED | REFUNDED | DISPUTED | SETTLED
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
    depositor_evidence: str  # party-authored description
    depositor_evidence_kind: str
    depositor_evidence_url: str  # artifact URL (the material evidence)
    depositor_evidence_hash: str  # keccak of the acquired artifact snapshot
    depositor_evidence_snapshot: str  # acquired artifact bytes, stored on-chain
    depositor_evidence_verified: bool
    recipient_evidence: str
    recipient_evidence_kind: str
    recipient_evidence_url: str
    recipient_evidence_hash: str
    recipient_evidence_snapshot: str
    recipient_evidence_verified: bool
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

    def _decrement_locked(self, amount: int):
        locked = int(self.escrow_locked)
        if locked < amount:
            raise gl.vm.UserError("escrow locked underflow; escrow already settled?")
        self.escrow_locked = u256(locked - amount)

    # ── escrow lifecycle ────────────────────────────────────────────────

    @gl.public.write.payable
    def create_escrow(
        self,
        recipient: Address,
        terms: str,
        evidence_window_days: u256,
        auto_release_days: u256,
    ) -> u256:
        # The calldata roundtrip may deliver Address as raw bytes (direct
        # tests) or as a plain hex string (Python SDK). Convert anything
        # that isn't already an Address object.
        if isinstance(recipient, bytes):
            recipient = Address(recipient)
        elif isinstance(recipient, str):
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
        # Release is an affirmative depositor action. A timeout never grants
        # arbitrary callers the right to choose the beneficiary.
        if gl.message.sender_address != e.depositor:
            raise gl.vm.UserError("only the depositor can release escrow")

        e.status = RELEASED
        self.escrows[escrow_id] = e
        self._decrement_locked(int(e.amount))
        _NativeRecipient(e.recipient).emit_transfer(value=u256(e.amount))

    @gl.public.write
    def refund_escrow(self, escrow_id: u256):
        e = self._escrow_or_revert(escrow_id)
        if e.status != ACTIVE:
            raise gl.vm.UserError("escrow is not active")
        if self._now() < int(e.auto_release_deadline):
            raise gl.vm.UserError("escrow cannot be refunded before the auto-release deadline")
        # After the deadline, refund is the single timeout outcome and is
        # callable by anyone, so keepers can finalize abandoned escrows.

        e.status = REFUNDED
        self.escrows[escrow_id] = e
        self._decrement_locked(int(e.amount))
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
            depositor_evidence_url="",
            depositor_evidence_hash="",
            depositor_evidence_snapshot="",
            depositor_evidence_verified=False,
            recipient_evidence="",
            recipient_evidence_kind="",
            recipient_evidence_url="",
            recipient_evidence_hash="",
            recipient_evidence_snapshot="",
            recipient_evidence_verified=False,
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
        evidence_url: str,
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
        if not _is_valid_evidence_url(evidence_url):
            raise gl.vm.UserError("evidence url must be an http(s) URL of at most 512 characters")
        if not (MIN_EVIDENCE_CHARS <= len(evidence_details) <= MAX_EVIDENCE_CHARS):
            raise gl.vm.UserError("evidence details must be 20-3000 characters")

        if slot == "depositor":
            if d.depositor_evidence != "":
                raise gl.vm.UserError("depositor has already submitted evidence")
        else:
            if d.recipient_evidence != "":
                raise gl.vm.UserError("recipient has already submitted evidence")

        # Independently acquire the material evidence. Validators fetch the
        # artifact, normalize it deterministically, and commit the exact
        # bytes and their keccak hash to the dispute record. A failed fetch
        # marks the evidence unverified instead of trusting the description.
        verified = False
        snapshot = ""
        artifact_hash = ""
        if evidence_url:
            acquired = self._acquire_artifact(evidence_url)
            if acquired is not None:
                snapshot, artifact_hash = acquired
                verified = True

        if slot == "depositor":
            d.depositor_evidence = evidence_details
            d.depositor_evidence_kind = evidence_kind
            d.depositor_evidence_url = evidence_url
            d.depositor_evidence_hash = artifact_hash
            d.depositor_evidence_snapshot = snapshot
            d.depositor_evidence_verified = verified
        else:
            d.recipient_evidence = evidence_details
            d.recipient_evidence_kind = evidence_kind
            d.recipient_evidence_url = evidence_url
            d.recipient_evidence_hash = artifact_hash
            d.recipient_evidence_snapshot = snapshot
            d.recipient_evidence_verified = verified

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
        if e.status == SETTLED:
            raise gl.vm.UserError("escrow already settled")

        product = int(d.delivery_pct) * int(d.quality_pct)  # 0..10000
        to_recipient = (int(e.amount) * product) // 10000
        to_depositor = int(e.amount) - to_recipient

        self._decrement_locked(int(e.amount))

        e.status = SETTLED
        self.escrows[d.escrow_id] = e

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
        if e.status != DISPUTED:
            raise gl.vm.UserError("escrow is not in disputed state")

        # A stale dispute has one deterministic outcome: full refund. Update
        # both records atomically before transferring, leaving no ambiguous
        # DISPUTED escrow behind and preventing a second payout path.
        d.delivery_pct = u8(0)
        d.quality_pct = u8(0)
        d.resolution_reason = "stale dispute closed; full refund"
        d.status = RESOLVED
        e.status = REFUNDED
        self.disputes[dispute_id] = d
        self.escrows[d.escrow_id] = e

        self._decrement_locked(int(e.amount))
        _NativeRecipient(e.depositor).emit_transfer(value=u256(e.amount))

    # ── evidence acquisition ────────────────────────────────────────────

    def _acquire_artifact(self, url: str):
        """Fetch an evidence artifact and commit its exact bytes via consensus.

        Runs inside ``prompt_comparative`` so every validator independently
        fetches and normalizes the artifact. The quorum only accepts the
        submission when the snapshots carry the exact same hash, which is
        what makes the stored bytes trustworthy: they are the bytes the
        network actually saw, not a hash a party claimed.
        """

        def acquire() -> str:
            try:
                raw = gl.nondet.web.render(url, mode="text")
            except Exception:
                return json.dumps({"error": "unavailable"})
            normalized = _normalize_content(str(raw))
            if not normalized:
                return json.dumps({"error": "unavailable"})
            snapshot = normalized[:MAX_EVIDENCE_SNAPSHOT_CHARS]
            return json.dumps({"snapshot": snapshot, "hash": _hash_text(snapshot)}, sort_keys=True)

        principle = (
            "Answers are equivalent only when both are unavailable errors, or when both "
            "contain a snapshot and the exact same 64-character hash."
        )
        try:
            result = json.loads(gl.eq_principle.prompt_comparative(acquire, principle))
            if "error" in result:
                return None
            snapshot = str(result.get("snapshot", ""))
            digest = str(result.get("hash", ""))
            if not snapshot or digest != _hash_text(snapshot):
                return None
            return snapshot, digest
        except Exception:
            return None

    # ── adjudication ────────────────────────────────────────────────────

    def _run_adjudication(self, dispute_id: u256):
        d = self._dispute_or_revert(dispute_id)
        e = self._escrow_or_revert(d.escrow_id)

        def do_adjudicate() -> str:
            def evidence_block(side: str) -> str:
                if side == "depositor":
                    details, kind = d.depositor_evidence, d.depositor_evidence_kind
                    url, snapshot = d.depositor_evidence_url, d.depositor_evidence_snapshot
                    verified = d.depositor_evidence_verified
                else:
                    details, kind = d.recipient_evidence, d.recipient_evidence_kind
                    url, snapshot = d.recipient_evidence_url, d.recipient_evidence_snapshot
                    verified = d.recipient_evidence_verified
                if not details:
                    return ""
                status = (
                    "ACQUIRED AND HASH-VERIFIED ON-CHAIN"
                    if verified
                    else "NOT ACQUIRED (no verifiable artifact)"
                )
                block = f"{side.capitalize()} evidence ({kind}): {status}\nDescription: {details}\n"
                if url:
                    block += f"Artifact URL: {url}\n"
                if snapshot:
                    block += f"Acquired artifact bytes:\n<<<ARTIFACT>>>\n{snapshot}\n<<<END ARTIFACT>>>\n"
                return block + "\n"

            evidence_text = evidence_block("depositor") + evidence_block("recipient")

            prompt = f"""You are a dispute arbitrator. Judge whether the terms were met.

TERMS OF THE ESCROW:
{e.terms_snapshot}

COMPLAINT:
{d.reason}

EVIDENCE:
{evidence_text if evidence_text else "Neither side submitted evidence."}

Scoring rules:
- Base delivery and quality primarily on the ACQUIRED ARTIFACT BYTES above versus the terms.
  Party descriptions are secondary context only.
- Evidence marked NOT ACQUIRED has no verifiable artifact: it carries minimal weight. Do not
  award high delivery or quality on unverified claims alone, and state that in the reason.
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

        escrow_amount = int(e.amount)
        principle = (
            f"The escrow amount is {escrow_amount} GEN. Both answers are JSON arbitration "
            "verdicts. They are equivalent if and only if the canonical recipient payout "
            "they imply is EXACTLY equal, where canonical payout = "
            f"floor({escrow_amount} * delivery_pct * quality_pct / 10000) in whole GEN. "
            "Two verdicts that imply different payouts are NOT equivalent, even if the "
            "scores look close. The reason text may differ in wording as long as it is "
            "consistent with the scores. If either answer contains an 'error' key, they "
            "are equivalent only if both contain an 'error' key."
        )

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
            "depositor_evidence_url": d.depositor_evidence_url,
            "depositor_evidence_hash": d.depositor_evidence_hash,
            "depositor_evidence_snapshot": d.depositor_evidence_snapshot,
            "depositor_evidence_verified": bool(d.depositor_evidence_verified),
            "recipient_evidence": d.recipient_evidence,
            "recipient_evidence_kind": d.recipient_evidence_kind,
            "recipient_evidence_url": d.recipient_evidence_url,
            "recipient_evidence_hash": d.recipient_evidence_hash,
            "recipient_evidence_snapshot": d.recipient_evidence_snapshot,
            "recipient_evidence_verified": bool(d.recipient_evidence_verified),
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
        elif isinstance(address, str):
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
        elif isinstance(address, str):
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
