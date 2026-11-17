"""goldctl — owner and developer command line (ARCHITECTURE §4). Phase 3 commands only.

Secrets are never passed as arguments: the real MT5 login reads MT5_LOGIN / MT5_PASSWORD /
MT5_SERVER through `goldtrader.secrets` (environment, then Windows Credential Manager; R-053).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import typer

from goldtrader.backtest.costs import CostModel
from goldtrader.backtest.engine import run_backtest
from goldtrader.backtest.gate import GateThresholds, bundle_id, offline_gate, render_report, sweep
from goldtrader.backtest.metrics import compute_metrics
from goldtrader.backtest.strategies import build_strategy
from goldtrader.config import ConfigError, config_hash, load_config, owner_hash
from goldtrader.config.schema import Config
from goldtrader.data.exness_csv import read_exness_ticks
from goldtrader.data.features import add_features
from goldtrader.data.resample import TickArrays, bars_from_arrays
from goldtrader.data.store import load_bars, save_bars, snapshot_id
from goldtrader.data.synthetic import generate_bars
from goldtrader.db import Repo, connect, migrate
from goldtrader.mt5.fake import FakeMT5
from goldtrader.mt5.onaccount import render_markdown, run_suite
from goldtrader.mt5.spec import find_gold_symbol, read_spec
from goldtrader.types import SymbolSpec

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

# Windows consoles default to a legacy code page; our reports use arrows and dashes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def _client(fake: bool):
    if fake:
        c = FakeMT5()
        c.initialize()
        c.set_price(4000.00, 4000.20)
        return c
    from goldtrader.mt5.real import RealMT5

    from goldtrader.secrets import get_secret

    c = RealMT5()
    login = (get_secret("MT5_LOGIN", required=False) or "").strip()
    password = get_secret("MT5_PASSWORD", required=False)
    server = (get_secret("MT5_SERVER", required=False) or "").strip()
    kwargs = {}
    if login.isdigit() and password and server:
        kwargs = {"login": int(login), "password": password, "server": server}
    else:
        typer.echo(
            "MT5 credentials incomplete or malformed (run goldctl doctor); using the account the "
            "terminal is already logged in to",
            err=True,
        )
    if not c.initialize(**kwargs):
        raise typer.Exit(code=2)
    return c


def _load(config: Path) -> Config:
    try:
        return load_config(config)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _default_spec(symbol: str) -> SymbolSpec:
    """Spec used for backtests when no live spec is stored: Exness-published XAUUSD values
    (contract 100, pip 0.01, 0.01–200 lots) plus Assumptions for the unpublished fields (KU-001)."""
    return SymbolSpec(
        name=symbol,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
        volume_limit=0.0,
        trade_stops_level=0,
        trade_freeze_level=0,
        trade_mode=4,
        trade_exemode=2,
        filling_mode=3,
        swap_mode=1,
        swap_long=0.0,
        swap_short=0.0,
        swap_rollover3days=3,
        margin_initial=0.0,
        margin_hedged=0.0,
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        spread=20,
        spread_float=True,
    )


@app.command("config-check")
def config_check(config: Path = typer.Argument(..., exists=True)) -> None:
    """Validate a config file and print its hashes."""
    cfg = _load(config)
    typer.echo(
        json.dumps(
            {
                "config_hash": config_hash(cfg),
                "owner_hash": owner_hash(cfg),
                "profile": cfg.owner.profile,
                "mode": cfg.owner.mode,
                "symbol": cfg.account.symbol,
            },
            indent=2,
        )
    )


@app.command("db-init")
def db_init(db: Path = typer.Option(Path("state/demo.sqlite"))) -> None:
    """Create or migrate the SQLite database."""
    conn = connect(db)
    v = migrate(conn)
    typer.echo(f"{db}: schema version {v}")


@app.command("synth-data")
def synth_data(
    out: Path = typer.Option(Path("data/bars/synthetic_1m.parquet")),
    days: float = 60,
    seed: int = 42,
    start: str = "2026-01-05",
) -> None:
    """Generate deterministic synthetic 1-minute gold bars for development and tests."""
    start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
    df = generate_bars(start_dt, days, seed=seed, seconds=60)
    p = save_bars(df, out)
    typer.echo(f"{len(df)} bars -> {p}  snapshot_id={snapshot_id(df)}")


@app.command("import-ticks")
def import_ticks(
    csv: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(Path("data/bars/exness_1m.parquet")),
    seconds: int = 60,
) -> None:
    """Convert an Exness Personal-Area tick CSV (Exness,Symbol,Timestamp,Bid,Ask) into bars."""
    ticks = read_exness_ticks(csv)
    df = bars_from_arrays(TickArrays.from_ticks(ticks), seconds)
    p = save_bars(df, out)
    typer.echo(f"{len(ticks)} ticks -> {len(df)} bars -> {p}  snapshot_id={snapshot_id(df)}")


def _prepare(data: Path):
    df = load_bars(data)
    return add_features(df), snapshot_id(df)


@app.command("backtest")
def backtest(
    config: Path = typer.Option(Path("config/demo.yaml")),
    data: Path = typer.Option(Path("data/bars/synthetic_1m.parquet")),
    strategy_id: str | None = None,
    equity: float = 5000.0,
    spread_mult: float = 1.0,
    slippage_points: float = 1.0,
    out: Path = typer.Option(Path("reports/backtests")),
    db: Path | None = None,
) -> None:
    """Run one strategy from the config over a bars file; write metrics and a gate report."""
    cfg = _load(config)
    strategies = [s for s in cfg.strategies if strategy_id is None or s.id == strategy_id]
    if not strategies:
        typer.echo("no matching strategy in config", err=True)
        raise typer.Exit(code=1)
    s = strategies[0]
    df, snap = _prepare(data)
    spec = _default_spec(cfg.account.symbol)
    cost = CostModel(
        spread_multiplier=spread_mult,
        slippage_points=slippage_points,
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
    )
    strat = build_strategy(s.kind, s.id, s.params)
    res = run_backtest(df, strat, cfg, spec, cost=cost, equity0=equity)
    m = compute_metrics(res, equity)
    ledger: list[float] = []
    if db is not None:
        conn = connect(db)
        migrate(conn)
        repo = Repo(conn)
        bid = bundle_id(s.id, s.kind, s.params, config_hash(cfg))
        repo.record_bundle(bid, s.id, s.kind, s.params, config_hash(cfg))
        repo.record_data_snapshot(
            snap,
            f"bars {data}",
            cfg.account.symbol,
            60,
            int(df["time"].iloc[0]),
            int(df["time"].iloc[-1]),
            len(df),
        )
        repo.append_trial(
            bundle_id=bid,
            change_type="baseline",
            description=f"backtest {s.id}",
            data_snapshot_id=snap,
            cost_model_json=cost.as_dict(),
            n_trades=m["n_trades"],
            n_days=m["n_days"],
            sharpe=m.get("sharpe"),
            net_return_pct=m.get("net_return_pct"),
            max_drawdown_pct=m.get("max_drawdown_pct"),
            created_by="owner",
            status="recorded",
        )
        ledger = repo.trial_sharpes()
    rep = offline_gate(
        res,
        m,
        df,
        thresholds=GateThresholds(spread_multiplier=spread_mult),
        ledger_sharpes=ledger,
        rng=np.random.default_rng(0),
    )
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = out / f"{s.id}_{stamp}"
    res.trades.to_csv(base.with_suffix(".trades.csv"), index=False)
    (base.with_suffix(".json")).write_text(
        encoding="utf-8",
        data=json.dumps(
            {
                "metrics": m,
                "gate": rep.as_dict(),
                "snapshot_id": snap,
                "config_hash": config_hash(cfg),
            },
            indent=2,
            default=str,
        ),
    )
    (base.with_suffix(".md")).write_text(render_report(rep, s.id), encoding="utf-8")
    typer.echo(render_report(rep, s.id))
    typer.echo(f"reports: {base}.json / .md / .trades.csv")


@app.command("sweep")
def sweep_cmd(
    config: Path = typer.Option(Path("config/demo.yaml")),
    data: Path = typer.Option(Path("data/bars/synthetic_1m.parquet")),
    kind: str = "trend_atr",
    strategy_id: str = "sweep",
    grid: str = typer.Option("stop_atr=0.6,1.0,1.4;target_atr=1.0,1.5,2.0"),
    equity: float = 5000.0,
    spread_mult: float = 1.5,
    db: Path = typer.Option(Path("state/research.sqlite")),
    out: Path = typer.Option(Path("reports/sweeps")),
) -> None:
    """Parameter sweep: every run goes to the trial ledger; the best run is gated incl. PBO."""
    cfg = _load(config)
    g: dict[str, list[float]] = {}
    for part in grid.split(";"):
        k, vals = part.split("=")
        g[k.strip()] = [float(v) for v in vals.split(",")]
    df, snap = _prepare(data)
    spec = _default_spec(cfg.account.symbol)
    cost = CostModel(
        spread_multiplier=spread_mult,
        commission_per_lot_per_side_usd=cfg.account.commission_per_lot_per_side_usd,
    )
    conn = connect(db)
    migrate(conn)
    repo = Repo(conn)
    repo.record_data_snapshot(
        snap,
        f"bars {data}",
        cfg.account.symbol,
        60,
        int(df["time"].iloc[0]),
        int(df["time"].iloc[-1]),
        len(df),
    )
    sw = sweep(
        df,
        kind,
        strategy_id,
        g,
        cfg,
        spec,
        repo=repo,
        data_snapshot_id=snap,
        config_hash=config_hash(cfg),
        cost=cost,
        equity0=equity,
    )
    params, res, m = sw.runs[sw.best_index]
    rep = offline_gate(
        res,
        m,
        df,
        thresholds=GateThresholds(spread_multiplier=spread_mult),
        ledger_sharpes=repo.trial_sharpes(),
        daily_matrix=sw.daily_matrix,
        rng=np.random.default_rng(0),
    )
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = out / f"{kind}_{stamp}"
    rows = [
        {"params": p, **{k: v for k, v in mm.items() if not isinstance(v, dict)}}
        for p, _, mm in sw.runs
    ]
    base.with_suffix(".runs.json").write_text(
        encoding="utf-8", data=json.dumps(rows, indent=2, default=str)
    )
    base.with_suffix(".md").write_text(
        render_report(rep, f"{kind} best {params}"), encoding="utf-8"
    )
    typer.echo(render_report(rep, f"{kind} best {params}"))
    typer.echo(f"{len(sw.runs)} runs recorded in {db}; ledger size {repo.trial_count()}")


@app.command("gateway")
def gateway(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    fake: bool = typer.Option(False, "--fake/--real"),
    port: int = 8750,
    minutes: float | None = None,
) -> None:
    """Run the gateway process: MT5 polling, risk, execution, reconciliation, loopback API."""
    import logging
    import threading

    from goldtrader.gateway.service import GatewayService
    from goldtrader.notify.redact import install as install_redaction
    from goldtrader.process.exitlog import ExitLog
    from goldtrader.process.lock import acquire
    from goldtrader.secrets import all_secret_values, get_secret

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_redaction(logging.getLogger(), all_secret_values())
    cfg = _load(config)
    state_dir = Path(cfg.data.db_dir)
    ExitLog(state_dir / "gateway.exit.log").install("gateway")
    with acquire("gateway", state_dir):
        conn = connect(db)
        migrate(conn)
        repo = Repo(conn)
        client = _client(fake)
        token = get_secret("GATEWAY_TOKEN", required=False)
        from goldtrader.feeds.calendar_store import CalendarStore

        events_fn = CalendarStore(conn).events_fn(lambda: int(__import__("time").time() * 1000))
        svc = GatewayService(client, cfg, repo, token=token, port=port, events_fn=events_fn)
        svc.start()
        repo.heartbeat("gateway", "started", {"port": svc.api.port if svc.api else None})
        typer.echo(
            f"gateway up: symbol={svc.symbol} port={svc.api.port if svc.api else None} "
            f"profile={cfg.owner.profile} mode={cfg.owner.mode}"
        )
        if token is None:
            typer.echo(
                "GATEWAY_TOKEN not set: a random token was generated; the brain must read it "
                "from state/gateway.token",
                err=True,
            )
            (state_dir / "gateway.token").write_text(svc.token, encoding="utf-8")
        stop = threading.Event()
        if minutes:
            threading.Timer(minutes * 60, stop.set).start()
        try:
            svc.run(stop)
        finally:
            svc.stop()


@app.command("brain")
def brain(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    gateway_url: str = "http://127.0.0.1:8750",
    provider: str = typer.Option("fake", help="fake | anthropic | none"),
    minutes: float | None = None,
) -> None:
    """Run the brain process: scheduler, bots, decision cycle, trade review, alerts."""
    import logging
    import threading

    from goldtrader.brain.service import BrainService
    from goldtrader.gateway.api import GatewayClient
    from goldtrader.llm.fake import FakeProvider
    from goldtrader.notify.redact import install as install_redaction
    from goldtrader.notify.telegram import HttpTelegramSender
    from goldtrader.process.exitlog import ExitLog
    from goldtrader.process.lock import acquire
    from goldtrader.secrets import all_secret_values, get_secret

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_redaction(logging.getLogger(), all_secret_values())
    cfg = _load(config)
    state_dir = Path(cfg.data.db_dir)
    ExitLog(state_dir / "brain.exit.log").install("brain")
    with acquire("brain", state_dir):
        conn = connect(db)
        migrate(conn)
        repo = Repo(conn)
        token = get_secret("GATEWAY_TOKEN", required=False)
        if token is None and (state_dir / "gateway.token").exists():
            token = (state_dir / "gateway.token").read_text().strip()
        if token is None:
            typer.echo(
                "no GATEWAY_TOKEN and no state/gateway.token; start the gateway first", err=True
            )
            raise typer.Exit(code=2)
        if provider == "fake":
            model: object | None = FakeProvider()
            typer.echo("provider=fake: bots return their deterministic fallbacks (D-031)", err=True)
        elif provider == "none":
            model = None
        elif provider == "anthropic":
            from goldtrader.llm.anthropic_provider import AnthropicProvider

            model = AnthropicProvider(cfg.llm)
            typer.echo("provider=anthropic", err=True)
        else:
            typer.echo(f"unknown provider {provider!r}; use fake | anthropic | none", err=True)
            raise typer.Exit(code=2)
        sender = None
        chat_id = get_secret("TELEGRAM_OWNER_CHAT_ID", required=False)
        tg_token = get_secret("TELEGRAM_BOT_TOKEN", required=False)
        chat_updates = None
        if chat_id and tg_token:
            from goldtrader.chat.telegram_loop import HttpTelegramUpdates

            sender = HttpTelegramSender(tg_token)
            # Without it the bot never reads messages. The brain polls with timeout 0 inside its
            # single-threaded loop, so the request timeout is short: a dead network path must
            # not hold up heartbeats, the decision cycle and the dashboard chat.
            from goldtrader.notify.telegram import urllib_transport

            chat_updates = HttpTelegramUpdates(
                tg_token, transport=urllib_transport(tg_token, timeout_s=5.0)
            )
        svc = BrainService(
            cfg,
            repo,
            GatewayClient(gateway_url, token),
            model,  # type: ignore[arg-type]
            sender=sender,
            owner_chat_id=int(chat_id) if chat_id else None,
            chat_updates=chat_updates,
            config_path=config,
        )
        typer.echo(
            f"brain up: strategy={svc.strat.id} gateway={gateway_url} alerts="
            f"{'telegram' if sender else 'off'} chat={'on' if svc.chat else 'off'}"
        )
        stop = threading.Event()
        if minutes:
            threading.Timer(minutes * 60, stop.set).start()
        svc.run(stop)


@app.command("status")
def status(
    db: Path = typer.Option(Path("state/demo.sqlite")), gateway_url: str = "http://127.0.0.1:8750"
) -> None:
    """Owner summary: heartbeats, open incidents, kill switch, positions, last cycle, today's P&L."""
    from goldtrader.db.connect import now_ms
    from goldtrader.gateway.api import GatewayClient
    from goldtrader.secrets import get_secret

    conn = connect(db)
    migrate(conn)
    now = now_ms()
    lines = []
    for r in conn.execute("SELECT process, last_ms, status FROM heartbeats ORDER BY process"):
        age = (now - r["last_ms"]) / 1000
        lines.append(
            f"{r['process']:<8} {r['status']:<12} last heartbeat {age:,.0f}s ago"
            + ("  <-- STALE" if age > 60 else "")
        )
    inc = conn.execute(
        "SELECT kind, severity, opened_ms FROM incidents WHERE closed_ms IS NULL"
    ).fetchall()
    lines.append(
        f"open incidents: {len(inc)}" + (" " + ", ".join(i["kind"] for i in inc) if inc else "")
    )
    token = get_secret("GATEWAY_TOKEN", required=False)
    tok_file = Path("state/gateway.token")
    if token is None and tok_file.exists():
        token = tok_file.read_text().strip()
    if token:
        st = GatewayClient(gateway_url, token).state()
        if st is None:
            lines.append("gateway: unreachable")
        else:
            lines.append(
                f"gateway: session={st.session} entries={'yes' if st.entries_allowed else 'no'} "
                f"kill={'ON' if st.kill_switch else 'off'} paused={'yes' if st.paused else 'no'} "
                f"equity={st.equity} day_pnl={st.day_pnl:+.2f} positions={st.open_positions}"
            )
            for p in st.positions:
                lines.append(
                    f"  {p['side']:<5} {p['volume']} @ {p['price_open']} sl={p['sl']} tp={p['tp']} "
                    f"pnl={p['profit']:+.2f}"
                )
    else:
        lines.append("gateway: no token available (set GATEWAY_TOKEN or start the gateway first)")
    try:
        cyc = conn.execute(
            "SELECT status, gate_reason, started_ms FROM brain_cycles ORDER BY started_ms DESC LIMIT 1"
        ).fetchone()
        if cyc:
            lines.append(
                f"last cycle: {cyc['status']} ({cyc['gate_reason'] or '-'}) "
                f"{(now - cyc['started_ms']) / 1000:,.0f}s ago"
            )
    except Exception:
        pass
    day_start = now - (now % 86_400_000)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_pnl),0) FROM trades WHERE exit_ms >= ?", (day_start,)
    ).fetchone()
    lines.append(f"trades today: {row[0]}  net P&L today: {row[1]:+.2f} USD (net of all costs)")
    typer.echo("\n".join(lines))


@app.command("timemachine")
def timemachine(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    days: int = typer.Option(60, help="Last N days of stored bars; 0 = everything stored"),
    strategy_id: str | None = None,
    timing: str = typer.Option("every_bar", help="every_bar | live_5min"),
    spread_mult: float = 1.0,
    equity: float = 5000.0,
) -> None:
    """Replay stored gold history through the strategy, risk rules and costs (code only, no AI
    agents) and store the result for the dashboard's Time machine."""
    import time as _time

    from goldtrader.backtest import timemachine as tm

    cfg = _load(config)
    conn = connect(db)
    migrate(conn)
    spec = Repo(conn).latest_spec(cfg.account.symbol) or _default_spec(cfg.account.symbol)
    params = {
        "days": days,
        "strategy_id": strategy_id,
        "timing": timing,
        "spread_mult": spread_mult,
        "equity": equity,
    }
    run_id = tm.start_run(conn, params, int(_time.time() * 1000))
    try:
        res = tm.run(
            conn,
            cfg,
            spec,
            days=days,
            strategy_id=strategy_id,
            timing=timing,
            spread_mult=spread_mult,
            equity=equity,
        )
    except Exception as e:  # noqa: BLE001 - the row must not stay "running"
        tm.finish_run(
            conn, run_id, int(_time.time() * 1000), None, f"{type(e).__name__}: {e}"[:500]
        )
        typer.echo(f"time machine failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    tm.finish_run(conn, run_id, int(_time.time() * 1000), res)
    m, r = res["metrics"], res["range"]
    start = datetime.fromtimestamp(r["start_s"], UTC).date()
    end = datetime.fromtimestamp(r["end_s"], UTC).date()
    typer.echo(
        f"time machine run {run_id}: {res['params']['strategy_id']} {timing} {start}..{end} "
        f"({r['bars']} bars)\n"
        f"  signals seen {res['signals_seen']}  trades {m.get('n_trades')}  "
        f"net {m.get('net_pnl', 0.0):+.2f} USD ({m.get('net_return_pct', 0.0):+.2f}%)  "
        f"win rate {(m.get('hit_rate') or 0) * 100:.0f}%  "
        f"max drawdown {m.get('max_drawdown_pct', 0.0):.2f}%  "
        f"buy-and-hold {res['buy_and_hold_pct'] or 0:+.2f}%\n"
        f"  gate: {'PASS' if res['gate']['passed'] else 'FAIL'}  rejected {m.get('rejected')}"
    )


@app.command("strategy-search")
def strategy_search(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    ledger_db: Path = typer.Option(Path("state/research.sqlite")),
    holdout: float = typer.Option(1 / 3, help="Share of the stored history held back"),
    out: Path = typer.Option(Path("reports/search")),
    store_runs: bool = typer.Option(True, help="Store finalists as Time machine runs"),
) -> None:
    """Search slower-bar versions of the trend strategy that survive recorded costs. Ranks on the
    earlier part of the stored history and checks the best on the held-out later part."""
    import time as _time

    from goldtrader.backtest import search as ss
    from goldtrader.backtest import timemachine as tm

    cfg = _load(config)
    conn = connect(db)
    migrate(conn)
    spec = Repo(conn).latest_spec(cfg.account.symbol) or _default_spec(cfg.account.symbol)
    lconn = connect(ledger_db)
    migrate(lconn)
    ledger = Repo(lconn)
    frame = tm.load_frame(conn, cfg)
    snap = snapshot_id(frame)
    chash = config_hash(cfg)
    ledger.record_data_snapshot(
        snap,
        f"bars {db} (in-sample part)",
        cfg.account.symbol,
        60,
        int(frame["time"].iloc[0]),
        int(frame["time"].iloc[-1]),
        len(frame),
    )

    def on_trial(c, m, _df) -> None:
        kind, sid, params = c
        bid = bundle_id(sid, kind, params, chash)
        ledger.record_bundle(bid, sid, kind, params, chash)
        ledger.append_trial(
            bundle_id=bid,
            change_type="search",
            description=f"strategy-search in-sample: {ss.describe(c)}",
            data_snapshot_id=snap,
            cost_model_json=CostModel().as_dict(),
            n_trades=m["n_trades"],
            n_days=m["n_days"],
            sharpe=m.get("sharpe"),
            net_return_pct=m.get("net_return_pct"),
            max_drawdown_pct=m.get("max_drawdown_pct"),
            created_by="owner",
            status="recorded",
        )
        typer.echo(
            f"  {sid:<28} trades {m['n_trades'] or 0:>4}  net {m.get('net_return_pct') or 0:+6.2f}%"
        )

    typer.echo("in-sample results (earlier part of the history):")
    res = ss.run_search(conn, cfg, spec, holdout=holdout, on_trial=on_trial, frame=frame)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"search_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(res, indent=2), encoding="utf-8")

    def line(tag: str, m: dict) -> str:
        pf = m.get("profit_factor")
        return (
            f"    {tag:<22} trades {m['n_trades'] or 0:>4}  net {m.get('net_return_pct') or 0:+6.2f}%"
            f"  win {(m.get('hit_rate') or 0) * 100:3.0f}%  PF {pf if pf is None else round(pf, 2)}"
            f"  max DD {m.get('max_drawdown_pct') or 0:.2f}%"
        )

    r = res["range"]
    d = lambda s: datetime.fromtimestamp(s, UTC).date()  # noqa: E731
    typer.echo(
        f"\n{res['n_tried']} candidates tried; {res['n_profitable_in_sample']} profitable in-sample "
        f"({d(r['start_s'])}..{d(r['split_s'])}); held-out period {d(r['split_s'])}..{d(r['end_s'])}"
    )
    for row in [res["baseline"], *res["finalists"]]:
        typer.echo(f"\n  {row['what']}  ->  {'HOLDS' if row['holds'] else 'does not hold'}")
        typer.echo(line("in-sample", row["in_sample"]))
        typer.echo(line("held out", row["held_out"]))
        typer.echo(line("held out, spread x1.5", row["held_out_stress"]))
    if not res["finalists"]:
        typer.echo("\n  no candidate was profitable in-sample with enough trades")
    if store_runs:
        for row in res["finalists"][:3]:
            c = tuple(row["candidate"])
            params = {"days": 0, "strategy_id": c[1], "timing": "every_bar", "spread_mult": 1.0}
            rid = tm.start_run(conn, params, int(_time.time() * 1000))
            full = tm.run(conn, cfg, spec, days=0, candidate=c, frame=frame, label=row["what"])
            tm.finish_run(conn, rid, int(_time.time() * 1000), full)
            typer.echo(f"  stored as Time machine run {rid}: {row['what']}")
    typer.echo(f"\nreport: {path}")


@app.command("paper-add")
def paper_add(
    bundle_id: str = typer.Argument(..., help="Short name, e.g. paper_htf60"),
    kind: str = typer.Option("trend_htf"),
    params: str = typer.Option(..., help="k=v;k=v, e.g. minutes=60;on_cross=0;stop_atr=1.5"),
    label: str = typer.Option(""),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Paper-test a strategy next to the live system: simulated on live bars by the brain's shadow
    ledger, logged to shadow_trades, never sent to the broker (D-059)."""
    import re
    import time as _time

    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", bundle_id):
        typer.echo("bundle id: 3-40 letters, digits, _ or -", err=True)
        raise typer.Exit(code=1)
    try:
        p = {k.strip(): float(v) for k, v in (kv.split("=", 1) for kv in params.split(";") if kv)}
        build_strategy(kind, bundle_id, p)  # refuses unknown kinds and bad values now, not later
    except Exception as e:  # noqa: BLE001
        typer.echo(f"bad strategy: {e}", err=True)
        raise typer.Exit(code=1) from e
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO paper_candidates(bundle_id, kind, params_json, label, created_ms, active) "
        "VALUES (?,?,?,?,?,1) ON CONFLICT(bundle_id) DO UPDATE SET active=1",
        (
            bundle_id,
            kind,
            json.dumps(p, sort_keys=True),
            label or bundle_id,
            int(_time.time() * 1000),
        ),
    )
    typer.echo(f"paper candidate {bundle_id} is active; the brain picks it up within a minute")


@app.command("paper-remove")
def paper_remove(
    bundle_id: str = typer.Argument(...),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Stop paper-testing a candidate (its recorded trades are kept)."""
    conn = connect(db)
    migrate(conn)
    n = conn.execute(
        "UPDATE paper_candidates SET active=0 WHERE bundle_id=?", (bundle_id,)
    ).rowcount
    typer.echo(f"{bundle_id}: {'stopped' if n else 'not found'}")


@app.command("paper-list")
def paper_list(db: Path = typer.Option(Path("state/demo.sqlite"))) -> None:
    """Paper candidates with their simulated results so far."""
    conn = connect(db)
    migrate(conn)
    rows = conn.execute(
        "SELECT c.bundle_id, c.label, c.active, c.created_ms, "
        "SUM(CASE WHEN t.status='closed' THEN 1 ELSE 0 END) AS n, "
        "COALESCE(SUM(t.net_pnl),0) AS net, AVG(t.r_multiple) AS mean_r, "
        "SUM(CASE WHEN t.status='open' THEN 1 ELSE 0 END) AS open_n "
        "FROM paper_candidates c LEFT JOIN shadow_trades t ON t.bundle_id=c.bundle_id "
        "GROUP BY c.bundle_id ORDER BY c.created_ms"
    ).fetchall()
    if not rows:
        typer.echo("no paper candidates")
    for r in rows:
        since = datetime.fromtimestamp(r["created_ms"] / 1000, UTC).strftime("%Y-%m-%d %H:%M")
        mean_r = "n/a" if r["mean_r"] is None else f"{r['mean_r']:+.2f}"
        typer.echo(
            f"{r['bundle_id']} [{'active' if r['active'] else 'stopped'}] since {since} UTC: "
            f"{r['n'] or 0} closed, net {r['net']:+.2f} USD at 0.01 lot, mean R {mean_r}, "
            f"{r['open_n'] or 0} open  - {r['label']}"
        )


@app.command("veto-report")
def veto_report(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Do the AI agents' vetoes add value? Signals the code wanted, what happened to them, and
    what each would have made as a code-only trade (in R)."""
    import time as _time

    from goldtrader.report import veto

    cfg = _load(config)
    conn = connect(db)
    migrate(conn)
    strat = ([s for s in cfg.strategies if s.enabled] or list(cfg.strategies))[0]
    rep = veto.build(conn, cfg, strat, int(_time.time() * 1000))
    typer.echo(
        f"{rep['decisions_recorded']} decisions recorded; {rep['signals']} had a signal "
        f"(code-only trade: stop {rep['stop_atr']} ATR, target {rep['target_atr']} ATR)"
    )
    names = {"taken": "taken", "ai_veto": "vetoed by AI", "code_block": "blocked by code"}
    names["ai_down"] = "AI unavailable"
    for key, label in names.items():
        c = rep["classes"][key]
        mean = "n/a" if c["mean_r"] is None else f"{c['mean_r']:+.2f}"
        typer.echo(
            f"  {label:<16} {c['n']:>4} signals, {c['resolved']:>4} played out, "
            f"{c['wins']:>3} would have won, total {c['sum_r']:+.2f} R, mean {mean} R"
        )
    typer.echo(rep["verdict"])


def _signal_api() -> tuple[int, str]:
    from goldtrader.secrets import get_secret

    api_id = get_secret("TELEGRAM_API_ID", required=False)
    api_hash = get_secret("TELEGRAM_API_HASH", required=False)
    if not api_id or not api_hash or not api_id.strip().isdigit():
        typer.echo(
            "Telegram app id and hash are not stored. Create them at https://my.telegram.org "
            "(API development tools), then run `goldctl secret-set TELEGRAM_API_ID` and "
            "`goldctl secret-set TELEGRAM_API_HASH`.",
            err=True,
        )
        raise typer.Exit(code=2)
    return int(api_id.strip()), api_hash.strip()


def _signal_errors(e: Exception) -> None:
    """Explain the usual failures without a stack trace."""
    name = type(e).__name__
    if name == "NotLoggedIn":
        typer.echo("Not logged in to Telegram. Run `goldctl signals-login` first.", err=True)
    elif "locked" in str(e).lower():
        typer.echo("The login file is in use: stop the signal reader, then try again.", err=True)
    elif name == "ModuleNotFoundError":
        typer.echo('Telethon is not installed: uv pip install "telethon>=1.36,<2"', err=True)
    elif isinstance(e, (ConnectionError, TimeoutError, OSError)):
        typer.echo(
            "Could not reach Telegram. Some networks block it over IPv4: run the same command "
            "again with --ipv6.",
            err=True,
        )
    else:
        typer.echo(f"Telegram error: {name}: {e}", err=True)
    raise typer.Exit(code=2) from e


@app.command("signals-login")
def signals_login(
    config: Path = typer.Option(Path("config/demo.yaml")),
    ipv6: bool = typer.Option(False, help="Connect to Telegram over IPv6"),
) -> None:
    """Log the signal reader in to your own Telegram account (once). Run this yourself: Telegram
    sends a code to your app and you type it here. The login is kept in state/ and never shown."""
    from goldtrader.signals import telegram_user as tu

    cfg = _load(config)
    api_id, api_hash = _signal_api()
    try:
        who = tu.login(Path(cfg.data.db_dir), api_id, api_hash, ipv6=ipv6)
    except Exception as e:  # noqa: BLE001
        _signal_errors(e)
    typer.echo(f"logged in as {who}. Next: `goldctl signals-channels` to pick channels.")


@app.command("signals-channels")
def signals_channels(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    find: str = typer.Option("", help="Only names containing this text"),
    ipv6: bool = False,
) -> None:
    """List the channels and groups your account is in, with the command that watches each."""
    from goldtrader.signals import telegram_user as tu

    cfg = _load(config)
    api_id, api_hash = _signal_api()
    try:
        chats = tu.list_chats(Path(cfg.data.db_dir), api_id, api_hash, ipv6=ipv6)
    except Exception as e:  # noqa: BLE001
        _signal_errors(e)
    conn = connect(db)
    migrate(conn)
    on = {r[0] for r in conn.execute("SELECT id FROM signal_channels WHERE active=1")}
    import unicodedata

    def plain(s: str) -> str:  # channel names often use styled letters ("𝐆𝐨𝐥𝐝"), so fold them
        return unicodedata.normalize("NFKC", s).casefold()

    want = plain(find.lstrip("@"))
    shown = [c for c in chats if want in plain(c["name"]) or want in plain(c["username"] or "")]
    for c in sorted(shown, key=lambda c: c["name"].lower()):
        mark = "WATCHED" if c["id"] in on else "       "
        typer.echo(f"{mark} {c['kind']:<7} {c['id']:>15}  {c['name']}")
    typer.echo(
        f"{len(shown)} of {len(chats)} chats. Watch one with: goldctl signals-add <id> "
        '--name "<name>"   (add --gold-only when every signal there is gold)'
    )


@app.command("signals-add", context_settings={"ignore_unknown_options": True})  # ids are negative
def signals_add(
    chat_id: int = typer.Argument(..., help="The id shown by signals-channels"),
    name: str = typer.Option(..., help="A name for the dashboard"),
    gold_only: bool = typer.Option(False, help="Treat every signal there as gold"),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Start recording a channel or group. The running reader picks it up within a minute."""
    import time as _time

    from goldtrader.signals.service import SignalService

    conn = connect(db)
    migrate(conn)
    SignalService(conn, "", lambda: int(_time.time() * 1000)).add_channel(
        chat_id, name, gold_only=gold_only
    )
    typer.echo(f"watching {name} ({chat_id}){' as gold-only' if gold_only else ''}")


@app.command("signals-remove", context_settings={"ignore_unknown_options": True})
def signals_remove(
    chat_id: int = typer.Argument(...), db: Path = typer.Option(Path("state/demo.sqlite"))
) -> None:
    """Stop recording a channel (what was recorded is kept)."""
    import time as _time

    from goldtrader.signals.service import SignalService

    conn = connect(db)
    migrate(conn)
    ok = SignalService(conn, "", lambda: int(_time.time() * 1000)).remove_channel(chat_id)
    typer.echo(f"{chat_id}: {'stopped' if ok else 'not found'}")


@app.command("signals")
def signals_run(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    ai: bool = typer.Option(True, help="Let a small model read messages the rules cannot"),
    ai_daily_cap_usd: float = 0.25,
    alerts: bool = typer.Option(True, help="Alert you through your bot for each fresh signal"),
    ipv6: bool = False,
    minutes: float | None = None,
) -> None:
    """Run the Telegram signal agent (D-061, D-062): record watched channels, extract gold signals,
    alert you, show fresh ones to the Strategy agent and score them on our own prices. It never
    posts to the channels and never reaches the gateway."""
    import logging
    import time as _time

    from goldtrader.notify.redact import install as install_redaction
    from goldtrader.process.exitlog import ExitLog
    from goldtrader.process.lock import acquire
    from goldtrader.secrets import all_secret_values, get_secret
    from goldtrader.signals import telegram_user as tu
    from goldtrader.signals.service import SignalService, make_ai

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    install_redaction(logging.getLogger(), all_secret_values())
    cfg = _load(config)
    api_id, api_hash = _signal_api()
    state_dir = Path(cfg.data.db_dir)
    ExitLog(state_dir / "signals.exit.log").install("signals")
    with acquire("signals", state_dir):
        conn = connect(db)
        migrate(conn)
        repo = Repo(conn)
        ask = None
        if ai and get_secret("ANTHROPIC_API_KEY", required=False):
            from goldtrader.llm.anthropic_provider import AnthropicProvider

            ask = make_ai(AnthropicProvider(cfg.llm))
        svc = SignalService(
            conn,
            cfg.account.symbol,
            lambda: int(_time.time() * 1000),
            ai=ask,
            ai_daily_cap_usd=ai_daily_cap_usd,
            alerts=alerts,
        )
        typer.echo(
            f"signal reader up: {len(svc.watched())} channels watched, "
            f"model fallback {'on' if ask else 'off'}"
        )
        try:
            tu.run(
                svc,
                state_dir,
                api_id,
                api_hash,
                heartbeat=lambda status: repo.heartbeat("signals", status),
                ipv6=ipv6,
                minutes=minutes,
            )
        except KeyboardInterrupt:
            pass
        except Exception as e:  # noqa: BLE001
            _signal_errors(e)


@app.command("signals-report")
def signals_report(db: Path = typer.Option(Path("state/demo.sqlite"))) -> None:
    """Scorecard per watched channel: signals posted, how they played out on our prices, in R."""
    import time as _time

    from goldtrader.signals.report import scorecard

    conn = connect(db)
    migrate(conn)
    rep = scorecard(conn, int(_time.time() * 1000))
    if not rep or not rep["channels"]:
        typer.echo("no channels are watched yet (goldctl signals-channels, then signals-add)")
        return
    typer.echo(f"reader: {'running' if rep['reader_running'] else 'NOT running'}")
    for c in rep["channels"]:
        mean = "n/a" if c["mean_r"] is None else f"{c['mean_r']:+.2f}"
        typer.echo(
            f"{c['name']} [{'on' if c['active'] else 'off'}]: {c['messages']} messages, "
            f"{c['signals']} signals; {c['closed']} played out, {c['wins']} won, total "
            f"{c['sum_r']:+.2f} R, mean {mean} R; {c['running']} running, {c['expired']} never "
            f"filled, {c['no_stop']} without a stop, {c['edited']} edited later, "
            f"{c['deleted']} deleted" + ("" if c["enough"] else "  (too few to judge)")
        )


@app.command("dashboard")
def dashboard(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    gateway_url: str = "http://127.0.0.1:8750",
    port: int = 8760,
    minutes: float | None = None,
) -> None:
    """Serve the read-only dashboard on this PC only (D-056): market, account, agents."""
    import threading

    from goldtrader.dashboard.server import DashboardServer
    from goldtrader.gateway.api import GatewayClient
    from goldtrader.process.exitlog import ExitLog
    from goldtrader.process.lock import acquire
    from goldtrader.secrets import get_secret

    cfg = _load(config)
    state_dir = Path(cfg.data.db_dir)
    if not db.exists():
        typer.echo(f"database {db} not found; start the gateway first", err=True)
        raise typer.Exit(code=2)
    ExitLog(state_dir / "dashboard.exit.log").install("dashboard")
    with acquire("dashboard", state_dir):
        token = get_secret("GATEWAY_TOKEN", required=False)
        if token is None and (state_dir / "gateway.token").exists():
            token = (state_dir / "gateway.token").read_text().strip()
        # a short timeout: the page must still load from the database when the gateway is down
        gateway = GatewayClient(gateway_url, token, timeout_s=1.5) if token else None
        srv = DashboardServer(
            db,
            gateway,
            symbol=cfg.account.symbol,
            port=port,
            config_path=config,
            strategies=tuple(st.id for st in cfg.strategies),
        )
        srv.start()
        typer.echo(
            f"dashboard up: http://127.0.0.1:{srv.port}  (read-only; "
            f"gateway={'on' if gateway else 'no token, database only'})"
        )
        stop = threading.Event()
        if minutes:
            threading.Timer(minutes * 60, stop.set).start()
        try:
            while not stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()


@app.command("llm-check")
def llm_check(config: Path = typer.Option(Path("config/demo.yaml"))) -> None:
    """Verify Anthropic connectivity and the configured model ids (needs ANTHROPIC_API_KEY)."""
    from goldtrader.llm.anthropic_provider import AnthropicProvider

    cfg = _load(config)
    p = AnthropicProvider(cfg.llm)
    for tier in cfg.llm.tiers:
        try:
            typer.echo(f"{tier:<9} {p.ping(tier)} ok")
        except Exception as e:  # noqa: BLE001
            typer.echo(f"{tier:<9} {cfg.llm.tiers[tier].model} FAILED: {e}", err=True)
            cause = e.__cause__ or e
            from goldtrader.llm.anthropic_provider import redact

            for attr in ("status_code", "body"):
                v = getattr(cause, attr, None)
                if v is not None:
                    typer.echo(f"  {attr}: {redact(str(v))[:600]}", err=True)
            resp = getattr(cause, "response", None)
            text = getattr(resp, "text", None)
            if text:
                typer.echo(f"  response: {redact(str(text))[:600]}", err=True)
            _llm_diagnose(getattr(getattr(p, "_client", None), "base_url", None))
            raise typer.Exit(code=1) from e


def _llm_diagnose(base_url: object) -> None:
    """Print what the failing request was made of, without ever printing the key itself."""
    from goldtrader.llm.anthropic_provider import redact
    from goldtrader.secrets import get_secret

    typer.echo("  -- diagnostics --", err=True)
    typer.echo(f"  base_url: {base_url}", err=True)
    for var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        if os.environ.get(var) or os.environ.get(var.lower()):
            typer.echo(f"  env {var} is set (affects the request)", err=True)
    key = get_secret("ANTHROPIC_API_KEY", required=False) or ""
    kind = (
        "console api key"
        if key.startswith("sk-ant-api")
        else (
            "Claude.ai/Claude Code OAuth token (not accepted by the API)"
            if key.startswith("sk-ant-oat")
            else "unknown shape"
        )
    )
    flags = []
    if key != key.strip():
        flags.append("has leading/trailing whitespace or newline")
    if any(ord(c) > 126 or ord(c) < 32 for c in key):
        flags.append("contains non-ASCII or control characters (paste artefact)")
    if key[:1] in "'\"" or key[-1:] in "'\"":
        flags.append("wrapped in quotes")
    typer.echo(f"  key: {kind}; length {len(key)}; {', '.join(flags) or 'clean'}", err=True)
    try:
        import httpx

        url = f"{str(base_url).rstrip('/')}/v1/models?limit=1"
        r = httpx.get(
            url, headers={"x-api-key": key.strip(), "anthropic-version": "2023-06-01"}, timeout=20
        )
        hdrs = {
            k: r.headers.get(k)
            for k in ("request-id", "cf-ray", "server", "content-type", "via")
            if r.headers.get(k)
        }
        typer.echo(f"  raw GET {url}: HTTP {r.status_code} {hdrs}", err=True)
        typer.echo(f"  raw body: {redact(r.text[:400])!r}", err=True)
    except Exception as ex:  # noqa: BLE001
        typer.echo(f"  raw GET failed: {redact(str(ex))[:300]}", err=True)
    typer.echo(
        "  credit balance too low -> link the Max plan's API credit to the key's Console "
        "organization (docs/WINDOWS_SETUP.md step 9); empty body / HTML -> a proxy or antivirus "
        "is intercepting the request",
        err=True,
    )


@app.command("eval")
def eval_cmd(
    set_name: str = typer.Argument(..., help="headlines | regimes | reviews"),
    config: Path = typer.Option(Path("config/demo.yaml")),
    provider: str = typer.Option("fake", help="fake | anthropic"),
    out: Path = typer.Option(Path("reports/evals")),
    limit: int | None = None,
    baseline: Path | None = typer.Option(None, help="previous summary JSON to gate against"),
) -> None:
    """Run one evaluation set and optionally gate it against a baseline summary (LOCAL_LLM §7)."""
    from goldtrader.evals.cli import gate as eval_gate
    from goldtrader.evals.cli import run_set
    from goldtrader.llm.fake import FakeProvider
    from goldtrader.llm.store import LLMStore

    cfg = _load(config)
    if provider == "anthropic":
        from goldtrader.llm.anthropic_provider import AnthropicProvider

        model = AnthropicProvider(cfg.llm)
    else:
        model = FakeProvider()
    conn = connect(None)
    migrate(conn)
    summary, path = run_set(set_name, model, cfg, LLMStore(conn), out, limit)
    typer.echo(json.dumps(summary.model_dump(mode="json"), indent=2, default=str)[:4000])
    typer.echo(f"summary: {path}")
    if baseline is not None:
        decision = eval_gate(baseline, path)
        typer.echo(f"gate: {'PASS' if decision.ok else 'FAIL'} {decision.reasons}")
        if not decision.ok:
            raise typer.Exit(code=1)


@app.command("learn")
def learn(
    action: str = typer.Argument(
        ..., help="status | nightly | unpause <bundle> | rollback <experiment>"
    ),
    target: str | None = typer.Argument(None),
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Learning system: show experiments, run the nightly job now, un-pause a bundle, roll back."""
    from goldtrader.brain.features import DbFeatures
    from goldtrader.brain.learning_job import LearningJob
    from goldtrader.db.connect import now_ms

    cfg = _load(config)
    enabled = [s for s in cfg.strategies if s.enabled]
    if not enabled:
        typer.echo("no enabled strategy in config", err=True)
        raise typer.Exit(code=1)
    conn = connect(db)
    migrate(conn)
    repo = Repo(conn)
    feats = DbFeatures(repo, cfg.account.symbol, enabled[0])
    job = LearningJob(
        cfg,
        repo,
        enabled[0],
        now_ms,
        spec=repo.latest_spec(cfg.account.symbol) or _default_spec(cfg.account.symbol),
        frame_fn=feats._frame,
    )
    if action == "status":
        typer.echo(json.dumps(job.status(), indent=2, default=str))
    elif action == "nightly":
        typer.echo(json.dumps(job.nightly(), indent=2, default=str))
    elif action == "unpause":
        if not target:
            raise typer.BadParameter("bundle id required")
        ok = job.monitors.unpause(target, actor="owner")
        typer.echo("un-paused" if ok else "bundle was not paused")
    elif action == "rollback":
        if not target:
            raise typer.BadParameter("experiment id required")
        exp = job.store.get(target)
        if exp is None:
            typer.echo("unknown experiment", err=True)
            raise typer.Exit(code=1)
        dec = job.pipeline.rollback(exp, "owner command")
        typer.echo(json.dumps(dec.as_dict(), indent=2, default=str))
    else:
        raise typer.BadParameter("action must be status | nightly | unpause | rollback")


@app.command("doctor")
def doctor(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Preflight for the owner's PC: Python, MT5 package, config, secrets (presence only), DB."""
    import platform
    import sys

    from goldtrader.secrets import KNOWN, get_secret, inspect_secret

    rows: list[tuple[str, bool, str]] = []
    rows.append(("python", sys.version_info >= (3, 12), platform.python_version()))
    rows.append(("os", True, f"{platform.system()} {platform.release()}"))
    try:
        import MetaTrader5 as _mt5  # type: ignore[import-not-found]

        rows.append(("MetaTrader5 package", True, getattr(_mt5, "__version__", "present")))
    except Exception as e:  # noqa: BLE001
        rows.append(
            (
                "MetaTrader5 package",
                platform.system() != "Windows",
                "missing (expected on Linux; required on the PC)"
                if platform.system() != "Windows"
                else f"missing: {e}",
            )
        )
    try:
        cfg = load_config(config)
        rows.append(
            (
                "config",
                True,
                f"{config} profile={cfg.owner.profile} mode={cfg.owner.mode} "
                f"symbol={cfg.account.symbol}",
            )
        )
        if cfg.owner.mode == "live":
            rows.append(
                (
                    "live mode",
                    bool(get_secret("ALLOW_LIVE", required=False)),
                    "ALLOW_LIVE phrase present"
                    if get_secret("ALLOW_LIVE", required=False)
                    else "ALLOW_LIVE missing (required for live)",
                )
            )
    except ConfigError as e:
        rows.append(("config", False, str(e)[:200]))
    for name in KNOWN:
        if name == "ALLOW_LIVE":
            continue
        value = get_secret(name, required=False)
        present = value is not None
        required = name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
        summary, problems = inspect_secret(value)
        rows.append(
            (
                f"secret {name}",
                (present and not problems) or (not present and not required),
                f"present, {summary}" + (f"; PROBLEM: {'; '.join(problems)}" if problems else "")
                if present
                else ("MISSING (required)" if required else "absent (optional)"),
            )
        )
    try:
        conn = connect(db)
        v = migrate(conn)
        rows.append(("database", True, f"{db} schema v{v}"))
    except Exception as e:  # noqa: BLE001
        rows.append(("database", False, f"{e}"[:200]))
    try:
        import keyring  # type: ignore[import-not-found]  # noqa: F401

        rows.append(("keyring", True, "available"))
    except Exception:  # noqa: BLE001
        rows.append(("keyring", True, "not installed: secrets come from the environment only"))
    width = max(len(r[0]) for r in rows)
    bad = 0
    for name, ok, detail in rows:
        bad += 0 if ok else 1
        typer.echo(f"{'OK  ' if ok else 'FAIL'} {name:<{width}}  {detail}")
    typer.echo(f"{len(rows) - bad}/{len(rows)} checks passed")
    if bad:
        raise typer.Exit(code=1)


@app.command("report")
def report(
    config: Path = typer.Option(Path("config/demo.yaml")),
    db: Path = typer.Option(Path("state/demo.sqlite")),
    period: str = typer.Option("all", help="today | week | month | all"),
    out: Path = typer.Option(Path("reports/phase7")),
) -> None:
    """Phase 7 performance report: scorecard, go-live checklist, monitors, learning status."""
    from goldtrader.db.connect import now_ms
    from goldtrader.report.scorecard import (
        go_live_checklist,
        render_checklist_text,
        render_scorecard_text,
        scorecard,
    )

    cfg = _load(config)
    conn = connect(db)
    migrate(conn)
    now = now_ms()
    sc = scorecard(conn, period, now)  # type: ignore[arg-type]
    checks = go_live_checklist(conn, cfg, now)
    hb = conn.execute("SELECT process, last_ms, status FROM heartbeats ORDER BY process").fetchall()
    inc = conn.execute(
        "SELECT kind, severity, opened_ms, acknowledged_ms, closed_ms FROM incidents "
        "ORDER BY opened_ms DESC LIMIT 20"
    ).fetchall()
    mon = conn.execute(
        "SELECT at_ms, bundle_id, monitor, level, detail FROM monitor_events "
        "ORDER BY at_ms DESC LIMIT 20"
    ).fetchall()
    exps = conn.execute(
        "SELECT experiment_id, stage, change_type, change_json, stage_entered_ms "
        "FROM experiments ORDER BY created_ms DESC LIMIT 20"
    ).fetchall()
    llm = conn.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls").fetchone()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        f"# Phase 7 report — {cfg.account.symbol} {cfg.owner.profile}/{cfg.owner.mode} — {stamp}",
        "",
        f"Config hash {config_hash(cfg)}; owner hash {owner_hash(cfg)}; period: {period}",
        "",
        "## Scorecard (net of spread, slippage, commission and swap)",
        "",
        "```",
        render_scorecard_text(sc),
        "```",
        "",
        "## Go-live checklist (TEST_PLAN §4 Phase 7)",
        "",
        "```",
        render_checklist_text(checks),
        "```",
        "",
        "## Processes",
        "",
    ]
    for r in hb:
        lines.append(
            f"- {r['process']}: {r['status']}, last heartbeat {(now - r['last_ms']) / 60000:.0f} min ago"
        )
    lines += ["", "## Incidents (latest 20)", ""]
    lines += [
        f"- {r['kind']} [{r['severity']}] opened {datetime.fromtimestamp(r['opened_ms'] / 1000, UTC):%Y-%m-%d %H:%M} "
        f"ack={'yes' if r['acknowledged_ms'] else 'no'} closed={'yes' if r['closed_ms'] else 'no'}"
        for r in inc
    ] or ["- none"]
    lines += ["", "## Monitor events (latest 20)", ""]
    lines += [
        f"- {datetime.fromtimestamp(r['at_ms'] / 1000, UTC):%Y-%m-%d} {r['bundle_id'][:8]} {r['monitor']} {r['level']} {r['detail'] or ''}"
        for r in mon
    ] or ["- none"]
    lines += ["", "## Experiments", ""]
    lines += [
        f"- {r['experiment_id'][:8]} {r['stage']} {r['change_type']} {r['change_json'][:80]}"
        for r in exps
    ] or ["- none"]
    lines += ["", f"AI calls: {llm[0]}, spend {llm[1]:.2f} USD", ""]
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"report_{period}_{stamp}.md"
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    typer.echo(text)
    typer.echo(f"written: {path}")


@app.command("secret-set")
def secret_set(
    name: str,
    from_clipboard: bool = typer.Option(
        False, "--from-clipboard", help="read the value from the clipboard instead of prompting"
    ),
    generate: bool = typer.Option(
        False, "--generate", help="store a new random 64-hex-character token (for GATEWAY_TOKEN)"
    ),
) -> None:
    """Store a secret in the OS credential store (prompts; the value is never echoed).

    On the Windows console a hidden prompt cannot receive Ctrl+V (it stores the character 0x16):
    copy the value, then run with --from-clipboard, or right-click in the window to paste.
    """
    from goldtrader.secrets import KNOWN, inspect_secret, set_secret

    cleaned = "".join(ch for ch in name if ch.isalnum() or ch == "_").upper()
    if cleaned not in KNOWN:
        typer.echo(
            f"unknown secret name {name!r} (read as {cleaned!r}); choose one of {KNOWN}", err=True
        )
        raise typer.Exit(code=1)
    if generate:
        import secrets as _secrets

        value = _secrets.token_hex(32)
    elif from_clipboard:
        value = _read_clipboard()
    else:
        value = typer.prompt(
            f"value for {cleaned} (or re-run with --from-clipboard)", hide_input=True
        )
    value = value.strip().strip("\"'")
    summary, problems = inspect_secret(value)
    if not value or problems:
        typer.echo(
            f"nothing stored: value {summary} looks wrong ({'; '.join(problems) or 'empty'}). "
            "Copy the value, then run: goldctl secret-set "
            f"{cleaned} --from-clipboard",
            err=True,
        )
        raise typer.Exit(code=1)
    where = set_secret(cleaned, value)
    typer.echo(f"{cleaned} stored in {where} ({summary})")


def _read_clipboard() -> str:
    """Clipboard text via the platform's own tool; nothing is printed."""
    import subprocess

    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    elif sys.platform == "darwin":
        cmd = ["pbpaste"]
    else:
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10).stdout
    except Exception as e:  # noqa: BLE001
        typer.echo(f"could not read the clipboard: {e}", err=True)
        raise typer.Exit(code=1) from e


@app.command("backfill")
def backfill(
    fake: bool = typer.Option(False, "--fake/--real"),
    symbol: str | None = None,
    days: int = 60,
    db: Path = typer.Option(Path("state/demo.sqlite")),
) -> None:
    """Pull M1 bars from the terminal's history into the database (features need ~20 days)."""
    from datetime import timedelta

    from goldtrader.mt5 import constants as K

    conn = connect(db)
    migrate(conn)
    repo = Repo(conn)
    client = _client(fake)
    total = 0
    try:
        sym = symbol or find_gold_symbol(client, preferred="XAUUSDm")
        client.symbol_select(sym, True)
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        cursor = start
        earliest = None
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=7), end)
            bars = client.copy_rates_range(sym, K.TIMEFRAME_M1, cursor, chunk_end) or []
            if bars:
                total += repo.upsert_bars(sym, bars, "mt5_rates")
                earliest = earliest or datetime.fromtimestamp(bars[0].time, UTC)
            cursor = chunk_end
        typer.echo(
            f"{sym}: {total} M1 bars stored in {db}"
            + (
                f" (earliest {earliest:%Y-%m-%d %H:%M} UTC)"
                if earliest
                else "; the terminal returned no bars for that range"
            )
        )
    finally:
        client.shutdown()
    if total == 0:
        raise typer.Exit(code=1)


@app.command("mt5-tests")
def mt5_tests(
    fake: bool = typer.Option(True, "--fake/--real"),
    symbol: str | None = None,
    trade: bool = typer.Option(False, "--trade/--no-trade"),
    allow_real: bool = False,
    out: Path | None = None,
) -> None:
    """Run the MT5-T00…T16 on-account suite (docs/MT5_EXNESS_INTEGRATION.md §8)."""
    client = _client(fake)
    try:
        sym = symbol or find_gold_symbol(client, preferred="XAUUSDm")
        results = run_suite(client, sym, allow_trading=trade, allow_real=allow_real)
    finally:
        client.shutdown()
    md = render_markdown(results)
    typer.echo(md)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    if any(r.status == "fail" for r in results):
        raise typer.Exit(code=1)


@app.command("spec-dump")
def spec_dump(
    fake: bool = typer.Option(True, "--fake/--real"),
    symbol: str | None = None,
    db: Path | None = None,
) -> None:
    """Read symbol_info() and account_info(); store them when --db is given (R-001, KU-001)."""
    client = _client(fake)
    try:
        sym = symbol or find_gold_symbol(client, preferred="XAUUSDm")
        spec = read_spec(client, sym)
        acct = client.account_info()
    finally:
        client.shutdown()
    d = (
        {k: v for k, v in spec.__dict__.items() if k != "CRITICAL"}
        if hasattr(spec, "__dict__")
        else {}
    )
    if not d:
        from dataclasses import asdict

        d = asdict(spec)
        d.pop("CRITICAL", None)
    typer.echo(
        json.dumps(
            {
                "spec": d,
                "account": acct.__dict__
                if acct and hasattr(acct, "__dict__")
                else (acct._asdict() if acct and hasattr(acct, "_asdict") else str(acct)),
            },
            indent=2,
            default=str,
        )
    )
    if db is not None:
        conn = connect(db)
        migrate(conn)
        repo = Repo(conn)
        prev = repo.latest_spec(sym)
        from goldtrader.mt5.spec import critical_changes, diff_specs

        changed = diff_specs(prev, spec) if prev else []
        crit = critical_changes(prev, spec) if prev else []
        repo.record_spec(spec, changed, bool(crit))
        if acct:
            repo.record_account(acct)
        if crit:
            typer.echo(f"CRITICAL SPEC CHANGE: {crit}", err=True)
            raise typer.Exit(code=3)


if __name__ == "__main__":
    app()
