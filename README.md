# Concord

**Equivocation as a first-class, on-chain, committee-attested event.**

Concord is a GenLayer Intelligent Contract primitive. It fetches a subject, reduces it to a
canonical digest, and admits it **only if the leader and every validator independently computed
the same signature**. Any disagreement aborts the round, and a separate committee-gated
transaction records the divergent signature.

What the committee can attest is *that* a signature diverges, unanimously. It cannot attest
*who* produced it, because GenVM exposes no validator address to contract code. See
[Limitations](#limitations). The address stored with a record is the reporter's label.

There is no model in the correctness path. Agreement is arithmetic over digests.

| | |
|---|---|
| **Network** | Studionet, chain **61999** |
| **Contract** | [`0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87`](https://explorer-studio.genlayer.com/address/0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87) |
| **Deploy tx** | [`0xd8c03ca8…4d4ec`](https://explorer-studio.genlayer.com/tx/0xd8c03ca8f077182a104c667e162daa8fef7260519bae2885be11d713eee4d4ec), FINALIZED, `MAJORITY_AGREE` |
| **Reads a** | Base Sepolia, `evm:` subjects, via a public JSON-RPC endpoint (no API key, $0) |
| **Tests** | 61 hermetic, no network |
| **Cost** | $0 |

---

## The problem

Every GenLayer contract that fetches web or chain data has the same latent bug. The leader and
each validator fetch **independently**. Put a volatile field into the evidence that crosses the
consensus boundary and two *honest* nodes compute different evidence for the *same* subject,
disagree, and burn the round. Nobody has to be malicious. This is the single most common way an
intelligent contract is built wrong.

The mitigation is known and unglamorous: only fields **fixed for a given subject forever** may
cross the boundary. Block number, gas, timestamps and raw bodies must be quantised away or
dropped. Band them (`value=ZERO`, `logs=MANY`), keep the band, discard the measurement.

**Nobody packages this.** Cordon, a sibling project, does the reduction and then:

```python
def digest_signature(digest: dict) -> str:
    """Short stable fingerprint of the digest, stored for audit. Never compared."""
```

It computes the fingerprint, stores it, and never checks it. **Concord is the missing
comparison**, extracted into a primitive other contracts can depend on.

---

## What it does

```
observe(subject)                          leader fetches + reduces subject to a canonical digest
                                        -> validators independently fetch + reduce the SAME subject
                                        -> each submits the signature it computed
                                        -> admitted only when signatures agree unanimously
                                        -> any disagreement aborts the round: nothing is admitted

report_equivocation(subject, label, what) a separate, committee-gated transaction that records
                                        a divergent signature under a reporter-supplied label
```

Key methods: `observe`, `get_observation`, `get_equivocations`, `equivocation_count`,
`was_admitted`, `admit_count`, `quarantine_threshold`, `is_quarantined`, `list_subjects`,
`stats`.

### State design

| Store | Purpose |
|---|---|
| `observations[subject]` | Admitted digest, its signature, admit timestamp and submitter |
| `equivocations[subject/validator]` | Divergent signature, count, first/last seen, reporter, artifact fingerprint |
| `admit_count[subject]` | How many independent fetches agreed on the digest |
| `equivocation_count[subject]` | Total attributed equivocations for the subject |
| `quarantined[address]` | Set once an address reaches `quarantine_threshold` |

Subjects are `evm:<0x tx hash>`, normalised to lowercase. Only EVM transactions are accepted;
see *Limitations*.

---

## Why consensus is used here

Consensus is doing exactly one job: establishing that **the leader did not show the validators
something they did not also see**.

That is not a judgement call a model should make, which is why there is no model in this path.
`gl.nondet.exec_prompt` is never called. What crosses the boundary is a flat `dict[str, str]` and
the validator's entire vote is:

```python
return str(theirs.get("signature", "")) == str(mine.get("signature", ""))
```

One string equality over `sha256` of a canonical digest. It can be read, checked by eye, and
argued with. That is the point: a correctness argument a reviewer can actually check is worth more
here than a decision no reviewer can audit.

The full argument, written to be disputed, is in **[`docs/CONSENSUS.md`](docs/CONSENSUS.md)**.

---

## The stable-field rule

**Dropped** because two fetches seconds apart can differ: `blockNumber`, `blockHash`,
`transactionIndex`, `confirmations`, every timestamp, `cumulativeGasUsed`, `effectiveGasPrice`,
`gasPrice`, `maxFeePerGas`, `nonce`, `v`/`r`/`s`, `logsBloom`, raw log `data`, the raw body.

`confirmations` and `logsBloom` are worth calling out: `logsBloom` is a rolling hash over the whole
block, so it changes for every transaction in the block including unrelated ones.

**Banded.** Keep the bucket, discard the measurement:

| Quantity | Bands |
|---|---|
| `value` | `ZERO` `SMALL` `MEDIUM` `LARGE` `ABOVE` `UNKNOWN` |
| `gasUsed` | `NONE` `LOW` `MID` `HIGH` `ABOVE` `UNKNOWN` |
| log count | `NONE` `FEW` `SOME` `MANY` `ABOVE` `UNKNOWN` |

**Kept**, fixed for a transaction forever: whether the receipt exists, receipt `status`, the `to`
address, the calldata selector, the sorted set of log topics, and which topics repeat.

Exact `gasUsed` *is* immutable for a mined transaction and is banded anyway. Deliberate
weakening, argued in `docs/CONSENSUS.md` §3.

The digest is then signed: `signature = sha256(canonical)`, **untruncated**. A truncated digest is
a collision invitation on the one field the primitive exists to protect.

---

## Worked example

A real, long-settled Base Sepolia transaction: an ERC-20 transfer at block 46400000.

```
subject : evm:0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a

digest  : concord/evm-tx-digest@1|1|1|0x5f9215dff5c01671e6e77469389d694ac4af2e97|0xac9650d8|
          0x468a25a7...,0xddf252ad...|NONE|ZERO|HIGH|FEW
signature: 66ae60fa7a112ae0ca54f025f40c57dc346528aed674746c3133d1d68e01e54d
```

Read it: found, succeeded, called `0x5f92…2e97` via selector `0xac9650d8`, emitted two event
topics (neither repeated), value band `ZERO`, gas band `HIGH`, two logs → `FEW`.

Notice what is **not** there: block 46400000, `gasUsed` 703310, any timestamp, any gas price.
`gasUsed` 703310 is what put `HIGH` there. The number is gone, the band remains. That is the
whole design in one line.

Now the divergence. `scripts/observe.py --report-harness` submits an artifact with
`receipt.status` flipped `0x1 → 0x0`. It reduces to a different digest, and the committee records
it:

```
validator : 0x000000000000000000000000000000000000dead   (a harness label, see below)
divergent : 173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2
recorded  : report_equivocation → equivocations[subject/validator].count = 1
```

Anyone can re-derive that digest off-chain. Regenerate the artifact and check it in one step:

```bash
.venv-deploy/bin/python scripts/observe.py --address 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87 --report-harness
.venv-deploy/bin/python scripts/verify_artifact.py \
  --recorded 173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2
# -> OK  the recorded signature is reproducible from the stored artifact
```

The artifact is **not** committed. A reviewer should reproduce it, not receive it. `verify_artifact.py`
reads whatever `observe.py` last wrote, so the two must be run in that order.

> **The on-chain equivocation is a test harness, not a real adversarial event.** It was produced
> by deliberately corrupting a fetch. No validator misbehaved. See *Limitations*: a real one
> cannot currently be produced at all.

---

## Verify it yourself

One read-only command. No key, no signing, prints expected-vs-actual, exits non-zero on any
mismatch.

```bash
.venv-deploy/bin/python scripts/verify_live.py --address 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87
```

```
OK  19/19 checks passed on 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87
```

It does not trust the contract. It fetches the same public receipt and recomputes the expected
digest with **its own independent implementation** of the rule, then compares. Sharing code with
the contract would make agreement a tautology.

The full hermetic suite:

```bash
.venv/bin/python -m pytest tests/direct -q     # 61 passed
.venv/bin/genvm-lint check contracts/concord.py
```

---

## Limitations

Stated plainly. The first three are protocol limits, not oversights.

1. **Concord cannot prove which validator equivocated.** It can prove *that* a signature
   diverges, unanimously, but not *who* produced it. Two independent reasons:
   `run_nondet_unsafe` terminates the VM when a validator votes `False`, rolling back every
   write including any record; and GenVM consensus requires byte-identical state from all
   validators, so per-validator values cannot be written at all. GenVM also exposes no validator
   address to contract code. So `attributed_to` is the **reporter's label**, and only the
   *content* is committee-attested. The contract's docstring says this too.
   `docs/CONSENSUS.md` §6.4 has the argument.

2. **Quarantine is recorded, never enforced.** Concord cannot influence which nodes GenLayer
   selects as validators. `quarantined[address]` is a signal for whoever does.

3. **`found` is racy for a not-yet-mined transaction.** A leader that fetches before mining and
   a validator that fetches after will disagree, with nobody misbehaving. The demo subject is
   deliberately old. `docs/CONSENSUS.md` §4.

4. **Bands are coarse.** 40 logs and 200 logs both read `MANY`. Costs resolution, never safety.

5. **EVM transactions only.** A general web-resource subject would need its own stability argument
   per URL. Resolved to the narrower option deliberately.

6. **One public endpoint, one chain.** `evm_rpc_url` is fixed at deploy time and reported by
   `stats()`. If that endpoint is down, observations degrade to the `HTTP_…` digest. That is
   consistent across nodes, so they still agree, but the observation is not about the subject.

7. **Not used in production.** It is not. No protocol depends on it.

8. **The harness simulates one GenVM rule.** `gltest` runs the leader inline, captures the
   validator, and does not roll state back on a `False` vote. `conftest.run_round` replays the
   documented rule on top of it. Labelled as a simulation in both files.

9. **No live adversarial equivocation exists.** Unverified and currently unproducible, per (1).
   The one on chain was manufactured and labelled. **Not** presented as real.

---

## What did not go to plan

Full record with error text in [`state/LEFT-OFF.md`](state/LEFT-OFF.md). The four that mattered:

- **A defect found on chain, not in review.** The first deployed version truncated the
  equivocation artifact at 6000 characters. A real Base Sepolia receipt is ~14.7 KB, so the
  truncated prefix did not parse, reduced to the `UNPARSEABLE` not-found digest, and the chain
  recorded *that* as the "divergent signature", blaming a validator for the reporter's upload
  size. The signature was reproducible from the artifact, so no check caught it; only comparing
  it against the digest the corrupted field *should* produce did. Truncation is now an outright
  rejection, with a regression test.

- **A `UnicodeDecodeError` that was really a request-encoding bug.** Hand-rolling `gen_call`
  calldata with `{"method": …, "args": [], "kwargs": {}}` looks right and is wrong:
  `make_calldata_object` *omits* empty `args`/`kwargs`. The node accepts the malformed calldata
  and returns a result the decoder cannot read, surfacing as
  `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc4 in position 35`, which reads as corrupt
  chain data. Cost several wrong hypotheses before the payloads were diffed byte for byte.

- **Two contract bugs the tests caught, not review.** `set_quarantine_limit` was documented
  owner-only and had no owner check. Found by a test asserting a non-owner is rejected, which
  failed to raise. And the calldata selector was sliced `raw_input[2:10]`, dropping the `0x` and
  returning six hex characters instead of eight.

- **`genlayer-py` did *not* have the reported read defect.** A sibling project recorded that 0.18.0
  reports `Contract 0x... not found` for a live Studionet contract that `genlayer-js` reads. That
  did not reproduce here: `gen_call` decodes correctly on 0.18.0 for this address. Recorded as
  unreproduced, not fixed.

---

## Layout

```
contracts/concord.py          the contract. Reduction, signature, gate, equivocation ledger
tests/direct/                 61 hermetic tests, no network
  conftest.py                 run_round(): replays GenVM's termination rule on the harness
  fixtures.py                 recorded fetches; three volatile views of one transaction
docs/CONSENSUS.md             the equivalence argument, written to be disputed
scripts/deploy.py             deploy to Studionet 61999
scripts/observe.py            drive observe / report_equivocation on chain
scripts/verify_live.py        read-only expected-vs-actual. Never prints a key or endpoint
scripts/verify_artifact.py    re-derive a recorded equivocation off-chain
state/LEFT-OFF.md             what did not work, with error text
```

Two virtualenvs, deliberately. `.venv` (`genlayer-test` 0.28.0, which pins `genlayer-py` 0.9.0)
runs the tests. `.venv-deploy` (`genlayer-py` 0.18.0) deploys and verifies. They are not
interchangeable.

`.env` is gitignored and has never been committed. No environment value, including public
endpoints, appears anywhere in this repository, in any script's output, or in any error message;
`verify_live.py` sanitises the endpoint out of every exception it catches, because
`genlayer-py`'s provider embeds it in its own error text.