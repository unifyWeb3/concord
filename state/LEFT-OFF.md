# Left off: next action

Updated 2026-10-03. Project: **Concord**, a GenLayer Intelligent Contract primitive.

## Status: deployed on Studionet 61999, verified live, not submitted.

| | |
|---|---|
| Contract | `0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87` (EIP-55) |
| Explorer | <https://explorer-studio.genlayer.com/address/0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87> |
| Deploy tx | `0xd8c03ca8...4d4ec`, FINALIZED, `MAJORITY_AGREE` |
| Admitted observation tx | `0xb0a91762...8172c`, FINALIZED |
| Equivocation record tx | `0x03b29a6d...e97527`, FINALIZED |
Second record tx | `0x2274320c...a6581`, FINALIZED |
| Admitted signature | `66ae60fa7a112ae0ca54f025f40c57dc346528aed674746c3133d1d68e01e54d` |
| Divergent signature | `173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2` |

An earlier deployment, `0x5d115AF0a0E18Bc2CACBC333872Ad5530b799786` (deploy tx
`0x7b82529da4ccbb5e3f95b26d830a1d188df1c29b44c1ae7a3085d74382f1f8b7`), carries a **known-defective**
equivocation record, see "Defect found on chain" below. It is superseded and should be treated as
a counter-example, not as a demo.

## Checks, all re-runnable

```bash
.venv/bin/python -m pytest tests/direct -q                    # 61 passed, hermetic, no network
.venv/bin/genvm-lint check contracts/concord.py              # Lint passed (3 checks)
.venv/bin/python scripts/secret_scan.py                      # PASS, working tree + full history

# read-only, expected-vs-actual, exits non-zero on mismatch
.venv-deploy/bin/python scripts/verify_live.py --address 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87
#   -> OK  19/19 checks passed

# re-derive the recorded equivocation off-chain
.venv-deploy/bin/python scripts/verify_artifact.py \
  --recorded 173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2
#   -> OK  the recorded signature is reproducible from the stored artifact
```

Two venvs, deliberately: `.venv` (`genlayer-test` 0.28.0, which pins `genlayer-py` 0.9.0) for
tests, `.venv-deploy` (`genlayer-py` 0.18.0) for deploy and verify. Not interchangeable.

## Hard claims that must not drift

1. **The committee attests that a signature diverges. It cannot attest which node produced it.**
   GenVM terminates the transaction in which a validator votes `False`, so the record is rolled
   back; and consensus requires byte-identical state, so per-validator values cannot be written
   at all. `attributed_to` is the reporter's label. Any copy implying Concord identifies a
   misbehaving validator is false. See `docs/CONSENSUS.md` §6.4.
2. **The on-chain equivocation is a labelled test harness**, from a deliberately corrupted fetch.
   Not evidence that any validator misbehaved. It is labelled in `scripts/observe.py`, the
   artifact filename, `verify_live.py` output, the README and this file.
3. **No live adversarial equivocation exists, and none can currently be produced.** Unverified by
   necessity, per (1).
4. **Quarantine is recorded, never enforced.** Concord cannot influence validator selection.
5. **Not used in production.** It is not.
6. **Never print, log or commit any `.env` value, public endpoints included.** Absolute.
   `verify_live.py` sanitises the endpoint out of every exception it catches, because
   `genlayer-py`'s provider embeds it in its own error text, confirmed below.

## Do not

- Do not use `GENLAYER_EXPLORER_URL`. It points at Bradbury, a different chain, and renders an empty page with
  HTTP 200. Studionet is `explorer-studio.genlayer.com`, path `/address/<addr>`, hardcoded in
  `scripts/deploy.py`.
- Do not deploy to studio-dev (61997). Fee-charging, and every deploy returns
  `FINISHED_WITH_ERROR` with `num_of_rounds=0`.
- Do not let `genvm-lint` and `gltest` fight over `~/.cache/gltest-direct`. `SDK_VERSION =
  "v0.3.0-rc7"` in `tests/direct/conftest.py` exists to stop this.
- Do not claim the harness enforces the GenVM termination rule. It does not; `run_round()`
  simulates it.
- Do not widen subjects beyond EVM transactions without a per-source stability argument.

## What did not work, with the actual error text

### 1. Defect found on chain, not in review: artifact truncation

The first deployed version truncated the equivocation artifact at `MAX_ARTIFACT_CHARS = 6000`. A
real Base Sepolia receipt is ~14.7 KB (`artifacts/harness_equivocation_artifact.json`, 14691 bytes).
The truncated prefix did not parse:

```
json.decoder.JSONDecodeError: Unterminated string starting at: line 1 column 1128 (char 1127)
```

so it reduced to the `UNPARSEABLE` not-found digest, and **the chain recorded that as the
"divergent signature"**, attributed to a validator, when what actually happened was that the
reporter's upload was too big:

```
on-chain : 465c1668356547225bcc6ed7c0f43963ba6f6fcafac9a5a079d143fbb090b00c
canonical: concord/evm-tx-digest@1|0|UNPARSEABLE|NONE|NONE|NONE|NONE|UNKNOWN|UNKNOWN|UNKNOWN
```

**Why no check caught it:** the recorded signature *was* reproducible from the artifact under the
same rule, so it passed its own consistency test. It was caught only by comparing the recorded
digest against the digest the *corrupted field* should produce, and noticing that the difference
was `0` vs `UNPARSEABLE` rather than `1` vs `0`. A self-consistent record of the wrong thing.

Fixed: limit raised to 60000, and over-long artifacts are now **rejected outright** rather than
truncated. Truncation converts a size limit into a claim about someone else's honesty.
Regression test: `test_an_oversized_artifact_is_rejected_not_truncated`. Redeployed; the new
deployment records `173664f4…`, the digest of the genuinely corrupted `status`.

### 2. `UnicodeDecodeError` that was really a request-encoding bug

Hand-rolled `gen_call` calldata as
`calldata.encode({"method": m, "args": [], "kwargs": {}})`. Wrong: `make_calldata_object`
**omits** empty `args`/`kwargs`. The node accepted the malformed calldata and returned a result
the decoder could not read:

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc4 in position 35: invalid continuation byte
```

Reads as corrupt chain data; is a request-encoding mistake. Cost several wrong hypotheses
(`transaction_hash_variant` spelling, `from` field, JSON-RPC `id`) before diffing the two payloads
byte for byte:

```
SDK make_calldata_object : 0xd08e0e066d6574686f642c737461747300
hand-built dict          : 0xde9c1e046172677305066b776172677306066d6574686f642c737461747300
```

Fixed: use `make_calldata_object`. The verifier now uses genlayer-py's transport and sanitises the
endpoint out of errors rather than hand-rolling HTTP at all.

### 3. `genlayer-py` client construction

Three errors in sequence, all from `scripts/deploy.py`:

```
TypeError: GenLayerClient.__init__() got an unexpected keyword argument 'chain'
```

0.18.0's signature is `GenLayerClient(chain_config, account=None)`. There is no `chain=`.

```
AttributeError: 'NoneType' object has no attribute 'address'
```

Constructing the client without an account does **not** fail loudly. It fails later, inside
`_encode_add_transaction_data`, as an error about a signer wearing a transaction-encoding
costume. The account must be passed to the constructor.

```
TypeError: string indices must be integers, not 'str'
```

`write_contract` returns the GenLayer tx id as a bare string, not a dict. Indexing it fails.

### 4. Contract bugs the tests caught, not review

- `set_quarantine_limit` was documented owner-only and **had no owner check**. The test asserting a
  non-owner is rejected failed with `Failed: DID NOT RAISE Exception`. A live authorization hole.
- The calldata selector was sliced `raw_input[2:10]`, which drops the `0x` and yields six hex
  characters instead of eight:

```
- 0xac9650d8      (expected)
+ ac9650d8        (actual)
```

Found by `test_stable_fields_survive_the_reduction`. Fixed to `raw_input[:10]`.

- Band-edge expectations in the tests were wrong, not the contract: `gasUsed` 703310 bands `HIGH`
  (not `MID`), and 33 logs bands `ABOVE` (not `MANY`). Tests corrected; worth recording because the
  tests were asserting a different table than the one in the contract, which is the failure mode
  you would otherwise "fix" by editing working code.

### 5. Intermittent network, not a contract fault

Reads failed repeatedly with DNS/connectivity errors that later succeeded unchanged:

```
requests.exceptions.ConnectionError: HTTPSConnectionPool(host=...): Max retries exceeded with url: /api
(Caused by NameResolutionError(... [Errno -3] Temporary failure in name resolution))
(Caused by NewConnectionError(... [Errno 101] Network is unreachable))
```

Cause: the network path from this host to the GenLayer node. `verify_live.py` retries with backoff
and reports it as a transport failure, never as a contract failure. Same class of defect as
A sibling project's recurring "a call that reports a result contradicting reality".

### 6. A second, independent equivocation record on chain

Two records now exist for the demo subject, from two separately corrupted artifacts, attributed
to two different labels:

| tx | attributed to | corrupted field | recorded signature |
|---|---|---|---|
| `0x03b29a6d…e97527` | `0x…dead` | `receipt.status` `0x1`→`0x0` | `173664f45f52567485456cd2eea29e6b4a644ccf04e4aca4d7be9ad3bbc3bac2` |
| `0x2274320c…706581` | `0x…badc` | dropped one log entry (2→1) | `c158a5dc505b87c84b7cfacc19ab88b65b5fcdd6f9ef0ce58b217060684ae079` |

`equivocation_count` is 2 and `get_equivocations` returns one row per address, confirming
per-address attribution rather than a single running total.

**Both are manufactured.** Same rule as the first: deliberately corrupted fetch, labelled, not
evidence of a byzantine node. Run to prove attribution is per-address and that a second, different
divergence is recorded independently, not to claim a real adversarial event.

### 7. `genlayer-py` did **not** have the reported live-read defect

A sibling project records genlayer-py 0.18.0 reporting `Contract 0x... not found` for a live
Studionet contract that `genlayer-js` reads, reproduced twice there. That did **not** reproduce
here: `gen_call` against `0x7c01a7c3…` decodes correctly on 0.18.0. Recorded as **unreproduced**,
not fixed. Cross-check with a second client before believing either report.

## Next step

1. **Submit.** Not done; no authenticated portal session has been available here, so the form's
   required fields are unverified. Read the form before writing the notes. Likely rubric lines:
   the primitive's reusability, the equivalence argument in `docs/CONSENSUS.md`, and the
   honesty of the limitations section.
2. **If a second live observation is wanted**, `scripts/observe.py` re-runs against the same
   address; a new subject needs a settled Base Sepolia transaction hash.
3. **Not worth doing, recorded so nobody attempts it:** producing a genuine live equivocation. It
   requires a byzantine node, and per claim (1) the resulting transaction could not record
   anything anyway.

## Open questions from the build spec, as resolved

All three resolved to the narrower/safer option, with the reasoning in the README and
`docs/CONSENSUS.md`:

| Question | Resolved | Why |
|---|---|---|
| `observe` permissionless or owner-gated? | **Permissionless** | A gated primitive is not a primitive. `set_quarantine_limit` is the only owner-gated method. |
| Quarantine or record only? | **Both** | Recorded, counted, and flagged at a threshold. Enforcement is out of scope, since Concord cannot influence validator selection. |
| EVM transaction only, or general web? | **EVM only** | A web subject needs its own stability argument per URL. Narrower is finishable and arguable. |

Additional assumption not in the brief: **the chain is fixed by `evm_rpc_url` at deploy time, not
by the subject string.** A subject cannot claim to be on a chain this contract is not reading.
`stats()` reports both.