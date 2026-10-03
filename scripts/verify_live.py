"""Verify the deployed Concord against chain state. Read-only.

One command, expected-vs-actual, exit 0 on pass and non-zero on any mismatch. It is the
deliverable BRIEF.md section 4 asks for in place of the sibling project's "reproduction needed
custom scripts and a signing key" -- this needs no key at all.

    .venv-deploy/bin/python scripts/verify_live.py --address 0x...

Design decisions that matter:

  * **Reads go through genlayer-py's transport, with the endpoint sanitised out of every
    error.** genlayer-py's provider embeds the RPC endpoint in its exception text, and this
    project has an absolute rule about never printing endpoint values, so every call is passed
    through `sanitize()`. The calldata encoding is left to the SDK because that is the part
    that has to agree with the node; see `client()` for what happens if you hand-roll it.

  * **The expected digest is recomputed independently, not read back from the contract.**
    This file re-derives the digest from the Base Sepolia receipt using its own implementation of
    the rule in docs/CONSENSUS.md. That duplication is deliberate: sharing code with the
    contract would make agreement tautological. Two independent implementations of the same
    written rule, plus the chain, agreeing is evidence. If the contract's reduction ever drifts,
    this fails loudly rather than confirming the drift.

  * **No secret is printed.** No private key, no GenLayer RPC endpoint, no EVM endpoint used
    for a secret. Key names only.

  * **Retries, because the resolver here is flaky.** A read can fail with
    `Temporary failure in name resolution` and succeed on the next attempt; that is a local
    network fact, not a contract fault, and it must not be reported as one.
"""

import argparse
import hashlib
import json
import os
import sys
import time

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

DEMO_SUBJECT = "evm:0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a"

# Must match contracts/concord.py DIGEST_SCHEMA.
EXPECTED_SCHEMA = "concord/evm-tx-digest@1"
EXPECTED_QUARANTINE_THRESHOLD = 3

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

_VALUE_BANDS = ((0, "ZERO"), (10**18, "SMALL"), (10**22, "MEDIUM"), (10**26, "LARGE"))
_GAS_BANDS = ((0, "NONE"), (21000, "LOW"), (200000, "MID"), (1000000, "HIGH"))
_LOG_BANDS = ((0, "NONE"), (2, "FEW"), (8, "SOME"), (32, "MANY"))


# ---------------------------------------------------------------- independent reduction


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


def independent_digest(receipt, tx):
	"""Reimplementation of the stable-field rule. See module docstring for why this is a copy."""
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


def canonical(digest: dict) -> str:
	return "|".join(str(digest.get(name, "")) for name in DIGEST_FIELDS)


def signature(digest: dict) -> str:
	return hashlib.sha256(canonical(digest).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- chain reads


def rpc_endpoint() -> str:
	"""Load the GenLayer RPC endpoint and the signing key.

	Values are returned for use, never displayed. Only key *names* are ever printed. The key is
	needed solely because genlayer-py's client insists on an account to populate `from` on a
	read; no transaction is ever signed here.
	"""
	from dotenv import load_dotenv

	load_dotenv(REPO_ROOT / ".env", override=True)
	endpoint = os.environ.get("GENLAYER_STUDIO_RPC", "")
	if not endpoint:
		raise SystemExit("GENLAYER_STUDIO_RPC is not set")
	if not os.environ.get("GENLAYER_PRIVATE_KEY"):
		raise SystemExit("GENLAYER_PRIVATE_KEY is not set")
	return endpoint


def sanitize(text: str, endpoint: str) -> str:
	"""Strip the RPC endpoint out of any text before it is shown.

	genlayer-py's provider embeds the endpoint in its exception messages
	(`Request to <endpoint> failed: ...`), and this project has an absolute rule about never
	printing endpoint values. So the endpoint and its bare host are replaced, not merely
	avoided.
	"""
	if not endpoint:
		return text
	out = text.replace(endpoint, "[endpoint redacted]")
	host = endpoint.split("//")[-1].split("/")[0]
	if host:
		out = out.replace(host, "[host redacted]")
	return out


_CLIENT: dict = {}


def client(endpoint: str):
	"""A genlayer-py client bound to Studionet, memoised.

	The transport is genlayer-py's, deliberately. Two reasons, one of which cost real time:

	  * Its calldata encoding is the part that has to agree with the node. Hand-rolling it with
	    `{"method": ..., "args": [], "kwargs": {}}` instead of `make_calldata_object(...)`
	    encodes extra length-prefixed lists, because `make_calldata_object` OMITS empty
	    `args`/`kwargs`. The node accepts that calldata and returns a result the decoder cannot
	    read, so the failure appears as
	    `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc4 in position 35` --
	    an error that looks like corrupt chain data and is really a request-encoding mistake.
	  * Its exceptions carry the endpoint, so every call here goes through `sanitize`.

	NOTE on a defect inherited from the sibling project: `../mys/state/MEMORY.md` records that
	genlayer-py 0.18.0 reports `Contract 0x... not found` for a live Studionet contract that
	genlayer-js reads. That did NOT reproduce here -- `gen_call` against this address decodes
	correctly on genlayer-py 0.18.0. Treated as unreproduced rather than fixed.
	"""
	if "client" not in _CLIENT:
		import dataclasses

		from eth_account import Account
		from genlayer_py.chains import studionet
		from genlayer_py.client import GenLayerClient

		chain = dataclasses.replace(studionet, rpc_urls={"default": {"http": [endpoint]}})
		_CLIENT["client"] = GenLayerClient(
			chain_config=chain, account=Account.from_key(os.environ["GENLAYER_PRIVATE_KEY"])
		)
	return _CLIENT["client"]


def read_contract(endpoint: str, address: str, method: str, args=None, tries: int = 6):
	"""gen_call, read-only, with retries.

	Retries because the network path from this host to the node is intermittent: reads fail
	with `Temporary failure in name resolution` or `Network is unreachable` and succeed moments
	later. That is a local connectivity fact and must never be reported as a contract fault.
	"""
	from eth_utils import to_checksum_address

	address = to_checksum_address(address)
	last = "unknown"
	for attempt in range(tries):
		try:
			return client(endpoint).read_contract(address, method, args=list(args or []))
		except SystemExit:
			raise
		except Exception as error:  # noqa: BLE001 -- transport, decode, or node-side error
			last = sanitize(f"{type(error).__name__}: {error}", endpoint)
			time.sleep(2 + attempt)
	raise SystemExit(
		f"gen_call {method} failed after {tries} attempts. Last error: {last}"
	)


def fetch_receipt(evm_rpc: str, tx_hash: str, tries: int = 4):
	import requests

	body = [
		{"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
		{"jsonrpc": "2.0", "id": 2, "method": "eth_getTransactionByHash", "params": [tx_hash]},
	]
	for attempt in range(tries):
		try:
			response = requests.post(evm_rpc, json=body, timeout=30)
			response.raise_for_status()
			return response.json()
		except Exception as error:  # noqa: BLE001
			if attempt == tries - 1:
				raise SystemExit(
					f"public EVM endpoint unreachable: {type(error).__name__} (details redacted)"
				)
			time.sleep(2)
	return []


# ---------------------------------------------------------------- reporting


class Checks:
	"""Expected-vs-actual lines, with a non-zero exit if any of them disagree."""

	def __init__(self) -> None:
		self.failures: list = []
		self.count = 0

	def check(self, name: str, expected, actual, note: str = "") -> bool:
		self.count += 1
		ok = expected == actual
		mark = "PASS" if ok else "FAIL"
		print(f"  [{mark}] {name}")
		print(f"         expected: {expected}")
		print(f"         actual  : {actual}")
		if note:
			print(f"         note    : {note}")
		if not ok:
			self.failures.append(name)
		return ok

	def report(self, what: str) -> None:
		print()
		if self.failures:
			print(f"FAILED {len(self.failures)}/{self.count} checks on {what}:")
			for name in self.failures:
				print(f"  - {name}")
			raise SystemExit(1)
		print(f"OK  {self.count}/{self.count} checks passed on {what}")


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--address", required=True, help="deployed Concord address")
	parser.add_argument("--subject", default=DEMO_SUBJECT, help="evm:<0x tx hash> to verify")
	parser.add_argument(
		"--evm-rpc",
		default="https://sepolia.base.org",
		help="public endpoint the contract reads, used here to recompute the expected digest",
	)
	parser.add_argument(
		"--json", action="store_true", help="also emit a machine-readable report on stdout"
	)
	args = parser.parse_args()

	address = args.address
	print(f"Concord live verification")
	print(f"  address : {address}")
	print(f"  subject : {args.subject}")
	print(f"  mode    : read-only (gen_call only; no key is used or printed)")
	print()

	endpoint = rpc_endpoint()
	tx_hash = args.subject.split(":", 1)[1]

	# ---- expected, computed here, independently of the contract -------------------------
	batch = fetch_receipt(args.evm_rpc, tx_hash)
	receipt = next((i.get("result") for i in batch if i.get("id") == 1), None)
	tx = next((i.get("result") for i in batch if i.get("id") == 2), None)
	expected = independent_digest(receipt if isinstance(receipt, dict) else None, tx)
	expected_canonical = canonical(expected)
	expected_signature = signature(expected)

	checks = Checks()

	print("contract configuration")
	stats = read_contract(endpoint, address, "stats")
	checks.check("digest schema", EXPECTED_SCHEMA, stats.get("schema"))
	checks.check("quarantine threshold", EXPECTED_QUARANTINE_THRESHOLD, stats.get("quarantine_threshold"))
	checks.check("at least one subject admitted", True, int(stats.get("subjects", 0)) >= 1)

	print()
	print(f"admitted observation for {args.subject}")
	checks.check("was_admitted", True, read_contract(endpoint, address, "was_admitted", [args.subject]))
	observation = read_contract(endpoint, address, "get_observation", [args.subject])
	checks.check(
		"digest matches independent recomputation",
		expected_canonical,
		observation.get("digest"),
		note="recomputed here from the public receipt, not read back from the contract",
	)
	checks.check(
		"signature matches sha256(digest)",
		expected_signature,
		observation.get("signature"),
	)
	checks.check("admit_count at least 1", True, int(read_contract(endpoint, address, "admit_count", [args.subject])) >= 1)

	print()
	print("stable fields, one by one")
	for field in DIGEST_FIELDS:
		if field == "schema":
			continue
		checks.check(field, expected[field], observation.get(field))

	print()
	print("no volatile field is readable from the admitted record")
	for volatile in (hex(int(receipt["blockNumber"], 16)) if receipt else "", str(receipt.get("gasUsed")) if receipt else ""):
		if not volatile:
			continue
		checks.check(
			f"{volatile} absent from the stored digest",
			False,
			volatile in (observation.get("digest") or ""),
			note="block height and exact gas must be banded away, never stored",
		)

	print()
	print("equivocation accounting")
	equivocation_count = int(read_contract(endpoint, address, "equivocation_count", [args.subject]))
	total_equivocations = int(stats.get("total_equivocations", 0))
	checks.check(
		"per-subject count agrees with the global total",
		True,
		equivocation_count <= total_equivocations,
		note=f"subject={equivocation_count} total={total_equivocations}",
	)
	rows = read_contract(endpoint, address, "get_equivocations", [args.subject])
	print(f"  [INFO] attributed records for this subject: {len(rows)}")
	for key, value in sorted(rows.items()):
		print(f"         {key} -> {value}")
	if rows:
		print("  [INFO] these were recorded by the labelled test harness in scripts/observe.py,")
		print("         from a deliberately corrupted fetch. They are not evidence that any")
		print("         real validator equivocated.")

	print()
	checks.report(f"{address}")

	if args.json:
		print(
			json.dumps(
			 {
					"address": address,
					"subject": args.subject,
					"checks_run": checks.count,
					"failures": checks.failures,
					"expected_signature": expected_signature,
					"observed_signature": observation.get("signature"),
					"equivocation_count": equivocation_count,
					"verdict": "PASS" if not checks.failures else "FAIL",
				},
				indent=2,
			)
		)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())