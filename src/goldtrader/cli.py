"""goldctl — owner and developer command line (ARCHITECTURE §4). Phase 3 commands only.

Secrets are never passed as arguments: the real MT5 login reads MT5_LOGIN / MT5_PASSWORD /
MT5_SERVER from the environment (Windows Credential Manager support arrives in Phase 4, R-053).
"""

from __future__ import annotations

import json
import os
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


def _client(fake: bool):
    if fake:
        c = FakeMT5()
        c.initialize()
        c.set_price(4000.00, 4000.20)
        return c
    from goldtrader.mt5.real import RealMT5

    c = RealMT5()
    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    kwargs = {}
    if login and password and server:
        kwargs = {"login": int(login), "password": password, "server": server}
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
        json.dumps(
            {
                "metrics": m,
                "gate": rep.as_dict(),
                "snapshot_id": snap,
                "config_hash": config_hash(cfg),
            },
            indent=2,
            default=str,
        )
    )
    (base.with_suffix(".md")).write_text(render_report(rep, s.id))
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
    base.with_suffix(".runs.json").write_text(json.dumps(rows, indent=2, default=str))
    base.with_suffix(".md").write_text(render_report(rep, f"{kind} best {params}"))
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
            (state_dir / "gateway.token").write_text(svc.token)
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
    provider: str = typer.Option("fake", help="fake | anthropic (Phase 5)"),
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
        else:
            typer.echo(
                "the Anthropic provider arrives in Phase 5 (D-031); use --provider fake", err=True
            )
            raise typer.Exit(code=2)
        sender = None
        chat_id = get_secret("TELEGRAM_OWNER_CHAT_ID", required=False)
        tg_token = get_secret("TELEGRAM_BOT_TOKEN", required=False)
        if chat_id and tg_token:
            sender = HttpTelegramSender(tg_token)
        svc = BrainService(
            cfg,
            repo,
            GatewayClient(gateway_url, token),
            model,  # type: ignore[arg-type]
            sender=sender,
            owner_chat_id=int(chat_id) if chat_id else None,
        )
        typer.echo(
            f"brain up: strategy={svc.strat.id} gateway={gateway_url} alerts="
            f"{'telegram' if sender else 'off'}"
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
            raise typer.Exit(code=1) from e


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


@app.command("secret-set")
def secret_set(name: str) -> None:
    """Store a secret in the OS credential store (prompts; the value is never echoed)."""
    from goldtrader.secrets import KNOWN, set_secret

    if name not in KNOWN:
        typer.echo(f"unknown secret name; choose one of {KNOWN}", err=True)
        raise typer.Exit(code=1)
    value = typer.prompt(f"value for {name}", hide_input=True)
    where = set_secret(name, value)
    typer.echo(f"{name} stored in {where}")


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
        out.write_text(md)
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
