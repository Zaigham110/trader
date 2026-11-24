"""Plain-language glossary for the dashboard's tooltips and Help page (D-083).

Pure data, no imports: the dashboard process loads it without loading any trading code. The
engineering glossary for the project is `docs/GLOSSARY.md`; this one is written for the owner.
"""

# term -> one or two plain sentences. Terms are matched exactly by the page (`data-term`).
TERMS = {
    "ATR": (
        "Average true range: the average size of a 1-minute price bar over the last few minutes. "
        "Stops and targets are set as multiples of it, so they adapt to how fast the market moves."
    ),
    "R": (
        "The amount a trade risks: the distance from its entry to its original stop. A trade that "
        "hits that stop loses 1 R; one that gains twice that distance makes 2 R."
    ),
    "Stop": (
        "The price at which a losing trade is closed automatically. It is held at the broker, so "
        "it still works if this PC is off."
    ),
    "Target": "The price at which a winning trade is closed automatically.",
    "Trailing stop": (
        "Once a trade is far enough in profit, the stop is moved up behind the best price reached "
        "and keeps following it. It only ever moves in the trade's favour."
    ),
    "Spread": (
        "The gap between the price you can buy at and the price you can sell at. It is the main "
        "cost of every trade, and it widens when the market is thin or nervous."
    ),
    "Slippage": (
        "The difference between the price an order asked for and the price it was filled at."
    ),
    "Pip": "0.01 USD in the gold price. A spread of 24 pips is 0.24 USD per ounce.",
    "Lot": "100 ounces of gold. The smallest trade, 0.01 lot, is 1 ounce: 1.00 of price is 1 USD.",
    "Equity": "The account balance plus the profit or loss of any open trade.",
    "Balance": "The money in the account counting closed trades only.",
    "Day P&L": (
        "The change in equity since 00:00 UTC, including open trades. It is what the daily loss "
        "limit is measured against."
    ),
    "Drawdown": (
        "How far the account or a result is below its highest point so far. The kill switch "
        "trips when the account's drawdown reaches its limit."
    ),
    "Win rate": "The share of closed trades that made money.",
    "Profit factor": (
        "Money won divided by money lost. Above 1 the trades made money overall; 2 means twice as "
        "much was won as lost."
    ),
    "Expectancy": "The average result of one trade, in USD or in R.",
    "Costs": "Spread, slippage, commission and swap. They are already taken out of every net result.",
    "Long": "A trade that buys, and gains when the price rises.",
    "Short": "A trade that sells, and gains when the price falls.",
    "Signal": "The moment a strategy's rule says a trade should be opened.",
    "Strategy agent": (
        "The AI agent that reads the market context and proposes enter, exit or wait. On the demo "
        "account the rule's signals are taken whatever it says."
    ),
    "Risk advisor": (
        "The AI agent that gives each decision a risk budget from 0 to 100. Code may only lower "
        "risk on its advice, never raise it above the limits."
    ),
    "AI opinion": (
        "What the Strategy agent said about a signal: agreed, unsure (it wanted the trade but was "
        "below the confidence bar), wait, or unavailable."
    ),
    "Gate": (
        "A check in code that a proposed trade must pass before any order is sent: session open, "
        "spread, risk per trade, loss limits, open positions and more."
    ),
    "Risk engine": (
        "The code that checks every order against the limits. No strategy and no AI agent can "
        "change it or go around it."
    ),
    "Kill switch": (
        "Closes every position, cancels pending orders and blocks new entries until you reset it."
    ),
    "Pause": "Blocks new entries. Open trades keep their stop and target.",
    "Loss pause": (
        "After a set number of losing trades in a row, new entries stop for a while so a bad "
        "stretch cannot compound quickly."
    ),
    "Session": (
        "Whether the gold market is open for this account. Entries are also blocked around the "
        "daily break, the London price fixes and scheduled high-impact news."
    ),
    "Daily break": (
        "Gold closes for about an hour each night. Every open trade is closed before it."
    ),
    "Paper test": (
        "A rule run on the live prices that records the trades it would have taken, with real "
        "spreads, at the smallest size. No order is sent."
    ),
    "Time machine": (
        "Replays stored price history through the rules, limits and costs to show how a strategy "
        "would have traded. The AI agents are not replayed."
    ),
    "Go-live checklist": (
        "The project's own conditions for the end of demo trading: enough days and trades, the "
        "system running almost all market hours, every trade stopped, recorded and reconciled."
    ),
    "Gateway": "The process that talks to the broker: prices, risk checks, orders, the trailing stop.",
    "Brain": "The process that runs the AI agents, the decision cycle, the reports and Telegram.",
    "Watchdog": (
        "A guard inside the gateway and the brain that ends the process if it freezes, so Windows "
        "starts a fresh one."
    ),
    "Demo account": "A practice account at the broker with real prices and pretend money.",
    "MFE": "The best point a trade reached while it was open (maximum favourable excursion).",
    "MAE": "The worst point a trade reached while it was open (maximum adverse excursion).",
}

# The life of one trade, in order, for the Help page.
WALKTHROUGH = (
    (
        "Prices arrive",
        "The gateway reads the gold price from the broker a few times a second and builds 1-minute bars.",
    ),
    (
        "The rule looks for a signal",
        "At the close of each bar the strategy's rule checks whether its entry condition has just become true.",
    ),
    (
        "The agents are asked",
        "The Risk advisor gives a risk budget and the Strategy agent says enter, exit or wait, with its reasoning. On the demo account a signal is taken at once and their answers are kept for the record.",
    ),
    (
        "Code checks the trade",
        "The gate and the risk engine check session, spread, size, risk per trade, loss limits, open positions and the loss pause. Any failure stops the trade and is logged with its reason.",
    ),
    (
        "The order goes to the broker",
        "Only the execution engine can send it. The stop and the target are attached to the position at the broker.",
    ),
    (
        "The trade is managed",
        "The stop never moves away from the price. Once the trade is far enough in profit the trailing stop follows it. Before the daily break every trade is closed.",
    ),
    (
        "The result is recorded",
        "The trade is booked net of all costs, checked against the broker's own records and reviewed by an agent, and the statistics on these pages update.",
    ),
)
