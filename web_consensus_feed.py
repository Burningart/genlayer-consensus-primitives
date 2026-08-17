# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
#
# WebConsensusFeed — a reusable numeric data-feed primitive for GenLayer.
# ---------------------------------------------------------------------------
# WHAT THIS IS
#   An on-chain feed that reads a real-world *number* (a price, an index, a
#   count, a rate...) from a set of human-described web sources and stores a
#   canonical fixed-point value that other contracts can consume. Think of it
#   as a decentralized price/metric feed, but the extraction logic is a natural
#   language description rather than a bespoke per-source scraper — so adding a
#   new feed is a governance action, not a code deploy.
#
# WHY IT NEEDS GENLAYER
#   Reading a number off arbitrary web pages is inherently non-deterministic:
#   pages differ per fetch, and an LLM is needed to interpret "the current 30-day
#   SOFR average" or "total open GitHub issues in repo X". A classic chain can't
#   agree on such a value. GenLayer can — provided we tell validators *how much
#   they are allowed to disagree*.
#
# HOW CONSENSUS IS USED (the interesting part)
#   Updates use `gl.eq_principle.prompt_comparative`. Unlike the oracle's
#   non-comparative judgement, here EVERY validator independently performs the
#   full job (fetch sources -> extract the number) and the leader's result is
#   accepted only if each validator's own number agrees with it. Agreement is
#   NOT byte-equality — that would fail on the last decimal. Instead the
#   `principle` string encodes an explicit tolerance band (in basis points), so
#   the network reaches consensus on "the same metric, within X%". This is the
#   canonical pattern for quantitative feeds and complements strict_eq (too
#   rigid for live numbers) and non-comparative judgement (for subjective calls).
#
# STORAGE / OUTPUT MODEL
#   Values are stored as integers scaled by 10**decimals (fixed-point), the way
#   on-chain price feeds do it, so downstream deterministic contracts never
#   touch a float. A short rolling history is kept for sanity checks / charts.

from genlayer import *

import json
import datetime
from dataclasses import dataclass

MAX_HISTORY = 20


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise Exception(msg)


def _now_ts() -> int:
    raw = str(gl.message_raw["datetime"])
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _parse_json_object(text: str) -> dict:
    """
    Tolerant JSON-object extraction. The comparative principle already forces
    the leader/validators to agree on the *number*; this just salvages the
    canonical string if a model wraps it in stray prose.
    """
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    _require(start != -1 and end > start, "no JSON object in model output")
    obj = json.loads(text[start : end + 1])
    _require(isinstance(obj, dict), "model output is not an object")
    return obj


@allow_storage
@dataclass
class Feed:
    description: str          # natural-language spec of what to read
    sources_json: str         # JSON list[str] of source URLs
    unit: str                 # e.g. "USD", "issues", "%"
    tolerance_bps: int        # allowed cross-validator deviation, basis points
    decimals: int             # fixed-point scale (value stored * 10**decimals)
    latest_value: int         # scaled integer; 0 before first successful update
    updated_at: int           # unix seconds of last update; 0 if never
    update_count: int
    history_json: str         # JSON list of [ts, scaled_value], newest last
    active: bool


class WebConsensusFeed(gl.Contract):
    feeds: TreeMap[str, Feed]
    owner: Address

    def __init__(self):
        self.owner = gl.message.sender_address

    def _only_owner(self) -> None:
        _require(gl.message.sender_address == self.owner, "owner only")

    def _get(self, label: str) -> Feed:
        _require(label in self.feeds, "no such feed")
        return self.feeds[label]

    # ------------------------------------------------------------------ #
    # administration
    # ------------------------------------------------------------------ #
    @gl.public.write
    def register_feed(
        self,
        label: str,
        description: str,
        sources: list[str],
        unit: str,
        tolerance_bps: int,
        decimals: int,
    ) -> None:
        """Register a new feed (owner only). Updates are then permissionless."""
        self._only_owner()
        _require(len(label) > 0, "empty label")
        _require(label not in self.feeds, "feed exists")
        _require(len(sources) > 0, "need at least one source")
        _require(0 < tolerance_bps <= 10000, "tolerance_bps in (0, 10000]")
        _require(0 <= decimals <= 18, "decimals in [0, 18]")

        self.feeds[label] = Feed(
            description=description,
            sources_json=json.dumps([str(s) for s in sources]),
            unit=unit,
            tolerance_bps=int(tolerance_bps),
            decimals=int(decimals),
            latest_value=0,
            updated_at=0,
            update_count=0,
            history_json="[]",
            active=True,
        )
        gl.advanced.emit_raw_event(
            "FeedRegistered", ["label"], {"label": label, "unit": unit}
        )

    @gl.public.write
    def set_active(self, label: str, active: bool) -> None:
        self._only_owner()
        rec = self._get(label)
        rec.active = active

    # ------------------------------------------------------------------ #
    # updates (consensus)
    # ------------------------------------------------------------------ #
    @gl.public.write
    def update_feed(self, label: str) -> None:
        """
        Refresh a feed's value. Permissionless — anyone can pay to refresh.

        The comparative equivalence principle makes each validator fetch and
        extract independently, accepting the leader's number only within the
        feed's tolerance band.
        """
        rec = self._get(label)
        _require(rec.active, "feed inactive")

        # copy into locals before the non-deterministic block
        description = str(rec.description)
        unit = str(rec.unit)
        tolerance_bps = int(rec.tolerance_bps)
        decimals = int(rec.decimals)
        sources = [str(u) for u in json.loads(rec.sources_json)]

        def extract() -> str:
            parts: list[str] = []
            for url in sources:
                try:
                    page = gl.get_webpage(url, mode="text")
                except Exception as exc:  # noqa: BLE001
                    page = f"[FETCH_ERROR for {url}: {exc}]"
                parts.append(f"SOURCE {url}:\n{str(page)[:8000]}")
            evidence = "\n\n".join(parts)
            prompt = (
                "You are a numeric data oracle. Extract ONE current value.\n"
                f"METRIC: {description}\n"
                f"UNIT: {unit}\n"
                "Return STRICT JSON and nothing else, of the exact form:\n"
                f'{{"value": <number>, "unit": "{unit}"}}\n'
                "Use a plain decimal number for value (no thousands separators, "
                "no currency symbols). If the metric cannot be found, use null.\n\n"
                f"EVIDENCE:\n{evidence}"
            )
            out = gl.exec_prompt(prompt)
            if isinstance(out, dict):
                out = json.dumps(out)
            return str(out)

        principle = (
            f"Both answers report the metric '{description}' in unit '{unit}'. "
            f"Their numeric 'value' fields must agree within {tolerance_bps} basis "
            f"points (a relative difference of at most {tolerance_bps / 100.0}%). "
            "Differences in formatting, wording, or insignificant digits are fine. "
            "If either value is null/missing, they are NOT equivalent."
        )

        leader_out = gl.eq_principle.prompt_comparative(extract, principle=principle)

        obj = _parse_json_object(leader_out)
        _require("value" in obj and obj["value"] is not None, "metric unavailable")
        value = float(obj["value"])
        scaled = int(round(value * (10 ** decimals)))

        ts = _now_ts()
        rec.latest_value = scaled
        rec.updated_at = ts
        rec.update_count = rec.update_count + 1

        history = json.loads(rec.history_json)
        history.append([ts, scaled])
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        rec.history_json = json.dumps(history)

        gl.advanced.emit_raw_event(
            "FeedUpdated",
            ["label"],
            {"label": label, "value": scaled, "decimals": decimals, "ts": ts},
        )

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    @gl.public.view
    def get_value(self, label: str) -> int:
        """Latest value as a fixed-point integer (scaled by 10**decimals)."""
        rec = self._get(label)
        _require(rec.updated_at > 0, "feed has no value yet")
        return rec.latest_value

    @gl.public.view
    def get_feed(self, label: str) -> dict:
        rec = self._get(label)
        return {
            "description": rec.description,
            "sources": json.loads(rec.sources_json),
            "unit": rec.unit,
            "tolerance_bps": rec.tolerance_bps,
            "decimals": rec.decimals,
            "latest_value": rec.latest_value,
            "updated_at": rec.updated_at,
            "update_count": rec.update_count,
            "active": rec.active,
        }

    @gl.public.view
    def get_history(self, label: str) -> list:
        rec = self._get(label)
        return json.loads(rec.history_json)
