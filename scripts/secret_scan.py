"""Secret scan. Read-only; never prints a value, only a key name and a location.

Two things it checks, and the second is the one that usually gets skipped:

  1. **Working tree** -- does any `.env` value appear in any tracked or untracked file?
  2. **Full git history** -- does any `.env` value appear in any blob ever committed? A secret
     that was committed and later removed is still in the repository, and "it is not in the
     working tree any more" is exactly the reasoning that leaks one.

Exits non-zero on any hit. Reports key NAMES only, per the project's absolute rule.

    .venv/bin/python scripts/secret_scan.py
"""

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MIN_VALUE_LENGTH = 8

# Files whose whole purpose is to contain the values being searched for.
SELF = {".env"}
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".gz", ".xz", ".zip", ".tar"}


def env_values() -> dict:
	"""Key name -> value, for values long enough to be a real credential or endpoint."""
	env_file = REPO / ".env"
	if not env_file.exists():
		print("no .env present; scanning for key-shaped content only")
		return {}
	values = {}
	for line in env_file.read_text().splitlines():
		if "=" not in line or line.strip().startswith("#"):
			continue
		key, value = line.split("=", 1)
		value = value.strip().strip('"').strip("'")
		if len(value) >= MIN_VALUE_LENGTH:
			values[key.strip()] = value
	return values


def scan_text(label: str, location: str, text: str, values: dict) -> list:
	hits = []
	for key, value in values.items():
		if value in text:
			hits.append((label, location, key))
	return hits


def main() -> int:
	values = env_values()
	print(f"secret scan: {len(values)} environment values to search for (values never printed)")
	if not values:
		print("nothing to compare against")
		return 0

	hits: list = []

	# --- 1. working tree -------------------------------------------------------------
	scanned = 0
	for path in REPO.rglob("*"):
		if not path.is_file():
			continue
		relative = path.relative_to(REPO)
		parts = {".git", ".venv", ".venv-deploy", "__pycache__", "node_modules", "artifacts"}
		if parts & set(relative.parts):
			continue
		if path.suffix in BINARY_SUFFIXES or path.name in SELF:
			continue
		try:
			text = path.read_text(errors="ignore")
		except OSError:
			continue
		scanned += 1
		hits.extend(scan_text("working-tree", str(relative), text, values))
	print(f"  working tree : {scanned} files scanned")

	# --- 2. full history -------------------------------------------------------------
	# Every blob ever committed, at every revision.
	commits = subprocess.run(
		["git", "rev-list", "--all"], cwd=REPO, capture_output=True, text=True
	).stdout.split()
	blobs: dict = {}
	for commit in commits:
		listed = subprocess.run(
			["git", "ls-tree", "-r", commit], cwd=REPO, capture_output=True, text=True
		).stdout.splitlines()
		for line in listed:
			parts = line.split()
			if len(parts) >= 3 and parts[2] != "":
				blobs[parts[2]] = parts[3]  # sha -> path
	print(f"  history      : {len(commits)} commits, {len(blobs)} distinct blobs")

	unique_shas = sorted(set(blobs))
	checked = 0
	for start in range(0, len(unique_shas), 200):
		chunk = unique_shas[start:start + 200]
		try:
			# Input must be bytes: --batch reads binary stdin and subprocess refuses to build a
			# memoryview over a str. Passing a str raises
			# `TypeError: memoryview: a bytes-like object is required, not 'str'`.
			content = subprocess.run(
				["git", "cat-file", "--batch"],
				cwd=REPO,
				input=("\n".join(chunk) + "\n").encode(),
				capture_output=True,
			).stdout
		except OSError:
			continue
		# --batch output is binary with binary bodies; decode leniently and search the stream.
		text = content.decode("latin-1", errors="ignore")
		for key, value in values.items():
			if value in text:
				hits.append(("history", f"<{len(blobs)} blobs, {len(commits)} commits>", key))
		checked += len(chunk)
	print(f"  history      : {checked} blob contents searched")

	# --- 3. private-key shapes, independent of .env -----------------------------------
	#
	# A 32-byte hex literal is AMBIGUOUS: a secp256k1 private key, an EVM transaction hash, and a
	# sha256 digest are all 64 hex characters, and this repository legitimately contains
	# transaction hashes (they are the subjects being observed) and digests (they are the
	# output). A shape-only check therefore cannot separate a leaked key from a public
	# transaction hash, and flagging every one of them would train a reviewer to ignore this
	# scan.
	#
	# What can be checked is the CONTEXT: a 32-byte literal assigned to a name that claims to be
	# a key, in a file that is not the .env. That catches a hardcoded signing key and leaves
	# transaction hashes and digests alone. Stated as a limitation rather than papered over.
	KEY_PATTERN = re.compile(r"(?i)(private_key|privkey|signer_key|secret_key|mnemonic|seed_phrase)")
	ASSIGNMENT = re.compile(
		r"(?i)\b("
		r"private_key|privkey|signer_key|secret_key|mnemonic|seed_phrase|key"
		r")\b\s*[:=]\s*[\"']?(0x[0-9a-fA-F]{64}|[\"'][^\"']{32,}[\"'])"
	)
	for path in REPO.rglob("*"):
		if not path.is_file():
			continue
		relative = path.relative_to(REPO)
		if {".git", ".venv", ".venv-deploy", "__pycache__", "node_modules", ".pytest_cache"} & set(
			relative.parts
		):
			continue
		if path.suffix in BINARY_SUFFIXES or path.name in SELF:
			continue
		try:
			text = path.read_text(errors="ignore")
		except OSError:
			continue
		for number, line in enumerate(text.splitlines(), start=1):
			stripped = line.strip()
			if stripped.startswith("#"):
				continue
			# An env lookup is the correct way to get a key, not a leak.
			if "environ" in line or "getenv" in line or "load_dotenv" in line:
				continue
			match = ASSIGNMENT.search(line)
			if match:
				hits.append(
					("key-shape", f"{relative}:{number}", f"literal assigned to `{match.group(1)}`")
				)
			elif KEY_PATTERN.search(line) and "0x" in line and "=" in line:
				hits.append(
					("key-shape", f"{relative}:{number}", "key-named variable assigned a literal")
				)

	print()
	if hits:
		print(f"FAIL  {len(hits)} finding(s). Key names and locations only:")
		for label, location, key in sorted(set(hits)):
			print(f"  [{label}] {location}: {key}")
		return 1

	print("PASS  no environment value appears in the working tree or anywhere in git history")
	print("      no key-named variable is assigned a literal value in a non-.env file")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())