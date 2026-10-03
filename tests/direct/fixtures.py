"""Recorded fetch artifacts for the hermetic suite. No network.

The point of these fixtures is the pair `RPC_VIEW_A` / `RPC_VIEW_B`: two *observations of the
same transaction* whose volatile fields all differ -- block height, timestamps, gas figures,
bloom filter, transaction index -- and whose stable fields are identical. Any reduction that
lets a volatile field through will make those two disagree, so `test_..._volatile_fields...`
is a real test of the reduction rather than a restatement of it.

`RPC_DIVERGENT_B` is the opposite case: same shape, one genuinely different stable field. That
is what a validator equivocating looks like from the outside.
"""

import json

TX_HASH = "0x919c3f9906382255a9acc19b350c900784dcff164ea30e5fd6a6f3df00283e5a"
OTHER_TX_HASH = "0x009a63e92f44c13499308e98deb03b279f9182a20765d0a10d8c3e2d2e58d5f8"
TX_HASH_UNKNOWN = "0x" + "11" * 32
TO = "0x5f9215dff5c01671e6e77469389d694ac4af2e97"

TRANSFER_TOPIC = "0xddf252ad1b2a2c7d431c033c692f3121d49b99a3662f26157a86d6c7ab65058d"
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


def _receipt(status: str, block: int, ts: int, gas_used: int, logs: list) -> dict:
	return {
		"type": "0x2",
		"root": "0x" + "00" * 32,
		"status": status,
		"cumulativeGasUsed": gas_used * 3,
		"logsBloom": "0x" + ("ab" * 256),
		"logs": logs,
		"transactionHash": TX_HASH,
		"contractAddress": None,
		"gasUsed": hex(gas_used),
		"effectiveGasPrice": "0x3b9aca00",
		"blockHash": "0x" + ("%064x" % (block * 7919)),
		"transactionIndex": "0x" + ("%02x" % (block % 200)),
		"blockNumber": hex(block),
		"confirmations": 1200,
		"cumulativeGasUsed": gas_used * 3,
	}


def _tx(block: int, gas_price: int) -> dict:
	return {
		"hash": TX_HASH,
		"type": "0x2",
		"nonce": "0x2f1a",
		"blockHash": "0x" + ("%064x" % (block * 7919)),
		"blockNumber": hex(block),
		"transactionIndex": "0x" + ("%02x" % (block % 200)),
		"from": "0x1111111111111111111111111111111111111111",
		"to": TO,
		"value": "0xde0b6b3a7640000",
		"gas": "0x493e0",
		"gasPrice": hex(gas_price),
		"maxFeePerGas": hex(gas_price),
		"maxPriorityFeePerGas": "0x5f5e100",
		"input": "0xac9650d8" + "0" * 64,
		"v": "0x1",
		"r": "0x" + "ab" * 32,
		"s": "0x" + "cd" * 32,
		"chainId": "0x14a34",
		"accessList": [],
		"maxFeePerBlobGas": "0x1",
		"blobVersionedHashes": [],
		"yParity": "0x1",
	}


def _logs(repeat_transfer: bool) -> list:
	logs = [
		{
			"address": TO,
			"topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + "1", "0x" + "0" * 24 + "2"],
			"data": "0x" + "0" * 63 + "0a",
			"blockNumber": hex(0),
			"transactionHash": TX_HASH,
			"transactionIndex": "0x00",
			"logIndex": "0x00",
			"blockHash": "0x" + "00" * 32,
			"removed": False,
		},
		{
			"address": TO,
			"topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + "1", "0x" + "0" * 24 + "2"],
			"data": "0x" + "0" * 63 + "14",
			"blockNumber": hex(0),
			"transactionHash": TX_HASH,
			"transactionIndex": "0x01",
			"logIndex": "0x01",
			"blockHash": "0x" + "00" * 32,
			"removed": False,
		},
	]
	if repeat_transfer:
		logs.append(dict(logs[0], logIndex="0x02", transactionIndex="0x02"))
	return logs


def batch(receipt: dict, tx: dict) -> str:
	"""The exact wire shape the contract's batched request would receive."""
	return json.dumps(
		[
			{"jsonrpc": "2.0", "id": 1, "result": receipt},
			{"jsonrpc": "2.0", "id": 2, "result": tx},
		]
	)


def not_found() -> str:
	return json.dumps(
		[
			{"jsonrpc": "2.0", "id": 1, "result": None},
			{"jsonrpc": "2.0", "id": 2, "result": None},
		]
	)


# --- the two views of the SAME transaction. Every volatile field differs. -------------------

RPC_VIEW_A = batch(_receipt("0x1", 46400000, 1788568288, 703310, _logs(False)), _tx(46400000, 0x3b9aca00))
RPC_VIEW_B = batch(_receipt("0x1", 47711999, 1799900000, 703311, _logs(False)), _tx(47711999, 0x77359400))
# View C: a different block, a different timestamp, a gasPrice a whole order of magnitude
# away, and a gasUsed 200k different from view A -- but inside the same band. Banding is the
# point: the exact figure moves, the band does not.
RPC_VIEW_C = batch(_receipt("0x1", 47123456, 1795000000, 500000, _logs(False)), _tx(47123456, 0x5f5e100))

# --- the opposite case: one genuinely different stable field. -------------------------------

# status flipped from success to failure
RPC_DIVERGENT_STATUS = batch(
	_receipt("0x0", 46400000, 1788568288, 703310, _logs(False)), _tx(46400000, 0x3b9aca00)
)
# an extra, different event topic -> different topic set
RPC_DIVERGENT_TOPIC = batch(
	_receipt("0x1", 46400000, 1788568288, 703310, _logs(False) + [{
		"address": TO,
		"topics": [APPROVAL_TOPIC],
		"data": "0x" + "0" * 63 + "01",
		"logIndex": "0x02",
	}]), _tx(46400000, 0x3b9aca00)
)
# value pushed out of the SMALL band into MEDIUM -> different value_band
RPC_DIVERGENT_VALUE = batch(
	_receipt("0x1", 46400000, 1788568288, 703310, _logs(False)),
	_tx(46400000, 0x3b9aca00) | {"value": hex(10**20)},
)

SUBJECT = "evm:" + TX_HASH
SUBJECT_UNKNOWN = "evm:" + TX_HASH_UNKNOWN
SUBJECT_MISSING = "evm:" + OTHER_TX_HASH

ALL_VIEWS = {
	"A": RPC_VIEW_A,
	"B": RPC_VIEW_B,
	"C": RPC_VIEW_C,
}