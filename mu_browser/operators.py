# vim:ts=4:et
# <pep8 compliant>

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import bpy
from bpy.props import StringProperty, BoolProperty, IntProperty

# ---------------------------------------------------------------------------
# Tunables (edit here)
# ---------------------------------------------------------------------------
JOB_POLL_INTERVAL = 0.25   # seconds between progress-bar / worker polls (file I/O)
THUMB_MAX_WORKERS = 6      # background Blender processes for Thumbs/Regen
THUMB_WORKER_BELOW_NORMAL = True  # reduce UI contention while workers start/render


# ---------------------------------------------------------------------------
# Shared non-blocking job state (Refresh / Thumbs / Regen are mutually exclusive)
# ---------------------------------------------------------------------------

_job = {
    "kind": None,           # None | "catalog" | "thumbs" | "regen"
    "title": "",            # short title for blue bar (no counts)
    "current": 0,           # real progress from workers (may be fractional)
    "total": 0,
    "cancel": False,
    "started": 0.0,
    "procs": [],
    "progress_files": [],
    "progress_cm": None,
    "progress_handle": None,
    "timer_registered": False,
    "last_ui_current": -1,
    "last_ui_time": 0.0,
    "load_only": [],        # cache-hit icons to warm after workers finish
    "base_title": "",       # title without "cur/tot" suffix
    "spawn_done": True,
}


def job_is_active(kind=None) -> bool:
    from . import catalog
    if _job.get("kind") == "catalog" or catalog.catalog_scan_running():
        if kind is None or kind == "catalog":
            return True
        if kind != "catalog":
            return False
    k = _job.get("kind")
    if not k:
        return False
    if kind is None:
        return True
    return k == kind


def job_progress():
    """Return (kind, title, current, total) for UI.

    *current* may be fractional for thumb jobs (e.g. 3.4 = item 3 at 40%).
    """
    from . import catalog
    k = _job.get("kind")
    if k == "catalog" or (not k and catalog.catalog_scan_running()):
        cur, tot, title, active = catalog.catalog_scan_progress()
        if active or catalog.catalog_scan_running() or k == "catalog":
            return (
                "catalog",
                title or _job.get("title") or "Loading categories",
                cur if tot else int(_job.get("current") or 0),
                tot if tot else int(_job.get("total") or 0),
            )
    if not k:
        return (None, "", 0, 0)
    try:
        cur = float(_job.get("current") or 0)
    except Exception:
        cur = 0.0
    return (k, _job.get("title") or k, cur, int(_job.get("total") or 0))


def _tag_mu_areas(force=False):
    """Lightweight redraw — only View3D UI regions, throttled unless force."""
    now = time.time()
    if not force and (now - float(_job.get("last_ui_time") or 0)) < 0.2:
        return
    _job["last_ui_time"] = now
    try:
        screen = bpy.context.screen
        if screen is None:
            return
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "UI":
                    region.tag_redraw()
    except Exception:
        pass


def context_areas_safe():
    try:
        return list(bpy.context.screen.areas)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Blue MU progress bar (progress_util) — quiet, throttled updates
# ---------------------------------------------------------------------------

def _progress_begin(context, total, title):
    try:
        from ..import_mu.progress_util import mu_progress_bar
        cm = mu_progress_bar(context, total=max(1, int(total)), title=title)
        if hasattr(cm, "__enter__"):
            handle = cm.__enter__()
            return cm, handle
        return cm, cm
    except Exception:
        return None, None


def _format_thumb_progress_title(base, cur_f, tot):
    """File counter only: integer N/M (which .mu / how many).

    Do NOT embed a percent here — progress_util appends
    " — Working… (P%)" itself. Embedding % caused a double display:
      "Generate Thumbnails 25/162 (15.6%) — Working… (15%)"
    Desired:
      "Generate Thumbnails 25/162 — Working… (15.6%)"
    The decimal percent is driven by float current/total on the handle.
    """
    tot = max(1, int(tot or 1))
    try:
        cur_f = float(cur_f)
    except Exception:
        cur_f = 0.0
    if cur_f < 0:
        cur_f = 0.0
    if cur_f > tot:
        cur_f = float(tot)
    n_done = int(cur_f) if cur_f < tot else tot
    if n_done < 0:
        n_done = 0
    base = (base or "").strip() or "Generate Thumbnails"
    return "%s %d/%d" % (base, n_done, tot)


def _progress_update(handle, current, total, title=""):
    """Update blue bar. *current* may be float (sub-item stages) for accurate %."""
    if handle is None:
        return
    try:
        cur_f = float(current)
    except Exception:
        cur_f = 0.0
    cur_i = int(cur_f)  # floor — integer file counter when API needs int
    tot = max(1, int(total or 1))
    t = (title or "").strip()
    if not t:
        t = _format_thumb_progress_title("Generate Thumbnails", cur_f, tot)

    # MuProgressSession.update(value=..., text=..., force=...) — prefer kwargs.
    ok = False
    fn = getattr(handle, "update", None)
    if callable(fn):
        for kwargs in (
            {"value": cur_f, "text": "Working…", "force": True},
            {"value": cur_f, "text": "Working…"},
            {"value": cur_f},
        ):
            try:
                fn(**kwargs)
                ok = True
                break
            except TypeError:
                continue
            except Exception:
                break
    if not ok:
        for name in ("step", "set", "set_progress", "tick", "set_text"):
            fn = getattr(handle, name, None)
            if not callable(fn):
                continue
            for args in ((cur_f, tot, t), (cur_i, tot, t), (cur_i,), (t,)):
                try:
                    fn(*args)
                    ok = True
                    break
                except TypeError:
                    continue
                except Exception:
                    break
            if ok:
                break

    for attr, val in (
        ("current", cur_f),
        ("progress", cur_f),
        ("value", cur_f),
        ("total", tot),
        ("max", tot),
        ("maximum", tot),
    ):
        if hasattr(handle, attr):
            try:
                setattr(handle, attr, val)
            except Exception:
                try:
                    setattr(
                        handle,
                        attr,
                        cur_i if attr in ("current", "progress", "value") else val,
                    )
                except Exception:
                    pass
    for attr in ("title", "text", "label", "message", "status"):
        if t and hasattr(handle, attr):
            try:
                setattr(handle, attr, t)
            except Exception:
                pass


def _progress_end(cm, handle=None):
    if cm is None:
        return
    if hasattr(cm, "__exit__"):
        try:
            cm.__exit__(None, None, None)
            return
        except Exception:
            pass
    for name in ("finish", "close", "done", "end"):
        fn = getattr(cm, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def _job_start(kind, title, total, context=None):
    _job["kind"] = kind
    _job["title"] = title
    _job["base_title"] = title
    _job["current"] = 0.0
    _job["total"] = max(1, int(total))
    _job["cancel"] = False
    _job["started"] = time.time()
    _job["last_ui_current"] = -1
    _job["last_ui_total"] = -1
    _job["procs"] = []
    _job["progress_files"] = []
    _job["load_only"] = []
    # Open blue bar
    if context is not None:
        cm, handle = _progress_begin(context, _job["total"], title)
        _job["progress_cm"] = cm
        _job["progress_handle"] = handle
    _tag_mu_areas(force=True)


def _job_update(current, total=None, title=None, force_ui=False):
    """Update progress. *title* is the clean base only (no counts); omit to keep base.

    *current* may be fractional (thumb sub-stages). Display title is only
    "Base N/M" (integers). progress_util appends " — Working… (P%)".
    """
    try:
        cur = float(current)
    except Exception:
        cur = 0.0
    if total is not None:
        _job["total"] = max(1, int(total))
    if title is not None:
        clean = str(title).strip()
        _job["base_title"] = clean
    _job["current"] = cur
    tot = int(_job.get("total") or 1)
    base = (_job.get("base_title") or "").strip() or "Generate Thumbnails"
    display = _format_thumb_progress_title(base, cur, tot)
    _job["title"] = display
    last_c = _job.get("last_ui_current")
    last_t = _job.get("last_ui_total")
    try:
        last_cf = float(last_c) if last_c is not None else None
    except Exception:
        last_cf = None
    if (
        not force_ui
        and last_cf is not None
        and tot == last_t
        and abs(cur - last_cf) < 0.05
    ):
        return
    _job["last_ui_current"] = cur
    _job["last_ui_total"] = tot
    handle = _job.get("progress_handle")
    if handle is not None:
        _progress_update(handle, cur, tot, display)
    else:
        try:
            cm, handle2 = _progress_begin(bpy.context, tot, display)
            if handle2 is not None:
                _job["progress_cm"] = cm
                _job["progress_handle"] = handle2
                _progress_update(handle2, cur, tot, display)
        except Exception:
            pass
    _tag_mu_areas(force=True)


def _job_end():
    _progress_end(_job.get("progress_cm"), _job.get("progress_handle"))
    _job["progress_cm"] = None
    _job["progress_handle"] = None
    _job["kind"] = None
    _job["title"] = ""
    _job["current"] = 0.0
    _job["total"] = 0
    _job["cancel"] = False
    _job["started"] = 0.0
    _job["procs"] = []
    _job["progress_files"] = []
    _job["load_only"] = []
    _job["base_title"] = ""
    _job["last_ui_current"] = -1
    _job["last_ui_total"] = -1
    _tag_mu_areas(force=True)


def _kill_workers_async():
    """Terminate workers without sleeping on the main thread."""
    procs = list(_job.get("procs") or [])
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass

    def _kill_later():
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        return None

    try:
        bpy.app.timers.register(_kill_later, first_interval=0.3)
    except Exception:
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass


def _job_request_cancel():
    _job["cancel"] = True
    _kill_workers_async()
    _tag_mu_areas(force=True)


# ---------------------------------------------------------------------------
# Unified poll timer (catalog + thumbs) — never blocks, no modal
# ---------------------------------------------------------------------------

def _ensure_job_timer():
    if _job.get("timer_registered"):
        return
    _job["timer_registered"] = True
    try:
        bpy.app.timers.register(_job_timer_tick, first_interval=float(JOB_POLL_INTERVAL))
    except Exception:
        _job["timer_registered"] = False


def _job_timer_tick():
    """Single global timer: poll catalog / worker progress, never do heavy work."""
    try:
        return _job_timer_tick_impl()
    except Exception as e:
        print("[mu_browser] job timer error:", type(e).__name__, e)
        _job["timer_registered"] = False
        try:
            _job_end()
        except Exception:
            pass
        return None


def _job_timer_tick_impl():
    kind = _job.get("kind")
    if not kind:
        _job["timer_registered"] = False
        return None

    if _job.get("cancel"):
        _finish_job_cleanup()
        return None

    if kind == "catalog":
        return _poll_catalog_job()
    if kind in ("thumbs", "regen"):
        return _poll_thumbs_job()
    _job["timer_registered"] = False
    return None


def _poll_catalog_job():
    from . import catalog as cat
    running = cat.catalog_scan_running()
    cur, tot, title, active = cat.catalog_scan_progress()
    if running or active:
        if tot > 0:
            if title and "/" not in title and "Categories ready" not in title:
                _job["base_title"] = title
            _job_update(cur, tot, title=None, force_ui=True)
        else:
            soft = min(int(_job.get("current") or 0) + 1, max(1, int(_job.get("total") or 100) - 1))
            _job_update(soft, _job.get("total") or 100, title=None)
        return float(JOB_POLL_INTERVAL)

    # Scan finished
    if title and "Categories ready" in title:
        _job["base_title"] = title
    _job_update(tot if tot else 1, tot if tot else 1, title=None, force_ui=True)
    try:
        from . import properties as props
        props.reset_category_cache()
    except Exception:
        pass
    try:
        _populate_part_list_from_cache(bpy.context)
    except Exception:
        pass
    _job_end()
    _job["timer_registered"] = False
    return None


def _poll_thumbs_job():
    files = list(_job.get("progress_files") or [])
    procs = list(_job.get("procs") or [])
    # Total is fixed at job start (len of queue). Sum fractional *current*
    # from workers (e.g. 2.7 + 1.4 = 4.1) for smooth sub-item progress.
    fixed_total = max(1, int(_job.get("total") or 1))
    cur_sum = 0.0
    for pf in files:
        info = _read_progress_file(pf)
        if info:
            c, _t, _msg = info
            try:
                cur_sum += float(c)
            except Exception:
                pass
    if cur_sum > fixed_total:
        cur_sum = float(fixed_total)

    _job_update(cur_sum, fixed_total, title=None)  # keep base_title + fixed total

    spawn_state = _job.get("spawn_done", True)
    if isinstance(spawn_state, dict) and not spawn_state.get("value", False):
        # Workers still being launched off the main thread — keep UI responsive.
        return float(JOB_POLL_INTERVAL)

    # Re-read procs after spawn may have finished / appended.
    procs = list(_job.get("procs") or [])
    if not procs:
        # Async spawn finished with nothing started.
        print("[mu_browser] thumb worker spawn finished with 0 processes", flush=True)
        try:
            _job_end()
        except Exception:
            pass
        _job["timer_registered"] = False
        return None

    alive = [p for p in procs if p.poll() is None]
    if alive:
        return float(JOB_POLL_INTERVAL)

    if getattr(_job, "get", lambda *a: None)("kind") in ("thumbs", "regen"):
        try:
            from . import thumbnails as _thumbs
            if bool(getattr(_thumbs, "DEV_OVERLAY", 0)):
                for p in procs:
                    print("[mu_thumb][DEV] WORKER EXIT: pid=%s returncode=%s" % (getattr(p, "pid", "?"), p.returncode), flush=True)
        except Exception:
            pass

    # Workers done — warm cache-hit icons in tiny chunks across ticks
    load_only = _job.get("load_only") or []
    if load_only:
        from . import thumbnails
        chunk = load_only[:6]
        _job["load_only"] = load_only[6:]
        for path, name in chunk:
            try:
                thumbnails.icon_id_for_part(path, name, ensure=False)
            except Exception:
                pass
        if _job["load_only"]:
            return 0.05

    try:
        from . import thumbnails
        br = bpy.context.window_manager.ksp_mu_browser
        # Drop stale preview entries so regenerated PNGs actually appear
        thumbnails.reload_browser_icons(br, force=True)
    except Exception:
        pass
    fixed_total = max(1, int(_job.get("total") or 1))
    _job_update(fixed_total, fixed_total, title=None, force_ui=True)
    _job_end()
    _job["timer_registered"] = False
    return None


def _finish_job_cleanup():
    """Cancel / hard stop: kill workers, refresh icons for whatever finished."""
    kind = _job.get("kind")
    _kill_workers_async()
    # Partial progress may have written new PNGs — force pcoll reload
    if kind in ("thumbs", "regen"):
        try:
            from . import thumbnails
            br = bpy.context.window_manager.ksp_mu_browser
            thumbnails.reload_browser_icons(br, force=True)
            thumbnails.schedule_preview_warmup(br, chunk_size=12)
        except Exception as e:
            print("[mu_browser] cancel icon reload:", type(e).__name__, e)
    _job_end()
    _job["timer_registered"] = False
    try:
        _tag_mu_areas(force=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Part list helpers
# ---------------------------------------------------------------------------

def _populate_part_list_from_cache(context):
    from . import catalog
    wm = context.window_manager
    br = wm.ksp_mu_browser
    cat = br.category or "Inne"
    filt = (br.filter or "").strip().lower()

    br.parts.clear()
    for entry in catalog.parts_in_category(cat):
        if filt:
            blob = "%s %s %s" % (entry.name, entry.title, entry.manufacturer)
            if filt not in blob.lower():
                continue
        item = br.parts.add()
        item.name = entry.name
        item.title = entry.title
        item.category = entry.category
        item.mu_path = entry.mu_path
        item.cfg_path = entry.cfg_path
        item.attach_rules = ",".join(str(x) for x in (entry.attach_rules or ()))

    from . import thumbnails
    thumbnails.schedule_preview_warmup(br)


def refresh_part_list_from_cached_catalog(context):
    """Called from catalog poll when worker finishes — UI fill only."""
    try:
        _populate_part_list_from_cache(context)
        _tag_mu_areas(force=True)
    except Exception:
        pass
    # Job end is owned by _poll_catalog_job when kind==catalog


def refresh_part_list(context):
    from . import catalog
    from ..preferences.preferences import Preferences

    wm = context.window_manager
    br = wm.ksp_mu_browser
    gd = (Preferences().GameData or "").strip()
    if not gd or not os.path.isdir(gd):
        br.parts.clear()
        return

    gd_norm = gd.replace("\\", "/").rstrip("/")
    if catalog.catalog_scan_running() or _job.get("kind") == "catalog":
        return
    if catalog._cache.get("root") == gd_norm and catalog._cache.get("parts"):
        _populate_part_list_from_cache(context)
        return
    _start_catalog_job(context, gd, force=False)


def _start_catalog_job(context, gd, force=False):
    from . import catalog
    # Cancel other jobs first
    if _job.get("kind") in ("thumbs", "regen"):
        _job_request_cancel()
        _job_end()

    _job_start("catalog", "Loading categories", 100, context=context)
    catalog.schedule_catalog_scan(gd, force=force)
    _ensure_job_timer()


# ---------------------------------------------------------------------------
# Background Blender workers for thumbnails
# ---------------------------------------------------------------------------

def _addon_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _worker_script() -> str:
    return str(Path(__file__).resolve().parent / "thumb_worker.py")


def _blender_exe() -> str:
    try:
        bp = getattr(bpy.app, "binary_path", "") or ""
        if bp and os.path.isfile(bp):
            return bp
    except Exception:
        pass
    return shutil.which("blender") or "blender"


def _thumb_work_dir() -> Path:
    d = Path(bpy.app.tempdir or "/tmp") / "mu_browser_thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _estimate_thumb_cost(path: str, name: str = "") -> float:
    """Heuristic cost for thumbnail generation (higher = slower, should run first).

    Multi-level, cheap (no full MU parse):
      1. .mu file size (dominant)
      2. Path/name complexity keywords (IVA, FX, nested spaces, animations…)
      3. Sibling .cfg size + quick keyword hits (MODEL/texture/anim proxies)
      4. Presence of sibling texture files next to the .mu
    """
    cost = 0.0
    try:
        cost += float(os.path.getsize(path))
    except OSError:
        pass

    key = ("%s/%s" % (path or "", name or "")).lower().replace("\\", "/")

    # Heavy geometry / nested content
    for kw, w in (
        ("freeiva", 8e5),
        ("/spaces/", 5e5),
        ("_iva", 4e5),
        ("/iva/", 4e5),
        ("internal", 3e5),
        ("/fx/", 3e5),
        ("plume", 2.5e5),
        ("particle", 2e5),
        ("waterfall", 2e5),
        ("anim", 1.5e5),
        ("engine", 1e5),
        ("fairing", 8e4),
        ("shroud", 6e4),
        ("collider", -5e4),  # pure colliders finish almost instantly (placeholder)
        ("/col", -3e4),
        ("depthmask", -4e4),
        ("overlaymask", -4e4),
        ("mask", -2e4),
    ):
        if kw in key:
            cost += w

    # Sibling cfg: size + cheap keyword scan (textures / models / anim)
    try:
        base, _ = os.path.splitext(path)
        cfg = base + ".cfg"
        if not os.path.isfile(cfg):
            # often part.cfg sits next to the mu with a different stem
            parent = os.path.dirname(path)
            for fn in os.listdir(parent) if os.path.isdir(parent) else []:
                if fn.lower().endswith(".cfg"):
                    cfg = os.path.join(parent, fn)
                    break
            else:
                cfg = ""
        if cfg and os.path.isfile(cfg):
            try:
                cost += 0.25 * float(os.path.getsize(cfg))
            except OSError:
                pass
            try:
                # Cap read — enough for MODEL/texture/anim blocks
                with open(cfg, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read(65536).lower()
                for kw, w in (
                    ("texture", 4e4),
                    ("model", 3e4),
                    ("mesh", 2e4),
                    ("animation", 5e4),
                    ("anim", 2e4),
                    ("fx", 2e4),
                    ("emitter", 3e4),
                    ("module", 5e3),
                ):
                    cost += text.count(kw) * w
            except Exception:
                pass
    except Exception:
        pass

    # Sibling textures next to the .mu (rough proxy for material complexity)
    try:
        parent = os.path.dirname(path)
        if os.path.isdir(parent):
            tex_n = 0
            for fn in os.listdir(parent):
                low = fn.lower()
                if low.endswith((".dds", ".png", ".mbm", ".tga", ".jpg", ".jpeg")):
                    tex_n += 1
                    if tex_n >= 24:
                        break
            cost += tex_n * 2.5e4
    except Exception:
        pass

    return cost


def _sort_thumb_queue_heaviest_first(queue):
    """Sort queue in-place: longest expected renders first, shortest last."""
    if len(queue) <= 1:
        return queue
    scored = []
    for item in queue:
        path = item[0] if item else ""
        name = item[1] if item and len(item) > 1 else ""
        scored.append((_estimate_thumb_cost(path, name), item))
    scored.sort(key=lambda t: t[0], reverse=True)
    queue[:] = [item for _, item in scored]
    return queue


def _thumb_queue(br, *, force: bool, max_count: int):
    from . import thumbnails
    queue = []
    dev = bool(getattr(thumbnails, "DEV_OVERLAY", 0))
    for idx, item in enumerate(br.parts):
        path = item.mu_path
        name = item.name or ""
        if not path:
            if dev:
                print("[mu_thumb][DEV] QUEUE SKIP: no mu_path | index=%d name=%s" % (idx, name), flush=True)
            continue
        if not os.path.isfile(path):
            if dev:
                print("[mu_thumb][DEV] QUEUE SKIP: MU file missing | %s | %s" % (path, name), flush=True)
            continue
        need = True
        existing = None
        if not force:
            try:
                existing = thumbnails._resolve_cache_path(path, name)
            except Exception as e:
                if dev:
                    print("[mu_thumb][DEV] QUEUE resolve error | %s | %s | %s: %s" % (path, name, type(e).__name__, e), flush=True)
            if existing:
                need = False
                if dev:
                    source = "@thumb" if thumbnails.find_ksp_thumb(name) == existing else "cache"
                    print("[mu_thumb][DEV] QUEUE SKIP: existing %s | %s | %s -> %s" % (source, path, name, existing), flush=True)
        if need or force:
            queue.append((path, name, bool(need or force)))
            if dev:
                print("[mu_thumb][DEV] QUEUE ADD: %s | %s | force=%s" % (path, name, force), flush=True)
        if len(queue) >= int(max_count):
            if dev:
                print("[mu_thumb][DEV] QUEUE STOP: max_count=%d reached at index=%d" % (int(max_count), idx), flush=True)
            break
    # Heaviest first so long IVA/FX/engine thumbs start while short colliders
    # finish at the tail (better wall-clock with parallel workers).
    _sort_thumb_queue_heaviest_first(queue)
    if dev and queue:
        try:
            head = queue[0][0] if queue else ""
            tail = queue[-1][0] if queue else ""
            print(
                "[mu_thumb][DEV] QUEUE SORTED heaviest-first: %d jobs | first=%s | last=%s"
                % (len(queue), os.path.basename(head), os.path.basename(tail)),
                flush=True,
            )
        except Exception:
            pass
    if dev:
        print("[mu_thumb][DEV] QUEUE RESULT: %d jobs from %d browser parts" % (len(queue), len(br.parts)), flush=True)
    return queue


def _split_queue(need_items, n_workers):
    if not need_items:
        return []
    n = max(1, min(int(n_workers), len(need_items)))
    buckets = [[] for _ in range(n)]
    for i, item in enumerate(need_items):
        buckets[i % n].append(item)
    return [b for b in buckets if b]


def _read_progress_file(path):
    """Parse worker progress file. *current* may be fractional (one decimal)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
        if not text:
            return None
        parts = text.split("\t")
        cur = float(parts[0])
        tot = int(float(parts[1])) if len(parts) > 1 else 0
        msg = parts[2] if len(parts) > 2 else ""
        return cur, tot, msg
    except Exception:
        return None


def _spawn_thumb_workers(queue_need, force, gamedata, max_workers=None, show_attach_points=False):
    """Prepare job files on the main thread, launch Blender workers in a daemon thread.

    Returning immediately keeps the UI responsive. The job timer waits until
    spawn_done is set, then polls the process list that this thread fills.
    """
    if max_workers is None:
        max_workers = int(THUMB_MAX_WORKERS)
    buckets = _split_queue(queue_need, max_workers)
    if not buckets:
        _job["spawn_done"] = True
        return [], []

    work = _thumb_work_dir()
    stamp = str(int(time.time() * 1000))
    addon = _addon_root()
    worker_py = _worker_script()
    blender = _blender_exe()

    # Shared lists already installed on _job by the caller (or created here).
    procs = _job.setdefault("procs", [])
    progress_files = _job.setdefault("progress_files", [])
    # Clear any leftovers from a previous job.
    del procs[:]
    del progress_files[:]

    dev = False
    try:
        from . import thumbnails as _thumbs
        dev = bool(getattr(_thumbs, "DEV_OVERLAY", 0))
    except Exception:
        pass

    if dev:
        popen_kwargs = {"stdout": None, "stderr": None}
    else:
        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

    if sys.platform == "win32":
        flags = 0x00000200 if dev else (0x00000200 | 0x00000008)
        if THUMB_WORKER_BELOW_NORMAL:
            flags |= 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    # Cheap disk prep on the main thread so progress files exist immediately.
    pending = []  # (cmd, progfile_str, bucket_len, jobfile)
    for i, bucket in enumerate(buckets):
        jobfile = work / ("jobs_%s_%d.txt" % (stamp, i))
        progfile = work / ("prog_%s_%d.txt" % (stamp, i))
        with jobfile.open("w", encoding="utf-8") as fh:
            for path, name in bucket:
                fh.write("%s\t%s\n" % (path, name))
        Path(progfile).write_text("0\t%d\t\n" % len(bucket), encoding="utf-8")

        cmd = [
            blender,
            "--background",
            "--quiet",
            "--factory-startup",
            "--python-exit-code", "1",
            "--python", worker_py,
            "--",
            "--addon", addon,
            "--jobfile", str(jobfile),
            "--progress", str(progfile),
        ]
        if gamedata:
            cmd.extend(["--gamedata", gamedata])
        if force:
            cmd.append("--force")
        if show_attach_points:
            cmd.append("--show-attach-points")
        pending.append((cmd, str(progfile), len(bucket), jobfile))

    spawn_state = {"value": False}
    _job["spawn_done"] = spawn_state

    def _spawn_all():
        try:
            for cmd, progfile, n_jobs, jobfile in pending:
                if _job.get("cancel"):
                    break
                try:
                    proc = subprocess.Popen(cmd, **popen_kwargs)
                except Exception as e:
                    print("[mu_browser] failed to spawn thumb worker:", e, flush=True)
                    continue
                procs.append(proc)
                progress_files.append(progfile)
                if dev:
                    print(
                        "[mu_thumb][DEV] WORKER START: pid=%s jobs=%d jobfile=%s"
                        % (getattr(proc, "pid", "?"), n_jobs, jobfile),
                        flush=True,
                    )
        finally:
            spawn_state["value"] = True

    import threading
    threading.Thread(target=_spawn_all, name="mu-thumb-spawn", daemon=True).start()
    return procs, progress_files


def _start_thumbs_job(context, *, force: bool, max_count: int = 200, max_workers: int = None):
    kind = "regen" if force else "thumbs"
    title = "Regen Thumbnails" if force else "Generate Thumbnails"

    if job_is_active(kind):
        _job_request_cancel()
        # Let timer observe cancel and clean up
        return "cancel"

    if job_is_active():
        other = job_progress()[0]
        _job_request_cancel()
        _job_end()

    br = context.window_manager.ksp_mu_browser
    queue = _thumb_queue(br, force=force, max_count=max_count)
    if not queue:
        return "empty"

    need = [(p, n) for p, n, ng in queue if ng]
    load_only = [(p, n) for p, n, ng in queue if not ng]

    if not need and not force:
        from . import thumbnails
        thumbnails.schedule_preview_warmup(br)
        return "none"

    from ..preferences.preferences import Preferences
    gd = (Preferences().GameData or "").strip()

    total = len(need) if need else 1
    _job_start(kind, title, total, context=context)
    _job["load_only"] = load_only
    _job["procs"] = []
    _job["progress_files"] = []
    # spawn_done becomes a dict{"value": False} inside _spawn_thumb_workers;
    # the job timer waits until value is True so UI stays responsive during Popen.
    _job["spawn_done"] = {"value": False}

    procs, prog_files = _spawn_thumb_workers(
        need, force=force, gamedata=gd,
        max_workers=int(max_workers if max_workers is not None else THUMB_MAX_WORKERS),
        show_attach_points=bool(getattr(br, "show_attach_points", False)),
    )
    # Same list objects the spawn thread appends to.
    _job["procs"] = procs
    _job["progress_files"] = prog_files

    # Do NOT treat empty procs as failure here — spawn is async. The timer
    # reports failure only after spawn_done is set and the list is still empty.
    _ensure_job_timer()
    return "started"


# ---------------------------------------------------------------------------
# Operators — execute only, no modal
# ---------------------------------------------------------------------------

class KSPMU_OT_MuBrowserRefresh(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_refresh"
    bl_label = "Refresh Parts"
    bl_description = "Rescan GameData and rebuild the current category list"
    bl_options = {"REGISTER"}

    force: BoolProperty(default=True)

    def execute(self, context):
        from . import catalog
        from . import thumbnails
        from ..preferences.preferences import Preferences

        if job_is_active("thumbs") or job_is_active("regen"):
            _job_request_cancel()
            self.report({"WARNING"}, "Cancelled background job")
            return {"CANCELLED"}

        if job_is_active("catalog"):
            # Soft dismiss only — scan thread cannot be killed mid-way
            self.report({"INFO"}, "Scan already running")
            return {"CANCELLED"}

        gd = (Preferences().GameData or "").strip()
        if not gd or not os.path.isdir(gd):
            self.report({"ERROR"}, "Set GameData path in Tool > Options")
            return {"CANCELLED"}

        catalog.invalidate_cache()
        thumbnails.invalidate_ksp_thumbs_index()
        thumbnails.schedule_ksp_thumbs_index_build(gd, force=True)
        context.window_manager.ksp_mu_browser.parts.clear()
        _start_catalog_job(context, gd, force=True)
        return {"FINISHED"}


class KSPMU_OT_MuBrowserGenThumbs(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_gen_thumbs"
    bl_label = "Generate Thumbnails"
    bl_description = "Generate missing part thumbnails in background Blender workers (click again to cancel)"
    bl_options = {"REGISTER"}

    force: BoolProperty(default=False)
    max_count: IntProperty(default=2000, min=1, max=5000)

    def execute(self, context):
        result = _start_thumbs_job(
            context, force=False, max_count=int(self.max_count), max_workers=int(THUMB_MAX_WORKERS)
        )
        if result == "cancel":
            self.report({"WARNING"}, "Cancelling Thumbs…")
            return {"CANCELLED"}
        if result == "empty":
            self.report({"INFO"}, "No parts in category")
            return {"FINISHED"}
        if result == "none":
            self.report({"INFO"}, "No thumbnails to generate")
            return {"FINISHED"}
        if result == "spawn_failed":
            self.report({"ERROR"}, "Failed to start background Blender workers")
            return {"CANCELLED"}
        return {"FINISHED"}


class KSPMU_OT_MuBrowserRegenThumbs(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_regen_thumbs"
    bl_label = "Regenerate Thumbnails"
    bl_description = "Force-regenerate part thumbnails in background Blender workers (click again to cancel)"
    bl_options = {"REGISTER"}

    force: BoolProperty(default=True)
    max_count: IntProperty(default=2000, min=1, max=5000)

    def execute(self, context):
        result = _start_thumbs_job(
            context, force=True, max_count=int(self.max_count), max_workers=int(THUMB_MAX_WORKERS)
        )
        if result == "cancel":
            self.report({"WARNING"}, "Cancelling Regen…")
            return {"CANCELLED"}
        if result == "empty":
            self.report({"INFO"}, "No parts in category")
            return {"FINISHED"}
        if result == "spawn_failed":
            self.report({"ERROR"}, "Failed to start background Blender workers")
            return {"CANCELLED"}
        return {"FINISHED"}


class KSPMU_OT_MuBrowserSelect(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_select"
    bl_label = "Select Part"
    bl_description = "Select this part in the browser"
    bl_options = {"INTERNAL"}

    index: IntProperty(default=0)

    def execute(self, context):
        br = context.window_manager.ksp_mu_browser
        n = len(br.parts)
        if n <= 0:
            return {"CANCELLED"}
        i = max(0, min(int(self.index), n - 1))
        br.parts_index = i
        return {"FINISHED"}


ATTACH_VIEW_RADIUS = 0.075
ATTACH_VIEW_ALPHA = 0.55
ATTACH_VIEW_COLOR = (0.05, 1.0, 0.10, 1.0)
_ATTACH_COLLECTION_NAME = "MU Attach Points"
_ATTACH_PREFIX = "MU_ATTACH_POINT_"

def _attach_collection(scene):
    col = bpy.data.collections.get(_ATTACH_COLLECTION_NAME)
    if col is None:
        col = bpy.data.collections.new(_ATTACH_COLLECTION_NAME)
        scene.collection.children.link(col)
    return col

def _clear_attach_markers(scene):
    col = bpy.data.collections.get(_ATTACH_COLLECTION_NAME)
    if col is None:
        return
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    try:
        bpy.data.collections.remove(col)
    except Exception:
        pass

def _unity_pos_to_blender(x, y, z):
    """KSP/Unity Y-up → Blender Z-up (same convention as typical MU imports).

    Unity (x, y, z) with Y up maps to Blender (x, z, y) with Z up.
    A missing swap here shows attach markers rotated 90° vs the mesh.
    """
    return (float(x), float(z), float(y))


def _parse_cfg_attach_nodes(cfg_path):
    """Parse node_* lines from a part .cfg.

    Format: node_stack_top = x, y, z, dx, dy, dz, size
    Positions are Unity/KSP model space (Y-up); we convert to Blender.
    """
    out = []
    if not cfg_path or not os.path.isfile(cfg_path):
        return out
    try:
        text = Path(cfg_path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        text = Path(cfg_path).read_text(encoding="cp1252", errors="replace")
    rx = re.compile(r"^\s*(node_[A-Za-z0-9_.-]+)\s*=\s*([^\r\n]+)", re.MULTILINE)
    for m in rx.finditer(text):
        vals = m.group(2).split("//", 1)[0].strip().split(",")
        if len(vals) < 3:
            continue
        try:
            ux, uy, uz = (float(vals[i].strip()) for i in range(3))
        except Exception:
            continue
        out.append((m.group(1), _unity_pos_to_blender(ux, uy, uz)))
    return out

def _make_attach_marker(col, location, index, label, *, parent=None, local_pos=None):
    """Create a green attach-point sphere without stealing viewport selection.

    If *parent* is given, the marker is parented so it follows part/node
    animation. *local_pos* is the translation in parent local space (CFG
    model-space already converted to Blender axes).
    """
    mesh = bpy.data.meshes.new(f"{_ATTACH_PREFIX}{index:03d}_mesh")
    # Low-poly UV sphere via bmesh — no bpy.ops, so selection stays intact.
    try:
        import bmesh
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(
            bm,
            u_segments=16,
            v_segments=8,
            radius=float(ATTACH_VIEW_RADIUS),
        )
        bm.to_mesh(mesh)
        bm.free()
    except Exception:
        # Fallback: single-vert "point" if bmesh fails
        mesh.from_pydata([(0, 0, 0)], [], [])
        mesh.update()

    obj = bpy.data.objects.new(f"{_ATTACH_PREFIX}{index:03d}", mesh)
    obj["ksp_attach_node"] = label
    obj.color = ATTACH_VIEW_COLOR
    # Helpers only — never become the active/selected object.
    try:
        obj.hide_select = True
    except Exception:
        pass
    try:
        obj.show_name = False
    except Exception:
        pass

    mat = bpy.data.materials.new(f"MU Attach Point {index:03d}")
    mat.diffuse_color = ATTACH_VIEW_COLOR
    mat.use_nodes = True
    try:
        bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Base Color"].default_value = ATTACH_VIEW_COLOR
        bsdf.inputs["Alpha"].default_value = ATTACH_VIEW_ALPHA
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.2
        if "Emission Color" in bsdf.inputs:
            bsdf.inputs["Emission Color"].default_value = ATTACH_VIEW_COLOR
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = 0.25
    except Exception:
        pass
    try:
        mat.surface_render_method = "DITHERED"
    except Exception:
        pass
    try:
        mesh.materials.append(mat)
    except Exception:
        pass

    col.objects.link(obj)

    if parent is not None:
        try:
            obj.parent = parent
            if local_pos is not None:
                obj.location = local_pos
            else:
                # Keep world position at *location* under this parent.
                from mathutils import Matrix, Vector
                mw = Matrix.Translation(Vector(location))
                obj.matrix_world = mw
                # Re-express as parent-local after parenting.
                obj.matrix_parent_inverse = parent.matrix_world.inverted()
                obj.location = parent.matrix_world.inverted() @ Vector(location)
                obj.rotation_euler = (0, 0, 0)
                obj.scale = (1, 1, 1)
        except Exception:
            try:
                obj.location = location
            except Exception:
                pass
    else:
        try:
            obj.location = location
        except Exception:
            pass
    return obj


def _find_node_empty(root, node_key):
    """Match CFG node name to an imported EMPTY (e.g. node_stack_top)."""
    if root is None or not node_key:
        return None
    key = str(node_key).lower().strip()
    try:
        objs = [root] + list(getattr(root, "children_recursive", []) or [])
    except Exception:
        objs = [root]
    for obj in objs:
        try:
            if getattr(obj, "type", None) != "EMPTY":
                continue
            n = (obj.name or "").lower()
            # Exact / startswith — Blender may suffix .001
            if n == key or n.startswith(key + ".") or n.startswith(key + "_"):
                return obj
        except Exception:
            pass
    return None


def update_attach_point_markers(context, enabled):
    """Show/hide green attach markers. Never changes the user's selection."""
    scene = bpy.context.scene

    # Snapshot selection + active — sphere ops used to steal both.
    prev_active = None
    prev_selected = []
    try:
        prev_active = getattr(context.view_layer.objects, "active", None)
    except Exception:
        pass
    try:
        prev_selected = [o for o in context.selected_objects]
    except Exception:
        try:
            prev_selected = [o for o in scene.objects if getattr(o, "select_get", lambda: False)()]
        except Exception:
            prev_selected = []

    _clear_attach_markers(scene)
    count = 0
    if enabled:
        from mathutils import Vector
        roots = []
        for obj in scene.objects:
            try:
                if obj.parent is None and obj.get("ksp_mu_path") and obj.get("ksp_part_name"):
                    roots.append(obj)
            except Exception:
                pass
        col = _attach_collection(scene)
        for root in roots:
            cfg = str(
                root.get("ksp_cfg_path")
                or Path(str(root.get("ksp_mu_path"))).with_suffix(".cfg")
            )
            for key, pos in _parse_cfg_attach_nodes(cfg):
                local = Vector(pos)
                # Prefer the real node EMPTY so markers ride node/armature anim.
                node_empty = _find_node_empty(root, key)
                parent = node_empty if node_empty is not None else root
                # CFG coords are model-space relative to the part root.
                # If parent is a node empty, put marker at local origin (empty
                # already sits on the attach point). If parent is root, use CFG pos.
                if node_empty is not None:
                    _make_attach_marker(
                        col, node_empty.matrix_world.translation, count, key,
                        parent=node_empty, local_pos=(0.0, 0.0, 0.0),
                    )
                else:
                    world = root.matrix_world @ local
                    _make_attach_marker(
                        col, world, count, key,
                        parent=root, local_pos=local,
                    )
                count += 1

    # Restore previous selection exactly.
    try:
        for o in list(getattr(context, "selected_objects", []) or []):
            try:
                o.select_set(False)
            except Exception:
                pass
    except Exception:
        pass
    for o in prev_selected:
        try:
            if o.name in scene.objects:
                o.select_set(True)
        except Exception:
            pass
    try:
        if prev_active is not None and prev_active.name in scene.objects:
            context.view_layer.objects.active = prev_active
    except Exception:
        pass
    return count


class KSPMU_OT_MuBrowserImportPart(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_import_part"
    bl_label = "Import Part"
    bl_description = "Import the selected part .mu into the scene"
    bl_options = {"REGISTER", "UNDO"}

    part_name: StringProperty(default="")

    def execute(self, context):
        from . import catalog

        br = context.window_manager.ksp_mu_browser
        name = self.part_name
        if not name:
            if 0 <= br.parts_index < len(br.parts):
                name = br.parts[br.parts_index].name
        entry = catalog.find_part(name)
        if entry is None or not entry.mu_path:
            self.report({"ERROR"}, "Part not found")
            return {"CANCELLED"}

        collection = context.view_layer.active_layer_collection.collection
        try:
            from ..import_mu.progress_util import mu_progress_bar
            from ..import_mu.import_mu import import_mu as import_mu_file
            with mu_progress_bar(context, total=100, title="MU Import"):
                ret = import_mu_file(collection, entry.mu_path, False, False)
            root = ret[0] if isinstance(ret, tuple) else ret
        except Exception as e:
            self.report({"ERROR"}, "Import failed: %s" % e)
            return {"CANCELLED"}
        try:
            import mathutils
            root.matrix_world = mathutils.Matrix.Identity(4)
        except Exception as e:
            print("[MU Browser] Failed to reset root:", e)

        try:
            root["ksp_part_name"] = entry.name
            root["ksp_part_category"] = entry.category
            root["ksp_attach_rules"] = ",".join(str(x) for x in entry.attach_rules)
            root["ksp_mu_path"] = entry.mu_path
            root["ksp_cfg_path"] = entry.cfg_path or ""
        except Exception:
            pass

        for o in context.scene.objects:
            try:
                o.select_set(False)
            except Exception:
                pass
        try:
            context.view_layer.objects.active = root
            root.select_set(True)
        except Exception:
            pass

        # Refresh attach-point markers if the user has them enabled.
        try:
            if bool(getattr(br, "show_attach_points", False)):
                update_attach_point_markers(context, True)
        except Exception:
            pass

        self.report({"INFO"}, "Imported %s" % (entry.title or entry.name))
        return {"FINISHED"}


class KSPMU_OT_MuBrowserSelectIndex(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_select_index"
    bl_label = "Select Part"
    bl_description = "Select this part in the browser grid"
    bl_options = {"REGISTER"}

    index: bpy.props.IntProperty(default=0, min=0)

    def execute(self, context):
        br = context.window_manager.ksp_mu_browser
        idx = int(self.index)
        if idx < 0 or idx >= len(br.parts):
            return {"CANCELLED"}
        br.parts_index = idx
        try:
            from .properties import _enum_cache_string
            br.part_preview = _enum_cache_string("PART_%d" % idx)
        except Exception:
            try:
                br.part_preview = "PART_%d" % idx
            except Exception:
                pass
        return {"FINISHED"}


classes_to_register = (
    KSPMU_OT_MuBrowserSelectIndex,
    KSPMU_OT_MuBrowserRefresh,
    KSPMU_OT_MuBrowserGenThumbs,
    KSPMU_OT_MuBrowserRegenThumbs,
    KSPMU_OT_MuBrowserSelect,
    KSPMU_OT_MuBrowserImportPart,
)

