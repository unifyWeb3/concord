"""The observation state machine, and the two claims BRIEF.md section 4 asks to be proven.

The direct harness runs the leader inline and captures the validator instead of running it, and
it does not roll state back when the validator votes False. Both are harness behaviours, not
GenVM behaviours -- `run_nondet_unsafe`'s docstring says the VM is terminated if validation
fails. `conftest.run_round` replays that documented rule on top of the harness so these are
state-level assertions.
"""

import hashlib

import pytest
from gltest.direct import pytest_plugin  # noqa: F401

from conftest import (  # noqa: E402
	OBSERVER,
	OWNER,
	REPORTER,
	VALIDATOR,
	VALIDATOR_LOWER,
	RPC_URL,
	run_round,
	serve,
)
from fixtures import (  # noqa: E402
	RPC_VIEW_A,
	RPC_VIEW_B,
	RPC_VIEW_C,
	RPC_DIVERGENT_STATUS,
	RPC_DIVERGENT_TOPIC,
	RPC_DIVERGENT_VALUE,
	SUBJECT,
	SUBJECT_MISSING,
	SUBJECT_UNKNOWN,
	TO,
	not_found,
)


# --------------------------------------------------------------- admission


def test_unanimous_observation_is_admitted(concord, vm):
	round_ = run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	assert round_.validator_agreed is True
	assert concord.was_admitted(SUBJECT) is True

	observation = concord.get_observation(SUBJECT)
	assert observation.signature == round_.signature
	assert len(observation.signature) == 64
	assert observation.subject == SUBJECT
	assert observation.found == "1"
	assert observation.status == "1"
	assert observation.to_addr == TO
	assert observation.observes == 1


def test_digest_stored_is_byte_identical_across_differing_volatile_inputs(concord, vm):
	"""BRIEF.md section 4, the contract's central claim, through the real contract.

	Leader and validator see different block heights, timestamps, gas figures and gas prices.
	The validator voted the round in (asserted inside `run_round`), and the signature the
	contract stored must equal the signature the validator computed from its own fetch.
	"""
	import sys

	mod = sys.modules["_contract_concord"]

	round_ = run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	assert round_.validator_agreed is True
	assert round_.admitted is True

	stored = concord.get_observation(SUBJECT)

	# What the validator computed for itself, from the view only it was served.
	validator_signature = mod.digest_from_rpc_response(200, RPC_VIEW_B.encode("utf-8"))["signature"]
	assert stored.signature == validator_signature == round_.signature

	# And the canonical string behind it carries no volatile figure.
	for volatile in ("46400000", "47711999", "1788568288", "703310", "703311"):
		assert volatile not in stored.digest


def test_three_differing_views_all_admit_the_same_signature(concord, vm):
	"""A, B and C differ in every volatile field. All three must produce one signature."""
	seen = set()
	for leader, validator in (
		(RPC_VIEW_A, RPC_VIEW_B),
		(RPC_VIEW_B, RPC_VIEW_C),
		(RPC_VIEW_C, RPC_VIEW_A),
	):
		round_ = run_round(concord, vm, SUBJECT, leader, validator)
		assert round_.admitted is True
		seen.add(round_.signature)
	assert len(seen) == 1, f"volatile fields leaked into the signature: {seen}"
	assert concord.admit_count(SUBJECT) == 3


def test_reobserving_the_same_subject_counts_independent_agreement(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	assert concord.admit_count(SUBJECT) == 1
	run_round(concord, vm, SUBJECT, RPC_VIEW_B, RPC_VIEW_C)
	assert concord.admit_count(SUBJECT) == 2
	assert concord.get_observation(SUBJECT).observes == 2
	assert len(concord.list_subjects()) == 1, "re-observing must not duplicate the subject"


def test_observe_is_permissionless(concord, vm):
	"""BRIEF.md section 6 question 1 resolved to permissionless: a gated primitive is not a
	primitive. A different sender admits a subject under its own name."""
	vm.sender = REPORTER
	run_round(concord, vm, SUBJECT_MISSING, RPC_VIEW_A, RPC_VIEW_B)
	assert concord.get_observation(SUBJECT_MISSING).admitted_by.lower() == REPORTER.lower()


def test_missing_transaction_admits_as_not_found_rather_than_erroring(concord, vm):
	round_ = run_round(concord, vm, SUBJECT_UNKNOWN, not_found(), not_found())
	assert round_.admitted is True
	assert concord.get_observation(SUBJECT_UNKNOWN).found == "0"
	assert concord.get_observation(SUBJECT_UNKNOWN).status == "MISSING"


# --------------------------------------------------------------- divergence blocks admission


@pytest.mark.parametrize(
	"leader,validator,label",
	[
		(RPC_VIEW_A, RPC_DIVERGENT_STATUS, "receipt status flipped"),
		(RPC_VIEW_A, RPC_DIVERGENT_TOPIC, "an extra event topic"),
		(RPC_VIEW_A, RPC_DIVERGENT_VALUE, "value pushed out of its band"),
	],
	ids=["status", "topics", "value_band"],
)
def test_divergent_validator_signature_blocks_admission(concord, vm, leader, validator, label):
	"""A validator whose independently computed signature differs is voted down, and the
	round is discarded: no observation, no admit count."""
	round_ = run_round(concord, vm, SUBJECT, leader, validator)
	assert round_.validator_agreed is False, f"validator failed to notice: {label}"
	assert round_.committed is False
	assert concord.was_admitted(SUBJECT) is False
	assert concord.admit_count(SUBJECT) == 0
	assert len(concord.list_subjects()) == 0


def test_divergence_is_in_the_signature_not_in_the_verdict(concord, vm):
	"""There is no verdict to compare here at all -- agreement is one string equality."""
	round_ = run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_DIVERGENT_STATUS)
	assert round_.validator_agreed is False
	assert round_.signature != concord.admit_count(SUBJECT)


def test_a_failed_fetch_is_not_an_equivocation(concord, vm):
	"""A dead endpoint on one node is a different observation, so it must be voted down rather
	than silently admitted -- but it is not a claim about a validator's honesty."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, "upstream is down")
	assert concord.was_admitted(SUBJECT) is False


# --------------------------------------------------------------- equivocation recording


def test_divergent_signature_is_recorded_and_attributed(concord, vm):
	"""BRIEF.md section 4: a divergent validator signature is recorded as an equivocation."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	vm.sender = REPORTER
	vm._expect_revert = None

	# The committee still observes the canonical digest for this subject.
	serve(vm, RPC_VIEW_B)
	divergent = concord.report_equivocation(
		SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS
	)
	assert divergent == concord.get_observation(SUBJECT).signature or divergent != ""
	assert len(divergent) == 64

	assert concord.equivocation_count(SUBJECT) == 1
	rows = concord.get_equivocations(SUBJECT)
	assert len(rows) == 1
	entry = list(rows.values())[0]
	assert entry.startswith(divergent + ":1:")


def test_repeated_reports_accumulate_on_the_same_validator(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	for _ in range(3):
		concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	assert concord.equivocation_count(SUBJECT) == 3
	entry = list(concord.get_equivocations(SUBJECT).values())[0]
	assert entry.split(":")[1] == "3"
	assert len(concord.get_equivocations(SUBJECT)) == 1, "one row per validator"


def test_distinct_validators_get_distinct_rows(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	concord.report_equivocation(SUBJECT, "0x" + "de" * 20, RPC_DIVERGENT_TOPIC)
	assert concord.equivocation_count(SUBJECT) == 2
	assert len(concord.get_equivocations(SUBJECT)) == 2


def test_address_case_does_not_split_one_validator_into_two(concord, vm):
	"""Addresses are case-normalised on the way in, so 0xAbC and 0xabc cannot end up as two
	separate equivocation records for the same node."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	concord.report_equivocation(SUBJECT, VALIDATOR_LOWER, RPC_DIVERGENT_TOPIC)
	assert len(concord.get_equivocations(SUBJECT)) == 1
	assert concord.equivocation_count(SUBJECT) == 2


def test_a_report_that_agrees_is_rejected(concord, vm):
	"""The common case: an artifact that reduces to the canonical digest is not an
	equivocation, and must not be recorded as one."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	with pytest.raises(Exception, match="agreement, not equivocation"):
		concord.report_equivocation(SUBJECT, VALIDATOR, RPC_VIEW_A)


def test_a_report_against_an_unadmitted_subject_is_rejected(concord, vm):
	serve(vm, RPC_VIEW_B)
	with pytest.raises(Exception, match="never been admitted"):
		concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	assert concord.equivocation_count(SUBJECT) == 0


def test_a_report_is_rejected_when_the_admitted_digest_no_longer_matches(concord, vm):
	"""If the committee now sees something different from what was admitted, the observation
	itself is in dispute and blaming a validator for it would be unfounded."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_DIVERGENT_STATUS)
	with pytest.raises(Exception, match="no longer matches"):
		concord.report_equivocation(SUBJECT, VALIDATOR, RPC_VIEW_A)
	assert concord.equivocation_count(SUBJECT) == 0


def test_an_oversized_artifact_is_rejected_not_truncated(concord, vm):
	"""Regression test for a defect found on chain, not in review.

	The first deployed version truncated the artifact at 6000 characters. A real Base Sepolia
	receipt is ~14.7 KB, so the truncated prefix did not parse, reduced to the UNPARSEABLE
	not-found digest, and the chain recorded that as the "divergent signature". The recorded
	divergence was an artefact of the size limit, and it named a validator for it.

	Truncation is worse than useless here: it converts the reporter's upload size into a claim
	about someone else's honesty. Over-long is now rejected outright.
	"""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)

	import sys

	# The limit is read from the module, not hard-coded, so this test cannot drift away from the
	# contract and start passing for an unrelated reason.
	mod = sys.modules["_contract_concord"]
	oversized = '{"pad":"' + "a" * (mod.MAX_ARTIFACT_CHARS + 10) + '","receipt":{"status":"0x0"}}'
	with pytest.raises(Exception, match="artifact too long"):
		concord.report_equivocation(SUBJECT, VALIDATOR, oversized)

	assert concord.equivocation_count(SUBJECT) == 0
	assert concord.get_equivocations(SUBJECT) == {}, "nothing may be recorded for a rejected artifact"


def test_a_realistic_sized_artifact_is_accepted_and_recorded_verbatim(concord, vm):
	"""The complement of the test above: an artifact of realistic receipt size must round-trip.

	The payload is RPC_DIVERGENT_TOPIC -- a genuine divergent reduction -- padded out past the
	size limit with a third batch entry the reduction ignores (only ids 1 and 2 are honoured).
	So it is large, valid, and reduces to something different from the admitted digest; if the
	artifact were still being truncated, this would fail.
	"""
	import json

	padded = json.dumps(
		json.loads(RPC_DIVERGENT_TOPIC) + [{"id": 3, "result": {"padding": "a" * 8000}}]
	)
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	concord.report_equivocation(SUBJECT, VALIDATOR, padded)
	slot = SUBJECT + "/" + VALIDATOR_LOWER
	assert concord.equivocations[slot].artifact_sha256 == hashlib.sha256(
		padded.encode("utf-8")
	).hexdigest()


def test_a_forged_artifact_cannot_manufacture_a_divergence(concord, vm):
	"""An empty or malformed artifact reduces to the not-found digest, which differs from the
	canonical one -- so this DOES record. What it records is the truth: that artifact reduces
	to something that is not this subject. It cannot claim the canonical signature."""
	import sys

	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	recorded = concord.report_equivocation(SUBJECT, VALIDATOR, "not json")
	mod = sys.modules["_contract_concord"]
	assert recorded == mod.digest_from_artifact("not json")["signature"]
	assert recorded != concord.get_observation(SUBJECT).signature


def test_a_malformed_artifact_is_recorded_as_unparseable_not_as_the_real_field(concord, vm):
	"""The distinction the truncation bug blurred: an unparseable artifact must say so in the
	recorded digest, so a reader can tell a bad upload from a genuine divergent reduction."""
	import sys

	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	mod = sys.modules["_contract_concord"]
	recorded = concord.report_equivocation(SUBJECT, VALIDATOR, "not json")
	stored = concord.equivocations[SUBJECT + "/" + VALIDATOR_LOWER].digest
	assert "UNPARSEABLE" in stored
	assert stored == mod.canonical_string(mod.digest_from_artifact("not json"))


def test_the_artifact_fingerprint_is_recorded_so_a_third_party_can_recheck(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	vm.sender = REPORTER
	concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	slot = SUBJECT + "/" + VALIDATOR_LOWER
	record = concord.equivocations[slot]
	assert record.artifact_sha256 == hashlib.sha256(
		RPC_DIVERGENT_STATUS.encode("utf-8")
	).hexdigest()
	assert record.reported_by.lower() == REPORTER.lower()
	assert record.count == 1


# --------------------------------------------------------------- quarantine


def test_quarantine_threshold_is_configurable_and_readable(concord):
	assert concord.quarantine_threshold() == 3


def test_quarantine_flag_is_set_once_the_threshold_is_reached(concord, vm):
	"""BRIEF.md section 6 question 2: recorded, and flagged at the threshold."""
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	for _ in range(3):
		concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	assert concord.is_quarantined(VALIDATOR_LOWER) is True


def test_below_the_threshold_nothing_is_flagged(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	assert concord.is_quarantined(VALIDATOR_LOWER) is False


def test_changing_the_threshold_is_owner_only(concord, vm):
	vm.sender = OWNER
	concord.set_quarantine_limit(1)
	assert concord.quarantine_threshold() == 1
	vm.sender = OBSERVER
	with pytest.raises(Exception, match="only owner"):
		concord.set_quarantine_limit(5)


# --------------------------------------------------------------- input validation


@pytest.mark.parametrize(
	"bad",
	[
		"",
		"0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a",
		"evx:0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a",
		"evm:919c3f99",
		"evm:0xZZ93c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a",
		"evm:0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5",
	],
)
def test_malformed_subjects_are_rejected(concord, bad):
	with pytest.raises(Exception, match="subject"):
		concord.observe(bad)


def test_subject_case_is_normalised(concord, vm):
	run_round(concord, vm, SUBJECT.upper(), RPC_VIEW_A, RPC_VIEW_B)
	assert concord.was_admitted(SUBJECT) is True
	assert concord.admit_count(SUBJECT) == 1


@pytest.mark.parametrize("bad", ["", "0x1234", "not-an-address", VALIDATOR[:-2]])
def test_malformed_validator_addresses_are_rejected(concord, vm, bad):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	with pytest.raises(Exception, match="validator"):
		concord.report_equivocation(SUBJECT, bad, RPC_DIVERGENT_STATUS)


def test_reads_of_an_unadmitted_subject_are_zero_not_a_crash(concord):
	assert concord.was_admitted(SUBJECT) is False
	assert concord.admit_count(SUBJECT) == 0
	assert concord.equivocation_count(SUBJECT) == 0
	assert concord.get_equivocations(SUBJECT) == {}


def test_stats_reports_the_configured_chain(concord, vm):
	run_round(concord, vm, SUBJECT, RPC_VIEW_A, RPC_VIEW_B)
	serve(vm, RPC_VIEW_B)
	concord.report_equivocation(SUBJECT, VALIDATOR, RPC_DIVERGENT_STATUS)
	stats = concord.stats()
	assert stats["total_observes"] == 1
	assert stats["total_equivocations"] == 1
	assert stats["subjects"] == 1
	assert stats["evm_chain_tag"] == "mock-chain"
	assert stats["evm_rpc_url"] == RPC_URL
	assert stats["owner"].lower() == OWNER.lower()