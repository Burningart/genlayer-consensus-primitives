"""
Tests for OptimisticOracle, written for the GenLayer testing suite (gltest).

Run against a local GenLayer Studio / localnet:

    gltest --network localnet tests/test_optimistic_oracle.py

Tests are split into two groups:
  * deterministic tests — the optimistic happy path and all guard rails; these
    never call an LLM or the web and run quickly on any network.
  * integration tests   — the dispute path, which triggers real web + LLM
    consensus. These need validators with model access and are skipped unless
    RUN_LLM_TESTS=1 is set (they are slower and depend on live web content).
"""

import os
import time

import pytest

from gltest import get_contract_factory, get_default_account, create_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed

RUN_LLM = os.environ.get("RUN_LLM_TESTS") == "1"
BOND = 1_000_000_000_000_000  # 0.001 GEN in wei-like base units

# A cheap, stable question for the optimistic (no-consensus) tests.
QUESTION = "Did the referenced page load successfully?"
CRITERIA = "Answer YES if the primary source is reachable, otherwise NO."
SOURCES = ["https://example.org"]
ANSWER_SPACE = ["YES", "NO"]


@pytest.fixture
def oracle():
    factory = get_contract_factory("OptimisticOracle")
    return factory.deploy(args=[])


def _create(oracle, window=3600):
    return oracle.create_request(
        args=[QUESTION, CRITERIA, SOURCES, ANSWER_SPACE, BOND, window]
    ).transact()


# --------------------------------------------------------------------------- #
# deterministic tests
# --------------------------------------------------------------------------- #
def test_starts_empty(oracle):
    assert oracle.get_request_count().call() == 0


def test_create_request(oracle):
    assert tx_execution_succeeded(_create(oracle))
    assert oracle.get_request_count().call() == 1

    req = oracle.get_request(args=[0]).call()
    assert req["question"] == QUESTION
    assert req["answer_space"] == ANSWER_SPACE
    assert req["bond"] == BOND
    assert req["status"] == 0  # OPEN
    assert req["final_answer"] == ""


def test_create_request_rejects_bad_input(oracle):
    # single-option answer space is not a real question
    assert tx_execution_failed(
        oracle.create_request(
            args=[QUESTION, CRITERIA, SOURCES, ["YES"], BOND, 3600]
        ).transact()
    )
    # no sources
    assert tx_execution_failed(
        oracle.create_request(
            args=[QUESTION, CRITERIA, [], ANSWER_SPACE, BOND, 3600]
        ).transact()
    )
    # zero bond
    assert tx_execution_failed(
        oracle.create_request(
            args=[QUESTION, CRITERIA, SOURCES, ANSWER_SPACE, 0, 3600]
        ).transact()
    )


def test_propose_requires_exact_bond(oracle):
    _create(oracle)
    # too little value posted
    assert tx_execution_failed(
        oracle.propose_answer(args=[0, "YES"]).transact(value=BOND - 1)
    )


def test_propose_rejects_unknown_token(oracle):
    _create(oracle)
    assert tx_execution_failed(
        oracle.propose_answer(args=[0, "MAYBE"]).transact(value=BOND)
    )


def test_propose_happy_path(oracle):
    _create(oracle)
    assert tx_execution_succeeded(
        oracle.propose_answer(args=[0, "yes"]).transact(value=BOND)  # case-normalised
    )
    req = oracle.get_request(args=[0]).call()
    assert req["status"] == 1              # PROPOSED
    assert req["proposed_answer"] == "YES"

    # can't propose twice
    assert tx_execution_failed(
        oracle.propose_answer(args=[0, "NO"]).transact(value=BOND)
    )


def test_finalize_after_window_pays_proposer(oracle):
    # tiny window so the challenge period lapses during the test.
    # NOTE: localnet derives time from the transaction datetime (wall clock),
    # so a short sleep is enough to cross the window.
    me = get_default_account()
    assert tx_execution_succeeded(_create(oracle, window=1))
    assert tx_execution_succeeded(
        oracle.propose_answer(args=[0, "YES"]).transact(value=BOND)
    )
    # cannot finalize while the window is open
    assert tx_execution_failed(oracle.finalize(args=[0]).transact())

    time.sleep(3)
    assert tx_execution_succeeded(oracle.finalize(args=[0]).transact())

    req = oracle.get_request(args=[0]).call()
    assert req["status"] == 2              # FINALIZED
    assert req["final_answer"] == "YES"
    assert oracle.credit_of(args=[me.address]).call() == BOND

    # proposer can pull the refunded bond
    assert tx_execution_succeeded(oracle.withdraw().transact())
    assert oracle.credit_of(args=[me.address]).call() == 0


def test_withdraw_nothing_fails(oracle):
    assert tx_execution_failed(oracle.withdraw().transact())


# --------------------------------------------------------------------------- #
# integration test (real web + LLM consensus)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not RUN_LLM, reason="set RUN_LLM_TESTS=1 to run consensus tests")
def test_dispute_resolves_via_consensus(oracle):
    """
    A proposer asserts a FALSE answer; a disputer challenges; the network reads
    the source and settles the two bonds toward whoever was right.
    """
    assert tx_execution_succeeded(_create(oracle, window=3600))

    # Distinct proposer/disputer. `.connect(account)` returns an instance bound
    # to that signer; if your gltest version exposes account switching
    # differently, adjust these two lines accordingly.
    proposer = create_account()
    disputer = create_account()

    # example.org is reachable, so the truthful answer is YES.
    # The proposer lies (NO) to set up an adversarial dispute.
    oracle.connect(proposer).propose_answer(args=[0, "NO"]).transact(value=BOND)
    receipt = oracle.connect(disputer).dispute(args=[0]).transact(value=BOND)
    assert tx_execution_succeeded(receipt)

    req = oracle.get_request(args=[0]).call()
    assert req["status"] == 2
    assert req["final_answer"] == "YES"    # consensus corrected the record

    # disputer was right and should hold both bonds
    assert oracle.credit_of(args=[disputer.address]).call() == 2 * BOND
    assert oracle.credit_of(args=[proposer.address]).call() == 0
