"""
Tests for WebConsensusFeed (gltest).

    gltest --network localnet tests/test_web_consensus_feed.py

Deterministic tests cover registration, access control and views. The
`update_feed` path performs real web + LLM consensus and is gated behind
RUN_LLM_TESTS=1.
"""

import os

import pytest

from gltest import get_contract_factory, get_default_account, create_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed

RUN_LLM = os.environ.get("RUN_LLM_TESTS") == "1"


@pytest.fixture
def feed():
    factory = get_contract_factory("WebConsensusFeed")
    return factory.deploy(args=[])


def _register(feed, label="eth_usd"):
    return feed.register_feed(
        args=[
            label,
            "The current price of 1 ETH in US dollars.",
            ["https://www.coingecko.com/en/coins/ethereum"],
            "USD",
            50,   # tolerance: 0.50%
            2,    # 2 decimals of fixed-point precision
        ]
    ).transact()


# --------------------------------------------------------------------------- #
# deterministic tests
# --------------------------------------------------------------------------- #
def test_register_and_read_feed(feed):
    assert tx_execution_succeeded(_register(feed))

    info = feed.get_feed(args=["eth_usd"]).call()
    assert info["unit"] == "USD"
    assert info["tolerance_bps"] == 50
    assert info["decimals"] == 2
    assert info["update_count"] == 0
    assert info["active"] is True


def test_duplicate_label_rejected(feed):
    assert tx_execution_succeeded(_register(feed))
    assert tx_execution_failed(_register(feed))


def test_register_is_owner_only(feed):
    stranger = create_account()
    assert tx_execution_failed(
        feed.connect(stranger).register_feed(
            args=["x", "d", ["https://example.org"], "U", 100, 0]
        ).transact()
    )


def test_value_before_update_fails(feed):
    _register(feed)
    assert tx_execution_failed(feed.get_value(args=["eth_usd"]).call())


def test_bad_tolerance_rejected(feed):
    assert tx_execution_failed(
        feed.register_feed(
            args=["bad", "d", ["https://example.org"], "U", 0, 2]
        ).transact()
    )
    assert tx_execution_failed(
        feed.register_feed(
            args=["bad2", "d", ["https://example.org"], "U", 20000, 2]
        ).transact()
    )


# --------------------------------------------------------------------------- #
# integration test (real web + LLM consensus)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not RUN_LLM, reason="set RUN_LLM_TESTS=1 to run consensus tests")
def test_update_feed_reaches_consensus(feed):
    _register(feed)
    receipt = feed.update_feed(args=["eth_usd"]).transact()
    assert tx_execution_succeeded(receipt)

    info = feed.get_feed(args=["eth_usd"]).call()
    assert info["update_count"] == 1
    assert info["updated_at"] > 0
    assert info["latest_value"] > 0        # a plausible ETH price, scaled x100

    value = feed.get_value(args=["eth_usd"]).call()
    assert value == info["latest_value"]

    history = feed.get_history(args=["eth_usd"]).call()
    assert len(history) == 1
    assert history[0][1] == value
