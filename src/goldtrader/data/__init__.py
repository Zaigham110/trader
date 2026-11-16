"""Data module: Exness tick CSV → ticks → bars → causal features; synthetic data; Parquet cache.

See ``docs/ARCHITECTURE.md`` §2.1 (``market``) and ``docs/TEST_PLAN.md`` §2. Pure, offline code.
"""
from goldtrader.data.exness_csv import (  # noqa: F401
    ReadReport,
    frame_to_ticks,
    load_exness_ticks,
    parse_timestamps,
    read_exness_ticks,
    ticks_to_frame,
)
from goldtrader.data.features import (  # noqa: F401
    FEATURE_COLUMNS,
    add_features,
    bars_per_day,
    bars_per_year,
    donchian,
    session_high_low,
    wilder_atr,
)
from goldtrader.data.resample import (  # noqa: F401
    BAR_COLUMNS,
    TickArrays,
    bars_to_frame,
    build_bars,
    frame_to_bars,
    infer_bar_seconds,
    sample_1s,
)
from goldtrader.data.store import (  # noqa: F401
    PARQUET_AVAILABLE,
    bars_path,
    load_bars,
    load_ticks,
    save_bars,
    save_ticks,
    snapshot_id,
    ticks_path,
)
from goldtrader.data.synthetic import (  # noqa: F401
    generate_bars,
    generate_gold_tick_arrays,
    generate_gold_ticks,
    is_open_winter,
    open_mask_winter,
)
