"""The rule language of the research lab (D-085): a rule is data, built from allowed blocks.

    {"entry":   {"type": "pullback", "minutes": 5, "trend_minutes": 120},
     "filters": [{"type": "hours", "from": 7, "to": 17},
                 {"type": "htf_trend", "minutes": 60, "fast": 8, "slow": 21, "mode": "with"}],
     "exit":    {"stop_atr": 4.0, "target_atr": 8.0, "trail_atr": 1.5, "trail_after_r": 2.0}}

`validate` refuses anything outside the blocks and their ranges (and outside the owner's stop
limits), `compile_rule` turns a valid rule into a strategy plus the backtester's exit options, and
`describe` says it in plain words. A research agent can only ever hand over such a document, so it
cannot write code, read a price that is not yet known, or touch anything but its own proposal.

Every filter is causal: it uses bars that have closed. Higher-timeframe values are those of the
last *completed* bar. `prepare` adds the columns the filters need to a whole frame once, so a rule
tested on a slice of history is not starved of warm-up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import numpy as np

from goldtrader.backtest.strategies import build_strategy

# entry type -> (strategy kind in backtest/strategies.py, {param: (lo, hi, is_int)})
ENTRIES: dict[str, tuple[str, dict[str, tuple[float, float, bool]]]] = {
    "trend_flip": ("trend_atr", {}),
    "trend_bars": (
        "trend_htf",
        {
            "minutes": (5, 240, True),
            "ema_fast": (5, 50, True),
            "ema_slow": (20, 200, True),
            "on_cross": (0, 1, True),
        },
    ),  # fmt: skip
    "breakout": ("channel_break", {"minutes": (5, 240, True), "lookback": (10, 200, True)}),
    "pullback": ("pullback_htf", {"minutes": (5, 60, True), "trend_minutes": (30, 480, True)}),
    "fade": (
        "range_fade",
        {"minutes": (5, 120, True), "lookback": (10, 100, True), "z_entry": (1.5, 4.0, False)},
    ),
    "session_range": (
        "breakout_session",
        {
            "range_start_hour": (0, 20, True),
            "range_end_hour": (1, 22, True),
            "trade_start_hour": (1, 22, True),
            "trade_end_hour": (2, 23, True),
        },
    ),  # fmt: skip
}
BAR_SIZES = (5, 15, 30, 60, 120, 240)
FILTERS: dict[str, dict[str, tuple[float, float, bool]]] = {
    "htf_trend": {"minutes": (15, 480, True), "fast": (3, 50, True), "slow": (10, 200, True)},
    "hours": {"from": (0, 23, True), "to": (1, 24, True)},
    "vol": {"min_pctile": (0.0, 0.9, False), "max_pctile": (0.1, 1.0, False)},
    "side": {},
    "range_pos": {"lookback_minutes": (30, 2880, True), "max": (0.3, 0.95, False)},
    "weekday": {},
}
EXIT_RANGES: dict[str, tuple[float, float]] = {
    "stop_atr": (0.5, 10.0),
    "target_atr": (0.5, 40.0),
    "trail_atr": (0.5, 10.0),
    "trail_after_r": (0.0, 5.0),
    "breakeven_at_r": (0.3, 5.0),
}
MAX_FILTERS = 3


class RuleError(ValueError):
    pass


def _num(block: str, name: str, value: Any, lo: float, hi: float, is_int: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError(f"{block}.{name} must be a number")
    v = float(value)
    if not lo <= v <= hi:
        raise RuleError(f"{block}.{name} must be between {lo:g} and {hi:g}")
    if is_int and v != int(v):
        raise RuleError(f"{block}.{name} must be a whole number")
    return int(v) if is_int else round(v, 4)


def validate(spec: Any, stop_atr_max: float = 10.0) -> dict[str, Any]:
    """The rule in canonical form, or RuleError saying what is wrong with it."""
    if not isinstance(spec, dict) or not isinstance(spec.get("entry"), dict):
        raise RuleError("a rule needs an entry block")
    entry = dict(spec["entry"])
    etype = entry.pop("type", None)
    if etype not in ENTRIES:
        raise RuleError(f"entry.type must be one of {sorted(ENTRIES)}")
    allowed = ENTRIES[etype][1]
    unknown = set(entry) - set(allowed)
    if unknown:
        raise RuleError(f"entry has settings this entry type does not take: {sorted(unknown)}")
    out_entry: dict[str, Any] = {"type": etype}
    for name, (lo, hi, is_int) in allowed.items():
        if name in entry:
            out_entry[name] = _num("entry", name, entry[name], lo, hi, is_int)
    if etype == "trend_bars" and out_entry.get("ema_fast", 20) >= out_entry.get("ema_slow", 50):
        raise RuleError("entry.ema_fast must be below entry.ema_slow")
    if etype == "session_range":
        rs, re_ = out_entry.get("range_start_hour", 0), out_entry.get("range_end_hour", 7)
        ts, te = out_entry.get("trade_start_hour", 7), out_entry.get("trade_end_hour", 12)
        if not (rs < re_ <= ts < te):
            raise RuleError(
                "session_range needs range start < range end <= trade start < trade end"
            )

    filters = spec.get("filters") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise RuleError(f"filters must be a list of at most {MAX_FILTERS}")
    out_filters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in filters:
        if not isinstance(f, dict) or f.get("type") not in FILTERS:
            raise RuleError(f"filter.type must be one of {sorted(FILTERS)}")
        ftype = f["type"]
        if ftype in seen:
            raise RuleError(f"filter {ftype} is used twice")
        seen.add(ftype)
        g: dict[str, Any] = {"type": ftype}
        extra = set(f) - set(FILTERS[ftype]) - {"type", "mode", "only", "days"}
        if extra:
            raise RuleError(f"filter {ftype} does not take {sorted(extra)}")
        for name, (lo, hi, is_int) in FILTERS[ftype].items():
            if name not in f:
                raise RuleError(f"filter {ftype} needs {name}")
            g[name] = _num(ftype, name, f[name], lo, hi, is_int)
        if ftype == "htf_trend":
            if g["fast"] >= g["slow"]:
                raise RuleError("htf_trend.fast must be below slow")
            if f.get("mode", "with") not in ("with", "against"):
                raise RuleError("htf_trend.mode must be with or against")
            g["mode"] = f.get("mode", "with")
        elif ftype == "hours" and g["from"] >= g["to"]:
            raise RuleError("hours.from must be below hours.to (UTC, no wrap past midnight)")
        elif ftype == "vol" and g["min_pctile"] >= g["max_pctile"]:
            raise RuleError("vol.min_pctile must be below max_pctile")
        elif ftype == "side":
            if f.get("only") not in ("long", "short"):
                raise RuleError("side.only must be long or short")
            g["only"] = f["only"]
        elif ftype == "weekday":
            days = f.get("days")
            if (
                not isinstance(days, list)
                or not days
                or any(isinstance(d, bool) or d not in (0, 1, 2, 3, 4, 6) for d in days)
            ):
                raise RuleError("weekday.days must list days 0 (Mon) to 4 (Fri), or 6 (Sun)")
            g["days"] = sorted(set(days))
        out_filters.append(g)
    out_filters.sort(key=lambda g: g["type"])

    ex = spec.get("exit")
    if not isinstance(ex, dict):
        raise RuleError("a rule needs an exit block")
    unknown = set(ex) - set(EXIT_RANGES) - {"use_target", "on_opposite"}
    if unknown:
        raise RuleError(f"exit does not take {sorted(unknown)}")
    out_exit: dict[str, Any] = {}
    for name in ("stop_atr", "target_atr"):
        if name not in ex:
            raise RuleError(f"exit needs {name}")
    for name, (lo, hi) in EXIT_RANGES.items():
        if ex.get(name) is not None:
            out_exit[name] = _num("exit", name, ex[name], lo, hi, False)
    if out_exit["stop_atr"] > stop_atr_max:
        raise RuleError(f"exit.stop_atr is above the owner's limit of {stop_atr_max:g}")
    if out_exit["target_atr"] < out_exit["stop_atr"]:
        raise RuleError("exit.target_atr must be at least the stop (reward at least equal to risk)")
    if "trail_after_r" in out_exit and "trail_atr" not in out_exit:
        raise RuleError("exit.trail_after_r needs trail_atr")
    if ex.get("use_target") is False:
        if "trail_atr" not in out_exit:
            raise RuleError("exit.use_target false needs a trailing stop to close the trade")
        out_exit["use_target"] = False
    if ex.get("on_opposite", "hold") not in ("hold", "close", "reverse"):
        raise RuleError("exit.on_opposite must be hold, close or reverse")
    if ex.get("on_opposite", "hold") != "hold":
        out_exit["on_opposite"] = ex["on_opposite"]
    return {"entry": out_entry, "filters": out_filters, "exit": out_exit}


def key(spec: dict[str, Any]) -> str:
    """A short stable id of a validated rule: the same rule always gets the same key."""
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ causal filter columns
def _col(f: dict[str, Any]) -> str | None:
    if f["type"] == "htf_trend":
        return f"lab_tr_{f['minutes']}_{f['fast']}_{f['slow']}"
    if f["type"] == "range_pos":
        return f"lab_rp_{f['lookback_minutes']}"
    return None


def _column(df: Any, f: dict[str, Any]) -> np.ndarray:
    if f["type"] == "htf_trend":
        span = f["minutes"] * 60
        bucket = (df["time"] // span) * span
        bars = df["mid_close"].groupby(bucket).last()
        up = (
            bars.ewm(span=f["fast"], adjust=False).mean()
            > bars.ewm(span=f["slow"], adjust=False).mean()
        ).astype(float)
        up.iloc[: f["slow"] * 2] = np.nan  # too little history to call a trend
        prev = up.shift(1)  # the last completed bar, never the one still forming
        return bucket.map(prev).fillna(-1).astype(int).to_numpy()
    n = f["lookback_minutes"]
    hi = df["mid_high"].rolling(n, min_periods=n).max()
    lo = df["mid_low"].rolling(n, min_periods=n).min()
    pos = (df["mid_close"] - lo) / (hi - lo).replace(0, np.nan)
    return pos.fillna(-1.0).to_numpy()


def prepare(df: Any, spec: dict[str, Any]) -> Any:
    """`df` with the columns this rule's filters need, computed over the whole frame."""
    need = [(c, f) for f in spec["filters"] if (c := _col(f)) and c not in df.columns]
    if not need:
        return df
    out = df.copy()
    for c, f in need:
        out[c] = _column(out, f)
    return out


class Composed:
    """An entry rule with filters applied to its signals before the backtester sees them."""

    kind = "composed"

    def __init__(self, spec: dict[str, Any]) -> None:
        e = dict(spec["entry"])
        kind = ENTRIES[e.pop("type")][0]
        self.spec = spec
        self.id = key(spec)
        self.inner = build_strategy(
            kind,
            self.id,
            {**e, "stop_atr": spec["exit"]["stop_atr"], "target_atr": spec["exit"]["target_atr"]},
        )

    def signals(self, df: Any) -> Any:
        sig = self.inner.signals(df)
        side = sig.side.astype(np.int8).copy()
        for f in self.spec["filters"]:
            t = f["type"]
            if t == "side":
                side[(side > 0) if f["only"] == "short" else (side < 0)] = 0
            elif t == "hours":
                h = df["hour_utc"].to_numpy()
                side[~((h >= f["from"]) & (h < f["to"]))] = 0
            elif t == "vol":
                v = df["atr_pctile"].to_numpy()
                ok = (v >= f["min_pctile"]) & (v <= f["max_pctile"])
                side[~ok] = 0
            elif t == "weekday":
                wd = ((df["time"].to_numpy() // 86400) + 3) % 7  # 1970-01-01 was a Thursday
                side[~np.isin(wd, f["days"])] = 0
            elif t == "htf_trend":
                c = _col(f)
                up = df[c].to_numpy() if c in df.columns else _column(df, f)
                agree = ((side > 0) & (up == 1)) | ((side < 0) & (up == 0))
                keep = agree if f["mode"] == "with" else (~agree & (up >= 0))
                side[~keep] = 0
            elif t == "range_pos":
                c = _col(f)
                pos = df[c].to_numpy() if c in df.columns else _column(df, f)
                # do not buy in the top part of the recent range or sell in the bottom part
                ok = ((side > 0) & (pos >= 0) & (pos <= f["max"])) | (
                    (side < 0) & (pos >= 1 - f["max"])
                )
                side[~ok] = 0
        return replace(sig, side=side)


def compile_rule(spec: dict[str, Any]) -> tuple[Composed, dict[str, Any]]:
    """(strategy, keyword options for `run_backtest`) for a validated rule."""
    ex = spec["exit"]
    opts: dict[str, Any] = {}
    if "trail_atr" in ex:
        opts["trail_atr"] = ex["trail_atr"]
        opts["trail_after_r"] = ex.get("trail_after_r", 0.0)
    if "breakeven_at_r" in ex:
        opts["breakeven_at_r"] = ex["breakeven_at_r"]
    if ex.get("use_target") is False:
        opts["use_target"] = False
    if "on_opposite" in ex:
        opts["on_opposite"] = ex["on_opposite"]
    return Composed(spec), opts


# ------------------------------------------------------------------ plain words
_ENTRY_WORDS = {
    "trend_flip": lambda e: "enter when the 1-minute trend turns",
    "trend_bars": lambda e: (
        f"enter with the trend on {e.get('minutes', 15)}-minute bars "
        f"(averages {e.get('ema_fast', 20)} and {e.get('ema_slow', 50)}"
        + (", only at the cross)" if e.get("on_cross", 1) else ", while it lasts)")
    ),
    "breakout": lambda e: (
        f"enter when a {e.get('minutes', 15)}-minute bar closes beyond the last "
        f"{e.get('lookback', 20)} bars"
    ),
    "pullback": lambda e: (
        f"enter after a pullback on {e.get('minutes', 15)}-minute bars in the direction of the "
        f"{e.get('trend_minutes', 60)}-minute trend"
    ),
    "fade": lambda e: (
        f"trade against a {e.get('minutes', 15)}-minute close more than {e.get('z_entry', 2.0):g} "
        f"standard deviations from its {e.get('lookback', 20)}-bar average"
    ),
    "session_range": lambda e: (
        f"enter on the first close outside the {e.get('range_start_hour', 0):02d}:00 to "
        f"{e.get('range_end_hour', 7):02d}:00 UTC range, between "
        f"{e.get('trade_start_hour', 7):02d}:00 and {e.get('trade_end_hour', 12):02d}:00"
    ),
}
_DAYS = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 6: "Sun"}


def describe(spec: dict[str, Any]) -> str:
    """One or two plain sentences for the owner."""
    e, ex = spec["entry"], spec["exit"]
    parts = [_ENTRY_WORDS[e["type"]](e)[0].upper() + _ENTRY_WORDS[e["type"]](e)[1:]]
    conds = []
    for f in spec["filters"]:
        t = f["type"]
        if t == "side":
            conds.append(f"{f['only']} trades only")
        elif t == "hours":
            conds.append(f"between {f['from']:02d}:00 and {f['to']:02d}:00 UTC")
        elif t == "vol":
            conds.append(
                f"volatility between the {round(100 * f['min_pctile'])}th and "
                f"{round(100 * f['max_pctile'])}th percentile"
            )
        elif t == "weekday":
            conds.append("on " + ", ".join(_DAYS[d] for d in f["days"]))
        elif t == "htf_trend":
            conds.append(f"{f['mode']} the {f['minutes']}-minute trend ({f['fast']}/{f['slow']})")
        elif t == "range_pos":
            conds.append(
                f"not in the outer {round(100 * (1 - f['max']))}% of the last "
                f"{f['lookback_minutes']} minutes' range"
            )
    if conds:
        parts.append("only " + "; ".join(conds))
    out = f"stop {ex['stop_atr']:g} x ATR, target {ex['target_atr']:g} x ATR"
    if ex.get("use_target") is False:
        out = f"stop {ex['stop_atr']:g} x ATR, no fixed target"
    if "trail_atr" in ex:
        out += f", trailing stop {ex['trail_atr']:g} x ATR after {ex.get('trail_after_r', 0):g} R"
    if "breakeven_at_r" in ex:
        out += f", stop to entry at {ex['breakeven_at_r']:g} R"
    if ex.get("on_opposite"):
        out += f", {ex['on_opposite']} on an opposite signal"
    return ", ".join(parts) + ". " + out[0].upper() + out[1:] + "."


def grammar() -> dict[str, Any]:
    """The blocks and ranges, for the research agent's instructions."""
    return {
        "entry": {k: {p: [lo, hi] for p, (lo, hi, _i) in v[1].items()} for k, v in ENTRIES.items()},
        "filters": {
            "htf_trend": {
                **{p: [lo, hi] for p, (lo, hi, _i) in FILTERS["htf_trend"].items()},
                "mode": ["with", "against"],
            },
            "hours": {"from": [0, 23], "to": [1, 24]},
            "vol": {"min_pctile": [0.0, 0.9], "max_pctile": [0.1, 1.0]},
            "side": {"only": ["long", "short"]},
            "range_pos": {"lookback_minutes": [30, 2880], "max": [0.3, 0.95]},
            "weekday": {"days": "list of 0 (Mon) .. 4 (Fri), 6 (Sun)"},
        },  # fmt: skip
        "exit": {
            **{k: list(v) for k, v in EXIT_RANGES.items()},
            "use_target": [True, False],
            "on_opposite": ["hold", "close", "reverse"],
        },
        "max_filters": MAX_FILTERS,
    }
