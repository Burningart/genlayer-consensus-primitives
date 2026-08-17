# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
#
# OptimisticOracle — a reusable, web-grounded truth oracle for GenLayer.
# ---------------------------------------------------------------------------
# WHAT THIS IS
#   A general-purpose primitive that lets *any* address (or contract) ask a
#   resolvable question in natural language and get a canonical, on-chain
#   answer that is economically secured and, when contested, resolved by
#   GenLayer's Optimistic Democracy over live web evidence.
#
#   It is the LLM-native cousin of UMA's Optimistic Oracle. The key idea from
#   optimistic oracles is preserved: the *happy path costs no consensus work*.
#   A proposer simply asserts an answer and backs it with a bond. Only if
#   someone disputes does the network spend an LLM + web round to decide the
#   truth. This keeps the primitive cheap for the common case while remaining
#   trustlessly correct under adversarial conditions.
#
# WHY IT NEEDS GENLAYER (not a normal smart contract)
#   Dispute resolution reads the open web and applies human-written resolution
#   criteria to messy, non-deterministic evidence. No two validators will fetch
#   byte-identical pages or phrase a judgement identically, so a classical chain
#   cannot agree on the outcome without a trusted off-chain reporter. GenLayer's
#   Equivalence Principle lets validators independently gather evidence and then
#   agree that the leader's verdict is a faithful application of the criteria —
#   the exact capability an oracle's "hard part" requires.
#
# HOW CONSENSUS IS USED
#   * Disputes call `gl.eq_principle.prompt_non_comparative`. The non-comparative
#     principle is the right tool here because resolution is subjective-but-
#     checkable: the leader produces a canonical verdict token, and every other
#     validator independently re-reads the sources and votes only on whether
#     that token is the correct, evidence-supported answer under the criteria.
#     Validators do NOT need to reproduce the leader's text, which is what makes
#     open-web judgement reachable.
#   * The verdict is constrained to a small, request-defined `answer_space`
#     (e.g. ["YES","NO"]) plus the reserved token "UNKNOWN". Constraining the
#     output to canonical tokens turns "did the proposer tell the truth?" into a
#     deterministic string comparison once consensus is reached, so bond
#     settlement is unambiguous.
#
# DESIGN NOTES
#   * Value handling uses the pull-payment pattern: bonds accrue to an internal
#     `credits` ledger and are paid out only when the owner calls `withdraw()`.
#     This follows checks-effects-interactions and avoids pushing native tokens
#     during settlement (safer and easier to reason about).
#   * Records are stored as all-scalar dataclasses (lists are JSON-encoded into
#     string fields). This keeps storage construction trivial and avoids nesting
#     dynamic collections inside stored structs.
#
# LIFECYCLE
#   OPEN --propose_answer(+bond)--> PROPOSED
#   PROPOSED --(window elapses)--> finalize() ----------------> FINALIZED (cheap)
#   PROPOSED --dispute(+bond)--> [consensus resolves] --------> FINALIZED (secured)

from genlayer import *

import json
import datetime
from dataclasses import dataclass

# --- status codes ----------------------------------------------------------
OPEN = 0
PROPOSED = 1
FINALIZED = 2

# reserved verdict for evidence that is missing/contradictory
UNKNOWN = "UNKNOWN"


def _require(cond: bool, msg: str) -> None:
    """Cheap revert helper. A raised exception rolls the transaction back."""
    if not cond:
        raise Exception(msg)


def _now_ts() -> int:
    """
    Deterministic 'now' for the current transaction.

    `gl.message_raw["datetime"]` is part of the consensus message, so it is
    identical for the leader and every validator — safe to use for on-chain
    timing. We normalise to UTC so `.timestamp()` is machine-independent.
    """
    raw = str(gl.message_raw["datetime"])
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


@allow_storage
@dataclass
class Request:
    # question + machine-checkable resolution rubric (both natural language)
    question: str
    criteria: str
    # JSON-encoded list[str] of allowed source URLs the resolver may read
    sources_json: str
    # JSON-encoded list[str] of canonical answer tokens, e.g. ["YES","NO"]
    answer_space_json: str
    bond: u256
    challenge_window: int          # seconds a proposal can be disputed
    status: int
    proposer: Address              # defaults to creator; set on propose
    disputer: Address              # defaults to creator; set on dispute
    proposed_answer: str
    final_answer: str
    proposed_at: int               # unix seconds
    created_by: Address


class OptimisticOracle(gl.Contract):
    requests: TreeMap[int, Request]
    credits: TreeMap[Address, u256]     # pull-payment ledger
    next_id: int
    owner: Address

    def __init__(self):
        self.next_id = 0
        self.owner = gl.message.sender_address

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #
    def _get(self, request_id: int) -> Request:
        _require(request_id in self.requests, "no such request")
        return self.requests[request_id]

    def _credit(self, addr: Address, amount: int) -> None:
        prior = int(self.credits[addr]) if addr in self.credits else 0
        self.credits[addr] = u256(prior + amount)

    # ------------------------------------------------------------------ #
    # requests
    # ------------------------------------------------------------------ #
    @gl.public.write
    def create_request(
        self,
        question: str,
        criteria: str,
        sources: list[str],
        answer_space: list[str],
        bond: int,
        challenge_window_seconds: int,
    ) -> int:
        """
        Register a new resolvable question. Permissionless.

        `answer_space` should be a small set of short, canonical tokens
        (they are upper-cased and de-duplicated here). `criteria` is the
        human-readable rubric validators apply when a dispute is resolved.
        """
        _require(len(question) > 0, "empty question")
        _require(len(sources) > 0, "need at least one source")
        _require(bond > 0, "bond must be positive")
        _require(challenge_window_seconds > 0, "window must be positive")

        space: list[str] = []
        for a in answer_space:
            tok = a.strip().upper()
            _require(len(tok) > 0, "empty answer token")
            _require(tok != UNKNOWN, "UNKNOWN is reserved")
            if tok not in space:
                space.append(tok)
        _require(len(space) >= 2, "answer_space needs >= 2 options")

        rid = self.next_id
        self.next_id = rid + 1
        creator = gl.message.sender_address

        self.requests[rid] = Request(
            question=question,
            criteria=criteria,
            sources_json=json.dumps([str(s) for s in sources]),
            answer_space_json=json.dumps(space),
            bond=u256(bond),
            challenge_window=int(challenge_window_seconds),
            status=OPEN,
            proposer=creator,
            disputer=creator,
            proposed_answer="",
            final_answer="",
            proposed_at=0,
            created_by=creator,
        )

        gl.advanced.emit_raw_event(
            "RequestCreated",
            ["request_id", "creator"],
            {"request_id": rid, "creator": creator, "bond": int(bond)},
        )
        return rid

    @gl.public.write.payable
    def propose_answer(self, request_id: int, answer: str) -> None:
        """
        Optimistically assert the answer, backing it with the exact bond.
        No LLM/web work happens here — this is the cheap happy path.
        """
        rec = self._get(request_id)
        _require(rec.status == OPEN, "request not open")

        space = set(json.loads(rec.answer_space_json))
        tok = answer.strip().upper()
        _require(tok in space, "answer not in answer_space")
        _require(int(gl.message.value) == int(rec.bond), "must post exact bond")

        rec.proposer = gl.message.sender_address
        rec.proposed_answer = tok
        rec.proposed_at = _now_ts()
        rec.status = PROPOSED

        gl.advanced.emit_raw_event(
            "AnswerProposed",
            ["request_id", "proposer"],
            {"request_id": request_id, "proposer": rec.proposer, "answer": tok},
        )

    @gl.public.write
    def finalize(self, request_id: int) -> None:
        """
        Settle an unchallenged proposal after its window elapses.
        Proposer's bond is returned; the proposed answer becomes canonical.
        """
        rec = self._get(request_id)
        _require(rec.status == PROPOSED, "nothing to finalize")
        _require(
            _now_ts() >= rec.proposed_at + rec.challenge_window,
            "challenge window still open",
        )

        rec.final_answer = rec.proposed_answer
        rec.status = FINALIZED
        self._credit(rec.proposer, int(rec.bond))  # refund proposer

        gl.advanced.emit_raw_event(
            "AnswerFinalized",
            ["request_id"],
            {"request_id": request_id, "answer": rec.final_answer, "disputed": False},
        )

    @gl.public.write.payable
    def dispute(self, request_id: int) -> None:
        """
        Challenge a live proposal (exact matching bond required). This triggers
        consensus resolution over the web in the SAME transaction:

          * every validator fetches the allowed sources,
          * the leader emits a canonical verdict token,
          * validators approve iff that token is the correct, evidence-backed
            answer under the request's criteria (non-comparative principle).

        Bond settlement:
          * verdict == proposed_answer  -> proposer was right, wins both bonds
          * verdict is another token     -> disputer was right, wins both bonds
          * verdict == UNKNOWN           -> inconclusive, both bonds refunded
        """
        rec = self._get(request_id)
        _require(rec.status == PROPOSED, "request not disputable")
        _require(
            _now_ts() < rec.proposed_at + rec.challenge_window,
            "challenge window closed",
        )
        _require(int(gl.message.value) == int(rec.bond), "must match bond")

        rec.disputer = gl.message.sender_address

        # --- copy everything the resolver needs into plain locals BEFORE the
        #     non-deterministic block. Storage is not readable inside it. ---
        question = str(rec.question)
        criteria = str(rec.criteria)
        sources = [str(u) for u in json.loads(rec.sources_json)]
        space = [str(a) for a in json.loads(rec.answer_space_json)]
        allowed = set(space) | {UNKNOWN}
        options_str = ", ".join(space)

        def collect_evidence() -> str:
            # Fetched inside the eq-principle block so validators gather their
            # own evidence. Failures are recorded as text rather than raised so
            # a single dead link cannot desync the validator set.
            parts: list[str] = []
            for url in sources:
                try:
                    page = gl.get_webpage(url, mode="text")
                except Exception as exc:  # noqa: BLE001 - want any fetch failure
                    page = f"[FETCH_ERROR for {url}: {exc}]"
                parts.append(f"SOURCE: {url}\n{str(page)[:8000]}")
            return "\n\n---\n\n".join(parts)

        task = (
            "You are the resolver for a prediction/claim oracle.\n"
            f"QUESTION: {question}\n"
            f"RESOLUTION CRITERIA: {criteria}\n"
            "Use ONLY the evidence provided as input. Decide the outcome and "
            f"respond with EXACTLY ONE token, uppercase, no other text: {options_str}. "
            f"If the evidence is missing or contradictory, respond with {UNKNOWN}."
        )
        check = (
            f"The response is exactly one token from: {options_str}, {UNKNOWN}. "
            f"It is the correct outcome for the question under the stated criteria "
            "and is supported by the evidence. Reject any answer that is the wrong "
            "token, unsupported by the evidence, or contains extra text."
        )

        verdict = gl.eq_principle.prompt_non_comparative(
            collect_evidence, task=task, criteria=check
        )
        verdict = verdict.strip().upper()
        if verdict not in allowed:
            verdict = UNKNOWN

        # --- settle bonds (deterministic post-processing of the verdict) ---
        bond = int(rec.bond)
        rec.final_answer = verdict
        rec.status = FINALIZED

        if verdict == UNKNOWN:
            self._credit(rec.proposer, bond)   # refund both
            self._credit(rec.disputer, bond)
        elif verdict == rec.proposed_answer:
            self._credit(rec.proposer, 2 * bond)   # proposer keeps both
        else:
            self._credit(rec.disputer, 2 * bond)   # disputer takes both

        gl.advanced.emit_raw_event(
            "AnswerFinalized",
            ["request_id"],
            {"request_id": request_id, "answer": verdict, "disputed": True},
        )

    # ------------------------------------------------------------------ #
    # payouts
    # ------------------------------------------------------------------ #
    @gl.public.write
    def withdraw(self) -> None:
        """Pull accrued credits (bonds won or refunded) to the caller."""
        who = gl.message.sender_address
        amount = int(self.credits[who]) if who in self.credits else 0
        _require(amount > 0, "nothing to withdraw")
        self.credits[who] = u256(0)          # effects before interaction
        gl.ContractAt(who).emit_transfer(value=amount)

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    @gl.public.view
    def get_answer(self, request_id: int) -> dict:
        """Consumer-facing read: is it final, and what is the answer?"""
        rec = self._get(request_id)
        return {
            "status": rec.status,
            "finalized": rec.status == FINALIZED,
            "answer": rec.final_answer,
        }

    @gl.public.view
    def get_request(self, request_id: int) -> dict:
        rec = self._get(request_id)
        return {
            "question": rec.question,
            "criteria": rec.criteria,
            "sources": json.loads(rec.sources_json),
            "answer_space": json.loads(rec.answer_space_json),
            "bond": int(rec.bond),
            "challenge_window": rec.challenge_window,
            "status": rec.status,
            "proposed_answer": rec.proposed_answer,
            "final_answer": rec.final_answer,
            "proposed_at": rec.proposed_at,
        }

    @gl.public.view
    def get_request_count(self) -> int:
        return self.next_id

    @gl.public.view
    def credit_of(self, addr: Address) -> int:
        return int(self.credits[addr]) if addr in self.credits else 0
