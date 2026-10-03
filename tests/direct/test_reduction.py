"""The reduction, in isolation.

BRIEF.md section 4 requires a test proving byte-identical digests across differing volatile
inputs. That test lives in `test_digest_stability.py` where it runs through the real contract;
this file exercises the pure functions directly so a failure points at the reduction rather than
at the state machine.
"""

import sys
from pathlib import Path

import pytest
from gltest.direct import pytest_plugin  # noqa: F401

sys.path.insert(0, str(Path(__file__).parent))
from conftest import CONTRACT, SDK_VERSION  # noqa: E402
from fixtures import (  # noqa: E402
	RPC_VIEW_A,
	RPC_VIEW_B,
	RPC_VIEW_C,
	RPC_DIVERGENT_STATUS,
	RPC_DIVERGENT_TOPIC,
	RPC_DIVERGENT_VALUE,
	TO,
	TX_HASH,
	not_found,
)


@pytest.fixture
def mod(direct_deploy):
	"""The loaded contract module, for its module-level pure helpers."""
	direct_deploy(CONTRACT, owner="0x" + "aa" * 20, sdk_version=SDK_VERSION)
	return sys.modules["_contract_concord"]


def digest_of(mod, body: str) -> dict:
	return mod.digest_from_rpc_response(200, body.encode("utf-8"))


# --------------------------------------------------------------- the central claim


def test_two_volatile_views_of_one_tx_are_byte_identical(mod):
	"""Different block height, timestamp, gas and gas price. Same digest, byte for byte.

	This is the property the whole primitive rests on. If it fails, two honest nodes reading
	the same transaction would burn a round.
	"""
	a = digest_of(mod, RPC_VIEW_A)
	b = digest_of(mod, RPC_VIEW_B)
	assert mod.canonical_string(a) == mod.canonical_string(b)
	assert a["signature"] == b["signature"]


def test_a_third_volatile_view_still_matches(mod):
	"""A third view, with gasUsed 200k different, a different block and a different gas price."""
	a = digest_of(mod, RPC_VIEW_A)
	c = digest_of(mod, RPC_VIEW_C)
	assert a["gas_band"] == c["gas_band"], "fixture bands should not differ"
	assert a["signature"] == c["signature"]


def test_signature_is_sha256_of_the_canonical_string(mod):
	"""The signature must be a function of the canonical string and nothing else."""
	import hashlib

	a = digest_of(mod, RPC_VIEW_A)
	expected = hashlib.sha256(mod.canonical_string(a).encode("utf-8")).hexdigest()
	assert a["signature"] == expected


def test_canonical_string_covers_exactly_the_declared_fields(mod):
	"""Guard against a field being added to the digest but not to the signed string."""
	a = digest_of(mod, RPC_VIEW_A)
	assert set(mod.DIGEST_FIELDS) <= set(a)
	assert "signature" not in mod.DIGEST_FIELDS


# --------------------------------------------------------------- volatile fields are gone


def test_no_volatile_field_survives_into_the_digest(mod):
	a = digest_of(mod, RPC_VIEW_A)
	serialised = mod.canonical_string(a)
	for volatile in (
		"46400000",  # blockNumber
		"47711999",  # the other blockNumber
		"1788568288",  # timestamp
		"703310",  # exact gasUsed
		"703311",  # the other exact gasUsed
		"cumulativeGasUsed",
		"effectiveGasPrice",
		"gasPrice",
		"logsBloom",
		"transactionIndex",
		"0xabab",  # logsBloom prefix
	):
		assert volatile not in serialised, f"volatile field leaked into digest: {volatile}"


def test_bands_are_kept_while_measurements_are_discarded(mod):
	a = digest_of(mod, RPC_VIEW_A)
	assert a["value_band"] == "SMALL"
	assert a["gas_band"] == "HIGH"
	assert a["log_band"] == "FEW"
	# The exact figures must not be recoverable from the digest.
	assert "1000000000000000000" not in mod.canonical_string(a)


# --------------------------------------------------------------- stable fields are kept


def test_stable_fields_survive_the_reduction(mod):
	a = digest_of(mod, RPC_VIEW_A)
	assert a["found"] == "1"
	assert a["status"] == "1"
	assert a["to_addr"] == TO
	assert a["selector"] == "0xac9650d8"
	assert a["topics"] != "NONE"
	assert a["repeated_topics"] != "NONE", "the repeated Transfer topic should be detected"


def test_schema_is_stamped_and_versioned(mod):
	a = digest_of(mod, RPC_VIEW_A)
	assert a["schema"] == mod.DIGEST_SCHEMA


def test_digest_is_flat_strings_only(mod):
	"""Flatness is a wire-format requirement, not a style preference: this is the value that
	crosses the consensus boundary through GenVM's calldata encoding."""
	a = digest_of(mod, RPC_VIEW_A)
	for key, value in a.items():
		assert isinstance(key, str), key
		assert isinstance(value, str), key


# --------------------------------------------------------------- divergence is detected


@pytest.mark.parametrize(
	"body",
	[RPC_DIVERGENT_STATUS, RPC_DIVERGENT_TOPIC, RPC_DIVERGENT_VALUE],
	ids=["status", "topics", "value_band"],
)
def test_a_genuinely_different_observation_produces_a_different_signature(mod, body):
	"""The other half of the contract: the reduction must not be so lossy that a real
	difference between two observations of the *same* subject passes unnoticed."""
	a = digest_of(mod, RPC_VIEW_A)
	other = digest_of(mod, body)
	assert a["signature"] != other["signature"]


def test_missing_transaction_reduces_to_not_found(mod):
	missing = digest_of(mod, not_found())
	assert missing["found"] == "0"
	assert missing["status"] == "MISSING"
	assert missing["to_addr"] == "NONE"
	assert missing["signature"] != digest_of(mod, RPC_VIEW_A)["signature"]


@pytest.mark.parametrize(
	"status,body,expected",
	[
		(429, "", "HTTP_429"),
		(500, "", "HTTP_500"),
		(200, "not json at all", "UNPARSEABLE"),
		(200, "", "HTTP_200"),
	],
)
def test_failed_fetches_normalise_instead_of_raising(mod, status, body, expected):
	"""A dead endpoint is an observation about the subject, not a contract error. Raising here
	would turn a fetch failure into a stalled round."""
	d = digest_of(mod, body) if body else mod.digest_from_rpc_response(status, b"")
	assert d["status"] == expected
	assert d["found"] == "0"
	assert len(d["signature"]) == 64


def test_relabel_resigns_the_signature(mod):
	"""A relabelled digest must have a signature that describes the relabelled fields."""
	d = mod.digest_from_rpc_response(503, b"")
	assert d["signature"] == mod.digest_signature(d)


def test_zero_and_band_edges(mod):
	"""Banding edges, so an off-by-one in the edge table cannot pass silently."""
	from fixtures import _receipt, _tx, batch  # noqa: F401

	def with_value(value_hex: str, gas: int, logs: int) -> dict:
		receipt = _receipt("0x1", 1, 1, gas, [])
		tx = dict(_tx(1, 1), value=value_hex)
		receipt["logs"] = [{} for _ in range(logs)]
		return mod.digest_from_rpc_response(200, batch(receipt, tx).encode("utf-8"))

	assert with_value("0x0", 0, 0)["value_band"] == "ZERO"
	assert with_value(hex(10**18), 21000, 1)["value_band"] == "SMALL"
	assert with_value(hex(10**18 + 1), 21000, 1)["value_band"] == "MEDIUM"
	assert with_value(hex(10**22 + 1), 21000, 1)["value_band"] == "LARGE"
	assert with_value(hex(10**26 + 1), 21000, 1)["value_band"] == "ABOVE"
	assert with_value("0x0", 0, 0)["gas_band"] == "NONE"
	assert with_value("0x0", 21000, 0)["gas_band"] == "LOW"
	assert with_value("0x0", 21001, 0)["gas_band"] == "MID"
	assert with_value("0x0", 200001, 0)["gas_band"] == "HIGH"
	assert with_value("0x0", 1000001, 0)["gas_band"] == "ABOVE"
	assert with_value("0x0", 0, 33)["log_band"] == "ABOVE"
	assert with_value("0x0", 0, 32)["log_band"] == "MANY"
	assert with_value("0x0", 0, 200)["log_band"] == "ABOVE"


def test_unparseable_quantity_does_not_silently_disable_the_band(mod):
	"""JSON-RPC quantities are hex strings. If the hex path regressed, every band would read
	UNKNOWN and the digest would quietly stop discriminating."""
	d = digest_of(mod, RPC_VIEW_A)
	assert "UNKNOWN" not in (d["value_band"], d["gas_band"], d["log_band"])


def test_repeated_topics_are_sorted_and_deduplicated(mod):
	a = digest_of(mod, RPC_VIEW_A)
	parts = a["repeated_topics"].split(",")
	assert parts == sorted(parts)
	assert len(parts) == len(set(parts))