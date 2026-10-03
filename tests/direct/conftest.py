"""Shared fixtures. Hermetic: the RPC endpoint is mocked, so nothing here touches the network.

`serve()` is the important one. `vm.run_validator()` re-runs the leader function, which
re-performs the fetch, so swapping the mock between the contract call and the validator call is
how this suite simulates the validator being a different node that saw different volatile data.
That is the whole equivocation surface.
"""

import sys
from pathlib import Path

import pytest
from gltest.direct import pytest_plugin  # noqa: F401  (registers direct_vm / direct_deploy)

sys.path.insert(0, str(Path(__file__).parent))

CONTRACT = "contracts/concord.py"

# Pin the GenVM SDK release explicitly. Without this, gltest picks the lexicographically newest
# tarball in ~/.cache/gltest-direct, and running `genvm-lint check` drops differently-named
# bundles into that same cache directory -- which breaks the whole suite with
# "No py-genlayer runners found in tarball". Pinning makes the suite independent of whatever
# else is lying around in ~/.cache.
SDK_VERSION = "v0.3.0-rc7"

OWNER = "0x" + "aa" * 20
OBSERVER = "0x" + "bb" * 20
REPORTER = "0x" + "cc" * 20
VALIDATOR = "0x5F9215DFF5C01671E6E77469389D694AC4AF2E97"  # deliberately mixed case
VALIDATOR_LOWER = VALIDATOR.lower()

RPC_URL = "https://rpc.mock.invalid"
RPC_PATTERN = ".*"


def serve(vm, body: str, status: int = 200) -> None:
	"""Make every subsequent web request to the mocked RPC return `body`.

	Replaces the mock list rather than appending, so swapping views is unambiguous and there is
	no ordering to get wrong.
	"""
	vm._web_mocks = []
	vm.mock_web(RPC_PATTERN, {"method": "POST", "status": status, "body": body})


class Round:
	"""Outcome of one `observe` round, replaying the leader/validator pair honestly.

	`signature` is what the leader computed, `validator_agreed` is the validator's vote against
	its own independent fetch, and `committed` is whether the resulting state survived.
	"""

	def __init__(self, signature: str, validator_agreed: bool, committed: bool) -> None:
		self.signature = signature
		self.validator_agreed = validator_agreed
		self.committed = committed

	@property
	def admitted(self) -> bool:
		return self.committed and self.validator_agreed

	def __repr__(self) -> str:
		return (
			f"Round(signature={self.signature[:12]}..., agreed={self.validator_agreed}, "
			f"committed={self.committed})"
		)


def run_round(contract, vm, subject: str, leader_body: str, validator_body: str) -> Round:
	"""Run one full observe round and apply GenVM's documented termination rule.

	The direct harness runs the leader inline and captures the validator rather than running it,
	and it does **not** roll state back when a validator votes False. Both of those are harness
	behaviour, not GenVM behaviour. `gl.vm.run_nondet_unsafe`'s own docstring states the real
	rule: *"The result from the leader (iff validation passes, otherwise VM will be terminated)"*.

	So this helper models that rule on top of the harness:

	  1. snapshot the state,
	  2. serve the leader one view of the subject and call `observe`,
	  3. swap the mock so the validator's own fetch sees a *different* view, and run it,
	  4. if it voted False, restore the snapshot -- the transaction is terminated and every
	     write in it, including any equivocation record, is rolled back.

	Step 4 is a harness simulation of a documented GenVM rule, not a claim that the harness does
	it. It is what makes "a divergence blocks admission" a state-level assertion rather than a
comment.
	"""
	snapshot = vm.snapshot()
	serve(vm, leader_body)
	signature = contract.observe(subject)
	serve(vm, validator_body)
	agreed = bool(vm.run_validator())
	if agreed:
		return Round(signature, True, True)
	vm.revert(snapshot)
	return Round(signature, False, False)


@pytest.fixture
def vm(direct_vm):
	return direct_vm


@pytest.fixture
def concord(direct_deploy, direct_vm):
	"""A deployed Concord with one observer as the sender and no mocks yet."""
	contract = direct_deploy(
		CONTRACT,
		owner=OWNER,
		evm_rpc_url=RPC_URL,
		evm_chain_tag="mock-chain",
		sdk_version=SDK_VERSION,
	)
	direct_vm.sender = OBSERVER
	return contract