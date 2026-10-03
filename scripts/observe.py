"""Drive Concord on Studionet: observe a real Base Sepolia transaction, or report a divergence.

Writes, so this sends transactions and costs nothing but time. Read-only checks live in
`verify_live.py`.

Two modes, and the distinction matters for honesty:

  --subject evm:<tx>                 A real observation of a real transaction. Every node
                                     fetches the same public RPC and the committee must agree
                                     on the digest for the observation to be admitted.

  --report-harness                   Provokes the equivocation path on chain. This uses a
                                     DELIBERATELY CORRUPTED fetch artifact and is a TEST
                                     HARNESS, not an adversarial event. It exists because a
                                     real validator cannot be induced to equivocate on demand,
                                     and the brief permits a corrupted fetch when labelled.
                                     The artifact is written to disk so a reviewer can see
                                     exactly what was corrupted.

No environment value is ever printed.

`write_contract` returns the GenLayer transaction id as a bare string, not a dict -- indexing it
raises `TypeError: string indices must be integers, not 'str'`.

Usage:
    .venv-deploy/bin/python scripts/observe.py --address 0x...             # real, demo tx
    .venv-deploy/bin/python scripts/observe.py --address 0x... --subject evm:0x...
    .venv-deploy/bin/python scripts/observe.py --address 0x... --report-harness
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_DIR = REPO_ROOT / "artifacts"
EXPLORER_BASE = "https://explorer-studio.genlayer.com"
STATUS_ORDER = ["PENDING", "PROPOSING", "NEW", "UNDETERMINED", "ACCEPTED", "FINALIZED"]

# A real, long-settled Base Sepolia transaction: an ERC-20 transfer at block 46400000.
# Long-settled matters -- a transaction still propagating could be seen by the leader and not
# by a validator, which is a genuine (not adversarial) source of `found=0` / `found=1` mismatch.
DEMO_SUBJECT = "evm:0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a"

# A validator label that is definitely not a real node. Used by --report-harness so the record
# cannot be mistaken for blame against a genuine participant.
HARNESS_VALIDATOR = "0x000000000000000000000000000000000000dEaD"


def poll_status(client, tx_hash: str, tries: int = 200) -> str:
	last = "?"
	for attempt in range(tries):
		try:
			result = client.provider.make_request("gen_getTransactionStatus", [tx_hash]).get("result")
		except Exception as error:  # noqa: BLE001 -- transient while not yet indexed
			last = f"RPC error: {type(error).__name__}"
			time.sleep(2)
			continue
		name = (
			result
			if isinstance(result, str)
			else str((result or {}).get("status", (result or {}).get("status_name", "?")))
		)
		last = name
		print(f"    {attempt:>3} {name}", flush=True)
		# A rejected consensus result is terminal for our purposes: retrying will not change it.
		if name in ("FINALIZED", "REJECTED", "FAILED", "UNDEFINED"):
			return name
		time.sleep(2)
	return f"TIMEOUT; last status {last}"


def build_client():
	from dotenv import load_dotenv
	from eth_account import Account
	from genlayer_py.chains import studionet
	from genlayer_py.client import GenLayerClient

	load_dotenv(REPO_ROOT / ".env", override=True)
	rpc = os.environ.get("GENLAYER_STUDIO_RPC", "")
	key = os.environ.get("GENLAYER_PRIVATE_KEY", "")
	if not rpc or not key:
		print("missing GENLAYER_STUDIO_RPC or GENLAYER_PRIVATE_KEY", file=sys.stderr)
		raise SystemExit(2)

	import dataclasses

	chain = dataclasses.replace(
		studionet,
		rpc_urls={"default": {"http": [rpc]}},
		block_explorers={"default": {"name": "Studionet Explorer", "url": EXPLORER_BASE}},
	)
	return GenLayerClient(chain_config=chain, account=Account.from_key(key))


def corrupt_artifact(rpc_url: str, subject: str) -> tuple[str, str]:
	"""Fetch the real receipt, flip one stable field, and hand back the corrupted batch.

	This is the DELIBERATE CORRUPTION the brief permits for exercising the equivocation path,
	and it is why the record it produces is a harness artefact and not evidence that any
	validator misbehaved. Exactly one field is changed -- the receipt `status` -- and that field
	is a genuine stable field, so it changes the digest by the same mechanism a real divergence
	would.
	"""
	import requests

	tx_hash = subject.split(":", 1)[1]
	body = [
		{"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
		{"jsonrpc": "2.0", "id": 2, "method": "eth_getTransactionByHash", "params": [tx_hash]},
	]
	response = requests.post(rpc_url, json=body, timeout=30)
	parsed = response.json()

	real_status = None
	for item in parsed:
		if isinstance(item, dict) and item.get("id") == 1 and isinstance(item.get("result"), dict):
			real_status = str(item["result"].get("status"))
			item["result"]["status"] = "0x0" if real_status == "0x1" else "0x1"

	corrupted = json.dumps(parsed)
	description = (
		f"receipt.status flipped from {real_status} to "
		f"{'0x0' if real_status == '0x1' else '0x1'}; every other field left as fetched"
	)
	return corrupted, description


def main() -> int:
	parser = argparse.ArgumentParser()
	group = parser.add_mutually_exclusive_group()
	group.add_argument("--subject", help="evm:<0x tx hash> to observe (default: the demo tx)")
	group.add_argument(
		"--report-harness",
		action="store_true",
		help="exercise the equivocation path with a deliberately corrupted artifact (TEST HARNESS)",
	)
	parser.add_argument("--address", required=True, help="deployed Concord address")
	parser.add_argument(
		"--evm-rpc",
		default="https://sepolia.base.org",
		help="public endpoint the contract reads; must match the deployed constructor arg",
	)
	parser.add_argument("--json", action="store_true")
	args = parser.parse_args()

	client = build_client()
	from eth_utils import to_checksum_address

	address = to_checksum_address(args.address)
	report: dict = {"address": address, "explorer": f"{EXPLORER_BASE}/address/{address}"}

	# Observe first: an equivocation report requires an admitted observation to exist.
	subject = args.subject or DEMO_SUBJECT
	if args.report_harness:
		print(f"observing {subject} so a record has something to attach to", flush=True)

	print(f"observe({subject})", flush=True)
	observe_tx = client.write_contract(address, "observe", args=[subject])
	report["observe_tx_hash"] = observe_tx
	report["observe_status"] = poll_status(client, observe_tx)
	print(f"  observe tx : {observe_tx}  -> {report['observe_status']}", flush=True)

	if not args.report_harness:
		if args.json:
			print(json.dumps(report, indent=2))
		else:
			print(f"\nsubject     : {subject}")
			print(f"observe tx  : {observe_tx}")
			print(f"status      : {report['observe_status']}")
		return 0 if report["observe_status"] == "FINALIZED" else 1

	# --- TEST HARNESS: provoke a divergence -------------------------------------------
	artifact, description = corrupt_artifact(args.evm_rpc, subject)
	ARTIFACT_DIR.mkdir(exist_ok=True)
	path = ARTIFACT_DIR / "harness_equivocation_artifact.json"
	path.write_text(artifact)
	report["harness"] = {
		"is_real_adversarial_event": False,
		"why": "deliberately corrupted fetch; no validator misbehaved",
		"corruption": description,
		"artifact_path": str(path.relative_to(REPO_ROOT)),
		"artifact_sha256": __import__("hashlib").sha256(artifact.encode()).hexdigest(),
		"attributed_to": HARNESS_VALIDATOR,
	}
	print(f"\nTEST HARNESS -- recording a divergence from a deliberately corrupted fetch")
	print(f"  corruption : {description}", flush=True)
	print(f"  artifact   : {path.relative_to(REPO_ROOT)}", flush=True)

	report_tx = client.write_contract(
		address,
		"report_equivocation",
		args=[subject, HARNESS_VALIDATOR, artifact],
	)
	report["report_tx_hash"] = report_tx
	report["report_status"] = poll_status(client, report_tx)
	print(f"  report tx  : {report_tx}  -> {report['report_status']}", flush=True)

	if args.json:
		print(json.dumps(report, indent=2))
	else:
		print(f"\naddress     : {address}")
		print(f"observe tx  : {observe_tx} -> {report['observe_status']}")
		print(f"report tx   : {report_tx} -> {report['report_status']}")
		print(f"explorer    : {report['explorer']}")
	return 0 if report["report_status"] == "FINALIZED" else 1


if __name__ == "__main__":
	raise SystemExit(main())