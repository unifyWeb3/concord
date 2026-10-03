# The equivalence argument

This document exists so a reviewer can argue with it. Every claim below is either checked by a
test in `tests/direct/` or reproducible on chain with one command. Where something is not
provable, it says so.

Write your objection against a numbered claim (`C1`, `C2`, …). Most of them reduce to either
"that field is not actually stable" or "the committee cannot actually see that value".

---

## 0. The setting

Concord is a GenLayer Intelligent Contract. When `observe(subject)` runs:

1. one node acts as **leader** and every other node acts as a **validator**;
2. each of them **independently** performs the non-deterministic fetch
   (`gl.nondet.web.request` to a public JSON-RPC endpoint);
3. each reduces the response to a canonical digest and a signature over it;
4. each validator's vote is `my_signature == leader_signature`;
5. the transaction finalises only if every validator votes true, and only then does anything
   get written to storage.

Step 5 is not a design choice. It is what `gl.vm.run_nondet_unsafe` documents:

> The result from the leader (iff validation passes, otherwise VM will be terminated)

So the two properties we need are separate, and it is worth being precise about which is which:

- **Safety** — an observation is admitted only if the whole committee computed the same
  signature. This is enforced by the chain and needs no help from us.
- **Liveness** — a round completes. This is threatened by *honest* nodes disagreeing, because
  two honest nodes can see different bytes. That is the whole hazard, and it is what the
  reduction addresses.

The reduction cannot make safety stronger. It can only stop honest disagreement from burning
rounds.

---

## 1. What may cross the boundary

### C1 (the stable-field rule)

Only fields that are **fixed for a given subject, forever, and equal on every honest node** may
enter the digest. Everything else is either dropped or replaced by a band.

*Proof obligation:* for every field in the digest, there is an argument that two honest nodes
fetching the same transaction produce the same value. Fields that fail this argument are not in
the digest. This is checkable field by field, below.

### C2 (banding)

A measurement that moves between two independent fetches may cross the boundary **only as a
band**: keep the bucket, discard the number.

*Why this direction and not the other:* banding is a function that is constant across a range
of inputs. If the underlying measurement moves but stays in its band, the band does not move, so
the digest does not move. A band that is too coarse costs discriminating power — two genuinely
different transactions could band identically — which is a liveness-adjacent weakness, not a
safety one. It cannot cause a wrong admission of a *different* transaction that differs in a
field we kept.

*Obligation accepted:* the bands are coarse. `log_band` distinguishes 0, 1–2, 3–8, 9–32 and
above 32 logs. Two transactions with 40 and 200 logs are indistinguishable to Concord. That is
a real loss of resolution and it is stated rather than hidden.

---

## 2. Field-by-field justification

Fetched: `eth_getTransactionReceipt` and `eth_getTransactionByHash`, batched into one request.

| Field in digest | Kept because | If a reviewer disputes this |
|---|---|---|
| `found` | A mined transaction's receipt existence is permanent. | See §4 — this is the weakest field for a *recent* transaction. |
| `status` | Written once at execution; never rewritten. | Hard to dispute. |
| `to` | Part of the signed transaction. Immutable. | Hard to dispute. |
| `selector` | First 4 bytes of calldata. Part of the signed payload. | Hard to dispute. |
| `topics` | Sorted **set** of log topic0 values. A mined receipt's log list is fixed. | Sorting is load-bearing: it makes the digest independent of log order. |
| `repeated_topics` | Subset of the above appearing more than once. Pure function of `topics`. | Free: it is derived, so it cannot introduce a new instability. |
| `value_band` | `value` is signed and immutable; the *band* of an immutable value is immutable. | Disputing this requires showing `value` can change post-mining. |
| `gas_band` | `gasUsed` is fixed for a mined transaction — but it is banded anyway. | See §3. |
| `log_band` | Log count is fixed for a mined receipt; banded anyway. | See §3. |

### Dropped, and why each would break it

`blockNumber`, `blockHash`, `transactionIndex`, `confirmations`, every timestamp,
`cumulativeGasUsed`, `effectiveGasPrice`, `gasPrice`, `maxFeePerGas`, `maxPriorityFeePerGas`,
`nonce`, `v`/`r`/`s`, `logsBloom`, `accessList`, the raw log `data` fields, and the raw response
body.

Any of these entering the digest is a live liveness bug, and most are *not* hypothetical:
`confirmations` and `blockNumber` change on every new block, so two nodes fetching seconds apart
would disagree and burn the round. `logsBloom` is a rolling hash over the whole block — it
changes for every transaction in the block, including ones that have nothing to do with this one.

*Check:* `test_no_volatile_field_survives_into_the_digest` asserts that a representative set of
these literals does not appear in the canonical string. `verify_live.py` additionally asserts
that the deployed contract's stored digest does not contain the real block height or the real
`gasUsed` for the observed transaction.

---

## 3. Deliberate weakening: exact gas is banded anyway

Exact `gasUsed` **is** immutable for a mined transaction. Concord bands it regardless. This is a
conscious loss of resolution, taken for three reasons:

1. **The primitive should not be transaction-shape-specific.** The rule a builder has to
   remember is "band anything that moves between two fetches", not "band the things that move
   for this particular RPC". A rule with exceptions does not survive contact with a new data
   source.
2. **It removes a whole class of false alarms.** RPC providers normalise `gasUsed` differently
   across clients, and a provider that adds fields to a receipt is a realistic failure mode.
   Banding makes Concord indifferent to that.
3. **The safety argument only needs one direction.** Agreement *under banding* implies agreement
   on the stable subset. A stricter digest would admit strictly fewer observations, not more.

The cost: two transactions with gas in the same band but different exact gas are
indistinguishable. Accepted, and stated in the README's limitations.

---

## 4. The weakest field, stated plainly

`found` is the one field that is genuinely not permanent for *every* transaction.

A transaction that is **not yet mined** has no receipt. If the leader fetches before it is
mined and a validator fetches after, they see `found=0` and `found=1`, the signatures differ,
and the round is burned — with **no misbehaviour by anyone**. This is a genuine liveness
limitation of any contract that observes recent chain state, and it is why:

- the bundled demo subject is a long-settled transaction (block 46400000);
- `found` is in the digest at all, rather than being treated as "absence is not information",
  because a receipt that does not exist yet and a receipt that does not exist forever are
  different facts and collapsing them would let a node that simply has a stale view look
  unanimous.

**Unverified:** we have not measured how often Studionet's validators fetch at divergent times
for a recently-mined transaction. We do not claim the field is safe under that condition; we
claim it is documented.

---

## 5. Why the signature, and not the digest string

The validator compares `sha256(canonical)`, not the canonical string itself. The digest is
already a canonical string, so the hash buys nothing for *correctness* — it exists because:

- a fixed-width 64-char value is a stable identity for an observation, which is what makes the
  equivocation ledger keyed by `(subject, validator)` meaningful;
- it gives a reviewer one string to compare by eye without reading ten fields.

**The security argument rests on collision-resistance of SHA-256.** Two different digests
producing the same signature would let a divergence pass unnoticed. That is the only
cryptographic assumption in the contract, and it is stated here so it can be argued with.

The hash is **not truncated.** A truncated digest is a collision invitation on the one field the
primitive exists to protect, and there was no size pressure justifying one.

The validator additionally refuses to compare across digest versions: if the leader's `schema`
is not the current `DIGEST_SCHEMA`, the vote is `False` rather than a comparison between two
different formats.

---

## 6. Equivocation recording, and the limit that cannot be removed

This is the part where honest engineering hits a protocol wall. Both facts below are read off
`genlayer/gl/vm.py`'s own docstring and behaviour.

### 6.1 Why a disagreement cannot be recorded in the round that caused it

If a validator votes `False`, the VM is **terminated** and every write in that transaction is
rolled back — including any equivocation record. So an equivocation is *never* recorded in the
transaction it causes.

### 6.2 Why a per-validator signature cannot be written to storage

GenVM consensus requires all validators to produce byte-identical state. If validator A writes
its own signature under its own key, the states differ, and the round cannot finalise. Per-
validator values are therefore not writable in a consensus-critical transaction at all.

### 6.3 What Concord does instead

`report_equivocation(subject, attributed_to, artifact)` is a **separate transaction**, and it is
gated on the committee rather than on the reporter:

1. the subject is re-observed under the same unanimous signature check, so the record rests on a
   digest the whole committee agreed on;
2. the admitted observation must still match it — if the committee now sees something else, the
   observation itself is in dispute and blaming a validator would be unfounded;
3. the contract reduces the supplied artifact **itself**, deterministically, and records the
   result. A reporter cannot assert a divergent signature; they must supply an artifact that
   genuinely reduces to one.

Checks 1–3 are what stop the record being fabricated. `scripts/verify_artifact.py` re-derives the
recorded signature off-chain from the stored artifact and compares it to what the chain holds.

### 6.4 The limit, which is a protocol limit and not a fixable bug

**The committee can unanimously attest that a signature diverges. It cannot attest which node
produced that signature.**

GenVM exposes no validator address to contract code (`gl.message` carries only sender, origin,
contract, value and chain id), and the disagreeing node is by definition not going to help
record itself. So:

- `attributed_to` is a **label supplied by the reporter**, and is only as strong as the reporter;
- what *is* unanimously attested is the **content** — that this artifact reduces to this digest,
  and that this digest is not the canonical one for this subject.

This is stated in the contract's own docstring, in the README's limitations, and in the verifier's
output. **Any claim that Concord identifies a misbehaving validator node is false.** The brief's
framing — "attributed to the specific validator address that diverged" — is the one part of the
spec that GenVM's execution model does not permit, and we say so rather than shipping a string
that looks like attribution and is not.

### 6.5 Quarantine is recorded, never enforced

`quarantined[validator]` is set once an address accumulates `quarantine_limit` attributed
equivocations. Concord **cannot** influence which nodes GenLayer selects as validators, so this
is a signal for whoever does control selection. It is not slashing, and it is not a defence
against a byzantine node — a byzantine node ignores it. BRIEF.md §6 question 2 asked whether to
quarantine or only record; we do both, and record the enforcement as out of scope.

---

## 7. What the tests actually establish

Run: `.venv/bin/python -m pytest tests/direct -q` (61 tests, no network).

| Test | Establishes |
|---|---|
| `test_two_volatile_views_of_one_tx_are_byte_identical` | Same transaction, different block/timestamp/gas/gasPrice → identical digest **and** identical signature. The central claim. |
| `test_three_differing_views_all_admit_the_same_signature` | Three pairwise-swapped volatile views, one signature. |
| `test_no_volatile_field_survives_into_the_digest` | Named volatile literals absent from the canonical string. |
| `test_a_genuinely_different_observation_produces_a_different_signature` | The reduction is not so lossy that a real difference passes: status, topics, and value-band changes each produce a distinct signature. |
| `test_divergent_validator_signature_blocks_admission` | A validator computing a different signature votes `False`, and the round is discarded — no observation, no admit count. |
| `test_an_oversized_artifact_is_rejected_not_truncated` | Regression for a defect **found on chain**: truncation turned a size limit into a claim about a validator's honesty. |
| `test_a_report_that_agrees_is_rejected` | An artifact reducing to the canonical digest is not an equivocation. |
| `test_a_report_is_rejected_when_the_admitted_digest_no_longer_matches` | No blame is attributed against an observation that is itself in dispute. |
| `test_zero_and_band_edges` | Every band edge, so an off-by-one in the edge tables cannot pass silently. |

### A caveat about the harness, stated because it matters

`gltest`'s direct runner executes the **leader inline** and *captures* the validator rather than
running it, and it does **not** roll state back when the validator votes `False`. Both are
harness behaviours. `run_nondet_unsafe` does neither: on a real node the validator runs and the
transaction is terminated.

`conftest.run_round` therefore replays the documented GenVM rule on top of the harness — snapshot,
observe, swap the mock so the validator's own fetch sees a different view, run the validator,
restore the snapshot if it voted `False`. That is a **simulation of a documented rule, not a
claim that the harness enforces it**, and it is what lets "a divergence blocks admission" be a
state-level assertion instead of a comment.

Swapping the mock between the two calls is how a validator on a different node, seeing different
bytes, is simulated. Everything else is mocked at the RPC boundary.

---

## 8. Reproducing the on-chain claims

```bash
# read-only, prints expected-vs-actual, exits non-zero on mismatch
.venv-deploy/bin/python scripts/verify_live.py --address 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87

# re-derive the recorded equivocation off-chain from the stored artifact
.venv-deploy/bin/python scripts/verify_artifact.py \
  --recorded 173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2
```

`verify_live.py` recomputes the expected digest **independently** — its own implementation of the
rule in this document, reading the same public receipt — rather than reading it back from the
contract. That duplication is deliberate: sharing code with the contract would make the
agreement a tautology.

---

## 9. Claims a reviewer should press on

Stated as objections, so they can be answered rather than discovered.

1. **"Banding is too coarse."** Agreed, and quantified in §2. It costs resolution on `log_band`
   and `gas_band`, never safety.
2. **"`found` is racy for recent transactions."** Agreed, and documented in §4. It is the one
   field that is not permanently stable, and the demo subject is deliberately old.
3. **"You cannot attribute a validator."** Correct, and not fixable in this architecture. §6.4.
4. **"SHA-256 over a short string is theatre."** The hash is an identity, not a security
   mechanism; collision-resistance is the actual assumption and it is stated in §5.
5. **"Your hermetic tests don't run the validator for real."** Correct — §7. `run_round`
   simulates the documented rule; the on-chain FINALIZED transactions are the real evidence that
   consensus ran.
6. **"The on-chain equivocation was manufactured."** It was, and it is labelled as a test
   harness everywhere it appears: in the script that produces it, in the artifact filename, in
   the verifier's output, and in the README. It is **not** evidence of a misbehaving validator.
   See §6.4 for why a real one cannot currently be produced at all.