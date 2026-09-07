#!/usr/bin/env python3
"""Check that the deployment is wired up correctly.

    python scripts/verify_setup.py

Runs every check that does not need a live collection, and tells you what to
fix rather than just failing. Safe to run repeatedly.

Windows note: this exists because the alternative is pasting multi-line Python
into a shell, and the quoting rules differ between cmd.exe, PowerShell and
bash. A file runs the same everywhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

OK, WARN, FAIL = "[ ok ]", "[warn]", "[FAIL]"
problems: list[str] = []


def say(status: str, msg: str, fix: str = "") -> None:
    print(f"{status} {msg}")
    if status == FAIL:
        problems.append(f"{msg}\n       fix: {fix}" if fix else msg)
    elif fix and status == WARN:
        print(f"       {fix}")


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 10):
        say(OK, f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        say(FAIL, f"Python {v.major}.{v.minor} is too old",
            "install Python 3.11")


def check_packages() -> None:
    missing = []
    for mod, pip_name in [("pandas", "pandas"), ("numpy", "numpy"),
                          ("yaml", "PyYAML"), ("pyarrow", "pyarrow"),
                          ("fastapi", "fastapi"), ("requests", "requests")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        say(FAIL, f"missing packages: {', '.join(missing)}",
            "pip install -r requirements.txt")
    else:
        say(OK, "core packages installed")

    try:
        __import__("supabase")
        say(OK, "supabase client installed")
    except ImportError:
        say(WARN, "supabase client not installed",
            "pip install supabase  (needed only for the database steps)")


def check_data_files() -> None:
    expected = {
        "data/dgca_domestic_city.csv": "DGCA city-pair traffic",
        "data/dgca_carrier_stats.csv": "carrier capacity",
        "data/cpi_groups.csv": "CPI group 07.3 benchmark",
        "data/cpi_general_index.csv": "CPI general index",
        "data/event_calendar.csv": "event calendar",
        "config/route_weights.yaml": "DGCA route weights",
        "config/basket.yaml": "basket definition",
        "sql/schema.sql": "database schema",
    }
    for path, label in expected.items():
        if Path(path).exists():
            say(OK, f"{label} ({path})")
        else:
            say(FAIL, f"missing {label} ({path})",
                "re-extract the zip; do not delete data/ or config/")

    if Path("data/cpi_2024_weights.csv").exists():
        say(OK, "CPI air-fare item weight present")
    else:
        say(WARN, "CPI air-fare item weight not fetched yet",
            "/v1/cpi-impact stays disabled until you add it — "
            "see docs/data-sources.md. Not blocking.")


def check_basket() -> None:
    try:
        import yaml
        b = yaml.safe_load(open("config/basket.yaml"))
    except Exception as exc:  # noqa: BLE001
        say(FAIL, f"could not read config/basket.yaml: {exc}", "re-extract the zip")
        return

    total = sum(r["weight"] for r in b["routes"])
    if abs(total - 1.0) < 0.01:
        say(OK, f"{len(b['routes'])} routes, weights sum to {total:.4f}")
    else:
        say(FAIL, f"route weights sum to {total:.4f}, not 1.0",
            "python -m scripts.build_weights --refresh")

    if "PLACEHOLDER" in b.get("weight_source", ""):
        say(FAIL, "basket still uses placeholder weights",
            "python -m scripts.build_weights --refresh")
    else:
        say(OK, f"weights sourced: {b['version']}")

    enabled = [s["name"] for s in b.get("sources", []) if s.get("enabled")]
    live = [n for n in enabled if not n.startswith("replay")]
    if live:
        say(OK, f"live source enabled: {', '.join(live)}")
    else:
        say(WARN, "no live source enabled — only the replay archive is on",
            "set enabled: true on gds:aggregator in config/basket.yaml "
            "once you have your Duffel token")


def check_env() -> None:
    want = {
        "SUPABASE_URL": "Supabase project URL",
        "SUPABASE_SERVICE_KEY": "Supabase service_role key",
        "AGGREGATOR_API_TOKEN": "Duffel or Amadeus token",
    }
    for var, label in want.items():
        val = os.environ.get(var)
        if val:
            say(OK, f"{var} is set ({label})")
        else:
            say(WARN, f"{var} not set in this shell",
                "only needed locally; GitHub and HF hold their own copies")

    url = os.environ.get("SUPABASE_URL", "")
    if url and not url.startswith("https://"):
        say(FAIL, "SUPABASE_URL should start with https://",
            "copy the Project URL from Supabase Settings -> API")

    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if key and not key.startswith("eyJ"):
        say(FAIL, "SUPABASE_SERVICE_KEY does not look like a JWT",
            "copy the service_role key, not the database password")


def check_supabase() -> None:
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")):
        print("\n-- skipping database checks: credentials not set in this shell")
        return
    try:
        from apix.store import Store
    except ImportError as exc:
        say(FAIL, f"cannot import apix: {exc}",
            "run this from the project root, not from inside scripts/")
        return

    try:
        store = Store()
    except Exception as exc:  # noqa: BLE001
        say(FAIL, f"could not create the Supabase client: {exc}")
        return

    tables = ["fare_quotes", "elementary_cells", "apix_daily",
              "apix_route_daily", "apix_monthly", "collection_runs",
              "basket_version"]
    missing = []
    for t in tables:
        try:
            store._client.table(t).select("*").limit(1).execute()
        except Exception:  # noqa: BLE001
            missing.append(t)
    if missing:
        say(FAIL, f"tables not reachable: {', '.join(missing)}",
            "run the whole of sql/schema.sql in the Supabase SQL Editor")
    else:
        say(OK, f"all {len(tables)} tables reachable")

    try:
        import yaml
        basket = yaml.safe_load(open("config/basket.yaml"))
        bid = store.register_basket(basket)
        say(OK, f"wrote basket version to the database (id {str(bid)[:8]}...)")
    except Exception as exc:  # noqa: BLE001
        say(FAIL, f"could not write the basket version: {exc}",
            "check the service_role key, not the anon key")

    try:
        days = store.collected_dates()
        if days:
            say(OK, f"{len(days)} collection days present, latest {days[-1]}")
        else:
            say(WARN, "no collection days yet",
                "expected until the collector has run once")
    except Exception as exc:  # noqa: BLE001
        say(FAIL, f"could not read collection dates: {exc}")


def main() -> int:
    if not Path("config/basket.yaml").exists():
        print("Run this from the project root (the folder holding config/ and apix/).")
        return 1

    print("APIx setup check\n" + "=" * 60)
    for section, fn in [("environment", check_python),
                        ("packages", check_packages),
                        ("data files", check_data_files),
                        ("basket", check_basket),
                        ("credentials", check_env),
                        ("database", check_supabase)]:
        print(f"\n-- {section}")
        fn()

    print("\n" + "=" * 60)
    if problems:
        print(f"{len(problems)} problem(s) to fix:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        return 1
    print("No blocking problems. Warnings above are fine at this stage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
