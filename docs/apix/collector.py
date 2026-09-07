"""Daily collection run: basket -> sources -> clean -> store."""
from __future__ import annotations

import importlib
import logging
import random
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List

import pandas as pd
import yaml

from .compliance import SourcePolicy
from .index import IndexConfig, build_index
from .models import FareQuote
from .normalize import drop_report, run_pipeline
from .sources.base import Source

log = logging.getLogger("apix.collector")


def load_basket(path: str | Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def index_config_from_basket(basket: dict) -> IndexConfig:
    return IndexConfig(
        route_weights={r["route"]: float(r["weight"]) for r in basket["routes"]},
        apw_weights={int(k): float(v) for k, v in basket["apw_weights"].items()},
        cabin=basket.get("cabin", "ECONOMY"),
        base_period=basket.get("base_period"),
        min_quotes_per_cell=basket["collection"].get("min_quotes_per_cell", 3),
        max_imputation_days=basket["collection"].get("max_imputation_days", 7),
    )


def build_sources(basket: dict) -> List[Source]:
    out: List[Source] = []
    for spec in basket.get("sources", []):
        if not spec.get("enabled"):
            log.info("source %s disabled, skipping", spec["name"])
            continue
        module_name, cls_name = spec["adapter"].split(":")
        cls = getattr(importlib.import_module(module_name), cls_name)
        policy = SourcePolicy(
            name=spec["name"],
            basis=spec["basis"],
            min_delay_seconds=float(spec.get("min_delay_seconds", 5.0)),
            daily_request_budget=int(spec.get("daily_request_budget", 500)),
            permission_ref=spec.get("permission_ref"),
        )
        try:
            out.append(cls(policy, **spec.get("options", {})))
        except Exception as exc:  # noqa: BLE001
            log.error("could not construct source %s: %s", spec["name"], exc)
    if not out:
        raise RuntimeError("no sources enabled — nothing to collect")
    return out


def collect_day(basket: dict, run_date: date | None = None) -> pd.DataFrame:
    run_date = run_date or date.today()
    sources = build_sources(basket)
    windows = basket["collection"]["windows"]
    quotes: List[FareQuote] = []

    targets = [(r["origin"], r["destination"], w) for r in basket["routes"] for w in windows]
    # Shuffle so a source never sees the basket in the same order twice; also
    # spreads load across a carrier's origins rather than hammering one hub.
    random.shuffle(targets)

    for src in sources:
        if hasattr(src, "set_clock"):
            src.set_clock(run_date)
        for origin, destination, apw in targets:
            dep = run_date + timedelta(days=apw)
            quotes.extend(src.collect(origin, destination, dep))

    df = run_pipeline(quotes)
    if not df.empty:
        log.info("run %s\n%s", run_date, drop_report(df).to_string(index=False))
    return df


def run(basket_path: str, run_date: date | None = None, out_dir: str = "out"):
    basket = load_basket(basket_path)
    df = collect_day(basket, run_date)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = (run_date or date.today()).isoformat()
    if df.empty:
        log.error("collection produced no rows for %s", stamp)
        return df
    df.to_parquet(f"{out_dir}/quotes_{stamp}.parquet", index=False)
    return df


def rebuild_index(quotes_glob: str, basket_path: str, out_dir: str = "out") -> dict:
    """Recompute the full index from all stored quotes. Idempotent by design —
    a statistic you cannot regenerate from raw inputs is not auditable."""
    files = sorted(Path().glob(quotes_glob))
    if not files:
        raise FileNotFoundError(f"no quote files matched {quotes_glob}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    basket = load_basket(basket_path)
    result = build_index(df, index_config_from_basket(basket))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for name, frame in result.items():
        frame.to_parquet(f"{out_dir}/apix_{name}.parquet", index=False)
    return result
