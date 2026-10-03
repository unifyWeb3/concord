"""Deploy Concord to Studionet (chain 61999).

Read this before using it. Three things are not obvious and each cost the sibling project
real time:

1. **Studionet, not studio-dev.** studio-dev (61997) is fee-charging and every deploy there
   returns FINISHED_WITH_ERROR with num_of_rounds=0, including a 20-line probe. Studionet
   executes contracts. See ../mys/state/MEMORY.md.

2. **Two virtualenvs, deliberately.** The test venv pins genlayer-py 0.9.0 via
   genlayer-test; this script needs genlayer-py 0.18.0. Run it with `.venv-deploy`.

3. **Do not use GENLAYER_EXPLORER_URL.** It points at Bradbury (4221) and renders an empty
   page while returning HTTP 200. Studionet's explorer is explorer-studio.genlayer.com with
   path /address/<addr>, and it is hardcoded below rather than read from the environment.

**No environment value is ever printed.** The RPC endpoint and the private key are read from
the environment and used, never displayed. The contract's own `evm_rpc_url` is a published
public endpoint that lives in the contract source, not here.

Usage:
    .venv-deploy/bin/python scripts/deploy.py --check     # pre-flight, no deploy
    .venv-deploy/bin/python scripts/deploy.py              # deploy, print tx + address
    .venv-deploy/bin/python scripts/deploy.py --json       # machine-readable, for CI
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "concord.py"

# Hardcoded rather than read from .env, for the reason in the module docstring: the env var
# points at Bradbury and renders an empty page with HTTP 200.
EXPLORER_BASE = "https://explorer-studio.genlayer.com"
EXPECTED_CHAIN_ID = 61999

STATUS_ORDER = ["PENDING", "NEW", "UNDETERMINED", "ACCEPTED", "FINALIZED"]


def to_checksum(address: str) -> str:
	"""EIP-55 checksum.

	Non-negotiable, not cosmetic: BRIEF.md section 3 records that a mis-cased address answers
	`Contract not found` for a contract that demonstrably exists.
	"""
	from eth_utils import to_checksum_address

	return to_checksum_address(address)


def load_env() -> dict:
	"""Names only. This function never returns or prints a value under a secret-looking name."""
	from dotenv import load_dotenv

	load_dotenv(REPO_ROOT / ".env", override=True)
	return {
		"rpc": os.environ.get("GENLAYER_STUDIO_RPC", ""),
		"key": os.environ.get("GENLAYER_PRIVATE_KEY", ""),
		"address": os.environ.get("GENLAYER_ADDRESS", ""),
	}


def build_client(env: dict, account):
	"""A client bound to Studionet, with GENLAYER_STUDIO_RPC as its endpoint.

	The account is passed to the constructor rather than to each call. Leaving it unset does not
	fail loudly at construction: it fails later, inside `_encode_add_transaction_data`, as
	`AttributeError: 'NoneType' object has no attribute 'address'` -- an error about a signer
	wearing a transaction-encoding costume.
	"""
	from genlayer_py.chains import studionet
	from genlayer_py.client import GenLayerClient

	# Derive from the shipped studionet config and override the endpoint, so the chain id and
	# the consensus contract addresses come from the SDK rather than from a literal here.
	import dataclasses

	chain = dataclasses.replace(
		studionet,
		rpc_urls={"default": {"http": [env["rpc"]]}},
		block_explorers={"default": {"name": "Studionet Explorer", "url": EXPLORER_BASE}},
	)
	# The 0.18.0 constructor signature is `GenLayerClient(chain_config, account=None)`. There is
	# no `chain=` keyword; passing one raises
	# `TypeError: GenLayerClient.__init__() got an unexpected keyword argument 'chain'`.
	return GenLayerClient(chain_config=chain, account=account)


def poll_status(client, tx_hash: str, until: str = "FINALIZED", tries: int = 150) -> str:
	"""Poll `gen_getTransactionStatus` until `until`.

	GenLayer statuses are not linear: PENDING -> NEW -> UNDETERMINED -> ACCEPTED -> FINALIZED,
	and UNDETERMINED is retried by the network rather than being terminal. So poll to the
	deadline instead of erroring on the first odd status.
	"""
	last = "?"
	for attempt in range(tries):
		try:
			result = client.provider.make_request(
				"gen_getTransactionStatus", [tx_hash]
			).get("result")
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
		progress = f" [{STATUS_ORDER.index(name) + 1}/{len(STATUS_ORDER)}]" if name in STATUS_ORDER else ""
		print(f"    {attempt:>3} {name}{progress}", flush=True)
		if name == until:
			return name
		time.sleep(2)
	return f"TIMEOUT(before {until}); last status {last}"


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--check", action="store_true", help="pre-flight only, do not deploy")
	parser.add_argument("--json", action="store_true", help="machine-readable output")
	parser.add_argument(
		"--owner",
		default="",
		help="contract owner address; defaults to GENLAYER_ADDRESS",
	)
	parser.add_argument(
		"--rpc-url",
		default="https://sepolia.base.org",
		help="public JSON-RPC endpoint the contract reads from. No API key needed.",
	)
	parser.add_argument(
		"--chain-tag",
		default="base-sepolia",
		help="descriptive label for the chain the contract reads",
	)
	parser.add_argument("--quarantine-limit", type=int, default=3)
	args = parser.parse_args()

	env = load_env()
	missing = [name for name, value in (("GENLAYER_STUDIO_RPC", env["rpc"]), ("GENLAYER_PRIVATE_KEY", env["key"])) if not value]
	if missing:
		print(f"missing required environment keys: {', '.join(missing)}", file=sys.stderr)
		return 2

	owner = args.owner or env["address"]
	if not owner:
		print("no owner address: pass --owner or set GENLAYER_ADDRESS", file=sys.stderr)
		return 2

	report = {
		"chain_id": EXPECTED_CHAIN_ID,
		"contract_path": str(CONTRACT_PATH.relative_to(REPO_ROOT)),
		"owner": owner,
		"evm_rpc_url": args.rpc_url,
		"env_keys_present": ["GENLAYER_STUDIO_RPC", "GENLAYER_PRIVATE_KEY"],
	}

	if args.check:
		report["deployed"] = False
		report["note"] = "pre-flight only; no transaction sent"
		print(json.dumps(report, indent=2))
		return 0

	from eth_account import Account

	# Derive the account before the owner check so a mismatched key fails here, loudly, rather
	# than surfacing later as an encoding error about a missing signer.
	account = Account.from_key(env["key"])
	report["owner"] = to_checksum_address_or_raw(owner)
	if account.address.lower() != owner.lower():
		print(
			"refusing to deploy: GENLAYER_PRIVATE_KEY does not match the owner address.\n"
			"  The contract's owner gate would then be unmanageable.",
			file=sys.stderr,
		)
		return 2

	client = build_client(env, account)
	code = CONTRACT_PATH.read_text()

	print(f"deploying Concord to Studionet {EXPECTED_CHAIN_ID} from {account.address}", flush=True)
	tx_id = client.deploy_contract(
		code,
		args=[account.address, args.rpc_url, args.chain_tag, args.quarantine_limit],
	)
	report["deploy_tx_hash"] = tx_id
	print(f"  deploy tx        : {tx_id}", flush=True)

	status = poll_status(client, tx_id)
	report["status"] = status

	tx = client.get_transaction(tx_id)
	address = tx.get("to_address")
	report["address_raw"] = address
	report["consensus"] = tx.get("result_name")
	report["status_name"] = tx.get("status_name")

	if address:
		report["address"] = to_checksum_address_or_raw(address)
		report["explorer"] = f"{EXPLORER_BASE}/address/{report['address']}"

	if args.json:
		print(json.dumps(report, indent=2))
	else:
		print()
		print(f"  address          : {report.get('address')}")
		print(f"  explorer         : {report.get('explorer')}")
		print(f"  status           : {report.get('status')}")
		print(f"  consensus        : {report.get('consensus')}")

	if status != "FINALIZED":
		print("deploy did not finalize", file=sys.stderr)
		return 1
	if not address:
		print("deploy finalized but returned no contract address", file=sys.stderr)
		return 1
	return 0


def to_checksum_address_or_raw(value: str) -> str:
	"""Checksum if it is an address, else hand it back untouched.

	Wrapping this rather than letting to_checksum_address raise means a malformed address
	surfaces as itself in the output instead of as a traceback.
	"""
	try:
		return to_checksum(value)
	except Exception:  # noqa: BLE001
		return value


if __name__ == "__main__":
	raise SystemExit(main())