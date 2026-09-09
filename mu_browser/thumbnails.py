# vim:ts=4:et
# <pep8 compliant>
"""Lazy OpenGL thumbnails for MU browser parts (disk-cached).

Priority when resolving an icon:
  1. Official KSP @thumbs PNG
  2. Our generated cache
  3. Generate thumbnail when ensure=True
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import json
from pathlib import Path
from typing import Dict, Optional

import bpy
from bpy.utils import previews


_CACHE_CLEARED = True

_pcoll = None
_ICON_SIZE = 128  # TEST SIZE: change manually to 128 / 256 / 512 when benchmarking render time
_DEFAULT_THUMB_NAME = "_default_thumbnail.png"
_DEFAULT_PREVIEW_KEY = "__MU_DEFAULT_THUMB__"

# ---------------------------------------------------------------------------
# Tunables (edit here)
# ---------------------------------------------------------------------------
# Extra padding around the fitted orthographic frame (1.0 = tight, 1.1 = 10%)
THUMB_FRAME_MARGIN = 1.3

# FX-only tunables. These are deliberately independent from normal-part framing.
FX_FRAME_MARGIN = 2.5      # padding when framing FX objects only (not full mesh)
FX_BRIGHTNESS_FACTOR = 0.35  # scale FX object emission only (not scene lights/world)

# KSP-like attach point marker tunables.
ATTACH_POINT_RADIUS = 0.06
ATTACH_POINT_ALPHA = 0.45
ATTACH_POINT_COLOR = (0.05, 1.0, 0.10, 1.0)

# Fixed isometric-ish camera direction (normalized later)
THUMB_CAM_DIR = (0.9, -1.1, 0.7)

# Render validation / retry thresholds. A file existing on disk is NOT enough
# to call a thumbnail valid: Blender can legitimately write a nearly-empty
# transparent PNG when the camera fit is pathological.
THUMB_MIN_CONTENT_PIXELS = 32
THUMB_MIN_CONTENT_BBOX_AREA = 40
THUMB_EDGE_TOLERANCE = 1
THUMB_ALPHA_THRESHOLD = 0.02
THUMB_MAX_RETRIES = 2
THUMB_RETRY_MARGIN = 1.60

# Developer diagnostics: render framing guides directly into generated thumbnails.
# 0 = normal thumbnails, 1 = show all framing guides.
DEV_OVERLAY = 0
# DEV console: 0 = only errors/critical diagnostics, 1 = compact fix-oriented diagnostics.
DEV_LOG_LEVEL = 0


def _dev_info(*args, **kwargs):
    """Compact developer-only log; kept intentionally quiet for batch runs."""
    if DEV_OVERLAY and DEV_LOG_LEVEL:
        print("[mu_thumb][DEV]", *args, flush=True)


def _info(*args, **kwargs):
    """Normal informational log; suppressed in DEV batch mode."""
    if not DEV_OVERLAY:
        print(*args, **kwargs)


# Persistent browser thumbnail preferences. Stored in Blender's user config,
# not in the .blend, so the value survives Blender restarts.
_THUMB_PREFS_FILE = Path(bpy.utils.user_resource("CONFIG", path="io_object_mu", create=True)) / "thumbnail_prefs.json"
_INCLUDE_STOCK_THUMBS_DEFAULT = True


def _read_thumb_prefs():
    try:
        data = json.loads(_THUMB_PREFS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def include_stock_thumbs() -> bool:
    return bool(_read_thumb_prefs().get("include_stock_thumbs", _INCLUDE_STOCK_THUMBS_DEFAULT))


def set_include_stock_thumbs(value: bool):
    data = _read_thumb_prefs()
    data["include_stock_thumbs"] = bool(value)
    try:
        _THUMB_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _THUMB_PREFS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, _THUMB_PREFS_FILE)
    except Exception as e:
        print("[mu_thumb] preference write failed:", type(e).__name__, e)


# Official KSP @thumbs index
_ksp_thumbs: Dict[str, str] = {}
_ksp_thumbs_root: str = ""
_ksp_thumbs_built: bool = False

_KSP_ICON_RE = re.compile(
    r"^(?P<name>.+)_icon(?P<var>0?)\.png$",
    re.IGNORECASE,
)

def _cache_dir() -> str:
    global _CACHE_CLEARED

    addon_dir = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    cache_dir = os.path.join(
        addon_dir,
        "cache",
        "thumbnails",
    )
    if not _CACHE_CLEARED:
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)

        os.makedirs(cache_dir, exist_ok=True)
        _CACHE_CLEARED = True
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _safe_filename(s: str, max_len: int = 60) -> str:
    if not s:
        return "unknown"
    safe = "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in s
    )
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_.")[:max_len] or "unknown"


def _new_cache_basename(mu_path: str, part_name: str = "") -> str:
    base = os.path.splitext(
        os.path.basename(mu_path or "")
    )[0]
    mu_safe = _safe_filename(base, 40)
    part_safe = _safe_filename(
        part_name or "part",
        40,
    )
    return "%s_%s" % (mu_safe, part_safe)


def _cache_path(mu_path: str, part_name: str = "") -> str:
    return os.path.join(
        _cache_dir(),
        _new_cache_basename(
            mu_path,
            part_name,
        ) + ".png",
    )


def _unique_write_path(mu_path: str, part_name: str = "") -> str:
    base = _new_cache_basename(
        mu_path,
        part_name,
    )
    primary = os.path.join(
        _cache_dir(),
        base + ".png",
    )
    if not os.path.isfile(primary):
        return primary
    for i in range(1, 1000):
        cand = os.path.join(
            _cache_dir(),
            "%s_%d.png" % (base, i),
        )
        if not os.path.isfile(cand):
            return cand
    return primary

def _part_name_keys(part_name: str):
    """Yield lookup keys for a part name."""
    n = (part_name or "").strip()
    if not n:
        return
    seen = set()
    for k in (
        n,
        n.replace("_", "."),
        n.replace(".", "_"),
        n.lower(),
        n.replace("_", ".").lower(),
        n.replace(".", "_").lower(),
    ):
        if k and k not in seen:
            seen.add(k)
            yield k


def _gamedata_root() -> str:
    try:
        from ..preferences.preferences import Preferences
        return (
            Preferences().GameData
            or ""
        ).strip().replace(
            "\\",
            "/",
        ).rstrip("/")
    except Exception:
        return ""


def invalidate_ksp_thumbs_index():
    global _ksp_thumbs
    global _ksp_thumbs_root
    global _ksp_thumbs_built

    _ksp_thumbs = {}
    _ksp_thumbs_root = ""
    _ksp_thumbs_built = False


_ksp_index_job = None
_ksp_index_timer_active = False

def schedule_ksp_thumbs_index_build(gamedata_root: Optional[str] = None, *, force: bool = False):
    """Build the official @thumbs index incrementally."""
    global _ksp_index_job, _ksp_index_timer_active
    root = (gamedata_root or _gamedata_root()).replace("\\", "/").rstrip("/")
    if not root or not os.path.isdir(root):
        invalidate_ksp_thumbs_index()
        return
    if not force and _ksp_thumbs_built and _ksp_thumbs_root == root:
        return
    invalidate_ksp_thumbs_index()
    _ksp_index_job = {"root": root, "walk": os.walk(root), "index": {}}
    if _ksp_index_timer_active:
        return
    _ksp_index_timer_active = True
    def tick():
        global _ksp_index_job, _ksp_index_timer_active, _ksp_thumbs, _ksp_thumbs_root, _ksp_thumbs_built
        job = _ksp_index_job
        if not job:
            _ksp_index_timer_active = False
            return None
        try:
            for _ in range(80):
                try:
                    dirpath, dirnames, filenames = next(job["walk"])
                except StopIteration:
                    _ksp_thumbs = job["index"]
                    _ksp_thumbs_root = job["root"]
                    _ksp_thumbs_built = True
                    _ksp_index_job = None
                    _ksp_index_timer_active = False
                    return None
                dirnames[:] = [d for d in dirnames if d and (d[0] not in "._" or d == "@thumbs")]
                if os.path.basename(dirpath) != "@thumbs":
                    continue
                for fn in filenames:
                    if not fn.lower().endswith('.png'):
                        continue
                    m = _KSP_ICON_RE.match(fn)
                    if not m:
                        continue
                    path = os.path.join(dirpath, fn).replace('\\','/')
                    try:
                        if os.path.getsize(path) <= 64:
                            continue
                    except OSError:
                        continue
                    for key in _part_name_keys(m.group('name')):
                        job["index"].setdefault(key, path)
            return 0.01
        except Exception as e:
            print('[mu_thumb] @thumbs index failed:', type(e).__name__, e)
            _ksp_index_job = None
            _ksp_index_timer_active = False
            return None
    try:
        bpy.app.timers.register(tick, first_interval=0.15)
    except Exception:
        _ksp_index_timer_active = False

def build_ksp_thumbs_index(gamedata_root: Optional[str] = None, *, force: bool = False) -> int:
    global _ksp_thumbs
    global _ksp_thumbs_root
    global _ksp_thumbs_built
    root = (
        gamedata_root or _gamedata_root()
    ).replace(
        "\\",
        "/",
    ).rstrip("/")
    if not root or not os.path.isdir(root):
        _ksp_thumbs = {}
        _ksp_thumbs_root = root or ""
        _ksp_thumbs_built = True
        return 0
    if (
        not force
        and _ksp_thumbs_built
        and _ksp_thumbs_root == root
        and _ksp_thumbs
    ):
        return len(_ksp_thumbs)
    index: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d
            and (
                d[0] not in "._"
                or d == "@thumbs"
            )
        ]
        if os.path.basename(dirpath) != "@thumbs":
            continue
        for fn in filenames:
            if not fn.lower().endswith(".png"):
                continue
            m = _KSP_ICON_RE.match(fn)
            if not m:
                continue
            part = m.group("name")
            path = os.path.join(
                dirpath,
                fn,
            ).replace(
                "\\",
                "/",
            )
            if (
                not os.path.isfile(path)
                or os.path.getsize(path) <= 64
            ):
                continue
            for key in _part_name_keys(part):

                if key not in index:
                    index[key] = path

    _ksp_thumbs = index
    _ksp_thumbs_root = root
    _ksp_thumbs_built = True

    #print("[mu_thumb] KSP @thumbs index: %d icons under %s" % (len(index), root))
    return len(index)


def _dev_log(*args):
    if DEV_OVERLAY:
        print("[mu_thumb][DEV]", *args, flush=True)


def _dev_part_label(mu_path: str, part_name: str) -> str:
    return "%s | part=%s" % (os.path.basename(mu_path or "?"), part_name or "?")


def find_ksp_thumb(part_name: str) -> Optional[str]:
    if not include_stock_thumbs():
        return None
    if not part_name:
        _dev_log("@thumb MISS: empty part name")
        return None
    if not _ksp_thumbs_built:
        # The async index may not have finished yet. For correctness, build it
        # synchronously on first lookup so stock @thumb icons are never treated
        # as missing merely because the timer has not completed.
        root = _gamedata_root()
        if root and os.path.isdir(root):
            try:
                build_ksp_thumbs_index(root, force=False)
                _dev_log("@thumb index was not ready -> synchronous build, entries=", len(_ksp_thumbs))
            except Exception as e:
                _dev_log("@thumb synchronous index FAILED:", type(e).__name__, e)
    for key in _part_name_keys(part_name):
        path = _ksp_thumbs.get(key)
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size > 64:
                return path
    return None


def _resolve_cache_path(mu_path: str, part_name: str = "") -> Optional[str]:
    ksp = find_ksp_thumb(part_name)
    if ksp:
        return ksp
    path = _cache_path(mu_path, part_name)
    if os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > 64:
            return path
    return None


def cached_icon_id_for_part(mu_path: str, part_name: str = "") -> int:
    """Return a registered preview icon, falling back to the default icon."""
    if not mu_path:
        return 0
    pcoll = _pcoll
    if pcoll is None:
        return 0
    key_src = "%s|%s" % (mu_path or "", part_name or "")
    key = hashlib.sha1(key_src.encode("utf-8", errors="replace")).hexdigest()[:20]
    try:
        item = pcoll.get(key)
        if item:
            return int(item.icon_id)

        # Preserve the original placeholder while background warmup has
        # not registered this part's real thumbnail yet.
        default = pcoll.get(_DEFAULT_PREVIEW_KEY)
        if default:
            return int(default.icon_id)
        default_path = _ensure_default_thumbnail()
        pcoll.load(_DEFAULT_PREVIEW_KEY, default_path, "IMAGE")
        return int(pcoll[_DEFAULT_PREVIEW_KEY].icon_id)
    except Exception:
        return 0


_preview_warmup_active = False
_preview_warmup_queue = []
_preview_warmup_pos = 0


def peek_icon_id(mu_path: str, part_name: str = "") -> int:
    """O(1) in-memory lookup only — never loads images or hits disk.

    Returns the real thumbnail if already registered, otherwise the default
    placeholder icon (so the grid is never empty/blank).
    """
    pcoll = _pcoll
    if pcoll is None:
        return 0
    if mu_path:
        key_src = "%s|%s" % (mu_path or "", part_name or "")
        key = hashlib.sha1(key_src.encode("utf-8", errors="replace")).hexdigest()[:20]
        try:
            item = pcoll.get(key)
            if item:
                return int(item.icon_id)
        except Exception:
            pass
    # Default placeholder (must already be in pcoll — ensure_previews loads it)
    try:
        default = pcoll.get(_DEFAULT_PREVIEW_KEY)
        if default:
            return int(default.icon_id)
    except Exception:
        pass
    return 0


def schedule_preview_warmup(br=None, *, chunk_size=8):
    """Warm cached thumbnails top→bottom, left→right without blocking the UI.

    template_icon_view opens instantly (icons may be 0); this fills them in
    place so the open popup populates row by row.
    """
    global _preview_warmup_active, _preview_warmup_queue, _preview_warmup_pos
    try:
        parts = list(br.parts)
        items = [(x.mu_path, x.name) for x in parts if x.mu_path]
    except Exception:
        return
    if not items:
        return
    # Prioritize currently exposed icons (preview_limit) so the open grid fills first
    try:
        lim = int(getattr(br, "preview_limit", 64) or 64)
        lim = max(64, min(lim, len(items)))
        head = items[:lim]
        tail = items[lim:]
        items = head + tail
    except Exception:
        pass
    sig = (getattr(br, "preview_limit", 64),) + tuple(items)
    current_sig = getattr(schedule_preview_warmup, "_sig", None)
    if sig != current_sig:
        schedule_preview_warmup._sig = sig
        _preview_warmup_queue = items
        _preview_warmup_pos = 0
    if _preview_warmup_active:
        return
    _preview_warmup_active = True

    def tick():
        global _preview_warmup_active, _preview_warmup_pos
        # First chunks a bit larger so the top rows of the 8x8 popup fill fast
        cs = int(chunk_size)
        if _preview_warmup_pos < 24:
            cs = max(cs, 12)
        end = min(_preview_warmup_pos + cs, len(_preview_warmup_queue))
        for path, name in _preview_warmup_queue[_preview_warmup_pos:end]:
            try:
                icon_id_for_part(path, name, ensure=False)
            except Exception:
                pass
        _preview_warmup_pos = end
        try:
            from .properties import bump_preview_enum_icons
            bump_preview_enum_icons()
        except Exception:
            pass
        try:
            br2 = bpy.context.window_manager.ksp_mu_browser
            cur = str(getattr(br2, "part_preview", "") or "")
            if cur.startswith("PART_"):
                br2.part_preview = cur
        except Exception:
            pass
        try:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
        except Exception:
            pass
        if _preview_warmup_pos >= len(_preview_warmup_queue):
            _preview_warmup_active = False
            return None
        return 0.02

    try:
        bpy.app.timers.register(tick, first_interval=0.01)
    except Exception:
        _preview_warmup_active = False


_preview_expand_active = False


def reset_preview_stream(br=None):
    """Call on category/filter change so the stream restarts from 64."""
    global _preview_expand_active, _preview_warmup_active
    _preview_expand_active = False
    # Allow warmup to rebuild for the new set
    try:
        schedule_preview_warmup._sig = None
    except Exception:
        pass
    if br is not None:
        try:
            from .properties import PREVIEW_INITIAL
            br.preview_limit = int(PREVIEW_INITIAL)
        except Exception:
            try:
                br.preview_limit = 64
            except Exception:
                pass


def schedule_preview_expand(br=None):

    """Grow preview_limit so the icon grid streams in like infinite scroll.

    Starts at 64; every tick adds PREVIEW_GROW_STEP until all parts are exposed.
    template_icon_view re-queries the enum on redraw, so the open popup gains
    rows as the limit rises — user can scroll through content as it arrives.
    """
    global _preview_expand_active
    if br is None:
        return
    try:
        from .properties import PREVIEW_INITIAL, PREVIEW_GROW_STEP
    except Exception:
        PREVIEW_INITIAL, PREVIEW_GROW_STEP = 64, 32
    try:
        n = len(br.parts)
    except Exception:
        return
    if n <= int(PREVIEW_INITIAL):
        try:
            br.preview_limit = n
        except Exception:
            pass
        return
    try:
        cur = int(getattr(br, "preview_limit", PREVIEW_INITIAL) or PREVIEW_INITIAL)
    except Exception:
        cur = int(PREVIEW_INITIAL)
    if cur >= n:
        return
    if _preview_expand_active:
        return
    _preview_expand_active = True

    def tick():
        global _preview_expand_active
        try:
            br2 = bpy.context.window_manager.ksp_mu_browser
            n2 = len(br2.parts)
            lim = int(getattr(br2, "preview_limit", PREVIEW_INITIAL) or PREVIEW_INITIAL)
            if lim >= n2 or n2 <= 0:
                _preview_expand_active = False
                return None
            # Grow in steps; first steps a bit larger so scroll gets content fast
            step = int(PREVIEW_GROW_STEP)  # default 32
            br2.preview_limit = min(n2, lim + step)
            # Warm icons for the newly exposed range and refresh open popup
            try:
                schedule_preview_warmup(br2, chunk_size=16)
            except Exception:
                pass
            try:
                cur_prev = str(getattr(br2, "part_preview", "") or "")
                if cur_prev.startswith("PART_"):
                    br2.part_preview = cur_prev
            except Exception:
                pass
            try:
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == "VIEW_3D":
                            area.tag_redraw()
            except Exception:
                pass
            return 0.08
        except Exception as e:
            print("[mu_browser] preview expand:", type(e).__name__, e)
            _preview_expand_active = False
            return None

    try:
        bpy.app.timers.register(tick, first_interval=0.15)
    except Exception:
        _preview_expand_active = False



def reload_browser_icons(br=None, *, force: bool = True):
    """Drop cached preview entries and reload icons from disk.

    Required after background Thumbs/Regen: ImagePreview keeps the old
    pixels for the same key even when the PNG on disk was overwritten.
    """
    global _preview_warmup_active, _preview_warmup_queue, _preview_warmup_pos
    pcoll = ensure_previews()
    items = []
    try:
        if br is not None:
            items = [(x.mu_path, x.name) for x in br.parts if x.mu_path]
    except Exception:
        items = []
    for mu_path, part_name in items:
        key_src = "%s|%s" % (mu_path or "", part_name or "")
        key = hashlib.sha1(
            key_src.encode("utf-8", errors="replace")
        ).hexdigest()[:20]
        if key in pcoll:
            try:
                del pcoll[key]
            except Exception:
                try:
                    pcoll.pop(key, None)
                except Exception:
                    pass
        # Also drop any bpy image that may keep stale pixels for the cache path
        try:
            path = _resolve_cache_path(mu_path, part_name)
            if path:
                img = bpy.data.images.get(os.path.basename(path))
                if img is not None:
                    bpy.data.images.remove(img)
        except Exception:
            pass
    # Force warmup queue rebuild
    try:
        schedule_preview_warmup._sig = None
    except Exception:
        pass
    _preview_warmup_active = False
    _preview_warmup_queue = []
    _preview_warmup_pos = 0
    if br is not None:
        schedule_preview_warmup(br, chunk_size=12)
        # Nudge EnumProperty so template_icon_view picks up new icon_ids
        try:
            idx = int(getattr(br, "parts_index", 0) or 0)
            cur = str(getattr(br, "part_preview", "") or "")
            want = "PART_%d" % max(0, idx)
            br.part_preview = ""
            br.part_preview = cur if cur.startswith("PART_") else want
        except Exception:
            pass
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()
                area.tag_redraw()
    except Exception:
        pass


def ensure_previews():
    global _pcoll
    if _pcoll is None:
        _pcoll = previews.new()
    # Always make sure the default placeholder is registered
    try:
        if _DEFAULT_PREVIEW_KEY not in _pcoll:
            path = _ensure_default_thumbnail()
            if path:
                _pcoll.load(_DEFAULT_PREVIEW_KEY, path, "IMAGE")
    except Exception as e:
        print("[mu_thumb] default preview load:", type(e).__name__, e)
    return _pcoll


def clear_previews():
    global _pcoll
    if _pcoll is not None:
        try:
            previews.remove(_pcoll)
        except Exception:
            pass
        _pcoll = None


def invalidate_preview_icons():
    """Drop cached per-part preview images without touching the default icon.

    A stock @thumb and our generated cache intentionally share the same
    in-memory preview key. Therefore changing Include stock thumbs must clear
    every per-part preview; otherwise Blender keeps displaying the old image
    until a later cache reload.
    """
    global _preview_warmup_active, _preview_warmup_queue, _preview_warmup_pos
    pcoll = ensure_previews()
    try:
        for key in list(pcoll.keys()):
            if key == _DEFAULT_PREVIEW_KEY:
                continue
            try:
                del pcoll[key]
            except Exception:
                try:
                    pcoll.pop(key, None)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        schedule_preview_warmup._sig = None
    except Exception:
        pass
    _preview_warmup_active = False
    _preview_warmup_queue = []
    _preview_warmup_pos = 0


def _is_fx_preview_obj(o):
    """
    Recognizes particle/FX objects used by the MU importer.
    IMPORTANT:
    Do not rely solely on mu_fx_preview, as some
    objects imported by MU only have mu_particles or the emitter name.
    """
    if o is None:
        return False
    try:
        if o.get("mu_particles"):
            return True
    except Exception:
        pass
    try:
        if o.get("mu_fx_preview"):
            return True
    except Exception:
        pass
    try:
        name = o.name or ""
    except Exception:
        name = ""
    lname = name.lower()
    if ".fx_preview" in lname:
        return True
    if ".fx_emitter" in lname:
        return True
    if ".cfg_preview" in lname:
        return True
    # MU particle emitters
    if "emitter" in lname:
        return True
    return False


def _collect_fx_objects(root=None):
    """
    Returns the FX belonging to the currently imported root.
    We don't search the entire user scene unnecessarily.
    """
    if root is not None:

        objs = [
            root
        ] + list(
            getattr(
                root,
                "children_recursive",
                [],
            ) or []
        )

    else:
        objs = list(
            bpy.context.scene.objects
        )

    result = []

    for obj in objs:

        try:
            if _is_fx_preview_obj(obj):
                result.append(obj)
        except Exception:
            pass
    return result


def _call_particle_show_operator(scene, root):
    """
    Attempts to execute EXACTLY the same operator that the user activates with the button:
    object.mu_toggle_particles_preview
    scope=ALL
    force=SHOW
    """

    fx_objects = _collect_fx_objects(root)

    if not fx_objects:
        _dev_info("particle operator: no FX")
        return []

    _dev_info("particle targets=", len(fx_objects))

    temporary_props = []

    for obj in fx_objects:

        try:
            had_prop = "mu_fx_preview" in obj
        except Exception:
            had_prop = False

        try:
            old_value = obj.get("mu_fx_preview")
        except Exception:
            old_value = None

        temporary_props.append(
            (
                obj,
                had_prop,
                old_value,
            )
        )

        try:
            obj["mu_fx_preview"] = True
        except Exception:
            pass

    result = None

    try:

        win = bpy.context.window

        if win is not None:
            try:
                win.scene = scene
            except Exception:
                pass

        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

        _dev_info("particle SHOW")

        try:
            result = bpy.ops.object.mu_toggle_particles_preview(
                scope="ALL",
                force="SHOW",
            )

            _dev_info("particle result=", result)

        except Exception as e:

            print(
                "[mu_thumb] particle operator failed:",
                type(e).__name__,
                e,
            )

    finally:
        for (
            obj,
            had_prop,
            old_value,
        ) in temporary_props:

            try:

                if had_prop:
                    obj["mu_fx_preview"] = old_value

                else:
                    if "mu_fx_preview" in obj:
                        del obj["mu_fx_preview"]

            except Exception:
                pass
    shown = []
    hidden = []
    for obj in fx_objects:
        try:
            hidden_state = bool(
                obj.hide_viewport
            )

            try:
                hidden_state = hidden_state or bool(
                    obj.hide_get()
                )
            except Exception:
                pass

            if hidden_state:
                hidden.append(obj.name)
            else:
                shown.append(obj.name)

        except Exception:
            pass

    _dev_info("FX shown=", len(shown))

    _dev_info("FX hidden=", len(hidden))
    return fx_objects


def _show_fx_for_thumbnail(root):
    objs = _collect_fx_objects(root)
    shown = []
    for obj in objs:
        try:
            obj.hide_set(False)
        except Exception:
            pass
        try:
            obj.hide_viewport = False
        except Exception:
            pass
        try:
            obj.hide_render = False
        except Exception:
            pass
        shown.append(obj.name)
    print(
        "[mu_thumb] FX fallback SHOW:",
        len(shown),
        shown,
    )
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    return shown


def _should_exclude_from_thumbnail(name: str, *, keep_shroud: bool = False) -> bool:
    """Objects excluded from thumbnails and from camera framing.

    colliders, nodes, fairings, shrouds (unless part is a shroud),
    higher LODs, .broken meshes, cfg/fx previews.
    """
    n = (name or "").lower()
    if not n:
        return False
    if n.startswith("node_") or n.startswith("node.") or ".node" in n:
        return True
    if "fairing" in n:
        return True
    if "shroud" in n and not keep_shroud:
        return True
    if ".broken" in n or n.endswith("broken") or "_broken" in n:
        return True
    for tag in ("lod1", "lod2", "lod3", "lod4", "lod 1", "lod 2", "lod 3"):
        if tag in n:
            return True
    if "cfg_preview" in n or "fx_preview" in n:
        return True
    if ".collider" in n or n.startswith("col_") or "collision" in n:
        return True
    if "collider" in n:
        return True
    try:
        from ..utils.utils import is_hideable_collider_name
        if is_hideable_collider_name(name or ""):
            return True
    except Exception:
        pass
    return False


def _hide_for_thumbnail(root, keep_shroud=False):
    stack = [root] + list(getattr(root, "children_recursive", []) or [])
    for obj in stack:
        if _is_fx_preview_obj(obj):
            continue
        try:
            name = obj.name or ""
        except Exception:
            continue
        if not _should_exclude_from_thumbnail(name, keep_shroud=keep_shroud):
            continue
        try:
            obj.hide_set(True)
        except Exception:
            pass
        try:
            obj.hide_render = True
        except Exception:
            pass


def _collect_frame_points(objs, *, keep_shroud: bool = False,
                          include_evaluated: bool = True,
                          include_raw: bool = True):
    """Collect conservative world-space bounds for rendered MU geometry.

    IMPORTANT: evaluated and raw bounds are collected independently. The old
    implementation stopped after the first successful evaluated bound-box,
    which meant a pathological/deferred evaluated bound could completely hide
    a perfectly valid raw mesh bound. For thumbnail framing we prefer the
    union of both representations.
    """
    from mathutils import Vector

    points = []
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None

    for o in objs:
        try:
            if getattr(o, "type", "") != "MESH":
                continue
            oname = o.name or ""
        except Exception:
            continue
        if _should_exclude_from_thumbnail(oname, keep_shroud=keep_shroud):
            continue
        try:
            if bool(getattr(o, "hide_render", False)):
                continue
        except Exception:
            pass
        try:
            if bool(o.hide_get()):
                continue
        except Exception:
            pass

        if include_evaluated and depsgraph is not None:
            try:
                eo = o.evaluated_get(depsgraph)
                eb = getattr(eo, "bound_box", None)
                emw = eo.matrix_world
                if eb and len(eb) == 8:
                    for corner in eb:
                        points.append(emw @ Vector(corner))
            except Exception:
                pass

        if include_raw:
            try:
                me = getattr(o, "data", None)
                mw = o.matrix_world.copy()
                if me is not None and len(me.vertices) > 0:
                    # Mesh.bound_box is the exact local AABB and gives all 8
                    # extrema without sampling vertices.
                    rb = getattr(me, "bound_box", None)
                    if rb and len(rb) == 8:
                        for corner in rb:
                            points.append(mw @ Vector(corner))
                    else:
                        # Last-resort vertex scan if bound_box is unavailable.
                        for v in me.vertices:
                            points.append(mw @ v.co)
            except Exception:
                pass

        # Object bound_box is only a final fallback when both requested sources
        # failed. It is intentionally not used as a substitute for raw mesh
        # geometry when raw data is available.
        if not points:
            try:
                for corner in o.bound_box:
                    points.append(o.matrix_world @ Vector(corner))
            except Exception:
                pass

    return points


def _frame_stats(points):
    """Return finite 3D AABB stats used to detect pathological bounds."""
    if not points:
        return None
    try:
        xs = [float(p.x) for p in points]
        ys = [float(p.y) for p in points]
        zs = [float(p.z) for p in points]
        vals = xs + ys + zs
        if not all(__import__('math').isfinite(v) for v in vals):
            return None
        mn = (min(xs), min(ys), min(zs))
        mx = (max(xs), max(ys), max(zs))
        size = max(mx[i] - mn[i] for i in range(3))
        return mn, mx, size
    except Exception:
        return None


def _load_render_metrics(path: str):
    """Validate the rendered PNG/container only; never classify pixels as cropped/tiny.

    Pixel-difference validation was fundamentally unreliable here: KSP materials,
    world lighting, transparent film, and Eevee shading can legitimately make the
    four corners different from the world background. That made correctly framed
    models look "cropped" and caused every generated thumbnail to be discarded.

    Framing is already guaranteed by _fit_ortho_camera() from the actual mesh bounds.
    Therefore this validator deliberately checks only that Blender produced a real
    image of the requested dimensions. Crop/tiny detection belongs to the geometric
    fit stage, not to post-render color analysis.
    """
    img = None
    try:
        img = bpy.data.images.load(path, check_existing=False)
        w, h = int(img.size[0]), int(img.size[1])
        if w != int(_ICON_SIZE) or h != int(_ICON_SIZE):
            return {
                "valid": False,
                "reason": "wrong_dimensions",
                "size": (w, h),
                "expected": (int(_ICON_SIZE), int(_ICON_SIZE)),
            }
        return {
            "valid": True,
            "reason": "ok",
            "size": (w, h),
        }
    except Exception as e:
        return {"valid": False, "reason": "image_inspect_error:%s" % e}
    finally:
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass

def _fit_ortho_camera(cam_obj, cam_data, world_coords, margin=1.30):
    """Fit an orthographic camera to the actual camera-plane extents.

    The old sphere fit is guaranteed not to crop only if *all rendered
    geometry* is represented by the supplied points. MU parts can have
    modifier/deformed bounds that are not present in the raw mesh AABB, so
    this function deliberately fits the supplied bounds conservatively and
    uses the exact same camera basis for measuring and rendering.
    """
    from mathutils import Vector

    if not world_coords:
        return False

    mn = Vector((
        min(p.x for p in world_coords),
        min(p.y for p in world_coords),
        min(p.z for p in world_coords),
    ))
    mx = Vector((
        max(p.x for p in world_coords),
        max(p.y for p in world_coords),
        max(p.z for p in world_coords),
    ))
    center = (mn + mx) * 0.5

    view_dir = Vector(tuple(float(v) for v in THUMB_CAM_DIR))
    if view_dir.length < 1e-8:
        view_dir = Vector((1.0, -1.0, 0.8))
    view_dir.normalize()

    # Blender's Track -Z / Y camera basis, constructed explicitly so the
    # fitting math cannot disagree with the camera transform.
    forward = -view_dir
    up_ref = Vector((0.0, 0.0, 1.0))
    right = forward.cross(up_ref)
    if right.length < 1e-8:
        up_ref = Vector((0.0, 1.0, 0.0))
        right = forward.cross(up_ref)
    right.normalize()
    up = right.cross(forward)
    up.normalize()

    distance = max((mx - mn).length * 2.0, 2.0)
    cam_obj.location = center + view_dir * distance
    cam_obj.rotation_euler = (
        center - cam_obj.location
    ).to_track_quat("-Z", "Y").to_euler()
    try:
        cam_obj.rotation_mode = "XYZ"
    except Exception:
        pass

    # Measure the actual world points in the camera plane. This is the
    # quantity an ORTHO camera really crops against; no 3-D diagonal guess.
    min_x = float("inf")
    max_x = float("-inf")
    min_y = float("inf")
    max_y = float("-inf")
    for p in world_coords:
        q = p - center
        x = q.dot(right)
        y = q.dot(up)
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)

    width = max(max_x - min_x, 0.05)
    height = max(max_y - min_y, 0.05)
    half_extent = max(width, height) * 0.5
    ortho = max(2.0 * half_extent * float(margin), 0.1)

    cam_data.type = "ORTHO"
    cam_data.ortho_scale = ortho
    try:
        cam_data.shift_x = 0.0
        cam_data.shift_y = 0.0
        cam_data.clip_start = 0.01
        cam_data.clip_end = max(distance * 4.0, (mx - mn).length * 10.0, 50.0)
    except Exception:
        pass

    print(
        "[mu_thumb] plane-fit center=%s extents=(%.4f, %.4f) ortho=%.4f margin=%.2f pts=%d"
        % (
            tuple(round(c, 4) for c in center),
            width,
            height,
            float(cam_data.ortho_scale),
            float(margin),
            len(world_coords),
        )
    )
    return True


def _dev_overlay_framing(scene, world_coords, center, view_dir, right, up, ortho_scale):
    """Render diagnostic framing guides when DEV_OVERLAY is enabled.

    Guides intentionally show several competing fitting ideas at once:
      - cyan-ish AABB wire box
      - magenta bounding sphere
      - yellow camera-plane rectangle
      - white centre cross
      - small camera-axis cross
    Everything is created in the temporary thumbnail scene and is therefore
    removed by the normal thumbnail cleanup pass.
    """
    if not DEV_OVERLAY or not world_coords:
        return

    from mathutils import Vector

    # Simple emission materials so guides remain visible on transparent PNGs.
    def mat(name, rgba):
        m = bpy.data.materials.new(name)
        m.diffuse_color = (*rgba, 1.0)
        m.use_nodes = True
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (*rgba, 1.0)
            bsdf.inputs["Emission Color"].default_value = (*rgba, 1.0)
            bsdf.inputs["Emission Strength"].default_value = 3.0
            bsdf.inputs["Roughness"].default_value = 0.35
        return m

    mats = {
        "box": mat("_mu_dbg_box", (0.1, 1.0, 1.0)),
        "sphere": mat("_mu_dbg_sphere", (1.0, 0.1, 1.0)),
        "frame": mat("_mu_dbg_frame", (1.0, 0.75, 0.05)),
        "cross": mat("_mu_dbg_cross", (1.0, 1.0, 1.0)),
        "axis": mat("_mu_dbg_axis", (1.0, 0.2, 0.1)),
    }

    def line_obj(name, pts, material, bevel=0.012):
        cu = bpy.data.curves.new(name, "CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = bevel
        cu.bevel_resolution = 0
        sp = cu.splines.new("POLY")
        sp.points.add(len(pts) - 1)
        for cp, p in zip(sp.points, pts):
            cp.co = (p.x, p.y, p.z, 1.0)
        ob = bpy.data.objects.new(name, cu)
        # Link into the same temporary collection as the camera.
        for c in scene.collection.children:
            if c.name.startswith("_mu_thumb"):
                c.objects.link(ob)
                break
        else:
            scene.collection.objects.link(ob)
        ob.data.materials.append(material)
        return ob

    mn = Vector((min(p.x for p in world_coords), min(p.y for p in world_coords), min(p.z for p in world_coords)))
    mx = Vector((max(p.x for p in world_coords), max(p.y for p in world_coords), max(p.z for p in world_coords)))

    # 1) World AABB (the exact box used to derive the centre).
    corners = [
        Vector((x, y, z))
        for x in (mn.x, mx.x)
        for y in (mn.y, mx.y)
        for z in (mn.z, mx.z)
    ]
    edges = ((0,1),(0,2),(0,4),(3,2),(3,1),(3,7),(5,1),(5,4),(5,7),(6,2),(6,4),(6,7))
    for a, b in edges:
        line_obj("_mu_dbg_aabb", (corners[a], corners[b]), mats["box"], 0.01)

    # 2) Bounding sphere around the same AABB centre.
    radius = max((mx - mn).length * 0.5, 0.05)
    # Two great circles are enough to make the sphere diagnostic obvious.
    for basis_a, basis_b in ((right, up), (right, view_dir), (up, view_dir)):
        pts = []
        for i in range(49):
            a = 6.283185307179586 * i / 48.0
            pts.append(center + radius * (basis_a * __import__('math').cos(a) + basis_b * __import__('math').sin(a)))
        line_obj("_mu_dbg_sphere", pts, mats["sphere"], 0.007)

    # 3) Actual camera-plane fit rectangle: ortho frame at the chosen scale.
    h = float(ortho_scale) * 0.5
    rect = [
        center + right * -h + up * -h,
        center + right *  h + up * -h,
        center + right *  h + up *  h,
        center + right * -h + up *  h,
    ]
    for i in range(4):
        line_obj("_mu_dbg_frame", (rect[i], rect[(i + 1) % 4]), mats["frame"], 0.012)

    # 4) Centre cross in the camera plane.
    cross = max(ortho_scale * 0.035, 0.03)
    line_obj("_mu_dbg_center", (center - right * cross, center + right * cross), mats["cross"], 0.012)
    line_obj("_mu_dbg_center", (center - up * cross, center + up * cross), mats["cross"], 0.012)

    # 5) Camera direction marker, just in front of the target.
    axis_len = max(ortho_scale * 0.12, 0.08)
    line_obj("_mu_dbg_axis", (center, center + view_dir * axis_len), mats["axis"], 0.014)

    _dev_info("DEV_OVERLAY: AABB+sphere+camera-frame+crosses")


def _autozoom_fx_camera(cam_obj, cam_data, fx_objects, margin=float(FX_FRAME_MARGIN)):
    from mathutils import Vector
    points = []
    for obj in fx_objects:
        try:
            if obj.hide_get() or obj.hide_viewport or obj.hide_render:
                continue
            # Bounding box object in world space
            for corner in obj.bound_box:
                points.append(obj.matrix_world @ Vector(corner))

        except Exception:
            continue

    if not points:
        return False

    mn = Vector((
        min(p.x for p in points),
        min(p.y for p in points),
        min(p.z for p in points),
    ))

    mx = Vector((
        max(p.x for p in points),
        max(p.y for p in points),
        max(p.z for p in points),
    ))

    size = (mx - mn).length

    if size <= 0.001:
        return False

    center = (mn + mx) * 0.5

    direction = cam_obj.location - center

    if direction.length < 0.001:
        direction = Vector((1.0, -1.0, 0.5))

    direction.normalize()

    cam_obj.location = center + direction * 3.5

    cam_obj.rotation_euler = (
        center - cam_obj.location
    ).to_track_quat("-Z", "Y").to_euler()

    # Autozoom.
    FX_ZOOM = 0.5
    cam_data.ortho_scale = max(size * margin * FX_ZOOM, 0.15)
    print(
        "[mu_thumb] FX AUTOZOOM:",
        "center=", tuple(center),
        "size=", round(size, 4),
        "ortho=", round(cam_data.ortho_scale, 4),
    )
    return True


def _setup_fx_camera(scene, root):
    from mathutils import Vector
    objs = [
        root
    ] + list(
        getattr(
            root,
            "children_recursive",
            [],
        ) or []
    )

    hosts = []

    for obj in objs:

        try:
            if obj.get("mu_particles"):
                hosts.append(obj)
                continue
        except Exception:
            pass

        try:
            if obj.get("mu_fx_preview"):
                hosts.append(obj)
        except Exception:
            pass

    # fallback
    if not hosts:
        hosts = [
            obj
            for obj in objs
            if _is_fx_preview_obj(obj)
        ]

    if not hosts:

        print(
            "[mu_thumb] FX camera: "
            "no particle hosts found"
        )

        return False

    points = []

    for obj in hosts:

        try:
            points.append(
                obj.matrix_world.translation.copy()
            )
        except Exception:
            pass

    if not points:

        print(
            "[mu_thumb] FX camera: "
            "no valid host positions"
        )

        return False

    center = (
        sum(
            points,
            Vector(
                (0.0, 0.0, 0.0)
            ),
        )
        / float(len(points))
    )

    cam = scene.camera

    if cam is None:
        return False

    distance = 3.5

    cam.location = (
        center
        + Vector(
            (
                distance * 0.7,
                -distance,
                distance * 0.35,
            )
        )
    )

    cam.rotation_euler = (
        center - cam.location
    ).to_track_quat(
        "-Z",
        "Y",
    ).to_euler()

    cam.data.type = "ORTHO"
    cam.data.ortho_scale = 0.8
    print(
        "[mu_thumb] FX camera:",
        "hosts=", len(hosts),
        "center=", tuple(center),
        "distance=", distance,
        "ortho=", cam.data.ortho_scale,
    )
    return True


def _add_sun(col, name, location, target, energy, color=(1.0, 1.0, 1.0)):
    from mathutils import Vector
    data = bpy.data.lights.new(
        name,
        "SUN",
    )
    data.energy = energy
    data.color = color
    try:
        data.angle = 0.15
    except Exception:
        pass

    obj = bpy.data.objects.new(
        name,
        data,
    )

    obj.location = location

    direction = (
        Vector(target)
        - Vector(location)
    )

    if direction.length > 1e-8:

        obj.rotation_euler = (
            direction
            .to_track_quat(
                "-Z",
                "Y",
            )
            .to_euler()
        )

    col.objects.link(obj)

    return obj


def _attach_nodes_under(root):
    """Return KSP attachment-node empties from the imported part."""
    if root is None:
        return []
    try:
        objs = [root] + list(getattr(root, "children_recursive", []) or [])
    except Exception:
        objs = [root]
    out = []
    for obj in objs:
        try:
            if not str(obj.name or "").lower().startswith("node_"):
                continue
            if getattr(obj, "type", None) != "EMPTY":
                continue
            out.append(obj)
        except Exception:
            pass
    return out


def _add_attach_point_markers(scene, col, root):
    """Draw translucent green spheres at KSP attachment nodes."""
    nodes = _attach_nodes_under(root)
    if not nodes:
        return 0
    mat = bpy.data.materials.new("_mu_attach_point")
    mat.diffuse_color = (*ATTACH_POINT_COLOR[:3], ATTACH_POINT_ALPHA)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = ATTACH_POINT_COLOR
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = float(ATTACH_POINT_ALPHA)
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.25
        if "Emission Color" in bsdf.inputs:
            bsdf.inputs["Emission Color"].default_value = ATTACH_POINT_COLOR
        if "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = 0.35
    try:
        mat.surface_render_method = "DITHERED"
    except Exception:
        try:
            mat.surface_render_method = "BLENDED"
        except Exception:
            try:
                mat.blend_method = "BLEND"
            except Exception:
                pass
    count = 0
    for node in nodes:
        try:
            bpy.ops.mesh.primitive_uv_sphere_add(
                segments=16, ring_count=8,
                radius=float(ATTACH_POINT_RADIUS),
                location=node.matrix_world.translation,
            )
            marker = bpy.context.object
            marker.name = "_mu_attach_point_%03d" % count
            marker.data.materials.append(mat)
            for c in list(marker.users_collection):
                try:
                    c.objects.unlink(marker)
                except Exception:
                    pass
            col.objects.link(marker)
            count += 1
        except Exception:
            pass
    return count


def _scale_fx_object_brightness(fx_objects, factor):
    """Dim only FX objects' emission/materials — never scene lights or world.

    FX_BRIGHTNESS_FACTOR must not darken the rest of the part mesh. We walk
    materials used by *fx_objects* and scale Emission Strength (Principled /
    Emission nodes) and, as a fallback, any BACKGROUND-like strength sockets.
    """
    f = max(0.0, float(factor))
    if f == 1.0 or not fx_objects:
        return
    seen_mats = set()
    for obj in fx_objects:
        try:
            slots = getattr(obj, "material_slots", None) or []
        except Exception:
            slots = []
        for slot in slots:
            mat = getattr(slot, "material", None)
            if mat is None:
                continue
            mat_key = mat.as_pointer() if hasattr(mat, "as_pointer") else id(mat)
            if mat_key in seen_mats:
                continue
            seen_mats.add(mat_key)
            try:
                if not mat.use_nodes or mat.node_tree is None:
                    # Viewport diffuse fallback
                    try:
                        mat.diffuse_color = (
                            mat.diffuse_color[0] * f,
                            mat.diffuse_color[1] * f,
                            mat.diffuse_color[2] * f,
                            mat.diffuse_color[3],
                        )
                    except Exception:
                        pass
                    continue
                for node in mat.node_tree.nodes:
                    ntype = getattr(node, "type", "")
                    # Principled BSDF: Emission Strength
                    if ntype in ("BSDF_PRINCIPLED",):
                        for key in ("Emission Strength", "Emission"):
                            if key in node.inputs:
                                try:
                                    sock = node.inputs[key]
                                    # Strength is float; Emission color is rgba — only scale floats
                                    if hasattr(sock, "default_value") and isinstance(sock.default_value, (int, float)):
                                        sock.default_value = float(sock.default_value) * f
                                except Exception:
                                    pass
                    # Pure Emission shader
                    if ntype in ("EMISSION",):
                        if "Strength" in node.inputs:
                            try:
                                node.inputs["Strength"].default_value = (
                                    float(node.inputs["Strength"].default_value) * f
                                )
                            except Exception:
                                pass
            except Exception:
                pass


def _setup_world(scene):
    world = bpy.data.worlds.new(
        "_mu_thumb_world"
    )
    scene.world = world
    world.use_nodes = True

    nt = world.node_tree

    bg = None

    for n in nt.nodes:

        if n.type == "BACKGROUND":
            bg = n
            break

    if bg is None:

        bg = nt.nodes.new(
            "ShaderNodeBackground"
        )

        out = None

        for n in nt.nodes:

            if n.type == "OUTPUT_WORLD":
                out = n
                break

        if out is not None:
            nt.links.new(
                bg.outputs[0],
                out.inputs[0],
            )

    bg.inputs[0].default_value = (
        0.75,
        0.80,
        0.88,
        1.0,
    )

    bg.inputs[1].default_value = 0.8
    return world


def _camera_basis(cam_obj):
    from mathutils import Vector
    m = cam_obj.rotation_euler.to_matrix()
    right = (m @ Vector((1.0, 0.0, 0.0))).normalized()
    up = (m @ Vector((0.0, 1.0, 0.0))).normalized()
    view = (m @ Vector((0.0, 0.0, -1.0))).normalized()
    return right, up, view


def _overlay_for_points(scene, points, cam_obj, cam_data):
    if not DEV_OVERLAY or not points:
        return
    from mathutils import Vector
    mn = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    mx = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center = (mn + mx) * 0.5
    right, up, view = _camera_basis(cam_obj)
    _dev_overlay_framing(scene, points, center, -view, right, up, cam_data.ortho_scale)


def _render_to_path(scene, path):
    """Render one thumbnail to a temporary PNG and validate it."""
    # Do not inherit resolution/border settings from the imported MU scene.
    # Blender's RenderSettings resolution_percentage and render-region flags directly
    # affect the actual output size.
    try:
        scene.render.resolution_x = int(_ICON_SIZE)
        scene.render.resolution_y = int(_ICON_SIZE)
        scene.render.resolution_percentage = 100
        scene.render.use_border = False
        scene.render.use_crop_to_border = False
        try:
            scene.render.film_transparent = True
            scene.render.overscan = 0.0
        except Exception:
            pass
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0
        scene.render.border_min_x = 0.0
        scene.render.border_max_x = 1.0
        scene.render.border_min_y = 0.0
        scene.render.border_max_y = 1.0
    except Exception as e:
        print("[mu_thumb] render settings reset failed:", type(e).__name__, e)
    if DEV_OVERLAY:
        _dev_info("render", os.path.basename(path), "size=%dx%d" % (scene.render.resolution_x, scene.render.resolution_y))
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    scene.render.filepath = path
    try:
        bpy.ops.render.render(write_still=True)
    except Exception as e:
        print("[mu_thumb] render failed:", type(e).__name__, e)
        return False, {"valid": False, "reason": "render_error:%s" % e}
    if not os.path.isfile(path) or os.path.getsize(path) <= 64:
        return False, {"valid": False, "reason": "missing_or_tiny_file"}
    metrics = _load_render_metrics(path)
    _dev_info("validation", os.path.basename(path), metrics)
    return bool(metrics.get("valid")), metrics


def _commit_render(tmp_path, out_path):
    try:
        os.replace(tmp_path, out_path)
        return True
    except Exception as e:
        print("[mu_thumb] commit failed:", type(e).__name__, e)
        return False


_GPU_PROBE_DONE = False

def probe_eevee_gpu():
    """Report the active Blender GPU backend once, after a render initialized it."""
    global _GPU_PROBE_DONE
    if _GPU_PROBE_DONE or not DEV_OVERLAY:
        return
    try:
        import gpu
        p = gpu.platform
        print("[mu_thumb][DEV] GPU:", p.device_type_get(), "|", p.vendor_get(), "|", p.renderer_get(), "| backend=", p.backend_type_get(), flush=True)
        _GPU_PROBE_DONE = True
    except Exception:
        # Background Blender may not have an initialized GPU context until the
        # first EEVEE render. Never spam the console or affect thumbnail output.
        pass


def generate_thumbnail(mu_path: str, part_name: str = "", *, force: bool = False, show_attach_points: bool = False) -> Optional[str]:
    _dev_info("generate", os.path.basename(mu_path), part_name or "?")

    if (
        not mu_path
        or not os.path.isfile(mu_path)
    ):

        print(
            "[mu_thumb] FAIL: "
            "path missing or not a file"
        )

        return None

    if not force:
        existing = _resolve_cache_path(mu_path, part_name)
        if existing:
            if DEV_OVERLAY:
                if find_ksp_thumb(part_name) == existing:
                    _dev_log("SKIP GENERATION (official @thumb):", _dev_part_label(mu_path, part_name), "->", existing)
                else:
                    _dev_log("SKIP GENERATION (cache exists):", _dev_part_label(mu_path, part_name), "->", existing)
            _dev_info("cache hit", os.path.basename(existing))
            return existing
        if DEV_OVERLAY:
            _dev_log("NO EXISTING THUMB -> GENERATE:", _dev_part_label(mu_path, part_name))
    elif DEV_OVERLAY:
        _dev_log("FORCE REGEN -> ignore existing @thumb/cache:", _dev_part_label(mu_path, part_name))

    if force:
        out = _cache_path(
            mu_path,
            part_name,
        )
    else:
        out = _unique_write_path(
            mu_path,
            part_name,
        )

    print(
        "[mu_thumb] cache write:",
        os.path.basename(out),
    )

    from ..import_mu import import_mu
    from mathutils import Vector
    import contextlib

    prog_mod = None
    orig_bar = None

    try:

        from ..import_mu import progress_util as prog_mod

        orig_bar = getattr(
            prog_mod,
            "mu_progress_bar",
            None,
        )

        if orig_bar is not None:

            def _silent_bar(
                *_a,
                **_k,
            ):
                return contextlib.nullcontext()

            prog_mod.mu_progress_bar = (
                _silent_bar
            )

    except Exception:

        prog_mod = None
        orig_bar = None

    before = {
        "objects": set(
            bpy.data.objects.keys()
        ),
        "meshes": set(
            bpy.data.meshes.keys()
        ),
        "materials": set(
            bpy.data.materials.keys()
        ),
        "images": set(
            bpy.data.images.keys()
        ),
        "armatures": set(
            bpy.data.armatures.keys()
        ),
        "actions": set(
            bpy.data.actions.keys()
        ),
        "collections": set(
            bpy.data.collections.keys()
        ),
        "cameras": set(
            bpy.data.cameras.keys()
        ),
        "lights": set(
            bpy.data.lights.keys()
        ),
        "worlds": set(
            bpy.data.worlds.keys()
        ),
        "node_groups": set(
            bpy.data.node_groups.keys()
        ),
    }

    def _purge_new():
        for name in list(
            bpy.data.objects.keys()
        ):
            if name not in before["objects"]:

                o = bpy.data.objects.get(name)

                if o is not None:

                    try:
                        bpy.data.objects.remove(
                            o,
                            do_unlink=True,
                        )
                    except Exception:
                        pass

        for name in list(
            bpy.data.collections.keys()
        ):

            if name not in before["collections"]:

                c = bpy.data.collections.get(name)

                if c is not None:

                    try:
                        bpy.data.collections.remove(
                            c
                        )
                    except Exception:
                        pass

        for key, coll in (
            (
                "meshes",
                bpy.data.meshes,
            ),
            (
                "materials",
                bpy.data.materials,
            ),
            (
                "images",
                bpy.data.images,
            ),
            (
                "armatures",
                bpy.data.armatures,
            ),
            (
                "actions",
                bpy.data.actions,
            ),
            (
                "cameras",
                bpy.data.cameras,
            ),
            (
                "lights",
                bpy.data.lights,
            ),
            (
                "worlds",
                bpy.data.worlds,
            ),
            (
                "node_groups",
                bpy.data.node_groups,
            ),
        ):

            for name in list(coll.keys()):

                if name not in before[key]:

                    block = coll.get(name)

                    if block is not None:

                        try:
                            coll.remove(block)
                        except Exception:
                            pass

    win = bpy.context.window

    orig_scene = (
        win.scene
        if win
        else None
    )

    scene = bpy.data.scenes.new(
        "_mu_thumb_tmp"
    )

    try:

        if win is not None:
            win.scene = scene

        col = bpy.data.collections.new(
            "_mu_thumb_col"
        )

        scene.collection.children.link(col)

        root = None

        try:

            ret = import_mu(
                col,
                mu_path,
                False,
                False,
            )

            root = (
                ret[0]
                if isinstance(
                    ret,
                    (tuple, list),
                )
                else ret
            )

            print(
                "[mu_thumb] import_mu returned "
                "type=%s root=%s"
                % (
                    type(ret).__name__,
                    root,
                )
            )

        except Exception as e:

            print(
                "[mu_thumb] FAIL import_mu:",
                type(e).__name__,
                e,
            )

            import traceback
            traceback.print_exc()

            return None

        finally:

            if (
                prog_mod is not None
                and orig_bar is not None
            ):

                try:
                    prog_mod.mu_progress_bar = (
                        orig_bar
                    )
                except Exception:
                    pass

        if root is None:

            print(
                "[mu_thumb] FAIL: "
                "import returned None"
            )

            return None

        try:

            if scene.view_layers:

                scene.view_layers[
                    0
                ].objects.active = None

        except Exception:
            pass

        fx_objects = _collect_fx_objects(
            root
        )
        is_fx = bool(fx_objects)
        if is_fx:
            print(
                "[mu_thumb] FX detected:",
                len(fx_objects),
                [
                    o.name
                    for o in fx_objects
                ],
            )
            _call_particle_show_operator(scene, root)
            still_hidden = []
            for obj in fx_objects:
                try:
                    hidden = bool(
                        obj.hide_viewport
                    )
                    try:
                        hidden = hidden or bool(
                            obj.hide_get()
                        )
                    except Exception:
                        pass
                    if hidden:
                        still_hidden.append(
                            obj.name
                        )
                except Exception:
                    pass
            if still_hidden:
                print(
                    "[mu_thumb] WARNING: "
                    "operator left FX hidden:",
                    still_hidden,
                )
                _show_fx_for_thumbnail(root)
            try:
                scene.frame_set(1)
            except Exception:
                pass
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            _dev_info("FX mode")
        else:
            _dev_info("normal mode")
            keep_shroud = False
            try:
                keep_shroud = "shroud" in (part_name or "").lower()
            except Exception:
                pass

        cam_data = bpy.data.cameras.new(
            "_mu_thumb_cam"
        )
        cam_data.type = "ORTHO"
        cam_obj = bpy.data.objects.new(
            "_mu_thumb_cam",
            cam_data,
        )

        col.objects.link(cam_obj)
        scene.camera = cam_obj

        objs = [
            root
        ] + list(
            getattr(
                root,
                "children_recursive",
                [],
            ) or []
        )

        print(
            "[mu_thumb] objects under root:",
            len(objs),
            [
                getattr(
                    o,
                    "name",
                    "?",
                )
                for o in objs[:8]
            ],
        )
        if is_fx:
            # FX mode: frame FX objects with FX_FRAME_MARGIN so the constant
            # actually controls FX thumb padding. Do NOT apply FX_FRAME_MARGIN
            # to the full part mesh (that was the "scales other elements" bug).
            fx_fit_points = _collect_frame_points(
                fx_objects, keep_shroud=False, include_evaluated=True, include_raw=True
            )
            if fx_fit_points:
                _dev_info("FX fit points (fx objs)=", len(fx_fit_points))
                if not _fit_ortho_camera(
                    cam_obj, cam_data, fx_fit_points, margin=float(FX_FRAME_MARGIN)
                ):
                    print("[mu_thumb] FX object fit FAILED")
                    fx_fit_points = None
            if not fx_fit_points:
                # Bounds from bound_box / autozoom on FX hosts
                print("[mu_thumb] FX: framing via FX object autozoom")
                if not _autozoom_fx_camera(
                    cam_obj, cam_data, fx_objects, margin=float(FX_FRAME_MARGIN)
                ):
                    # Last resort: whole part mesh with normal margin
                    print("[mu_thumb] FX autozoom FAILED, mesh fallback")
                    fx_fit_points = _collect_frame_points(
                        objs, keep_shroud=False, include_evaluated=True, include_raw=True
                    )
                    if fx_fit_points:
                        _fit_ortho_camera(
                            cam_obj, cam_data, fx_fit_points,
                            margin=float(THUMB_FRAME_MARGIN),
                        )
                else:
                    fx_fit_points = _collect_frame_points(
                        fx_objects, keep_shroud=False,
                        include_evaluated=True, include_raw=True,
                    )
        else:
            # Rest pose for animated parts (frame 0), then depsgraph update
            try:
                scene.frame_set(0)
            except Exception:
                try:
                    scene.frame_set(1)
                except Exception:
                    pass
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass

            # Mute actions so animated parents (heatshields etc.) don't
            # collapse matrix_world to ~zero at frame 0 during framing.
            _saved_actions = []
            for o in objs:
                try:
                    ad = o.animation_data
                    if ad is not None and ad.action is not None:
                        _saved_actions.append((o, ad.action))
                        ad.action = None
                except Exception:
                    pass
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass

            # IMPORTANT: hide the same helper/collider objects BEFORE collecting
            # framing points. Previously framing was calculated first and only
            # then _hide_for_thumbnail() was called. That meant a hidden helper
            # could define a gigantic AABB while the actual render contained only
            # a tiny visible mesh (the exact symptom seen with restock-wheel-1-T).
            try:
                _hide_for_thumbnail(root, keep_shroud=keep_shroud)
            except Exception as e:
                print("[mu_thumb] hide_for_thumbnail before fit:", e)
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass

            use_coords = _collect_frame_points(objs, keep_shroud=keep_shroud)

            if not use_coords:
                print("[mu_thumb] FAIL: no visible frame points")
                return None

            ok = _fit_ortho_camera(
                cam_obj, cam_data, use_coords, margin=float(THUMB_FRAME_MARGIN)
            )
            if not ok:
                print("[mu_thumb] FAIL: camera fit")
                return None

            if DEV_OVERLAY:
                from mathutils import Vector
                _mn = Vector((min(p.x for p in use_coords), min(p.y for p in use_coords), min(p.z for p in use_coords)))
                _mx = Vector((max(p.x for p in use_coords), max(p.y for p in use_coords), max(p.z for p in use_coords)))
                _center = (_mn + _mx) * 0.5
                _view = Vector(tuple(float(v) for v in THUMB_CAM_DIR)).normalized()
                _forward = -_view
                _up_ref = Vector((0.0, 0.0, 1.0))
                _right = _forward.cross(_up_ref)
                if _right.length < 1e-8:
                    _up_ref = Vector((0.0, 1.0, 0.0))
                    _right = _forward.cross(_up_ref)
                _right.normalize()
                _up = _right.cross(_forward).normalized()
                _dev_overlay_framing(scene, use_coords, _center, _view, _right, _up, cam_data.ortho_scale)

        _setup_world(scene)
        if is_fx:
            from mathutils import Vector
            hosts = [
                o
                for o in fx_objects
                if o.get("mu_particles")
            ]
            if hosts:
                pts = [
                    o.matrix_world.translation.copy()
                    for o in hosts
                ]
                center = (
                    sum(
                        pts,
                        Vector(),
                    )
                    / float(len(pts))
                )
            else:
                center = (
                    cam_obj.location
                    + cam_obj.rotation_euler.to_matrix()
                    @ Vector(
                        (
                            0.0,
                            0.0,
                            -3.5,
                        )
                    )
                )
            light_size = 4.0
        else:
            light_size = max(
                cam_data.ortho_scale,
                0.05,
            )
            # Look-at point in front of the camera (same centre used for fit)
            center = (
                cam_obj.location
                + cam_obj.rotation_euler.to_matrix()
                @ Vector((0.0, 0.0, -max(light_size, 0.5)))
            )
        key_loc = (
            cam_obj.location.copy()
        )
        _add_sun(
            col,
            "_mu_thumb_key",
            key_loc,
            center,
            energy=2.0,
            color=(
                1.0,
                0.98,
                0.94,
            ),
        )
        fill_loc = Vector(
            (
                center.x - light_size * 1.1,
                center.y + light_size * 0.8,
                center.z + light_size * 0.4,
            )
        )
        _add_sun(
            col,
            "_mu_thumb_fill",
            fill_loc,
            center,
            energy=0.8,
            color=(
                0.75,
                0.82,
                1.0,
            ),
        )
        rim_loc = Vector(
            (
                center.x - light_size * 0.4,
                center.y + light_size * 1.2,
                center.z + light_size * 1.0,
            )
        )
        _add_sun(
            col,
            "_mu_thumb_rim",
            rim_loc,
            center,
            energy=1.0,
            color=(
                1.0,
                1.0,
                1.0,
            ),
        )
        if is_fx:
            _scale_fx_object_brightness(fx_objects, FX_BRIGHTNESS_FACTOR)
        if show_attach_points:
            marker_count = _add_attach_point_markers(scene, col, root)
            if marker_count:
                _dev_info("attach points:", marker_count)
        _dev_info("lights+world set")
        scene.render.resolution_x = _ICON_SIZE
        scene.render.resolution_y = _ICON_SIZE
        scene.render.resolution_percentage = 100
        scene.render.filepath = out
        scene.render.image_settings.file_format = ("PNG")
        scene.render.film_transparent = True
        try:
            scene.render.engine = ("BLENDER_EEVEE_NEXT")
        except Exception:
            try:
                scene.render.engine = ("BLENDER_EEVEE")
            except Exception:
                pass
        try:
            scene.view_settings.view_transform = (
                "Standard"
            )
            scene.view_settings.look = (
                "None"
            )
            scene.view_settings.exposure = 0.0
            scene.view_settings.gamma = 1.0
        except Exception:
            pass
        if is_fx:
            hidden_before_render = []
            for obj in fx_objects:
                try:
                    hidden = bool(
                        obj.hide_viewport
                    )
                    try:
                        hidden = hidden or bool(
                            obj.hide_get()
                        )
                    except Exception:
                        pass
                    if hidden:
                        hidden_before_render.append(
                            obj.name
                        )
                except Exception:
                    pass
            _dev_info("FX hidden before render:", hidden_before_render)
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
        # Render into a temporary file first. In DEV mode the overlay is added
        # BEFORE the render because it is now guaranteed to use the exact same
        # point set/camera framing. This removes the old second render pass and
        # makes large DEV batches roughly 2x faster on the rendering portion.
        tmp_out = out + ".tmp.png"
        if os.path.isfile(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

        valid = False
        metrics = None
        chosen_points = None
        if not is_fx:
            fit_sets = [
                (use_coords, float(THUMB_FRAME_MARGIN), "combined"),
                (_collect_frame_points(objs, keep_shroud=keep_shroud,
                                       include_evaluated=False, include_raw=True),
                 float(THUMB_RETRY_MARGIN), "raw-only"),
            ]
            for attempt, (fit_points, margin, label) in enumerate(fit_sets[:THUMB_MAX_RETRIES + 1]):
                if not fit_points:
                    continue
                _dev_info("fit", attempt + 1, label)
                if not _fit_ortho_camera(cam_obj, cam_data, fit_points, margin=margin):
                    continue
                chosen_points = fit_points
                if DEV_OVERLAY:
                    _overlay_for_points(scene, chosen_points, cam_obj, cam_data)
                valid, metrics = _render_to_path(scene, tmp_out)
                if valid:
                    break
                try:
                    os.remove(tmp_out)
                except OSError:
                    pass
                chosen_points = None
        else:
            chosen_points = fx_fit_points if fx_fit_points else None
            valid, metrics = _render_to_path(scene, tmp_out) if not DEV_OVERLAY else (False, None)
            if not valid:
                # FX camera has its own framing logic; retry with a more generous
                # scale if the first render is suspicious.
                if DEV_OVERLAY and chosen_points:
                    _overlay_for_points(scene, chosen_points, cam_obj, cam_data)
                valid, metrics = _render_to_path(scene, tmp_out)
            if not valid:
                try:
                    cam_data.ortho_scale *= 1.75
                except Exception:
                    pass
                if DEV_OVERLAY and chosen_points:
                    _overlay_for_points(scene, chosen_points, cam_obj, cam_data)
                valid, metrics = _render_to_path(scene, tmp_out)

        if valid:
            probe_eevee_gpu()

        if not valid:
            print("[mu_thumb] FAIL: rendered PNG validation failed:", metrics)
            try:
                if os.path.isfile(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass
            return None

        if not _commit_render(tmp_out, out):
            try:
                if os.path.isfile(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass
            return None

        _dev_info("OK", os.path.basename(out), "bytes=", os.path.getsize(out))
        return out

    finally:
        try:
            if (
                scene
                and scene.view_layers
            ):
                scene.view_layers[
                    0
                ].objects.active = None
        except Exception:
            pass
        try:
            if (
                win is not None
                and orig_scene is not None
            ):
                win.scene = orig_scene
        except Exception:
            pass
        try:
            if (
                win is not None
                and win.view_layer
            ):
                win.view_layer.objects.active = None
            for o in list(
                getattr(
                    bpy.context,
                    "selected_objects",
                    [],
                )
                or []
            ):
                try:
                    o.select_set(False)
                except Exception:
                    pass
        except Exception:
            pass
        if (
            prog_mod is not None
            and orig_bar is not None
        ):
            try:
                prog_mod.mu_progress_bar = (
                    orig_bar
                )
            except Exception:
                pass
        try:
            _purge_new()
        except Exception as e:

            print(
                "[mu_thumb] purge warning:",
                type(e).__name__,
                e,
            )
        try:
            if (
                scene
                and scene.name
                in bpy.data.scenes
            ):
                bpy.data.scenes.remove(
                    scene,
                    do_unlink=True,
                )
        except Exception:
            pass


def _default_thumb_path() -> str:
    return os.path.join(_cache_dir(), _DEFAULT_THUMB_NAME)


def _ensure_default_thumbnail() -> str:
    path = _default_thumb_path()

    if (
        os.path.isfile(path)
        and os.path.getsize(path) > 64
    ):
        return path

    img = bpy.data.images.new(
        "_mu_default_thumbnail",
        width=_ICON_SIZE,
        height=_ICON_SIZE,
        alpha=True,
    )

    pixels = []

    for y in range(_ICON_SIZE):

        for x in range(_ICON_SIZE):

            pixels.extend(
                (
                    0.30,
                    0.30,
                    0.30,
                    1.0,
                )
            )

    img.pixels = pixels

    img.filepath_raw = path
    img.file_format = "PNG"

    img.save()

    bpy.data.images.remove(img)

    return path


def icon_id_for_part(mu_path: str, part_name: str = "", *, ensure: bool = True) -> int:
    pcoll = ensure_previews()
    key_src = "%s|%s" % (
        mu_path or "",
        part_name or "",
    )
    key = hashlib.sha1(
        key_src.encode(
            "utf-8",
            errors="replace",
        )
    ).hexdigest()[:20]
    if key in pcoll:
        return pcoll[key].icon_id
    path = _resolve_cache_path(
        mu_path,
        part_name,
    )
    if (
        path is None
        and ensure
    ):
        path = generate_thumbnail(
            mu_path,
            part_name,
        )
    if (
        not path
        or not os.path.isfile(path)
    ):
        try:
            if (
                _DEFAULT_PREVIEW_KEY
                not in pcoll
            ):
                default_path = (
                    _ensure_default_thumbnail()
                )
                pcoll.load(
                    _DEFAULT_PREVIEW_KEY,
                    default_path,
                    "IMAGE",
                )

            return pcoll[
                _DEFAULT_PREVIEW_KEY
            ].icon_id

        except Exception:
            return 0
    try:
        pcoll.load(
            key,
            path,
            "IMAGE",
        )
        return pcoll[key].icon_id
    except Exception:
        return 0