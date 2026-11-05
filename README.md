# goldtrader — AI gold (XAUUSD) trading system for Exness via MetaTrader 5

Status: Phase 3 (data pipeline, MT5 integration, backtesting). Documentation is the source of
truth: start with `docs/00_PROJECT_BRIEF.md`, then `docs/README.md`.

Nothing here trades live. Phase 8 (limited live) requires the criteria in `docs/TEST_PLAN.md` and
the owner's explicit approval.

## Install (developer, any OS)

```bash
uv venv .venv
uv pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Install on the owner's Windows PC (MT5 terminal required)

1. Install MetaTrader 5 from Exness, log in to the **demo** account once, and enable
   *Tools → Options → Expert Advisors → Allow algorithmic trading*.
2. In PowerShell, from the repo folder:

```powershell
uv venv .venv
uv pip install -e ".[dev,windows]"
```

3. Put the demo credentials in the environment for the session only (never in files or chat):

```powershell
$env:MT5_LOGIN="12345678"; $env:MT5_PASSWORD="..."; $env:MT5_SERVER="Exness-MT5Trial..."
```

4. Run the on-account suite (read-only first, then with 0.01-lot test trades on demo):

```powershell
.venv\Scripts\goldctl mt5-tests --real --no-trade --out reports\mt5\readonly.md
.venv\Scripts\goldctl mt5-tests --real --trade --out reports\mt5\trade.md
.venv\Scripts\goldctl spec-dump --real --db state\demo.sqlite
```

Paste the Markdown table into `docs/MT5_EXNESS_INTEGRATION.md` §8. The suite refuses to place
orders on a real account unless `--allow-real` is passed.

## Everyday commands (`goldctl`)

| Command | Purpose |
| --- | --- |
| `goldctl config-check config/demo.yaml` | Validate config, print config and owner hashes |
| `goldctl db-init --db state/demo.sqlite` | Create or migrate the database |
| `goldctl synth-data --days 60` | Deterministic synthetic 1-minute bars for development |
| `goldctl import-ticks <exness.csv>` | Exness Personal-Area tick CSV → 1-minute bid/ask bars |
| `goldctl backtest --data data/bars/synthetic_1m.parquet --db state/research.sqlite` | One strategy, metrics net of costs, offline-gate report |
| `goldctl sweep --grid "stop_atr=0.6,1.0;target_atr=1.0,1.5"` | Parameter sweep → trial ledger → gate with PBO on the best run |
| `goldctl mt5-tests --fake` | The MT5-T00…T16 suite against the in-memory FakeMT5 |

## Layout

`src/goldtrader/{config,mt5,session,data,backtest,db}` plus `cli.py`; see the package docstring in
`src/goldtrader/__init__.py` and `docs/ARCHITECTURE.md`.
