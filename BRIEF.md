# Concord — build brief

**Category:** Intelligent Contracts (standalone primitive). **One submission slot. This is the whole week's capacity.**
**Working directory:** `/home/unify/concord` — do all work here, not in `mys`.
**Status:** nothing built yet. This file is the spec.

---

## 1. What to build

**Concord** is a GenLayer Intelligent Contract primitive that makes **equivocation a first-class,
on-chain, attributable event** — and refuses to admit an observation that the validator committee
did not independently agree on.

### The problem it solves

Every GenLayer contract that fetches web or chain data has the same latent bug. The leader and each
validator fetch **independently**. If the contract puts a volatile field into the evidence that
crosses the consensus boundary, two honest nodes can compute different evidence for the *same*
subject, disagree, and burn the round. This is not hypothetical — it is the single most common way
an intelligent contract is built wrong.

The mitigation is known and unglamorous: only fields that are **fixed for a given subject forever**
may cross the boundary. Block number, gas, timestamps, raw bodies and raw log payloads must be
quantised away or dropped. Band them (`value=ZERO`, `logs=MANY`), keep the band, discard the
measurement.

**Nobody packages this.** Cordon — a separate, already-submitted project in `../mys` — implements the
reduction but then does the thing this contract exists to prevent:

```python
def digest_signature(digest: dict) -> str:
    """Short stable fingerprint of the digest, stored for audit. Never compared."""
```

It computes the fingerprint, stores it, and **never compares it**. Concord is the missing
comparison, extracted into a primitive that other contracts can depend on.

### The contract, concretely

```
observe(subject)  ->  leader fetches + reduces subject to a canonical digest
                  ->  validators independently fetch + reduce the SAME subject
                  ->  each submits the signature it computed
                  ->  contract admits the observation only when signatures agree unanimously
                  ->  any mismatch is recorded on-chain as an equivocation event,
                     attributed to the specific validator address that diverged
```

State design:

| Store | Purpose |
|---|---|
| `observations[subject]` | admitted digest + signature + admit timestamp |
| `equivocations[subject]` | per-validator divergent signature, counted and attributed |
| `admit_count[subject]` | independent fetches that agreed |

Key methods: `observe`, `get_observation`, `get_equivocations`, `equivocation_count`,
`was_admitted`, `quarantine_threshold`.

### Why this is worth a reviewer's time

Any builder writing a contract that reads a transaction, an API, or a webpage needs this and almost
none will get it right. It is educational, reusable, and it has a correctness argument a reviewer
can check. It is **not** a demo: there is no frontend, no product surface, no scenario narrative.

---

## 2. How this must beat the previous submission

A prior submission, **Knot** (repo `unifyWeb3/knot`), scored **100/500**. It was substantial work, so
the gap is worth understanding. **These are hypotheses, not known rubric findings** — treat them as
design constraints to beat, not as stated reasons.

| Knot | Concord's structural advantage |
|---|---|
| Targeted **studio-dev (chain 61997)** | Deploy to **Studionet (61999)** — the network carrying real traffic, independently checkable by any reviewer |
| Core semantics decided by LLM judgment ("did the participant satisfy this natural-language postcondition?") | **No LLM in the core path.** Agreement is arithmetic over digests. Removes the "generic AI decides X" disqualification entirely |
| Evidence `externally_corroborated = false` | Every admitted observation is reproducible from chain state by a third party |
| Reproduction needed custom scripts and a signing key | A reviewer runs **one read-only command** and sees expected-vs-actual |
| Abstract workflow/saga use case | Concrete, self-evident hazard every builder already half-knows |

**The single most important decision: keep the LLM out of the correctness path.** Concord may fetch
web data, but what it *decides* must be deterministic. That is what separates a primitive from a demo.

---

## 3. Constraints — these are verified, do not re-litigate

- **Deploy to Studionet, chain 61999.** RPC key `GENLAYER_STUDIO_RPC`.
  **studio-dev (61997) is unusable**: fee-charging with no published Python SDK speaking its ABI, and
  every deploy returns `FINISHED_WITH_ERROR` with `num_of_rounds=0` — including a 20-line probe.
- **Explorer is `explorer-studio.genlayer.com`, path `/address/<addr>`.**
  Do **NOT** use `GENLAYER_EXPLORER_URL` — it points at **Bradbury, chain 4221**, and renders an
  empty page while returning HTTP 200. Verified wrong.
- **Addresses are case-sensitive to `gen_call`.** A mis-cased EIP-55 address answers
  `Contract not found` for a contract that demonstrably exists. Use checksummed addresses.
- **`genlayer-js` must be pinned to 1.1.8** if any JS client is used. 2.0.0-rc.1 encodes the method
  under an empty-string key and silently breaks every read.
- **Never print, log, or commit any `.env` value.** Report key names only. This is absolute and
  includes public endpoints. `.env` is already present here and must stay untracked.
- **No frontend, no dashboard, no product narrative.** This category rewards primitives.
- **Budget is $0.** No paid APIs or API keys. Concord's design must not require one.

### Landmines inherited from the previous build

Full detail in `../mys/AGENTS.md` and `../mys/state/MEMORY.md` — read them before writing contract
code. The ones that will bite:

- `@gl.evm.contract_interface` methods need **positional-only** params (`/`). Omitting it fails at
  import with a bare `AssertionError`.
- `gl.nondet.web` response field is `.status`, **not** `.status_code`.
- Contract classes must inherit `gl.Contract`; exactly one subclass per module.
- Storage containers cannot be constructed in `__init__` — annotate, and let generated storage
  initialise them.
- `.view()` / `EthCall` is broken in the test SDK.
- `.emit()` **silently no-ops** — it reports success and delivers nothing. Verified under GLSim and
  then on real GenVM. Do not build any path that depends on it.
- Never use wall-clock time. Use `gl.message_raw["datetime"]`.
- `genvm-linter` false-positives E010 on `gl.nondet` calls in nested helper functions. Inline the
  call in the leader function.
- Pin `sdk_version` on every `direct_deploy`, or `genvm-lint` breaks the suite.

---

## 4. Definition of done

Do not stop until every one of these is true and verified. Report evidence, not assertion.

- [ ] `git init` with `.env` gitignored, working from a clean commit
- [ ] Contract implemented in `contracts/`, inheriting `gl.Contract`
- [ ] `genvm-lint check` passes
- [ ] Hermetic test suite in `tests/direct/` — no network — **all passing**
- [ ] A test asserting **byte-identical digest across differing volatile inputs** (block, gas,
      timestamps) — this is the contract's central claim and must be proven, not asserted in prose
- [ ] A test asserting that a **divergent validator signature is recorded as an equivocation** and
      **blocks admission**
- [ ] Deployed to Studionet 61999, with the deploy tx hash and contract address in EIP-55
- [ ] `read_contract` evidence captured showing at least one **admitted** observation and its digest
- [ ] An equivocation path exercised on-chain or in a direct test with a real receipt
- [ ] `README.md`: purpose, why consensus is used, the stable-field rule and why, state design,
      worked example, and honest limitations
- [ ] `docs/CONSENSUS.md`: the equivalence argument, stated so a reviewer can argue with it
- [ ] `scripts/verify_live.py` — read-only, prints expected-vs-actual, exits non-zero on mismatch,
      **never prints the key**
- [ ] Secret scan across the tree and full git history: clean
- [ ] `state/LEFT-OFF.md` written so a human can resume cold

### Honest-reporting rules

These matter more than the score.

- **If a live equivocation cannot be produced, say so and do not manufacture one.** Provoking a
  mismatch by deliberately corrupting a fetch is acceptable *only* if labelled as a test harness,
  never presented as a real adversarial event.
- **Do not claim the primitive is used in production.** It will not be.
- **If a claim cannot be verified on-chain, mark it unverified in the README** rather than implying
  otherwise.
- Record what did not work, with the error text. The previous project's most valued section was
  "what did not go to plan".

---

## 5. Suggested order of work

1. Read `../mys/AGENTS.md` and `../mys/state/MEMORY.md`. Do not skip this.
2. `git init`, `.gitignore` including `.env`, first commit.
3. Set up the test venv exactly as `../mys` does it, and get one trivial test passing **before**
   writing contract logic.
4. Write the reduction (volatile-field stripping + banding) as a pure function with direct tests.
   This is the highest-risk piece; do it first and in isolation.
5. Add the observation/equivocation state machine.
6. Only then deploy. Deploy early and deploy often — a contract that never reached Studionet scores
   near zero, and a deploy is cheap.
7. Write the verifier, then the docs last.

---

## 6. Open questions for the human

Flag these rather than guessing silently:

- Should `observe` be permissionless, or gated by an owner? Permissionless is the stronger primitive
  claim; owner-gated is easier to demo.
- Should a validator be *quarantined* (excluded from future selection) after N equivocations, or
  only recorded? Quarantine is a stronger claim but reaches further into protocol semantics.
- Fetch target: EVM transaction only (reuses `../mys` knowledge), or a general web resource?
  EVM-only is narrower and safer to finish.