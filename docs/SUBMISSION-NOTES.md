# Submission notes: Concord

Paste material for the Intelligent Contracts form. Trim to taste, but keep the honesty section, and
keep the explorer warning at the top. A reviewer will open the explorer before reading anything else.

Repo: <https://github.com/unifyWeb3/concord>
Contract: <https://explorer-studio.genlayer.com/address/0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87>
Deploy tx: <https://explorer-studio.genlayer.com/tx/0xd8c03ca8f077182a104c667e162daa8fef7260519bae2885be11d713eee4d4ec>

Studionet, chain 61999. FINALIZED, `MAJORITY_AGREE`.

---

## READ THIS FIRST: the explorer shows two equivocation records

The contract's transaction list contains **two equivocation records**. They are **not** evidence that
any validator misbehaved. Nothing adversarial happened.

They were produced by `scripts/observe.py --report-harness`, which submits an artifact with one
field deliberately corrupted, so the divergence path could be exercised on a network nobody
controls. The labels `0x...dead` and `0x...badc` are harness sentinels, not addresses.

- `scripts/verify_live.py` prints this warning in its own output.
- `scripts/verify_artifact.py` re-derives each recorded signature off-chain from the stored artifact,
  so a reviewer can confirm each record matches its artifact instead of taking it on trust.

**A real adversarial equivocation cannot currently be produced at all.** The reason is a protocol
limit, not an omission, and it is the most important thing to understand about this submission:

GenVM exposes no validator address to contract code (`gl.message` carries only sender, origin,
contract, value and chain id). Separately, `run_nondet_unsafe` terminates the VM when a validator
votes `False`, rolling back every write including any record, and consensus requires byte-identical
state across validators, so per-validator values cannot be written at all.

So the committee can attest, unanimously, **that** a signature diverges and **what** the divergent
content was. It cannot attest **who** produced it. `attributed_to` is the reporter's label.
**Any claim that Concord identifies a misbehaving validator node is false**, and this repository
says so in the contract docstring, `docs/CONSENSUS.md` section 6.4, the README limitations, and the
verifier's output.

There is also an **earlier deployment**, `0x5d115AF0a0E18Bc2CACBC333872Ad5530b799786`, carrying a
record produced by a since-fixed artifact-truncation defect. It is superseded. Treat it as a
documented counter-example, not a second result. `README.md` (*What did not go to plan*) and
`state/LEFT-OFF.md` carry the full record with error text.

---

## What it does

Concord is a GenLayer Intelligent Contract primitive that makes equivocation a committee-attested,
on-chain event, and refuses to admit any observation the validator committee did not independently
agree on.

```
observe(subject)
  -> leader fetches the subject and reduces it to a canonical digest
  -> validators independently fetch and reduce the SAME subject
  -> each votes on one string equality over sha256 of that digest
  -> admitted only when signatures agree unanimously
  -> any disagreement aborts the round; nothing is admitted

report_equivocation(subject, label, artifact)
  -> a separate, committee-gated transaction that records a divergent signature
```

The problem it addresses: in any GenLayer contract that fetches web or chain data, the leader and
each validator fetch **independently**. Put a volatile field into the evidence that crosses the
consensus boundary and two *honest* nodes compute different evidence for the *same* subject,
disagree, and burn the round. No misbehaviour is required. This is the most common way an
intelligent contract is built wrong.

The mitigation is unglamorous and almost never packaged: only fields **fixed for a given subject
forever** may cross the boundary. Block number, gas, timestamps, `logsBloom`, raw bodies and raw log
payloads are dropped or quantised into bands. Concord implements that reduction, and then does the
thing its sibling project does not: it **compares** the signatures.

## How consensus is used

Consensus does exactly one job here: establishing that the leader did not show the validators
something they did not also see.

That is not a judgement call a model should make, so **there is no model in the correctness path**.
`gl.nondet.exec_prompt` is never called. What crosses the boundary is a flat `dict[str, str]`, and a
validator's entire vote is:

```python
return str(theirs.get("signature", "")) == str(mine.get("signature", ""))
```

One string equality over `sha256` of a canonical digest. It can be read, checked by eye, and argued
with. The full argument is written to be disputed in `docs/CONSENSUS.md`.

## How to use it

Read-only verification, one command, no key, no signing. It recomputes the expected digest with its
own independent implementation of the rule rather than sharing code with the contract, so agreement
is not a tautology:

```bash
python scripts/verify_live.py --address 0x7c01a7c38c04f4BE6f66a967a5EaB26f73c48F87
# OK  19/19 checks passed
```

Hermetic suite, no network:

```bash
python -m pytest tests/direct -q          # 61 passed
```

## Honest limitations

1. **Concord cannot prove which validator equivocated.** Protocol limit, argued above. What is
   attested is the content, not the author.
2. **Quarantine is recorded, never enforced.** Concord cannot influence which nodes GenLayer selects
   as validators. The flag is a signal for whoever does.
3. **`found` is racy for a not-yet-mined transaction.** A leader fetching before mining and a
   validator fetching after will disagree with nobody misbehaving. The demo subject is deliberately
   old.
4. **Bands are coarse.** 40 logs and 200 logs both read `MANY`. Costs resolution, never safety.
5. **EVM transactions only.** A general web-resource subject would need its own stability argument
   per URL. Resolved to the narrower option deliberately.
6. **One public endpoint, one chain,** fixed at deploy time. If it is down, observations degrade to
   a consistent error digest: nodes still agree, but the observation is not about the subject.
7. **Not used in production.** No protocol depends on it.
8. **No live adversarial equivocation exists,** and none can currently be produced. The records on
   chain were manufactured and labelled as such everywhere they appear.

## What I would like feedback on

- **Is the band set right?** It is deliberately coarse. I chose safety over resolution; a reviewer
  who thinks resolution matters more would want to know which bands are too tight.
- **Is per-validator attribution genuinely impossible,** or is there a GenVM surface I missed? I
  found none, and would rather be corrected here than ship a string that looks like attribution and
  is not.
- **Is committee-gated reporting the right substitute?** Anyone can call it, but the record is only
  admitted if the artifact genuinely reduces to the claimed non-canonical digest. Is that sufficient,
  or should it be narrower?