# vim:ts=4:et
# <pep8 compliant>
"""Lightweight GameData PART scanner for the MU browser tab."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..cfgnode import ConfigNode, ConfigNodeError

# Stock VAB-ish category order (unknowns after, then Other)
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
)

OTHER_CATEGORY = "Other"

_cache = {
    "root": "",
    "mtime": 0.0,
    "parts": [],  # List[PartEntry]
    "by_category": {},  # str -> List[PartEntry]
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
    try:
        from ..preferences.preferences import Preferences
        return (Preferences().GameData or "").strip()
    except Exception:
        return ""


def _resolve_loc(text: str, locs: Dict[str, str]) -> str:
    """Resolve KSP localization keys, stripping // comments and falling back
    to the English text that Squad puts after the comment.

    Typical cfg line:
        title = #autoLOC_500319 //#autoLOC_500319 = KV-1 'Onion' Reentry Module
    """
    if not text:
        return ""
    t = text.strip()

    # Strip trailing // comment (common in stock / DLC cfgs)
    comment = ""
    if "//" in t:
        t, comment = t.split("//", 1)
        t = t.strip()
        comment = comment.strip()

    # Exact key match
    if t.startswith("#") and t in locs:
        resolved = locs[t]
        if resolved:
            return resolved

    # autoLOC-style key
    if t.startswith("#autoLOC") or t.startswith("#AutoLOC"):
        resolved = locs.get(t)
        if resolved:
            return resolved

    # Fallback: English text living in the comment, e.g.
    #   //#autoLOC_500319 = KV-1 'Onion' Reentry Module
    #   // = Some Title
    if comment:
        # Prefer the part after the last '='
        if "=" in comment:
            eng = comment.rsplit("=", 1)[-1].strip()
            if eng:
                return eng
        # Otherwise whole comment if it doesn't look like a key
        if not comment.startswith("#"):
            return comment

    # Last resort: the (cleaned) original string
    return t


def _parse_attach_rules(cfg) -> Tuple[int, ...]:
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


def _mu_from_part_cfg(cfg_path: str, cfg, gamedata_root: str) -> str:
    """Resolve absolute .mu path for a PART cfg."""
    folder = os.path.dirname(cfg_path).replace("\\", "/")
    # MODEL { model = Squad/... }
    try:
        for n in cfg.GetNodes("MODEL") or []:
            url = (n.GetValue("model") or "").replace("\\", "/").strip()
            if not url:
                continue
            cand = os.path.join(gamedata_root, url + ".mu").replace("\\", "/")
            if os.path.isfile(cand):
                return cand
            # sometimes url already has extension omitted and path relative
            cand2 = os.path.join(gamedata_root, url).replace("\\", "/")
            if cand2.lower().endswith(".mu") and os.path.isfile(cand2):
                return cand2
    except Exception:
        pass
    # mesh = foo  (same folder)
    try:
        if cfg.HasValue("mesh"):
            mesh = (cfg.GetValue("mesh") or "").strip()
            if mesh:
                if not mesh.lower().endswith(".mu"):
                    mesh = mesh + ".mu"
                cand = os.path.join(folder, mesh).replace("\\", "/")
                if os.path.isfile(cand):
                    return cand
    except Exception:
        pass
    # first .mu in folder
    try:
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".mu"):
                return os.path.join(folder, fn).replace("\\", "/")
    except Exception:
        pass
    return ""


def _walk_cfgs(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d and d[0] not in "._"]
        for fn in filenames:
            if fn.lower().endswith(".cfg"):
                yield os.path.join(dirpath, fn)


def scan_gamedata(root: Optional[str] = None, *, force: bool = False) -> List[PartEntry]:
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
    seen = set()

    for cfg_path in _walk_cfgs(root):
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
            if nname == "Localization":
                try:
                    # Prefer en-us (or first English-like) language block;
                    # fall back to the first sub-node if none match.
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
                            # Strip accidental // comments from dictionary values too
                            if isinstance(val, str) and "//" in val:
                                val = val.split("//", 1)[0].strip()
                            locs[key] = val
                except Exception:
                    pass
                continue
            if nname != "PART":
                continue
            if not node.HasValue("name"):
                continue
            name = node.GetValue("name").replace("_", ".")
            if name in seen:
                continue
            seen.add(name)
            title_raw = node.GetValue("title") if node.HasValue("title") else name
            title = _resolve_loc(title_raw, locs) or name
            cat_raw = ""
            if node.HasValue("category"):
                cat_raw = (node.GetValue("category") or "").strip()
            category = cat_raw if cat_raw else OTHER_CATEGORY
            # Normalize none / empty
            if category.lower() in {"none", "n/a", ""}:
                category = OTHER_CATEGORY
            mu_path = _mu_from_part_cfg(cfg_path, node, root)
            if not mu_path:
                continue
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

    parts.sort(key=lambda p: ((p.title or p.name).lower(), p.name.lower()))
    by_cat: Dict[str, List[PartEntry]] = {}
    for p in parts:
        by_cat.setdefault(p.category, []).append(p)

    _cache = {
        "root": root,
        "mtime": mtime,
        "parts": parts,
        "by_category": by_cat,
    }
    return list(parts)


def categories() -> List[str]:
    scan_gamedata()
    keys = set((_cache.get("by_category") or {}).keys())
    ordered = [c for c in STOCK_CATEGORY_ORDER if c in keys]
    rest = sorted(k for k in keys if k not in STOCK_CATEGORY_ORDER and k != OTHER_CATEGORY)
    if OTHER_CATEGORY in keys:
        rest.append(OTHER_CATEGORY)
    return ordered + rest


def parts_in_category(category: str) -> List[PartEntry]:
    scan_gamedata()
    return list((_cache.get("by_category") or {}).get(category, []))


def find_part(name: str) -> Optional[PartEntry]:
    scan_gamedata()
    for p in _cache.get("parts") or []:
        if p.name == name:
            return p
    return None


def invalidate_cache():
    global _cache
    _cache = {"root": "", "mtime": 0.0, "parts": [], "by_category": {}}