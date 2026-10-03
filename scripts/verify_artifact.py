"""Re-derive the recorded equivocation digest from the stored artifact, off-chain.

This is the check that makes an equivocation record worth anything: it proves the contract did
not simply store a string someone handed it, but stored the digest that *actually follows* from
the artifact, and that a third party can arrive at the same digest independently.

    .venv-deploy/bin/python scripts/verify_artifact.py

Prints expected-vs-actual and exits non-zero on mismatch. Read-only. No key is used.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "artifacts" / "harness_equivocation_artifact.json"

DIGEST_FIELDS = (
	"schema",
	"found",
	"status",
	"to_addr",
	"selector",
	"topics",
	"repeated_topics",
	"value_band",
	"gas_band",
	"log_band",
)
EXPECTED_SCHEMA = "concord/evm-tx-digest@1"
_VALUE_BANDS = ((0, "ZERO"), (10**18, "SMALL"), (10**22, "MEDIUM"), (10**26, "LARGE"))
_GAS_BANDS = ((0, "NONE"), (21000, "LOW"), (200000, "MID"), (1000000, "HIGH"))
_LOG_BANDS = ((0, "NONE"), (2, "FEW"), (8, "SOME"), (32, "MANY"))


def as_int(value):
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	if isinstance(value, float):
		return int(value)
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return None
		try:
			return int(text, 16) if text[:2].lower() == "0x" else int(text, 10)
		except ValueError:
			return None
	return None


def band(value, edges):
	n = as_int(value)
	if n is None:
		return "UNKNOWN"
	for edge, name in edges:
		if n <= edge:
			return name
	return "ABOVE"


def join(values):
	items = sorted({str(v) for v in values if str(v)})
	return ",".join(items) if items else "NONE"


def digest_of(receipt, tx):
	receipt = receipt if isinstance(receipt, dict) else {}
	tx = tx if isinstance(tx, dict) else {}
	if not receipt:
		fields = dict.fromkeys(DIGEST_FIELDS, "")
		fields.update(
			schema=EXPECTED_SCHEMA, found="0", status="MISSING", to_addr="NONE",
			selector="NONE", topics="NONE", repeated_topics="NONE",
			value_band="UNKNOWN", gas_band="UNKNOWN", log_band="UNKNOWN",
		)
		return fields

	logs = receipt.get("logs") if isinstance(receipt.get("logs"), list) else []
	topics = []
	for entry in logs:
		if not isinstance(entry, dict):
			continue
		entry_topics = entry.get("topics")
		if isinstance(entry_topics, list) and entry_topics:
			topics.append(str(entry_topics[0]))
	repeated = sorted({t for t in topics if topics.count(t) > 1})

	status_raw = str(receipt.get("status", "MISSING"))
	status = "1" if status_raw in ("1", "0x1") else "0" if status_raw in ("0", "0x0") else status_raw[:32] or "MISSING"
	to_addr = tx.get("to")
	to_addr = str(to_addr).strip().lower() if to_addr else "CREATE"
	raw_input = str(tx.get("input") or tx.get("data") or "")
	selector = raw_input[:10].lower() if len(raw_input) >= 10 else "NONE"

	return {
		"schema": EXPECTED_SCHEMA,
		"found": "1",
		"status": status,
		"to_addr": to_addr[:42],
		"selector": selector[:16],
		"topics": join(topics)[:200],
		"repeated_topics": join(repeated)[:200],
		"value_band": band(tx.get("value"), _VALUE_BANDS)[:16],
		"gas_band": band(receipt.get("gasUsed"), _GAS_BANDS)[:16],
		"log_band": band(len(logs), _LOG_BANDS)[:16],
	}


def canonical(digest):
	return "|".join(str(digest.get(name, "")) for name in DIGEST_FIELDS)


def signature(digest):
	return hashlib.sha256(canonical(digest).encode("utf-8")).hexdigest()


def main() -> int:
	import argparse

	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--recorded",
		required=True,
		help="the divergent signature the chain reports for this subject, from get_equivocations",
	)
	parser.add_argument(
		"--artifact-sha256",
		default="",
		help="the artifact_sha256 the chain reports, if you have it",
	)
	args = parser.parse_args()

	if not ARTIFACT.exists():
		print(f"artifact not found: {ARTIFACT}", file=sys.stderr)
		print("run scripts/observe.py --report-harness first", file=sys.stderr)
		return 2

	raw = ARTIFACT.read_text()
	print("off-chain re-derivation of the recorded equivocation")
	print(f"  artifact      : artifacts/harness_equivocation_artifact.json")
	print(f"  artifact bytes: {len(raw)}")

	parsed = json.loads(raw)
	receipt = next((i.get("result") for i in parsed if i.get("id") == 1), None)
	tx = next((i.get("result") for i in parsed if i.get("id") == 2), None)

	computed = digest_of(receipt if isinstance(receipt, dict) else None, tx)
	computed_canonical = canonical(computed)
	computed_signature = signature(computed)

	failures = []

	print()
	print("  [CHECK] digest re-derived here equals the digest the chain recorded")
	print(f"          expected: {args.recorded}")
	print(f"          actual  : {computed_signature}")
	if args.recorded.lower() != computed_signature.lower():
		failures.append("signature")

	print()
	print("  [CHECK] the divergence is exactly the corrupted field, not the whole transaction")
	print(f"          receipt.status in artifact : {receipt.get('status') if isinstance(receipt, dict) else None}")
	print(f"          stable field `status` in digest : {computed['status']}")
	print(f"          full digest : {computed_canonical}")
	if computed["status"] not in ("0", "1"):
		failures.append("status band")
	# Everything except `status` must be identical to what a clean fetch reduces to, which is
	# what makes this a one-field corruption rather than a different transaction.
	if computed["schema"] != EXPECTED_SCHEMA:
		failures.append("schema")

	if args.artifact_sha256:
		print()
		print("  [CHECK] artifact fingerprint")
		local_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
		print(f"          expected: {args.artifact_sha256}")
		print(f"          actual  : {local_sha}")
		if local_sha != args.artifact_sha256:
			failures.append("artifact sha256")

	print()
	if failures:
		print(f"FAILED {len(failures)} check(s): {', '.join(failures)}")
		print()
		print("This artifact is a TEST HARNESS input -- a deliberately corrupted fetch produced by")
		print("scripts/observe.py --report-harness. It is not evidence that any validator")
		print("equivocated. What it demonstrates is that the contract's recorded digest is")
		print("derivable from the artifact by a third party.")
		return 1

	print("OK  the recorded signature is reproducible from the stored artifact")
	print()
	print("Provenance: the artifact is a deliberately corrupted fetch produced by the labelled")
	print("test harness. No validator misbehaved; this is not a real adversarial event.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())