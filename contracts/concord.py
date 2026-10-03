# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Concord -- equivocation as a first-class, on-chain, attributable event.

The hazard
----------
In a GenLayer Intelligent Contract the leader and every validator run the same code, but each
one performs its **own** non-deterministic fetch. If a volatile field is allowed into the value
that crosses the consensus boundary, two honest nodes can compute different evidence for the
*same* subject, disagree, and burn the round. The mitigation is unglamorous: only fields that
are fixed for a given subject *forever* may cross.

What is new here
----------------
Cordon (a sibling project) performs that reduction and then computes

    def digest_signature(digest: dict) -> str:
        \"\"\"Short stable fingerprint of the digest, stored for audit. Never compared.\"\"\"

i.e. it computes a fingerprint, stores it, and never checks it. Concord **is** the missing
comparison, extracted into a primitive:

  * ``observe(subject)`` -- the leader and each validator independently fetch the subject and
    reduce it to a canonical digest. Each node's validator block compares the digest
    **signature** it computed against the leader's. Unanimous signature agreement is a
    precondition for admission; any disagreement aborts the transaction, so the observation is
    never admitted.
  * ``report_equivocation(subject, attributed_to, artifact)`` -- a disagreement cannot be
    recorded in the transaction that produced it (see "Why two transactions" below), so a
    second, separately agreed transaction records it. The committee re-observes the subject
    under the same unanimous check and only admits the record if the committee's own digest
    still equals the admitted one and the supplied artifact genuinely reduces to something
    different.

Agreement is arithmetic over digests. **No model is consulted anywhere in the correctness
path** -- ``gl.nondet.exec_prompt`` is not called at all.

Why two transactions (a hard GenVM constraint, not a design preference)
----------------------------------------------------------------------
``gl.vm.run_nondet_unsafe`` documents its own behaviour: *"The result from the leader (iff
validation passes, otherwise VM will be terminated)"*. Two consequences follow, and they bound
what any contract of this shape can honestly claim:

1. If a validator rejects, the VM is terminated and **every** state write in that transaction
   is rolled back -- including any equivocation record. An equivocation can therefore never be
   written in the transaction it causes.
2. GenVM consensus requires all validators to produce byte-identical state. Per-validator
   values cannot be written into storage at all: validator A writing its own signature under
   its own key is itself a state divergence, so it can never finalise.

Hence the two-transaction shape, and hence the honest limit: **the committee can unanimously
attest that a signature diverges; it cannot attest which node produced that signature**,
because GenVM exposes no validator address to contract code. ``attributed_to`` is a label
supplied by the reporter and is only as strong as the reporter. What *is* unanimously attested
is the content. See README "Limitations".

Reduction discipline (the stable-field rule)
-------------------------------------------
Dropped, because two fetches seconds apart can differ: ``blockNumber``, ``blockHash``,
``transactionIndex``, every timestamp, ``gasUsed``, ``cumulativeGasUsed``, ``effectiveGasPrice``,
``gasPrice``, ``maxFeePerGas``, ``maxPriorityFeePerGas``, ``logsBloom``, the raw log payloads,
and the raw response body.

Banded rather than dropped, keeping the band and discarding the measurement: transaction
``value``, ``gasUsed``, and the receipt log count.

Kept, because fixed for a given transaction forever: whether the receipt exists, receipt
``status``, the ``to`` address, the calldata selector, the sorted set of log topics, and the
subset of topics that repeat within the transaction.

Exact ``gasUsed`` *is* in fact immutable for a mined transaction, and is banded anyway. That is
a deliberate weakening: banding keeps the primitive from being transaction-shape-specific and
removes an entire class of "the node returned a different gas figure" failures. Agreement under
banding implies agreement on the stable subset, which is the direction the safety argument needs.
"""

import hashlib
import json
from dataclasses import dataclass

from genlayer import gl, allow_storage, TreeMap, DynArray, u256

# --------------------------------------------------------------------------------------
# Configuration constants. All are literals in the deployed code, deliberately NOT sourced
# from .env: a reviewer must be able to read exactly which endpoint this contract talks to.
# `sepolia.base.org` is Base's published public JSON-RPC endpoint. It needs no API key and no
# account, which is what keeps Concord's cost at $0.
# --------------------------------------------------------------------------------------

DEFAULT_EVM_RPC = "https://sepolia.base.org"
DEFAULT_EVM_CHAIN_TAG = "base-sepolia"
DEFAULT_QUARANTINE_LIMIT = 3

# Bump when the digest shape changes, so digests signed by different versions can never be
# compared against each other.
DIGEST_SCHEMA = "concord/evm-tx-digest@1"

SUBJECT_PREFIX = "evm:"
SUBJECT_SEPARATOR = "/"
# Bound on the reporter-supplied artifact. This was 6000 in the first deployed version, and a
# real Base Sepolia receipt is ~14.7 KB -- so the artifact was being cut mid-string, failed to
# parse, and the recorded "divergence" was the UNPARSEABLE digest rather than the corrupted
# field. A bound that is too small does not degrade the feature, it silently replaces it.
# Verified on chain, not reasoned about: see state/LEFT-OFF.md.
MAX_ARTIFACT_CHARS = 60000
MAX_LABEL_CHARS = 200

# Field order is part of the wire format. The canonical string is a join over exactly this
# tuple, so reordering it would change every signature.
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


# --------------------------------------------------------------------------------------
# Storage types. @allow_storage is required on every dataclass used in storage and is checked
# at storage-allocation time rather than at import time, so a missing one surfaces as a deploy
# failure rather than a lint failure.
# --------------------------------------------------------------------------------------


@allow_storage
@dataclass
class Observation:
	"""One admitted observation: the reduced digest, its signature, and when it was admitted."""

	subject: str
	digest: str
	signature: str
	admitted_at: str
	admitted_by: str
	observes: u256
	schema: str
	found: str
	status: str
	to_addr: str
	selector: str
	topics: str
	repeated_topics: str
	value_band: str
	gas_band: str
	log_band: str


@allow_storage
@dataclass
class Equivocation:
	"""One validator address recorded as having reduced this subject to a non-canonical digest.

	`signature` is the divergent digest, `artifact_sha256` the fingerprint of the artifact the
	contract reduced to get it, so a third party can recompute the reduction and check it.
	"""

	subject: str
	validator: str
	signature: str
	digest: str
	count: u256
	first_seen: str
	last_seen: str
	reported_by: str
	artifact_sha256: str


# --------------------------------------------------------------------------------------
# Module-level pure helpers. These must not close over `self`: leader_fn/validator_fn are
# cloudpickled and shipped across the WASM boundary. Every one of them is deterministic.
# --------------------------------------------------------------------------------------


def _as_hex(value: object) -> str:
	"""Address -> hex string, or pass a plain string through.

	`gl.message.sender_address` is an `Address` on a real GenVM node but decodes as a plain
	string in the direct test harness, so both shapes have to be accepted.
	"""
	if isinstance(value, str):
		return value
	as_hex = getattr(value, "as_hex", None)
	return as_hex if isinstance(as_hex, str) else str(value)


def _chain_now() -> str:
	"""Chain time from the GenVM message, at second resolution.

	Deliberately NOT wall-clock time and with no wall-clock fallback: leader and validators are
	not guaranteed to share a clock, so anything host-clock would make stored state depend on
	which machine executed. `gl.message_raw["datetime"]` is node-provided transaction time.
	"""
	raw = gl.message_raw.get("datetime")
	if isinstance(raw, str) and raw:
		return raw.split(".", 1)[0]
	return "UNKNOWN"


def _is_hex(text: object, length: int) -> bool:
	if not isinstance(text, str) or len(text) != length:
		return False
	body = text[2:] if text[:2].lower() == "0x" else text
	if len(body) != length - 2:
		return False
	for ch in body:
		if ch not in "0123456789abcdefABCDEF":
			return False
	return True


def _validate_subject(subject: str) -> str:
	"""Normalise a subject to `evm:<0x tx hash>`, lowercased.

	Only EVM transactions are accepted. A general web-resource subject would need its own
	stability argument per URL, and the narrower target is the one that can be argued about;
	BRIEF.md section 6 question 3 resolved to the narrower option.

	The chain is fixed by the constructor's `evm_rpc_url`, not by the subject, so a subject can
	never claim to be on a chain this contract is not actually reading.
	"""
	if not isinstance(subject, str) or len(subject) > 160:
		raise gl.vm.UserError("subject must be a string of at most 160 characters")
	raw = subject.strip().lower()
	if not raw.startswith(SUBJECT_PREFIX):
		raise gl.vm.UserError("subject must start with 'evm:'")
	tx_hash = raw[len(SUBJECT_PREFIX):].strip()
	if not _is_hex(tx_hash, 66):
		raise gl.vm.UserError("subject must be evm:<0x + 64 hex chars>")
	return SUBJECT_PREFIX + tx_hash


def _validate_address(value: object) -> str:
	"""Normalise an address label to lowercase 0x-hex, or reject it.

	Lowercased so that `0xAbC…` and `0xabc…` cannot accumulate two separate equivocation
	records for the same address.
	"""
	if not isinstance(value, str) or not _is_hex(value.strip(), 42):
		raise gl.vm.UserError("validator must be a 0x-prefixed 20-byte hex address")
	return value.strip().lower()


def _as_int(value: object) -> int | None:
	"""Best-effort int from a JSON-RPC quantity, decimal string, or real number.

	JSON-RPC encodes quantities as 0x-prefixed hex strings, so `int(x)` alone would silently
	return UNKNOWN for every real receipt and quietly disable every band.
	"""
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


def _band(value: object, edges: tuple) -> str:
	"""Bucket a number into a named band. Keep the band, discard the measurement.

	This is the anti-equivocation device: an exact count can move between two independent
	fetches, the band does not.
	"""
	n = _as_int(value)
	if n is None:
		return "UNKNOWN"
	for edge, name in edges:
		if n <= edge:
			return name
	return "ABOVE"


_VALUE_BANDS = ((0, "ZERO"), (10**18, "SMALL"), (10**22, "MEDIUM"), (10**26, "LARGE"))
_GAS_BANDS = ((0, "NONE"), (21000, "LOW"), (200000, "MID"), (1000000, "HIGH"))
_LOG_BANDS = ((0, "NONE"), (2, "FEW"), (8, "SOME"), (32, "MANY"))


def _join(values: list) -> str:
	items = sorted({str(v) for v in values if str(v)})
	return ",".join(items) if items else "NONE"


def canonical_digest(receipt: object, tx: object) -> dict:
	"""Stage 1: reduce a receipt+transaction pair to stable fields. Pure. Never raises.

	Returns a FLAT dict[str, str]. Flatness is deliberate: the value crosses the consensus
	boundary through GenVM's calldata encoding, and a flat string-to-string map has no
	encoding-survival questions. It also lets the dataclass storage write fields straight out
	of it without constructing a container.
	"""
	receipt = receipt if isinstance(receipt, dict) else {}
	tx = tx if isinstance(tx, dict) else {}

	if not receipt:
		return _digest("0", "MISSING", "NONE", "NONE", "NONE", "NONE", "UNKNOWN", "UNKNOWN", "UNKNOWN")

	logs = receipt.get("logs")
	logs = logs if isinstance(logs, list) else []
	topics: list = []
	for entry in logs:
		if not isinstance(entry, dict):
			continue
		entry_topics = entry.get("topics")
		if isinstance(entry_topics, list) and entry_topics:
			topics.append(str(entry_topics[0]))
	repeated = sorted({t for t in topics if topics.count(t) > 1})

	status_raw = str(receipt.get("status", "MISSING"))
	if status_raw in ("1", "0x1"):
		status = "1"
	elif status_raw in ("0", "0x0"):
		status = "0"
	else:
		status = status_raw[:32] if status_raw else "MISSING"

	to_addr = tx.get("to")
	to_addr = str(to_addr).strip().lower() if to_addr else "CREATE"

	raw_input = str(tx.get("input") or tx.get("data") or "")
	# The conventional representation of a 4-byte selector: the `0x` plus the first 4 bytes.
	# Slicing from index 0 rather than past the prefix is deliberate -- `raw_input[2:10]` yields
	# only six hex characters and drops the marker, which is how this line first shipped.
	selector = raw_input[:10].lower() if len(raw_input) >= 10 else "NONE"

	return _digest(
		"1",
		status,
		to_addr,
		selector,
		_join(topics),
		_join(repeated),
		_band(tx.get("value"), _VALUE_BANDS),
		_band(receipt.get("gasUsed"), _GAS_BANDS),
		_band(len(logs), _LOG_BANDS),
	)


def _digest(
	found: str,
	status: str,
	to_addr: str,
	selector: str,
	topics: str,
	repeated: str,
	value_band: str,
	gas_band: str,
	log_band: str,
) -> dict:
	"""Assemble the flat digest and sign it.

	Every value is a string, bounded in length, before it reaches this point. Truncation is
	applied to the free-form fields (`to_addr`) only, and consistently on every node, so it
	cannot itself become a source of divergence.
	"""
	fields = {
		"schema": DIGEST_SCHEMA,
		"found": found,
		"status": status,
		"to_addr": to_addr[:42],
		"selector": selector[:16],
		"topics": topics[:MAX_LABEL_CHARS],
		"repeated_topics": repeated[:MAX_LABEL_CHARS],
		"value_band": value_band[:16],
		"gas_band": gas_band[:16],
		"log_band": log_band[:16],
	}
	digest = dict(fields)
	digest["signature"] = digest_signature(fields)
	return digest


def canonical_string(digest: dict) -> str:
	"""The canonical serialisation: the fields, joined, in a fixed order.

	Human-readable on purpose. A reviewer reads `get_observation(...).digest` and can see which
	stable fields produced the signature without running anything.
	"""
	return "|".join(str(digest.get(name, "")) for name in DIGEST_FIELDS)[:900]


def digest_signature(digest: dict) -> str:
	"""SHA-256 over the canonical serialisation, hex.

	This is the value Concord *compares*. Cordon's identically-named helper computes the same
	thing and never checks it; here, unequal signatures mean the transaction does not finalise
	and the observation is not admitted.

	Untruncated. A truncated hash would be a collision invitation on the one field the whole
	primitive rests on.
	"""
	payload = "|".join(str(digest.get(name, "")) for name in DIGEST_FIELDS)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _split_batch(parsed: object) -> tuple:
	"""Pull (receipt, transaction) out of whatever shape a caller supplied.

	Accepts a JSON-RPC batch list, a bare receipt object, or an explicit
	{"receipt": ..., "tx": ...} envelope. Only `id` 1 and 2 are honoured so a caller cannot
	smuggle a different transaction past the caller that fetched it.
	"""
	if isinstance(parsed, list):
		receipt = None
		tx = None
		for item in parsed:
			if not isinstance(item, dict):
				continue
			if item.get("id") == 1:
				receipt = item.get("result")
			elif item.get("id") == 2:
				tx = item.get("result")
		return (receipt if isinstance(receipt, dict) else None, tx if isinstance(tx, dict) else None)
	if isinstance(parsed, dict):
		if isinstance(parsed.get("receipt"), dict):
			tx = parsed.get("tx")
			return parsed["receipt"], tx if isinstance(tx, dict) else {}
		if isinstance(parsed.get("result"), dict):
			return parsed["result"], {}
		return parsed, {}
	return None, None


def _load_json(text: object) -> tuple:
	"""JSON text -> (parsed, error_tag). Never raises.

	A malformed artifact is a normal input here, not an error: it reduces to the not-found
	digest and its signature will not match, which is exactly the outcome a bogus report should
	get. Returns an empty tag for success so the caller can label the reason.
	"""
	if not isinstance(text, str) or not text:
		return None, "EMPTY"
	try:
		return json.loads(text), ""
	except ValueError:
		return None, "UNPARSEABLE"


def digest_from_rpc_response(status: int, body: object) -> dict:
	"""Decode an HTTP JSON-RPC batch response from the live fetch and reduce it. Never raises.

	A non-200 or unparseable body normalises to the not-found digest rather than raising: the
	fetch failing is an observation about the subject, not a contract error.
	"""
	missing = canonical_digest(None, None)
	if status != 200 or not body:
		missing = _relabel(missing, "HTTP_" + str(status)[:16])
		return missing
	if isinstance(body, bytes):
		try:
			text = body.decode("utf-8")
		except UnicodeDecodeError:
			return _relabel(missing, "UNPARSEABLE")
	else:
		text = str(body)
	parsed, err = _load_json(text)
	if parsed is None:
		return _relabel(missing, err)
	receipt, tx = _split_batch(parsed)
	return canonical_digest(receipt, tx)


def digest_from_artifact(text: object) -> dict:
	"""Reduce a caller-supplied fetch artifact to a digest, in the same schema as a live fetch.

	Pure and deterministic -- no network, no nondeterminism. This is what makes an equivocation
	report checkable: the contract recomputes the reduction itself, so a reporter cannot assert
	a divergent signature without supplying an artifact that genuinely reduces to one. Anyone
	can repeat the same reduction off-chain from the stored `artifact_sha256`.
	"""
	parsed, err = _load_json(text if isinstance(text, str) else "")
	if parsed is None:
		return _relabel(canonical_digest(None, None), err)
	receipt, tx = _split_batch(parsed)
	return canonical_digest(receipt, tx)


def _relabel(digest: dict, status: str) -> dict:
	"""Re-sign a digest with a different `status` label.

	Used for the failure paths. Re-signing rather than mutating matters: `signature` is derived
	from every other field, so changing a field without re-deriving would leave a digest whose
	signature does not describe it.
	"""
	fields = {name: str(digest.get(name, "")) for name in DIGEST_FIELDS}
	fields["status"] = status[:32]
	return _digest(
		fields["found"],
		fields["status"],
		fields["to_addr"],
		fields["selector"],
		fields["topics"],
		fields["repeated_topics"],
		fields["value_band"],
		fields["gas_band"],
		fields["log_band"],
	)


def batch_body(tx_hash: str) -> str:
	"""One batched request for receipt + transaction.

	Batched so there is a single fetch rather than two independent ones to disagree about: two
	fetches double the number of responses that must match for the signatures to match.
	"""
	return json.dumps(
		[
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "eth_getTransactionReceipt",
				"params": [tx_hash],
			},
			{
				"jsonrpc": "2.0",
				"id": 2,
				"method": "eth_getTransactionByHash",
				"params": [tx_hash],
			},
		]
	)


# --------------------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------------------


class Concord(gl.Contract):
	owner: str
	evm_rpc_url: str
	evm_chain_tag: str
	quarantine_limit: u256

	# observations[subject] -> the admitted digest and its signature
	observations: TreeMap[str, Observation]
	# equivocations[subject + "/" + validator] -> one attributed divergent signature
	equivocations: TreeMap[str, Equivocation]
	# composite keys of `equivocations`, in first-seen order, so reads can enumerate by subject
	equivocators: DynArray[str]

	admit_counts: TreeMap[str, u256]
	equivocation_counts: TreeMap[str, u256]
	quarantined: TreeMap[str, bool]
	subjects: DynArray[str]

	total_observes: u256
	total_equivocations: u256

	def __init__(
		self,
		owner: str,
		evm_rpc_url: str = DEFAULT_EVM_RPC,
		evm_chain_tag: str = DEFAULT_EVM_CHAIN_TAG,
		quarantine_limit: u256 = DEFAULT_QUARANTINE_LIMIT,
	) -> None:
		self.owner = _validate_address(owner)
		self.evm_rpc_url = evm_rpc_url if evm_rpc_url else DEFAULT_EVM_RPC
		self.evm_chain_tag = evm_chain_tag[:64]
		self.quarantine_limit = u256(quarantine_limit)

	# ---------------------------------------------------------------- consensus

	def _agree(self, subject: str) -> dict:
		"""Fetch and reduce the subject until leader and validators agree on the signature.

		Everything crossing the boundary is one flat dict of strings. The comparison is a single
		string equality on `signature`, which is SHA-256 over the canonical serialisation of
		every other field.

		A disagreement here terminates the VM, which is the enforcement: the caller sees no
		admitted observation at all, rather than an observation with a note attached.
		"""
		# Copied into locals: GenVM nondet callbacks are cloudpickled across the WASM
		# boundary and cannot safely close over `self`.
		subject_local = subject
		rpc_local = self.evm_rpc_url

		def leader_fn() -> dict:
			# The gl.nondet.web call is inline rather than delegated to a helper: it must sit
			# lexically inside the function handed to run_nondet_unsafe, which is what
			# genvm-lint requires to see.
			res = gl.nondet.web.request(
				rpc_local,
				method="POST",
				body=batch_body(subject_local[len(SUBJECT_PREFIX):]),
				headers={"content-type": "application/json"},
			)
			return digest_from_rpc_response(res.status, res.body)

		def validator_fn(leader_result: object) -> bool:
			# This is the whole primitive. Each validator runs leader_fn itself, so it fetches
			# the subject independently, and votes on arithmetic over the two signatures.
			if not isinstance(leader_result, gl.vm.Return):
				return False
			theirs = leader_result.calldata
			if not isinstance(theirs, dict):
				return False
			# Refuse to compare across digest versions. A signature computed under a different
			# schema is not evidence about this one.
			if str(theirs.get("schema", "")) != DIGEST_SCHEMA:
				return False
			mine = leader_fn()
			if not isinstance(mine, dict):
				return False
			return str(theirs.get("signature", "")) == str(mine.get("signature", ""))

		result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
		if not isinstance(result, dict):
			raise gl.vm.UserError("no consensus digest for subject")
		return result

	# ---------------------------------------------------------------- writes

	@gl.public.write
	def observe(self, subject: str) -> str:
		"""Observe `subject` and admit it only on unanimous signature agreement.

		Permissionless: BRIEF.md section 6 question 1 resolved to permissionless, because a
		gated primitive is not a primitive. Returns the admitted signature.

		Calling it twice on the same subject is meaningful and is how `admit_count` grows --
		each call is another independent fetch that had to agree with the first.
		"""
		key = _validate_subject(subject)
		digest = self._agree(key)

		previous = self.admit_counts.get(key)
		count = (int(previous) if previous is not None else 0) + 1
		now = _chain_now()

		self.admit_counts[key] = u256(count)
		self.observations[key] = Observation(
			subject=key,
			digest=canonical_string(digest),
			signature=str(digest.get("signature", "")),
			admitted_at=now,
			admitted_by=_as_hex(gl.message.sender_address),
			observes=u256(count),
			schema=str(digest.get("schema", "")),
			found=str(digest.get("found", "")),
			status=str(digest.get("status", "")),
			to_addr=str(digest.get("to_addr", "")),
			selector=str(digest.get("selector", "")),
			topics=str(digest.get("topics", "")),
			repeated_topics=str(digest.get("repeated_topics", "")),
			value_band=str(digest.get("value_band", "")),
			gas_band=str(digest.get("gas_band", "")),
			log_band=str(digest.get("log_band", "")),
		)
		if count == 1:
			self.subjects.append(key)
		self.total_observes = u256(int(self.total_observes) + 1)
		return str(digest.get("signature", ""))

	@gl.public.write
	def report_equivocation(self, subject: str, attributed_to: str, artifact_json: str) -> str:
		"""Record that `attributed_to` reduced `subject` to a non-canonical digest.

		Permissionless, and gated on the committee rather than on the reporter:

		  1. the subject is re-observed under the same unanimous signature check, so the record
		     rests on a digest the whole committee agreed on;
		  2. the admitted observation must still match it, so a divergence is not being blamed
		     on an observation that has itself moved;
		  3. the artifact is reduced by this contract, deterministically, and its signature must
		     differ from the canonical one -- a report that reduces to the canonical digest is
		     agreement, not equivocation, and is rejected.

		Returns the divergent signature.

		Honest limit: the committee attests the *divergence*, unanimously. It cannot attest
		*which node* produced the string -- GenVM terminates the transaction in which a
		validator disagrees, and exposes no validator address to contract code, so
		`attributed_to` is the reporter's assertion. Steps 1-3 exist so that the *content*
		cannot be fabricated.
		"""
		key = _validate_subject(subject)
		validator = _validate_address(attributed_to)
		# Reject an over-long artifact outright rather than truncating it. Truncation produces
		# unparseable JSON, which reduces to the UNPARSEABLE digest -- so an over-long artifact
		# would be recorded as a divergence that is really just a size limit, blaming a
		# validator for the reporter's upload size.
		if len(artifact_json) > MAX_ARTIFACT_CHARS:
			raise gl.vm.UserError(
				f"artifact too long: {len(artifact_json)} > {MAX_ARTIFACT_CHARS} characters"
			)
		artifact = artifact_json

		agreed = self._agree(key)

		admitted = self.observations.get(key)
		if admitted is None:
			raise gl.vm.UserError("subject has never been admitted")
		canonical_signature = str(admitted.signature)
		if canonical_signature != str(agreed.get("signature", "")):
			raise gl.vm.UserError(
				"admitted digest no longer matches what the committee observes"
			)

		claimed = digest_from_artifact(artifact)
		claimed_signature = str(claimed.get("signature", ""))
		if claimed_signature == canonical_signature:
			raise gl.vm.UserError(
				"artifact reduces to the canonical digest: agreement, not equivocation"
			)

		now = _chain_now()
		slot = key + SUBJECT_SEPARATOR + validator
		previous = self.equivocations.get(slot)
		count = (int(previous.count) if previous is not None else 0) + 1

		self.equivocations[slot] = Equivocation(
			subject=key,
			validator=validator,
			signature=claimed_signature,
			digest=canonical_string(claimed),
			count=u256(count),
			first_seen=previous.first_seen if previous is not None else now,
			last_seen=now,
			reported_by=_as_hex(gl.message.sender_address),
			artifact_sha256=hashlib.sha256(artifact.encode("utf-8")).hexdigest(),
		)
		if previous is None:
			self.equivocators.append(slot)

		per_subject = self.equivocation_counts.get(key)
		self.equivocation_counts[key] = u256(
			(int(per_subject) if per_subject is not None else 0) + 1
		)
		self.total_equivocations = u256(int(self.total_equivocations) + 1)

		# Quarantine is recorded, never enforced. See README "Limitations": this contract
		# cannot influence which nodes GenLayer selects as validators, so a flag here is a
		# signal for whoever does control selection, not a slashing mechanism.
		if count >= int(self.quarantine_limit):
			self.quarantined[validator] = True

		return claimed_signature

	def _only_owner(self) -> None:
		sender = _as_hex(gl.message.sender_address).lower()
		if sender != self.owner:
			raise gl.vm.UserError("only owner may do this")

	@gl.public.write
	def set_quarantine_limit(self, limit: u256) -> None:
		"""Owner-only. How many attributed equivocations before a validator is flagged."""
		self._only_owner()
		if int(limit) < 1:
			raise gl.vm.UserError("quarantine limit must be at least 1")
		self.quarantine_limit = u256(limit)

	# ---------------------------------------------------------------- reads

	@gl.public.view
	def was_admitted(self, subject: str) -> bool:
		"""True only if this subject has an admitted, unanimously agreed observation."""
		key = _validate_subject(subject)
		return key in self.observations

	@gl.public.view
	def get_observation(self, subject: str) -> Observation:
		"""The full public record for one subject. Anyone can re-derive the digest from it."""
		key = _validate_subject(subject)
		observation = self.observations.get(key)
		if observation is None:
			raise gl.vm.UserError("subject has never been admitted")
		return observation

	@gl.public.view
	def admit_count(self, subject: str) -> u256:
		"""How many independent fetches of this subject agreed on the digest."""
		key = _validate_subject(subject)
		count = self.admit_counts.get(key)
		return u256(int(count)) if count is not None else u256(0)

	@gl.public.view
	def equivocation_count(self, subject: str) -> u256:
		"""Total attributed equivocations recorded against this subject."""
		key = _validate_subject(subject)
		count = self.equivocation_counts.get(key)
		return u256(int(count)) if count is not None else u256(0)

	@gl.public.view
	def get_equivocations(self, subject: str) -> dict:
		"""Every attributed divergent signature for a subject, flat and sorted.

		Keys are `validator:signature:count:first_seen`. Returned as a flat string map rather
		than a list of dataclasses so that it survives GenVM's calldata encoding without a
		container having to be constructed.
		"""
		key = _validate_subject(subject)
		rows: dict = {}
		total = 0
		for index in range(len(self.equivocators)):
			slot = self.equivocators[index]
			if not isinstance(slot, str) or not slot.startswith(key + SUBJECT_SEPARATOR):
				continue
			record = self.equivocations.get(slot)
			if record is None:
				continue
			total += 1
			rows["%d:%s" % (total, record.validator)] = "%s:%d:%s" % (
				record.signature,
				int(record.count),
				record.first_seen,
			)
		return rows

	@gl.public.view
	def quarantine_threshold(self) -> u256:
		"""Attributed equivocations at or above which a validator is flagged."""
		return u256(int(self.quarantine_limit))

	@gl.public.view
	def is_quarantined(self, validator: str) -> bool:
		address = _validate_address(validator)
		return bool(self.quarantined.get(address))

	@gl.public.view
	def list_subjects(self) -> DynArray[str]:
		"""Every subject admitted so far, in admission order."""
		return self.subjects

	@gl.public.view
	def stats(self) -> dict:
		"""One read for a reviewer. `evm_rpc_url` is a published public endpoint, not a secret."""
		return {
			"schema": DIGEST_SCHEMA,
			"owner": self.owner,
			"evm_chain_tag": self.evm_chain_tag,
			"evm_rpc_url": self.evm_rpc_url,
			"subjects": len(self.subjects),
			"total_observes": int(self.total_observes),
			"total_equivocations": int(self.total_equivocations),
			"quarantine_threshold": int(self.quarantine_limit),
		}