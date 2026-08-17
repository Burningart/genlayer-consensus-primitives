# GenLayer Consensus Primitives

Two standalone, reusable **Intelligent Contract primitives** for GenLayer. Each
solves a problem that ordinary smart contracts *cannot* solve on their own —
reaching agreement over live, messy, non-deterministic reality — and each does
it with a deliberately chosen consensus strategy rather than a generic
"AI decides X" call.

| Contract | Primitive | Consensus strategy | Value flow |
|---|---|---|---|
| `OptimisticOracle` | Economically-secured natural-language truth oracle | **non-comparative** equivalence + optimistic dispute game | bonded (native) |
| `WebConsensusFeed` | Quantitative web data feed (price/metric) | **comparative** equivalence with an explicit tolerance band | none |

Together they exercise all three GenLayer equivalence principles
(`strict_eq`, `prompt_comparative`, `prompt_non_comparative`) and two very
different economic/state designs, so they double as a reference for *when* to
reach for which consensus tool.

---

## Why these need GenLayer

A classical chain is deterministic: every validator must compute the identical
result from identical inputs. That breaks the moment a contract needs to *read
the world* — web pages differ per fetch, and language-model judgements never
match word-for-word. GenLayer's **Optimistic Democracy** solves this with the
**Equivalence Principle**: a leader proposes a result and validators
independently decide whether it is *acceptably equivalent* to what they
themselves produced. These primitives are built entirely around choosing the
right notion of "acceptably equivalent."

---

## 1. `OptimisticOracle`

**Purpose.** Let any address or contract ask a *resolvable* question in plain
language ("Was proposal 42 executed on-chain before block X?", "Did team A win
the match on 2026-08-17?") and receive a canonical, on-chain answer that is
cheap in the common case and trustlessly correct under dispute.

It is the LLM-native analogue of an optimistic oracle (à la UMA). The core
insight is preserved: **the happy path costs no consensus work.**

### Lifecycle

```
create_request(question, criteria, sources, answer_space, bond, window)
        │
        ▼   OPEN
propose_answer(id, token)  + bond        # proposer just asserts an answer
        │
        ▼   PROPOSED
   ┌────┴───────────────┐
   │ window elapses      │ someone disputes(id) + bond
   ▼                     ▼
finalize(id)        dispute(id)  ── triggers web+LLM consensus this tx ──┐
   │                     │                                                │
   ▼   FINALIZED         ▼   FINALIZED                                    │
proposer refunded    winner takes both bonds; record corrected ──────────┘
```

### How consensus is used

Only a **dispute** spends consensus. Resolution calls:

```python
gl.eq_principle.prompt_non_comparative(collect_evidence, task=..., criteria=...)
```

The **non-comparative** principle is the right choice because resolution is
*subjective-but-checkable*. Each validator fetches the allowed sources itself,
the leader emits a single canonical verdict token, and every validator votes
only on whether that token is the correct, evidence-supported answer under the
request's `criteria`. Validators never have to reproduce the leader's exact
text — which is precisely what makes open-web judgement reachable.

The verdict is constrained to the request's `answer_space` (e.g.
`["YES","NO"]`) plus a reserved `UNKNOWN`. Constraining the output to canonical
tokens turns "did the proposer tell the truth?" into a **deterministic string
comparison** once consensus is reached, so bond settlement is unambiguous:

- verdict == proposed → proposer keeps both bonds
- verdict is a different token → disputer takes both bonds
- verdict == `UNKNOWN` (evidence missing/contradictory) → both refunded

### Design choices worth noting

- **Pull payments.** Bonds accrue to an internal `credits` ledger and leave the
  contract only via `withdraw()` (checks-effects-interactions), so settlement
  never pushes native tokens mid-logic.
- **Storage-safe records.** Requests are all-scalar dataclasses; list fields are
  JSON-encoded into strings, avoiding nested dynamic collections inside stored
  structs.
- **Deterministic timing.** Windows use `gl.message_raw["datetime"]` (identical
  for leader and validators), normalised to UTC.
- **Fault-tolerant fetching.** A dead source is recorded as `[FETCH_ERROR ...]`
  text instead of raising, so one bad link can't desync the validator set.

### Consuming it from another contract

Reads are synchronous views; a consumer polls after finality:

```python
ans = gl.ContractAt(oracle_addr).view().get_answer(request_id)
if ans["finalized"] and ans["answer"] == "YES":
    ...  # release funds, flip a flag, etc.
```

(Resolution itself must be its own transaction because it needs consensus, so
the pattern is request → later → read, not a synchronous call.)

---

## 2. `WebConsensusFeed`

**Purpose.** Put a real-world *number* on-chain — a price, an index, a rate, a
count — read from human-described web sources. Adding a new feed is a governance
action (`register_feed`), not a new code deploy, because the extraction logic is
a natural-language description instead of a bespoke scraper.

### How consensus is used

Updates call:

```python
gl.eq_principle.prompt_comparative(extract, principle=...)
```

The **comparative** principle is the right choice for quantitative feeds. Every
validator runs the *full* job — fetch sources, extract the number — and the
leader's value is accepted only if each validator's own number agrees with it.
Crucially, agreement is **not** byte-equality (`strict_eq` would fail on the
last decimal of a live price). The `principle` string encodes an explicit
**tolerance band in basis points**, so the network agrees on "the same metric,
within X%." This is the canonical middle ground between rigid strict equality
and open-ended subjective judgement.

Values are stored as integers scaled by `10**decimals` (fixed-point), exactly
like production price feeds, so downstream deterministic contracts never touch a
float. A short rolling history is kept for sanity checks and charts.

---

## Repository layout

```
contracts/
  optimistic_oracle.py     # the truth oracle
  web_consensus_feed.py    # the numeric feed
tests/
  test_optimistic_oracle.py
  test_web_consensus_feed.py
README.md
```

Both contracts pin the GenVM std library via the header comment:

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
```

For quick local iteration in GenLayer Studio you can swap this for
`# { "Depends": "py-genlayer:test" }`.

---

## Running the tests

Tests target the GenLayer testing suite (`gltest`, built on `pytest`).

```bash
npm install -g genlayer          # GenLayer CLI
genlayer init && genlayer up     # start a local Studio / localnet
pip install genlayer-test        # the gltest runner

# deterministic tests only (fast, no models):
gltest --network localnet tests/

# include the real web + LLM consensus paths (dispute / feed update):
RUN_LLM_TESTS=1 gltest --network localnet tests/
```

**Test strategy.** Every guard rail and the optimistic no-consensus path are
covered by deterministic tests that never touch a model. The two paths that
genuinely use consensus — an *adversarial* dispute where a lying proposer is
overturned by the network, and a live feed update — are marked integration
tests (gated by `RUN_LLM_TESTS=1`) because they require validators with model
access and depend on live web content.

---

## Security & extension notes

- **Griefing / spam.** `create_request` and `update_feed` are permissionless;
  in production, gate them behind a fee or allowlist, or require the requester
  to pre-fund the first proposer's bond.
- **Source quality is the trust root.** The oracle is only as honest as its
  `criteria` and `sources`; keep sources authoritative and criteria precise
  enough that two reasonable readers would agree.
- **Tolerance tuning.** For `WebConsensusFeed`, set `tolerance_bps` from the
  metric's real volatility — too tight and honest validators disagree, too loose
  and a manipulated source slips through.
- **Composability.** A curated registry, an insurance vault, or a milestone
  escrow can all consume `OptimisticOracle` answers; a lending market or an
  options contract can consume `WebConsensusFeed` values. Both are written as
  general infrastructure, not one-off demos.
