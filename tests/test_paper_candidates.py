"""Paper candidates (D-059): owner-chosen strategies simulated by the shadow ledger, no orders."""

from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from goldtrader.brain.service import PAPER_EXPERIMENT, BrainService
from goldtrader.cli import _default_spec, app
from goldtrader.config import load_config
from goldtrader.dashboard import data
from goldtrader.db import Repo, connect, migrate
from goldtrader.shadow.ledger import ShadowLedger

PARAMS = "minutes=60;on_cross=0;stop_atr=1.5;target_atr=29.05"


def test_cli_adds_lists_and_stops_a_candidate_and_the_brain_follows(tmp_path):
    db = tmp_path / "p.sqlite"
    run = CliRunner().invoke
    assert run(app, ["paper-add", "x", "--params", PARAMS, "--db", str(db)]).exit_code == 1
    bad = run(
        app, ["paper-add", "paper_bad", "--kind", "nope", "--params", PARAMS, "--db", str(db)]
    )
    assert bad.exit_code == 1
    ok = run(
        app, ["paper-add", "paper_htf60", "--params", PARAMS, "--label", "60m", "--db", str(db)]
    )
    assert ok.exit_code == 0, ok.output

    conn = connect(db)
    migrate(conn)
    cfg = load_config("config/demo.yaml")
    ledger = ShadowLedger(conn, cfg, _default_spec(cfg.account.symbol), lambda: 0)
    fake = SimpleNamespace(repo=Repo(conn), learning=SimpleNamespace(shadow=ledger))
    BrainService._sync_paper_candidates(fake)
    BrainService._sync_paper_candidates(fake)  # idempotent
    assert ledger.bundles == ["paper_htf60"]
    b = ledger._bundles["paper_htf60"]
    assert b.experiment_id == PAPER_EXPERIMENT and b.strategy.minutes == 60

    conn.execute(
        "INSERT INTO shadow_trades (shadow_id, experiment_id, bundle_id, side, volume, signal_ms, "
        "entry_ms, exit_ms, entry_price, exit_price, sl, tp, net_pnl, r_multiple, exit_reason, "
        "status) VALUES ('s1','paper','paper_htf60','long',0.01,1,2,3,4000,4010,3998,4040,9.8,4.2,"
        "'target','closed'), ('s2','paper','paper_htf60','short',0.01,4,5,NULL,4010,NULL,4012,"
        "3970,NULL,NULL,NULL,'open')"
    )
    listed = run(app, ["paper-list", "--db", str(db)]).output
    assert "paper_htf60 [active]" in listed and "1 closed, net +9.80 USD" in listed
    view = data.paper(conn)
    json.dumps(view)
    assert view[0]["closed"] == 1 and view[0]["wins"] == 1 and view[0]["net_pnl"] == 9.8
    assert view[0]["open"][0]["side"] == "short" and view[0]["params"]["minutes"] == 60.0

    assert "stopped" in run(app, ["paper-remove", "paper_htf60", "--db", str(db)]).output
    BrainService._sync_paper_candidates(fake)
    assert ledger.bundles == []
    assert not data.paper(conn)[0]["active"]
