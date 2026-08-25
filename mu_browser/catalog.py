# vim:ts=4:et
# <pep8 compliant>
"""Lightweight GameData PART / INTERNAL / FX / Assets scanner for MU browser."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..cfgnode import ConfigNode, ConfigNodeError


STOCK_CATEGORY_ORDER = (
    "Pods",
    "FuelTank",
    "Engine",
    "Control",
    "Structural",
    "Coupling",
    "Payload",
    "Aero",
    "Ground",
    "Thermal",
    "Electrical",
    "Communication",
    "Science",
    "Cargo",
    "Utility",
    "Robotics",
    "Props",
    "Assets",
    "FX",
    "Internal",
)

OTHER_CATEGORY = "Other"

_cache = {
    "root": "",
    "mtime": 0.0,
    "parts": [],
    "by_category": {},
}


@dataclass
class PartEntry:
    name: str
    title: str
    category: str
    cfg_path: str
    mu_path: str
    attach_rules: Tuple[int, ...] = field(default_factory=tuple)
    manufacturer: str = ""
    description: str = ""


def _prefs_gamedata() -> str:
    """Return configured KSP GameData path."""
    try:
        from ..preferences.preferences import Preferences
        return (Preferences().GameData or "").strip()
    except Exception:
        return ""


def _resolve_loc(text: str, locs: Dict[str, str]) -> str:
    """Resolve KSP localization keys and fall back to inline English text."""
    if not text:
        return ""
    t = text.strip()
    comment = ""
    if "//" in t:
        t, comment = t.split("//", 1)
        t = t.strip()
        comment = comment.strip()
    if t.startswith("#") and t in locs:
        resolved = locs[t]
        if resolved:
            return resolved
    if t.startswith("#autoLOC") or t.startswith("#AutoLOC"):
        resolved = locs.get(t)
        if resolved:
            return resolved
    if comment:
        if "=" in comment:
            eng = comment.rsplit("=", 1)[-1].strip()
            if eng:
                return eng
        if not comment.startswith("#"):
            return comment
    return t


def _parse_attach_rules(cfg) -> Tuple[int, ...]:
    """Parse KSP attachRules into a tuple of integers."""
    if not cfg.HasValue("attachRules"):
        return ()
    raw = cfg.GetValue("attachRules") or ""
    out = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            out.append(0)
    return tuple(out)


def _norm(path: str) -> str:
    """Normalize a filesystem path for comparisons."""
    return os.path.normcase(os.path.normpath(path.replace("\\", "/")))


def _model_path_from_url(gamedata_root: str, url: str) -> str:
    """Resolve a KSP model URL to an existing .mu file."""
    if not url:
        return ""
    url = url.replace("\\", "/").strip()
    if url.lower().endswith(".mu"):
        candidate = os.path.join(gamedata_root, url)
    else:
        candidate = os.path.join(gamedata_root, url + ".mu")
    candidate = os.path.normpath(candidate)
    if os.path.isfile(candidate):
        return candidate.replace("\\", "/")
    return ""


def _first_model_from_node(node, gamedata_root: str) -> str:
    """Return the first MODEL or mesh referenced by a config node."""
    try:
        for model_node in node.GetNodes("MODEL") or []:
            if not model_node.HasValue("model"):
                continue
            url = (model_node.GetValue("model") or "").replace("\\", "/").strip()
            path = _model_path_from_url(gamedata_root, url)
            if path:
                return path
    except Exception:
        pass
    try:
        if node.HasValue("mesh"):
            mesh = (node.GetValue("mesh") or "").replace("\\", "/").strip()
            if mesh:
                if not mesh.lower().endswith(".mu"):
                    mesh += ".mu"
                return mesh
    except Exception:
        pass
    return ""


def _mu_from_part_cfg(cfg_path: str, cfg, gamedata_root: str) -> str:
    """Resolve the .mu model used by a PART config."""
    folder = os.path.dirname(cfg_path).replace("\\", "/")
    try:
        for node in cfg.GetNodes("MODEL") or []:
            url = (node.GetValue("model") or "").replace("\\", "/").strip()
            if not url:
                continue
            cand = _model_path_from_url(gamedata_root, url)
            if cand:
                return cand
    except Exception:
        pass
    try:
        if cfg.HasValue("mesh"):
            mesh = (cfg.GetValue("mesh") or "").replace("\\", "/").strip()
            if mesh:
                if not mesh.lower().endswith(".mu"):
                    mesh += ".mu"
                cand = os.path.join(folder, mesh).replace("\\", "/")
                if os.path.isfile(cand):
                    return cand
    except Exception:
        pass
    try:
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".mu"):
                return os.path.join(folder, fn).replace("\\", "/")
    except Exception:
        pass
    return ""


def _walk_cfgs(root: str):
    """Yield all .cfg files below GameData."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d and d[0] not in "._"]
        for fn in filenames:
            if fn.lower().endswith(".cfg"):
                yield os.path.join(dirpath, fn)


def _scan_internal_node(node, cfg_path: str, gamedata_root: str, parts: List[PartEntry], seen_internal: set, claimed_mu: set, locs: Dict[str, str]):
    """Scan an INTERNAL node and add its IVA model to the Internal category."""
    if not node.HasValue("name"):
        return
    name = (node.GetValue("name") or "").strip()
    if not name:
        return
    mu_path = _first_model_from_node(node, gamedata_root)
    if mu_path and not os.path.isabs(mu_path):
        mu_path = os.path.join(os.path.dirname(cfg_path), mu_path).replace("\\", "/")
        if not os.path.isfile(mu_path):
            mu_path = ""
    if not mu_path:
        return
    mu_key = _norm(mu_path)
    key = (name.lower(), mu_key)
    if key in seen_internal:
        return
    seen_internal.add(key)
    claimed_mu.add(mu_key)
    title = name
    try:
        if node.HasValue("title"):
            title = _resolve_loc(node.GetValue("title"), locs) or name
    except Exception:
        pass
    parts.append(
        PartEntry(
            name=name,
            title=title,
            category="Internal",
            cfg_path=cfg_path,
            mu_path=mu_path,
            attach_rules=(),
            manufacturer="",
            description="",
        )
    )


def _scan_fx_folder(root: str, parts: List[PartEntry], seen: set, claimed_mu: set):
    """Add all .mu files from Squad/FX as virtual FX entries."""
    fx_root = os.path.join(root, "Squad", "FX").replace("\\", "/")
    if not os.path.isdir(fx_root):
        return
    for dirpath, dirnames, filenames in os.walk(fx_root):
        dirnames[:] = [d for d in dirnames if d and d[0] not in "._"]
        for fn in filenames:
            if not fn.lower().endswith(".mu"):
                continue
            mu_path = os.path.join(dirpath, fn).replace("\\", "/")
            key_path = _norm(mu_path)
            key = "FX::" + key_path
            if key in seen:
                continue
            seen.add(key)
            claimed_mu.add(key_path)
            name = os.path.splitext(fn)[0]
            parts.append(
                PartEntry(
                    name=name,
                    title=name,
                    category="FX",
                    cfg_path="",
                    mu_path=mu_path,
                    attach_rules=(),
                    manufacturer="",
                    description="",
                )
            )


def _scan_assets(root: str, parts: List[PartEntry], claimed_mu: set, fx_paths: set):
    """Add standalone .mu files not belonging to PART, INTERNAL or FX."""
    seen_assets = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d and d[0] not in "._" and d != "@thumbs"
        ]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == "Squad/FX" or rel_dir.startswith("Squad/FX/"):
            continue
        for fn in filenames:
            if not fn.lower().endswith(".mu"):
                continue
            mu_path = os.path.join(dirpath, fn).replace("\\", "/")
            key_path = _norm(mu_path)
            if key_path in claimed_mu or key_path in fx_paths:
                continue
            if key_path in seen_assets:
                continue
            seen_assets.add(key_path)
            base = os.path.splitext(fn)[0]
            rel = os.path.relpath(mu_path, root).replace("\\", "/")
            name = os.path.splitext(rel)[0]
            parts.append(
                PartEntry(
                    name=name,
                    title=base,
                    category="Props" if "props" in [p.lower() for p in mu_path.replace("\\", "/").split("/")] else "Assets",
                    cfg_path="",
                    mu_path=mu_path,
                    attach_rules=(),
                    manufacturer="",
                    description=rel,
                )
            )


def scan_gamedata(root: Optional[str] = None, *, force: bool = False) -> List[PartEntry]:
    """Scan GameData for PARTs, INTERNALs, FX and standalone Assets."""
    global _cache
    root = (root or _prefs_gamedata()).replace("\\", "/").rstrip("/")
    if not root or not os.path.isdir(root):
        _cache = {"root": root or "", "mtime": 0.0, "parts": [], "by_category": {}}
        return []
    try:
        mtime = os.path.getmtime(root)
    except Exception:
        mtime = 0.0
    if (
        not force
        and _cache.get("root") == root
        and abs(float(_cache.get("mtime") or 0) - mtime) < 1e-6
        and _cache.get("parts") is not None
    ):
        return list(_cache["parts"])
    locs: Dict[str, str] = {}
    parts: List[PartEntry] = []
    seen_parts = set()
    seen_internal = set()
    claimed_mu = set()
    cfg_files = list(_walk_cfgs(root))
    # Read localization first so titles can be resolved regardless of
    # the order of cfg files returned by the filesystem.
    for cfg_path in cfg_files:
        cfg_path = cfg_path.replace("\\", "/")
        try:
            cfg = ConfigNode.loadfile(cfg_path)
        except ConfigNodeError:
            continue
        except Exception:
            continue
        if not cfg:
            continue
        for node in cfg.nodes:
            try:
                nname = node.name
            except Exception:
                continue
            if nname != "Localization":
                continue
            try:
                lang_nodes = list(getattr(node, "nodes", []) or [])
                preferred = None
                for ln in lang_nodes:
                    lname = (getattr(ln, "name", "") or "").lower()
                    if lname in ("en-us", "en", "en_us", "english"):
                        preferred = ln
                        break
                if preferred is None and lang_nodes:
                    preferred = lang_nodes[0]
                if preferred is not None:
                    for loc in getattr(preferred, "values", []) or []:
                        key = getattr(loc, "name", None)
                        val = getattr(loc, "value", None)
                        if not key or val is None:
                            continue
                        if isinstance(val, str) and "//" in val:
                            val = val.split("//", 1)[0].strip()
                        locs[key] = val
            except Exception:
                pass
    # Read PART and INTERNAL nodes.
    for cfg_path in cfg_files:
        cfg_path = cfg_path.replace("\\", "/")
        try:
            cfg = ConfigNode.loadfile(cfg_path)
        except ConfigNodeError:
            continue
        except Exception:
            continue
        if not cfg:
            continue
        for node in cfg.nodes:
            try:
                nname = node.name
            except Exception:
                continue
            if nname == "INTERNAL":
                try:
                    _scan_internal_node(
                        node,
                        cfg_path,
                        root,
                        parts,
                        seen_internal,
                        claimed_mu,
                        locs,
                    )
                except Exception as e:
                    print(
                        "[mu_catalog] INTERNAL scan warning:",
                        cfg_path,
                        type(e).__name__,
                        e,
                    )
                continue
            if nname != "PART":
                continue
            try:
                if not node.HasValue("name"):
                    continue
                name = (node.GetValue("name") or "").replace("_", ".")
                if not name or name in seen_parts:
                    continue
                seen_parts.add(name)
                title_raw = node.GetValue("title") if node.HasValue("title") else name
                title = _resolve_loc(title_raw, locs) or name
                cat_raw = (node.GetValue("category") or "").strip() if node.HasValue("category") else ""
                path_parts = [p.lower() for p in cfg_path.replace("\\", "/").split("/")]
                if "internal" in path_parts:
                    category = "Internal"
                elif "props" in path_parts:
                    category = "Props"
                elif "assets" in path_parts:
                    category = "Assets"
                else:
                    cat_raw = ""
                    if node.HasValue("category"):
                        cat_raw = (node.GetValue("category") or "").strip()
                    category = cat_raw if cat_raw else OTHER_CATEGORY
                    if category.lower() in {"none", "n/a", ""}:
                        category = OTHER_CATEGORY
                mu_path = _mu_from_part_cfg(cfg_path, node, root)
                if not mu_path:
                    continue
                claimed_mu.add(_norm(mu_path))
                manu = ""
                if node.HasValue("manufacturer"):
                    manu = _resolve_loc(node.GetValue("manufacturer"), locs)
                desc = ""
                if node.HasValue("description"):
                    desc = _resolve_loc(node.GetValue("description"), locs)
                parts.append(
                    PartEntry(
                        name=name,
                        title=title,
                        category=category,
                        cfg_path=cfg_path,
                        mu_path=mu_path,
                        attach_rules=_parse_attach_rules(node),
                        manufacturer=manu,
                        description=desc,
                    )
                )
            except Exception as e:
                print(
                    "[mu_catalog] PART scan warning:",
                    cfg_path,
                    type(e).__name__,
                    e,
                )
    # FX models are claimed before scanning generic Assets.
    before_fx = set(claimed_mu)
    _scan_fx_folder(root, parts, set(), claimed_mu)
    fx_paths = claimed_mu - before_fx
    # Anything left over is a standalone asset.
    _scan_assets(root, parts, claimed_mu, fx_paths)
    parts.sort(key=lambda p: ((p.title or p.name).lower(), p.name.lower()))
    by_cat: Dict[str, List[PartEntry]] = {}
    for p in parts:
        by_cat.setdefault(p.category, []).append(p)
    _cache = {"root": root, "mtime": mtime, "parts": parts, "by_category": by_cat}
    all_mu = set()
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".mu"):
                all_mu.add(_norm(os.path.join(dirpath, fn)))
    catalogued_mu = {_norm(p.mu_path) for p in parts if p.mu_path}
    cat_counts = {cat: len(by_cat.get(cat, [])) for cat in categories()}
    #print("[mu_catalog] MU: %d/%d %s" % (len(catalogued_mu), len(all_mu), " ".join("%s=%d" % (cat, count) for cat, count in cat_counts.items())))
    missing_mu = sorted(all_mu - catalogued_mu)
    if missing_mu:
        print("[mu_catalog] MISSING MU:")
        for path in missing_mu:
            print("   ", path)
    thumb_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "thumbnails")
    thumb_files = {f for f in os.listdir(thumb_dir) if f.lower().endswith(".png")} if os.path.isdir(thumb_dir) else set()
    def _thumb_name(p):
        mu = os.path.splitext(os.path.basename(p.mu_path))[0]
        mu = "".join(c if c.isalnum() or c in "-_." else "_" for c in mu).strip("_.")[:40] or "unknown"
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (p.name or "part")).strip("_.")[:40] or "unknown"
        return "%s_%s.png" % (mu, name)
    used_thumbs = {_thumb_name(p) for p in parts if p.mu_path}
    used_thumbs.add("_default_thumbnail.png") # Prevent from delete
    used = len(thumb_files & used_thumbs)
    print("[mu_catalog] MU: %d/%d THUMBS: %d/%d junk=%d" % (len(catalogued_mu), len(all_mu), used, len(thumb_files), len(thumb_files) - used))
    if (len(thumb_files) - used) > 0:
        for fn in thumb_files - used_thumbs:
            try:
                os.remove(os.path.join(thumb_dir, fn))
            except Exception as e:
                print("[mu_catalog] thumbnail delete failed:", fn, e)
        print("[mu_catalog] removed junk thumbnails:", len(thumb_files - used_thumbs))
    return list(parts)


def categories() -> List[str]:
    """Return available categories in browser display order."""
    scan_gamedata()
    keys = set((_cache.get("by_category") or {}).keys())
    ordered = [c for c in STOCK_CATEGORY_ORDER if c in keys]
    rest = sorted(k for k in keys if k not in STOCK_CATEGORY_ORDER and k != OTHER_CATEGORY)
    if OTHER_CATEGORY in keys:
        rest.append(OTHER_CATEGORY)
    return ordered + rest


def parts_in_category(category: str) -> List[PartEntry]:
    """Return all entries belonging to a category."""
    scan_gamedata()
    return list((_cache.get("by_category") or {}).get(category, []))


def find_part(name: str) -> Optional[PartEntry]:
    """Find a catalog entry by its internal name."""
    scan_gamedata()
    for p in _cache.get("parts") or []:
        if p.name == name:
            return p
    return None


def invalidate_cache():
    """Clear the GameData catalog cache."""
    global _cache
    _cache = {"root": "", "mtime": 0.0, "parts": [], "by_category": {}}