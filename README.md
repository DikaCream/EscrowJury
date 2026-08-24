# EscrowJury

A GenLayer Intelligent Contract primitive for escrow and dispute resolution — deposit GEN against written terms, and if the two sides can't agree on whether the terms were met, a quorum of validators reads the evidence and rules a partial or full payout.

## What makes it a primitive

There's no marketplace, no sales pipeline, no skill listings. Just escrow and adjudication. Any contract or dapp that needs buyer-seller escrow with AI-backed dispute resolution can drop this in — the API is four write calls for a full lifecycle.

- **Immutable terms.** Terms are hashed at creation and never re-fetched. Validators only see the snapshot that was locked in when the escrow was funded.
- **Evidence that stays on chain.** Each side submits raw details that the contract hashes with Keccak-256 and stores in the dispute record. Mismatched hashes revert. External URLs aren't accepted — the hash locates the canonical bytes in the dispute data itself.
- **Two-axis scoring.** Validators score delivery (did the work happen?) and quality (was it good?) on 0-100 scales. The product gives the payout ratio. A zero on either axis zeroes the payout.
- **Bucket equivalence.** Two verdicts agree when their payout products land in the same thousand-bucket (0-999, 1000-1999, ..., 9000-9999, 10000). Validators who weigh delivery and quality differently can still reach consensus as long as the final split is the same.
- **Evidence window.** Filing a dispute opens a 24-hour window. Validators aren't called until both sides submit or the clock runs out. No one gets ambushed by an AI verdict before they've had a chance to make their case.

## Lifecycle

1. **Deposit.** The depositor locks GEN plus the terms, naming a recipient. The contract hashes and stores the terms. Status: ACTIVE.
2. **Release or refund.** The depositor can release funds to the recipient at any time. After the auto-release deadline (7 days), anyone can release or refund.
3. **Dispute.** Either side files a dispute with a written reason. The escrow becomes DISPUTED. A 24-hour evidence deadline starts ticking.
4. **Evidence.** Buyer and seller each submit one structured record: a kind (EXECUTION_LOG, ERROR_REPORT, TRANSACTION_RECEIPT, SCREENSHOT, OTHER), the evidence bytes, and a hash that the contract verifies on-chain.
5. **Adjudication.** Once both sides submit or the evidence deadline passes, anyone calls finalize. Validators read the terms, the complaint, and the evidence. They return delivery_pct and quality_pct. The contract computes the payout.
6. **Settle.** Anyone calls settle to distribute funds. Payout to recipient = amount * delivery_pct * quality_pct / 10000. Remainder returns to depositor.
7. **Stale close.** If a dispute stays unresolved for 7 days after the evidence deadline, anyone can close it — the full amount refunds to the depositor.

## Contract API

### Write methods

| Method | Args | Description |
|---|---|---|
| `create_escrow` | recipient (Address), terms, evidence_window_days, auto_release_days | Deposits GEN, stores immutable terms. Payable. |
| `release_escrow` | escrow_id | Sends escrow funds to recipient. Depositor only before deadline; anyone after. |
| `refund_escrow` | escrow_id | Returns escrow funds to depositor. Only after auto-release deadline. |
| `file_dispute` | escrow_id, reason | Opens a dispute. Either party. |
| `submit_dispute_evidence` | dispute_id, kind, hash, details | Submits one evidence record. Hash verified on-chain with Keccak-256. |
| `finalize_dispute` | dispute_id | Starts validator adjudication. Requires both evidence or expired deadline. |
| `retry_dispute` | dispute_id | Re-runs adjudication after a failed verdict. Throttled. |
| `settle_dispute` | dispute_id | Distributes funds per the adjudication verdict. |
| `close_stale_dispute` | dispute_id | Refunds depositor if dispute stayed open too long. |

### View methods

| Method | Args | Returns |
|---|---|---|
| `get_config` | — | escrow_count, dispute_count, escrow_locked, window configs |
| `get_escrow` | escrow_id | Escrow fields (terms omitted; see get_escrow_terms) |
| `get_escrow_terms` | escrow_id | terms_hash, terms_snapshot (the committed terms) |
| `get_dispute` | dispute_id | Full dispute record including evidence, verdict, status |
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

EscrowJury uses `gl.eq_principle.prompt_comparative` for the two-axis adjudication. The validator prompt asks for `delivery_pct` and `quality_pct` as separate integers. Two verdicts are equivalent when their product falls in the same thousand-bucket — the reason text can differ.

This means a validator who scores (100 delivery, 50 quality) agrees with one who scores (50 delivery, 100 quality). Both produce a 5000 product and a 50% payout. The contract doesn't care which axis was penalized, only that the resulting split is consistent across the quorum.

For evidence integrity, every submission passes through `Keccak256(evidence_details) == evidence_hash` on-chain. The raw details live in the dispute record and are retrievable through `get_dispute`. Validators see the stored bytes, not a URL the parties control.

## Status

31 direct tests covering escrow lifecycle, evidence validation, hash binding, adjudication mocks, stale closing, and full-path integration.

**Live on StudioNet:** `0xa9A2F7422D4d56153989287bF601Bb5Cb8aEa14A` ([Studio explorer](https://explorer-studio.genlayer.com/address/0xa9A2F7422D4d56153989287bF601Bb5Cb8aEa14A) 
