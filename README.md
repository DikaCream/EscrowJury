# EscrowJury

A GenLayer Intelligent Contract primitive for escrow and dispute resolution — deposit GEN against written terms, and if the two sides can't agree on whether the terms were met, a quorum of validators reads the evidence and rules a partial or full payout.

## What makes it a primitive

There's no marketplace, no sales pipeline, no skill listings. Just escrow and adjudication. Any contract or dapp that needs buyer-seller escrow with AI-backed dispute resolution can drop this in — the API is four write calls for a full lifecycle.

- **Immutable terms.** Terms are hashed at creation and never re-fetched. Validators only see the snapshot that was locked in when the escrow was funded.
- **Evidence the contract acquires itself.** Evidence is an artifact URL plus a short description. At submission, validators independently fetch the artifact, normalize it deterministically, and commit the exact bytes and their Keccak-256 hash to the dispute record through consensus. Adjudication judges those acquired bytes, not the parties' retellings.
- **Two-axis scoring.** Validators score delivery (did the work happen?) and quality (was it good?) on 0-100 scales. The product gives the payout ratio. A zero on either axis zeroes the payout.
- **Exact-payout consensus.** Two verdicts are equivalent only when they imply the exact same payout in whole GEN (floor(amount * delivery_pct * quality_pct / 10000)). Scores that differ by even one point on a large escrow are not equivalent, so consensus is bound to the money, not to a score bucket.
- **Evidence window.** Filing a dispute opens a 24-hour window. Validators aren't called until both sides submit or the clock runs out. No one gets ambushed by an AI verdict before they've had a chance to make their case.

## Lifecycle

1. **Deposit.** The depositor locks GEN plus the terms, naming a recipient. The contract hashes and stores the terms. Status: ACTIVE.
2. **Release or refund.** Only the depositor can release funds to the recipient. After the auto-release deadline (7 days), refund to the depositor is the single deterministic timeout outcome and anyone may call it as a keeper.
3. **Dispute.** Either side files a dispute with a written reason. The escrow becomes DISPUTED. A 24-hour evidence deadline starts ticking.
4. **Evidence.** Buyer and seller each submit one structured record: a kind (EXECUTION_LOG, ERROR_REPORT, TRANSACTION_RECEIPT, SCREENSHOT, OTHER), the artifact URL (the material evidence), and a short description. Validators fetch the artifact, normalize it, and commit its bytes plus keccak hash to the dispute record. If the artifact can't be acquired, the evidence is marked unverified.
5. **Adjudication.** Once both sides submit or the evidence deadline passes, anyone calls finalize. Validators read the terms, the complaint, the description, and the acquired artifact bytes. They return delivery_pct and quality_pct, and consensus requires the exact payout to match. The contract computes the payout.
6. **Settle.** Anyone calls settle to distribute funds. Payout to recipient = amount * delivery_pct * quality_pct / 10000. Remainder returns to depositor.
7. **Stale close.** If a dispute stays unresolved for 7 days after the evidence deadline, anyone can close it. The dispute becomes RESOLVED with a zero payout and the escrow itself becomes REFUNDED before the full amount is returned to the depositor.

## Contract API

### Write methods

| Method | Args | Description |
|---|---|---|
| `create_escrow` | recipient (Address), terms, evidence_window_days, auto_release_days | Deposits GEN, stores immutable terms. Payable. |
| `release_escrow` | escrow_id | Sends escrow funds to recipient. Only the depositor may call it, before or after the deadline. |
| `refund_escrow` | escrow_id | Returns escrow funds to depositor. Anyone may call it after the deadline; refund is the only timeout outcome. |
| `file_dispute` | escrow_id, reason | Opens a dispute. Either party. |
| `submit_dispute_evidence` | dispute_id, kind, url, details | Submits one evidence record. Validators fetch the artifact URL and commit its bytes + keccak hash on-chain. |
| `finalize_dispute` | dispute_id | Starts validator adjudication. Requires both evidence or expired deadline. |
| `retry_dispute` | dispute_id | Re-runs adjudication after a failed verdict. Throttled. |
| `settle_dispute` | dispute_id | Distributes funds per the adjudication verdict. |
| `close_stale_dispute` | dispute_id | Deterministically refunds the depositor, marks the dispute RESOLVED and escrow REFUNDED, and can be called by anyone after the stale threshold. |

### View methods

| Method | Args | Returns |
|---|---|---|
| `get_config` | — | escrow_count, dispute_count, escrow_locked, window configs |
| `get_escrow` | escrow_id | Escrow fields (terms omitted; see get_escrow_terms) |
| `get_escrow_terms` | escrow_id | terms_hash, terms_snapshot (the committed terms) |
| `get_dispute` | dispute_id | Full dispute record including acquired evidence bytes + hash, verdict, status |
| `list_escrows` | offset, limit | Paginated list of all escrows |
| `list_depositor_escrows` | address, offset, limit | Escrows by depositor |
| `list_recipient_escrows` | address, offset, limit | Escrows by recipient |

## Project structure

```
EscrowJury/
├── contracts/
│   └── escrow_jury.py    # The Intelligent Contract
├── tests/
│   └── direct/
│       ├── conftest.py   # Fixtures, time travel, evidence helpers
│       ├── test_escrow.py       # Create, release, refund, validation
│       ├── test_dispute.py      # Dispute, evidence, adjudication
│       └── test_smoke.py        # Full-path integration
├── pyproject.toml
└── README.md
```

## Requirements

- Python 3.12+
- genlayer-test (`pip install genlayer-test`)
- genlayer CLI (`npm install -g genlayer`)

## Development

```bash
# Run all direct tests (in-memory, no network)
pytest tests/direct/ -v

# Deploy to StudioNet (gasless)
genlayer account use <your-account>
genlayer deploy --contract contracts/escrow_jury.py
```

## Consensus design

EscrowJury uses `gl.eq_principle.prompt_comparative` for both evidence acquisition and adjudication.

**Evidence acquisition.** When a party submits evidence, the equivalence principle requires every validator's independently fetched snapshot to carry the exact same keccak hash. Only then are the bytes stored. This is what makes the stored evidence trustworthy: it is the bytes the network actually saw, not a hash a party claimed. A fetch that fails marks the evidence unverified, and the judge is instructed to give unverified claims minimal weight.

**Adjudication.** The validator prompt asks for `delivery_pct` and `quality_pct` as separate integers, judged primarily against the acquired artifact bytes. Two verdicts are equivalent if and only if they imply the exact same canonical payout, `floor(escrow_amount * delivery_pct * quality_pct / 10000)` in whole GEN — the amount is embedded in the equivalence principle so validators can compute it. The reason text may differ in wording. This means validators who score (100 delivery, 50 quality) and (50 delivery, 100 quality) still agree (both imply a 50% payout), but a 49% vs 50% split does not: consensus is bound to the payout, not to a bucket.

If the quorum can't agree on a payout, the dispute stays OPEN and `retry_dispute` re-runs adjudication after a throttle window.

## Status

37 direct tests covering escrow lifecycle, evidence validation, artifact acquisition and verification, adjudication, stale closing, and full-path integration.

The full lifecycle was live-tested on StudioNet: create escrow, file dispute, submit evidence on both sides (validators fetched example.com and committed matching snapshots, verified on-chain), adjudicate (verdict grounded in the acquired artifact bytes), and settle (escrow_locked released).

**Live on StudioNet:** `0x3c3d66A8c0a399C119Ee7a3d0e8923Fb8Ee5dD1A` ([Studio explorer](https://explorer-studio.genlayer.com/address/0x3c3d66A8c0a399C119Ee7a3d0e8923Fb8Ee5dD1A))
