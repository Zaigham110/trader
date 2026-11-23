"""Plain-language descriptions of every strategy kind, for the dashboard's Strategies page (D-083).

Pure data: this module imports nothing, so the dashboard process can load it without loading any
trading code. `tests/test_strategy_docs.py` fails when a kind or a parameter accepted by
`backtest/strategies.py:build_strategy` is missing here, so the page cannot drift from the code.

A diagram is a schematic, not real data: `line` is a price path on a 0-100 by 0-100 grid (y up),
`levels` are horizontal guide lines and `marks` are labelled points on the path.
"""

STOP = {
    "label": "Stop distance",
    "unit": "x ATR",
    "meaning": (
        "How far from the entry the protective stop is placed, as a multiple of the average "
        "1-minute price range (ATR) at that moment. A losing trade that hits it costs 1 R."
    ),
}
TARGET = {
    "label": "Target distance",
    "unit": "x ATR",
    "meaning": (
        "How far from the entry the profit target is placed, as a multiple of the same ATR. "
        "Target divided by stop is the reward for each unit of risk."
    ),
}
MINUTES = {
    "label": "Bar size",
    "unit": "minutes",
    "meaning": (
        "The rule looks only at completed bars of this length. Longer bars mean fewer, slower "
        "signals; the order itself is still placed on the next 1-minute bar."
    ),
}

DOCS = {
    "trend_atr": {
        "name": "Trend flip (1-minute)",
        "family": "Trend following",
        "summary": (
            "Follows the short-term trend. It buys when the trend turns up and sells when it "
            "turns down, and it acts only at the moment of the turn."
        ),
        "enters": (
            "Two moving averages of the price are compared, a fast one and a slow one. The trend "
            "is up when the fast average is above the slow one and the price is above the fast "
            "average; down is the mirror image. A trade is opened on the 1-minute bar where that "
            "state flips, in the new direction."
        ),
        "exits": (
            "At the stop, at the target, by the trailing stop once the trade is far enough in "
            "profit, or before the daily market break. An opposite signal does not close it."
        ),
        "works_when": "The price keeps moving in one direction for an hour or more after a turn.",
        "struggles_when": (
            "The price chops sideways: the trend flips back and forth and the stop is hit "
            "several times in a row."
        ),
        "params": {
            "stop_atr": STOP,
            "target_atr": TARGET,
            "ema_fast": {
                "label": "Fast average",
                "unit": "bars",
                "meaning": "Length of the fast moving average. Shorter reacts sooner to a turn.",
            },
            "ema_slow": {
                "label": "Slow average",
                "unit": "bars",
                "meaning": "Length of the slow moving average that defines the larger direction.",
            },
            "atr_period": {
                "label": "ATR length",
                "unit": "bars",
                "meaning": "How many 1-minute bars the average range (ATR) is measured over.",
            },
            "min_atr_pct": {
                "label": "Quiet-market filter",
                "unit": "% of price",
                "meaning": "No entry when the ATR is below this share of the price. 0 = off.",
            },
            "max_atr_pctile": {
                "label": "Stress filter",
                "unit": "0 to 1",
                "meaning": (
                    "No entry when volatility is above this percentile of its recent history. "
                    "1 = off."
                ),
            },
            "entry_on_flip_only": {
                "label": "Only at the turn",
                "unit": "1 = yes",
                "meaning": (
                    "1: enter only on the bar where the trend flips. 0: a signal on every bar "
                    "while the trend lasts."
                ),
            },
        },
        "diagram": {
            "line": [
                [0, 46],
                [10, 40],
                [20, 44],
                [30, 36],
                [40, 40],
                [48, 34],
                [56, 46],
                [64, 54],
                [72, 50],
                [82, 66],
                [92, 72],
                [100, 84],
            ],
            "levels": [
                {"y": 28, "text": "stop", "kind": "stop"},
                {"y": 90, "text": "target", "kind": "target"},
            ],
            "marks": [{"x": 56, "y": 46, "text": "trend turns up: buy", "kind": "entry"}],
        },
    },
    "trend_htf": {
        "name": "Trend on longer bars",
        "family": "Trend following",
        "summary": (
            "The same trend idea as the 1-minute rule, decided on longer bars (for example 15 or "
            "60 minutes) so that small wiggles do not trigger it."
        ),
        "enters": (
            "On completed bars of the chosen size it compares a fast and a slow moving average. "
            "With 'only at the cross' it enters when the fast one crosses the slow one; without "
            "it, it may enter whenever the trend state is in force and no trade is open."
        ),
        "exits": "At the stop, at the target, or before the daily market break.",
        "works_when": "Moves last for hours. It trades rarely and aims for large targets.",
        "struggles_when": "The market ranges for days: few signals, and those it takes go nowhere.",
        "params": {
            "minutes": MINUTES,
            "ema_fast": {
                "label": "Fast average",
                "unit": "bars",
                "meaning": "Fast moving average on the longer bars.",
            },
            "ema_slow": {
                "label": "Slow average",
                "unit": "bars",
                "meaning": "Slow moving average on the longer bars.",
            },
            "stop_atr": STOP,
            "target_atr": TARGET,
            "on_cross": {
                "label": "Only at the cross",
                "unit": "1 = yes",
                "meaning": "1: signal only when the averages cross. 0: signal while the trend state holds.",
            },
        },
        "diagram": {
            "line": [
                [0, 30],
                [14, 34],
                [28, 30],
                [40, 38],
                [52, 48],
                [64, 46],
                [76, 60],
                [88, 70],
                [100, 78],
            ],
            "levels": [
                {"y": 26, "text": "stop", "kind": "stop"},
                {"y": 92, "text": "target", "kind": "target"},
            ],
            "marks": [{"x": 40, "y": 38, "text": "averages cross: buy", "kind": "entry"}],
        },
    },
    "channel_break": {
        "name": "Channel breakout",
        "family": "Breakout",
        "summary": (
            "Waits for the price to close beyond the highest or lowest close of the recent past "
            "and trades in the direction of the break."
        ),
        "enters": (
            "On completed bars of the chosen size, it buys when a bar closes above the highest "
            "close of the previous N bars and sells when a bar closes below the lowest."
        ),
        "exits": "At the stop, at the target, or before the daily market break.",
        "works_when": "A quiet range ends with a real move that carries on.",
        "struggles_when": "Breaks fail and the price falls back into the range (a false breakout).",
        "params": {
            "minutes": MINUTES,
            "lookback": {
                "label": "Channel length",
                "unit": "bars",
                "meaning": "How many earlier bars define the channel. More bars = rarer, stronger breaks.",
            },
            "stop_atr": STOP,
            "target_atr": TARGET,
        },
        "diagram": {
            "line": [
                [0, 40],
                [10, 48],
                [20, 42],
                [30, 50],
                [40, 44],
                [50, 49],
                [60, 45],
                [70, 58],
                [82, 66],
                [100, 80],
            ],
            "levels": [
                {"y": 51, "text": "highest recent close", "kind": "guide"},
                {"y": 36, "text": "stop", "kind": "stop"},
            ],
            "marks": [{"x": 70, "y": 58, "text": "close above the channel: buy", "kind": "entry"}],
        },
    },
    "pullback_htf": {
        "name": "Pullback in the trend",
        "family": "Trend following",
        "summary": (
            "Trades with the larger trend, but waits for the price to dip back first, so the "
            "entry is closer to the stop than a breakout entry."
        ),
        "enters": (
            "The larger trend is read from moving averages on long bars (for example 120 "
            "minutes). In an up-trend it waits for the price on the shorter bars to pull back to "
            "its own moving average and then close back above it, and buys there. Down-trends "
            "are the mirror image."
        ),
        "exits": "At the stop, at the target, or before the daily market break.",
        "works_when": "A steady trend that pauses and resumes.",
        "struggles_when": "The pullback is in fact the start of a reversal.",
        "params": {
            "minutes": MINUTES,
            "trend_minutes": {
                "label": "Trend bar size",
                "unit": "minutes",
                "meaning": "Bar length on which the larger trend is judged.",
            },
            "ema_entry": {
                "label": "Pullback average",
                "unit": "bars",
                "meaning": "The average the price must pull back to.",
            },
            "trend_fast": {
                "label": "Trend fast average",
                "unit": "bars",
                "meaning": "Fast average on the trend bars.",
            },
            "trend_slow": {
                "label": "Trend slow average",
                "unit": "bars",
                "meaning": "Slow average on the trend bars.",
            },
            "stop_atr": STOP,
            "target_atr": TARGET,
        },
        "diagram": {
            "line": [
                [0, 20],
                [14, 34],
                [28, 48],
                [40, 58],
                [50, 50],
                [58, 46],
                [66, 54],
                [80, 68],
                [100, 86],
            ],
            "levels": [{"y": 38, "text": "stop", "kind": "stop"}],
            "marks": [
                {"x": 58, "y": 46, "text": "dip to the average", "kind": "guide"},
                {"x": 66, "y": 54, "text": "closes back up: buy", "kind": "entry"},
            ],
        },
    },
    "range_fade": {
        "name": "Range reversal",
        "family": "Mean reversion",
        "summary": (
            "Bets that a stretched price snaps back. It sells after an unusually strong rise and "
            "buys after an unusually strong fall."
        ),
        "enters": (
            "On completed bars it measures how far the close is from its recent average, in "
            "standard deviations. When that distance is beyond the chosen threshold it trades "
            "against the move."
        ),
        "exits": "At the stop, at the target, or before the daily market break.",
        "works_when": "The market is ranging and spikes are followed by a return to the middle.",
        "struggles_when": "A spike is the start of a trend: it keeps going and the stop is hit.",
        "params": {
            "minutes": MINUTES,
            "lookback": {
                "label": "Average length",
                "unit": "bars",
                "meaning": "How many bars the average and its spread are measured over.",
            },
            "z_entry": {
                "label": "Stretch threshold",
                "unit": "standard deviations",
                "meaning": "How far from the average the close must be before it trades against the move.",
            },
            "stop_atr": STOP,
            "target_atr": TARGET,
        },
        "diagram": {
            "line": [
                [0, 50],
                [12, 46],
                [24, 54],
                [36, 48],
                [48, 56],
                [58, 74],
                [66, 86],
                [76, 72],
                [88, 60],
                [100, 52],
            ],
            "levels": [
                {"y": 50, "text": "recent average", "kind": "guide"},
                {"y": 96, "text": "stop", "kind": "stop"},
            ],
            "marks": [{"x": 66, "y": 86, "text": "stretched too far: sell", "kind": "entry"}],
        },
    },
    "breakout_session": {
        "name": "Session range breakout",
        "family": "Breakout",
        "summary": (
            "Marks the high and low of a quiet part of the day and trades the first close "
            "outside that range during the busier hours that follow."
        ),
        "enters": (
            "Each day it records the price range between two hours (UTC). During the trading "
            "window after it, the first bar that closes above the range is bought and the first "
            "that closes below is sold. At most one trade a day."
        ),
        "exits": "At the stop, at the target, or before the daily market break.",
        "works_when": "The busy session picks a direction and keeps it.",
        "struggles_when": "The first break is a fake and the price reverses through the range.",
        "params": {
            "range_start_hour": {
                "label": "Range starts",
                "unit": "hour UTC",
                "meaning": "First hour of the quiet range.",
            },
            "range_end_hour": {
                "label": "Range ends",
                "unit": "hour UTC",
                "meaning": "Hour at which the range is fixed.",
            },
            "trade_start_hour": {
                "label": "Trading from",
                "unit": "hour UTC",
                "meaning": "A break counts only from this hour.",
            },
            "trade_end_hour": {
                "label": "Trading until",
                "unit": "hour UTC",
                "meaning": "No new entry after this hour.",
            },
            "stop_atr": STOP,
            "target_atr": TARGET,
        },
        "diagram": {
            "line": [
                [0, 46],
                [10, 52],
                [20, 44],
                [30, 50],
                [40, 45],
                [50, 51],
                [60, 48],
                [70, 60],
                [84, 70],
                [100, 82],
            ],
            "levels": [
                {"y": 53, "text": "range high", "kind": "guide"},
                {"y": 43, "text": "range low", "kind": "guide"},
            ],
            "marks": [{"x": 70, "y": 60, "text": "first close above: buy", "kind": "entry"}],
        },
    },
    "flat": {
        "name": "No trading",
        "family": "Placeholder",
        "summary": "Never gives a signal. Used when no strategy is enabled.",
        "enters": "Never.",
        "exits": "Nothing to exit.",
        "works_when": "",
        "struggles_when": "",
        "params": {},
        "diagram": {
            "line": [[0, 50], [25, 52], [50, 49], [75, 51], [100, 50]],
            "levels": [],
            "marks": [],
        },
    },
}

# How every strategy's trade is protected and closed, whatever its entry rule.
COMMON = {
    "atr": (
        "ATR is the average size of a 1-minute price bar over the last few minutes. Stops and "
        "targets are set as multiples of it, so they widen when the market is moving fast and "
        "tighten when it is quiet."
    ),
    "r": (
        "R is the amount a trade risks: the distance from entry to its original stop. A trade "
        "that hits that stop loses 1 R; one that makes twice that distance gains 2 R. Results in "
        "R can be compared between trades of different sizes."
    ),
    "trailing": (
        "Once a trade has gained a set multiple of its stop distance, the system moves the stop "
        "up behind the best price reached and keeps following it. The stop only ever moves in "
        "the trade's favour."
    ),
    "session": (
        "Every open trade is closed before the daily market break, so nothing is held over it."
    ),
    "paper": (
        "A paper test runs a rule on the live prices and records the trades it would have "
        "taken, with real spreads, at the smallest size. No order is sent."
    ),
}

KINDS = tuple(DOCS)
