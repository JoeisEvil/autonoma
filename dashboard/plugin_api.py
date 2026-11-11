import asyncio
import importlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

PLUGIN = "autonoma"
_PKG = "autonoma_dashboard_pkg"
_ROOT = Path(__file__).resolve().parent.parent


def _pkg():
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, _ROOT / "__init__.py",
                                                      submodule_search_locations=[str(_ROOT)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = mod
        spec.loader.exec_module(mod)
    return {n: importlib.import_module(f"{_PKG}.{n}") for n in ("ledger", "trust", "insights", "handoff",
                                                                    "taxonomy")}


def _settings():
    try:
        from hermes_cli.config import load_config
        entry = ((load_config() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN) or {}
        return {**(entry.get("config") or {}), **(entry.get("settings") or {})}
    except Exception:
        return {}


def _open():
    from plugins.plugin_storage import plugin_data_dir
    m = _pkg()
    db = sqlite3.connect(plugin_data_dir(PLUGIN) / "data.db", timeout=5)
    m["ledger"].init(db)
    r = db.execute("SELECT profile FROM trust GROUP BY profile ORDER BY MAX(updated_ts) DESC LIMIT 1").fetchone()
    profile = r[0] if r else "default"
    cfg = _settings()
    engine = m["trust"].TrustEngine(db, {k: cfg.get(k) for k in (
        "enabled", "start_score", "thresholds", "gain_rate", "decay_days", "hard_floor_enabled",
        "hard_floor_score", "hard_floor_turns")})
    return m, db, engine, profile, cfg


def _state(window_h):
    m, db, engine, profile, cfg = _open()
    try:
        return m["insights"].build_state(db, engine, profile, m["ledger"].turn_counter(db, profile),
                                         window_s=window_h * 3600, taxonomy=m["taxonomy"].Taxonomy.load(),
                                         config=cfg)
    finally:
        db.close()


def _trust(op, cls, value):
    m, db, engine, profile, _ = _open()
    try:
        known = engine.known_classes(profile)
        if cls and cls not in known:
            raise HTTPException(400, f"unknown class {cls}")
        if op == "reset":
            engine.reset(profile, cls or None)
        elif op == "set":
            if not cls or value is None or not 0 <= float(value) <= 100:
                raise HTTPException(400, "set needs class and value 0-100")
            engine.set_score(profile, cls, float(value))
        elif op in ("freeze", "unfreeze"):
            engine.set_frozen(profile, op == "freeze")
        else:
            raise HTTPException(400, f"unknown op {op}")
        return {"ok": True}
    finally:
        db.close()


def _clear_handoff(session_id):
    m, db, _, profile, _ = _open()
    try:
        return {"cleared": m["handoff"].clear(db, session_id or None, profile)}
    finally:
        db.close()


@router.get("/state")
async def state(window_h: int = 24):
    return await asyncio.to_thread(_state, max(1, min(window_h, 24 * 30)))


@router.post("/trust")
async def trust(body: dict):
    return await asyncio.to_thread(_trust, str(body.get("op", "")), body.get("class"), body.get("value"))


@router.post("/handoff/clear")
async def clear_handoff(body: dict | None = None):
    return await asyncio.to_thread(_clear_handoff, (body or {}).get("session_id"))
