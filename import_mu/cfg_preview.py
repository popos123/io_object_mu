# vim:ts=4:et
# <pep8 compliant>
"""Viewport-only helpers from sibling part.cfg (inspired by modding workflows).

Markers are tagged mu_fx_preview / .cfg_preview and skipped by export filters.

ModulePartVariants: parse GAMEOBJECTS / TEXTURE switches, hide non-default
branches, store ``mu_variants`` JSON on the import root for Options UI.

TEXTURE / GAMEOBJECTS preview swaps are viewport-only: normal .mu export must
call ``prepare_stock_for_export`` (or ``restore_default_textures`` + base
variant) so stock materials are written. Use Export Active Variant to bake
the currently selected look.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import bpy
from mathutils import Vector


_CFG_PREVIEW_SUFFIX = ".cfg_preview"
_flag_preview_import_failed = False  # set if flag_preview.py is corrupt (UTF-16/nulls)
MU_VARIANTS_KEY = "mu_variants"
MU_COLOR_CHANGER_KEY = "mu_color_changer"
MU_BROKEN_KEY = "mu_broken_names"
MU_JETTISON_KEY = "mu_jettison_names"
# Implicit KSP look when cfg has VARIANT blocks but no baseVariant= —
# stock .mu materials (e.g. Serenity "Gray with Stripes" color DDS).
STOCK_VARIANT_NAME = "Stock"



def _parse_vec3(raw):
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if len(parts) < 3:
        return None
    try:
        # Unity XYZ → Blender X Z Y
        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
        return Vector((x, z, y))
    except Exception:
        return None


def _muname_match_stems(muname):
    """Stems used to match a .mu against part.cfg (strip tester ``_temp``).

    Exported round-trips embed the Unity transform name (often ``Size1.5_…``
    with dots) while GameData paths / cfg use underscores (``Size1_5_…``).
    Always emit both forms so reimport can still find ModulePartVariants.
    """
    if not muname:
        return []
    # Strip Blender ∧ / .001 noise if a full object name sneaks in
    muname = muname.split("\u2227")[0].strip()
    if "." in muname:
        base, suf = muname.rsplit(".", 1)
        if suf.isdigit():
            muname = base
    stems = []

    def _add(s):
        if s and s not in stems:
            stems.append(s)

    _add(muname)
    # Size1_5_Tank_04_temp / Size1_5_Tank_04_temp_1 → Size1_5_Tank_04
    stripped = re.sub(r"_temp(?:_\d+)?$", "", muname, flags=re.I)
    _add(stripped)
    # Dot ↔ underscore aliases (export header vs MODEL= path)
    for s in list(stems):
        if "." in s:
            _add(s.replace(".", "_"))
        if "_" in s:
            # Only swap digit-boundary underscores that look like Size1_5 → Size1.5
            _add(re.sub(r"(?<=\d)_(?=\d)", ".", s))
    return stems


def _find_part_cfg(mudir, muname, filepath_stem=None):
    """Locate part.cfg next to .mu or referencing this model.

    ``filepath_stem`` is the on-disk .mu basename (e.g. ``Size1_5_Tank_01_temp``)
    — preferred over the Unity transform name embedded in the .mu header after
    export/reimport (``Size1.5_Tank_01``), which otherwise misses underscore cfgs.
    """
    if not mudir:
        return None
    stems = []
    for src in (filepath_stem, muname):
        for s in _muname_match_stems(src):
            if s not in stems:
                stems.append(s)
    if not stems:
        return None
    candidates = []
    for stem in stems:
        for name in (stem + ".cfg", "part.cfg"):
            p = os.path.join(mudir, name)
            if os.path.isfile(p) and p not in candidates:
                candidates.append(p)
    if os.path.isdir(mudir):
        for fn in os.listdir(mudir):
            if fn.lower().endswith(".cfg"):
                p = os.path.join(mudir, fn)
                if p not in candidates:
                    candidates.append(p)
    parent = os.path.dirname(mudir)
    if parent and os.path.isdir(parent):
        for fn in os.listdir(parent):
            if fn.lower().endswith(".cfg"):
                p = os.path.join(parent, fn)
                if p not in candidates:
                    candidates.append(p)
    stem_lows = [s.lower() for s in stems if s]
    scored = []
    for p in candidates:
        try:
            text = open(p, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if "PART" not in text and "part" not in text[:200].lower():
            continue
        low = text.lower().replace("\\", "/")
        hit = next((s for s in stem_lows if s in low), None)
        # PART { name = science_module } matches mu stem science_module_small
        m_part = re.search(r"(?im)^\s*name\s*=\s*([^\s#/\"]+)", text)
        part_name = (m_part.group(1).strip() if m_part else "").lower()
        if part_name and hit is None:
            pn = part_name.replace(".", "_")
            for s in stem_lows:
                sn = s.replace(".", "_")
                if sn == pn or sn.startswith(pn + "_") or pn.startswith(sn + "_"):
                    hit = s
                    break
        if stem_lows and hit is None:
            # Also try basename match (Size1p5_Tank_01.cfg vs Size1_5_Tank_01)
            base = os.path.splitext(os.path.basename(p))[0].lower()
            base_norm = base.replace(".", "_").replace("p", "_")
            if not any(
                s.replace(".", "_").replace("p", "_") in base_norm
                or base_norm in s.replace(".", "_")
                for s in stem_lows
            ):
                continue
            hit = base
        score = 0
        if "modulepartvariants" in low:
            score += 10
        if part_name:
            pn = part_name.replace(".", "_")
            for s in stem_lows:
                sn = s.replace(".", "_")
                if sn == pn:
                    score += 24
                    break
                if sn.startswith(pn + "_") or pn.startswith(sn + "_"):
                    score += 20
                    break
        # Prefer cfg living next to the .mu over parent-folder noise
        try:
            if os.path.normcase(os.path.dirname(p)) == os.path.normcase(mudir):
                score += 6
        except Exception:
            pass
        for s in stem_lows:
            if f"mesh = {s}" in low or f"mesh={s}" in low:
                score += 5
            if f"model = " in low and s in low:
                score += 4
            if os.path.basename(p).lower().startswith(s.replace(".", "").replace("_", "")[:8]):
                score += 1
            if os.path.basename(p).lower().startswith(s.lower()):
                score += 3
            # Prefer non-temp stem over Foo_temp when both match
            if s == stem_lows[-1] and len(stem_lows) > 1:
                score += 2
        # Prefer cfg whose basename shares the part number token
        bn = os.path.splitext(os.path.basename(p))[0].lower()
        for s in stem_lows:
            sn = s.lower().replace(".", "_")
            bn_n = bn.replace(".", "_").replace("p", "_")
            if sn.replace("p", "_") == bn_n or sn in bn_n or bn_n in sn:
                score += 8
                break
        scored.append((score, p))
    if scored:
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1]
    # Fallback: search GameData for ``{stem}.cfg`` when mudir is outside
    # (e.g. future OS-temp round-trip). Prefer non-recursive shallow match.
    gd = _game_data_root(mudir)
    if not gd:
        # Addon bundled test GameData
        try:
            addon_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            test_gd = os.path.join(addon_root, "test tools v2", "GameData")
            if os.path.isdir(test_gd):
                gd = test_gd
        except Exception:
            gd = None
    if gd and stem_lows:
        # Prefer underscore / stripped forms for filename match
        wants = []
        for s in stem_lows:
            wants.append(s.lower())
            wants.append(s.lower().replace(".", "_"))
            wants.append(s.lower().replace(".", "p").replace("_", "p"))
            # Size1_5 → Size1p5
            wants.append(re.sub(r"(\d)_(\d)", r"\1p\2", s.lower()))
        wants = list(dict.fromkeys(wants))
        # Avoid small.cfg matching science_module_small via ``base in w``.
        _trivial = frozenset({
            "small", "large", "tiny", "mini", "model", "part", "tank",
            "engine", "wing", "nose", "base", "body", "panel", "door",
            "light", "probe", "core",
        })

        def _gd_cfg_matches(base, w):
            if not base or not w:
                return False
            if base == w:
                return True
            if base in _trivial or w in _trivial:
                return False
            if len(base) < 6 or len(w) < 6:
                return False
            if (w.startswith(base + "_") or w.startswith(base + ".")
                    or base.startswith(w + "_") or base.startswith(w + ".")):
                return True
            # Long containment only (Size1p5Tank… aliases); never short tokens
            if len(base) >= 8 and len(w) >= 8 and (base in w or w in base):
                return True
            return False

        try:
            for root, _dirs, files in os.walk(gd):
                for fn in files:
                    if not fn.lower().endswith(".cfg"):
                        continue
                    base = os.path.splitext(fn)[0].lower()
                    if any(_gd_cfg_matches(base, w) for w in wants):
                        return os.path.join(root, fn)
        except Exception:
            pass
    # Never return an unmatched first candidate — that picked MonoPropMini.cfg
    # for Size1.5_Tank_* reimports and dropped ModulePartVariants.
    return None

def _make_marker(collection, parent, name, loc, display="PLAIN_AXES", size=0.15, color=(1, 0.4, 0.1, 1)):
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = display
    obj.empty_display_size = size
    collection.objects.link(obj)
    obj.parent = parent
    obj.location = loc
    obj["mu_fx_preview"] = 1
    try:
        obj.color = color
    except Exception:
        pass
    return obj


def _variant_object_key(name):
    """Map Blender object name → ModulePartVariants GAMEOBJECTS key.

    Importer appends ``∧`` after Unity transform names (e.g. ``Orthogonal∧``),
    and Blender may add ``.001``. Stock cfg uses bare names (``Orthogonal``).
    """
    if not name:
        return ""
    try:
        from ..utils.utils import strip_nnn
        return strip_nnn(name) or ""
    except Exception:
        raw = name
        ind = raw.rfind("\u2227")
        if ind >= 0:
            raw = raw[:ind]
        ind = raw.rfind(".")
        if ind >= 0 and len(raw) - ind == 4 and raw[ind + 1:].isdigit():
            raw = raw[:ind]
        return raw


def _index_named_objects(root):
    """Map GAMEOBJECTS key → list of objects under root."""
    named = {}
    if root is None:
        return named
    stack = [root]
    while stack:
        o = stack.pop()
        raw = _variant_object_key(o.name or "")
        if raw:
            named.setdefault(raw, []).append(o)
        stack.extend(list(o.children))
    return named


def _set_object_hide(obj, hide):
    """Hide/show a single object (not children)."""
    if obj is None:
        return
    try:
        obj.hide_viewport = hide
        obj.hide_render = hide
        try:
            obj.hide_set(hide)
        except Exception:
            pass
        # ModuleLight Flare cards must never regain visible_shadow (opaque EEVEE blotches)
        flare = bool(obj.get("mu_light_flare"))
        for attr, val in (
            ("visible_camera", not hide),
            ("visible_shadow", False if flare else (not hide)),
            ("visible_transmission", not hide),
            ("visible_volume_scatter", not hide),
        ):
            if hasattr(obj, attr):
                try:
                    setattr(obj, attr, val)
                except Exception:
                    pass
        if hide:
            obj["mu_variant_hidden"] = 1
        elif "mu_variant_hidden" in obj:
            del obj["mu_variant_hidden"]
    except Exception:
        pass


def _set_branch_hide(obj, hide, keyed_names=None):
    """Hide/show obj and descendants, skipping children listed in GAMEOBJECTS.

    Critical for RCSBlock Orthogonal + thruster1..5: unhiding Orthogonal must
    NOT force all thrusters visible — each thrusterN has its own flag.
    """
    keyed = keyed_names or set()
    stack = [obj]
    while stack:
        o = stack.pop()
        _set_object_hide(o, hide)
        for ch in o.children:
            ckey = _variant_object_key(ch.name or "")
            if ckey and ckey in keyed:
                # Controlled by its own GAMEOBJECTS entry
                continue
            stack.append(ch)


def parse_module_jettison_names(cfg_text):
    """Return lowercased GO names from all ModuleJettison ``jettisonName`` lines.

    Used so fairing/shroud preview hide still covers jettison meshes that are
    also listed in ModulePartVariants GAMEOBJECTS (Poodle Shroud1/2, Terrier
    Short/TallShroud) without hiding non-jettison variant GOs (LV-1 ``Shroud``
    chamber).
    """
    if not cfg_text or "jettisonName" not in cfg_text:
        return []
    names = []
    seen = set()
    for m in re.finditer(
        r"^\s*jettisonName\s*=\s*(.+)$", cfg_text, re.M | re.I
    ):
        raw = m.group(1).strip().strip('"').split("//")[0].strip()
        for part in raw.replace(";", ",").split(","):
            n = part.strip()
            if not n:
                continue
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(n)
    return names



def _parse_brace_blocks(text, keyword):
    """Yield body strings of KEYWORD { ... } with correct brace depth."""
    if not text:
        return
    for m in re.finditer(rf"{keyword}\s*\{{", text, re.I):
        s0 = m.end() - 1
        depth = 0
        j = s0
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    yield text[s0 + 1:j]
                    break
            j += 1


def _parse_b9_texture_block(body):
    """Parse one B9 SUBTYPE → TEXTURE { ... } block into a dict."""
    tex = {
        "texture": None,
        "currentTexture": None,
        "isNormalMap": False,
        "shaderProperty": None,
        "transforms": [],
        "baseTransforms": [],
    }
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lower()
        v = v.strip().split("//")[0].strip().strip('"')
        if k == "texture":
            tex["texture"] = v
        elif k == "currenttexture":
            tex["currentTexture"] = v
        elif k == "isnormalmap":
            tex["isNormalMap"] = v.lower() in ("true", "1", "yes")
        elif k == "shaderproperty":
            tex["shaderProperty"] = v
        elif k == "transform":
            if v:
                tex["transforms"].append(v)
        elif k == "basetransform":
            if v:
                tex["baseTransforms"].append(v)
    if not tex["texture"]:
        return None
    if not tex["shaderProperty"]:
        tex["shaderProperty"] = "_BumpMap" if tex["isNormalMap"] else "_MainTex"
    return tex


def parse_b9_part_switch(cfg_text):
    """Parse ModuleB9PartSwitch: transforms + full TEXTURE nodes per SUBTYPE.

    B9 enables listed transforms per subtype and disables the rest. TEXTURE
    blocks swap maps (optionally filtered by currentTexture / transform /
    baseTransform / shaderProperty / isNormalMap) — same coverage as the
    in-game plugin for viewport preview.
    """
    if not cfg_text or "ModuleB9PartSwitch" not in cfg_text:
        return None
    modules = []
    for body in _parse_brace_blocks(cfg_text, "MODULE"):
        if not re.search(r"^\s*name\s*=\s*ModuleB9PartSwitch\b", body, re.M | re.I):
            continue
        mid = re.search(r"^\s*moduleID\s*=\s*(.+)$", body, re.M | re.I)
        module_id = (
            mid.group(1).strip().strip('"').split("//")[0].strip()
            if mid else "B9"
        )
        subtypes = []
        all_transforms = set()
        for sbody in _parse_brace_blocks(body, "SUBTYPE"):
            nm = re.search(r"^\s*name\s*=\s*(.+)$", sbody, re.M | re.I)
            if not nm:
                continue
            sname = nm.group(1).strip().strip('"').split("//")[0].strip()
            title_m = re.search(r"^\s*title\s*=\s*(.+)$", sbody, re.M | re.I)
            title = (
                title_m.group(1).strip().strip('"').split("//")[0].strip()
                if title_m else sname
            )
            transforms = []
            for line in sbody.splitlines():
                line = line.strip()
                # Only top-level transform = (not inside TEXTURE/TRANSFORM nodes)
                # Approximate: skip lines after we already collected from bare lines
                # by only reading lines that are direct "transform =" not nested keys
                if re.match(r"(?i)^transform\s*=", line):
                    _, _, v = line.partition("=")
                    t = v.strip().split("//")[0].strip().strip('"')
                    if t:
                        transforms.append(t)
                        all_transforms.add(t)
            textures = []
            for tbody in _parse_brace_blocks(sbody, "TEXTURE"):
                td = _parse_b9_texture_block(tbody)
                if td:
                    textures.append(td)
            subtypes.append({
                "name": sname,
                "title": title,
                "transforms": transforms,
                "textures": textures,
            })
        if not subtypes:
            continue
        modules.append({
            "moduleID": module_id,
            "subtypes": subtypes,
            "all_transforms": sorted(all_transforms),
        })
    return modules or None


def b9_to_variant_info(modules):
    """Convert B9 modules into a ModulePartVariants-compatible info dict.

    ``textures`` is kept as a list of B9 TEXTURE descriptors under key
    ``b9_textures`` (applied by ``_apply_b9_textures``). Flat slot map is
    also filled for the simple single-TEXTURE case so stock path still works.
    """
    if not modules:
        return None
    variants = []
    base = None
    has_tex = False
    has_obj = False
    for mod in modules:
        mid = mod.get("moduleID") or "B9"
        all_t = mod.get("all_transforms") or []
        for i, st in enumerate(mod.get("subtypes") or []):
            sname = st.get("name") or ("Subtype%d" % i)
            enum_name = ("%s/%s" % (mid, sname)) if len(modules) > 1 else sname
            enabled = set(st.get("transforms") or [])
            objects = {}
            for t in all_t:
                objects[t] = t in enabled
            if objects:
                has_obj = True
            b9_tex = list(st.get("textures") or [])
            if b9_tex:
                has_tex = True
            # Flat map for the common single _MainTex case (ModulePartVariants path)
            flat = {}
            for td in b9_tex:
                slot = td.get("shaderProperty") or (
                    "_BumpMap" if td.get("isNormalMap") else "_MainTex"
                )
                url = td.get("texture")
                if url and slot not in flat:
                    flat[slot] = url
                    if slot == "_MainTex":
                        flat["mainTextureURL"] = url
            entry = {
                "name": enum_name,
                "displayName": st.get("title") or sname,
                "objects": objects,
                "textures": flat,
                "b9_textures": b9_tex,
                "b9": True,
                "b9_moduleID": mid,
            }
            variants.append(entry)
            if base is None:
                base = enum_name
    if not variants:
        return None
    kind = "both" if (has_obj and has_tex) else ("texture" if has_tex else "model")
    return {
        "base": base,
        "variants": variants,
        "kind": kind,
        "source": "B9PartSwitch",
    }


def _object_key_plain(name):
    n = (name or "").split("\u2227", 1)[0].strip()
    return n


def _collect_objects_for_b9_tex(root, transforms, base_transforms):
    """Objects targeted by B9 TEXTURE transform / baseTransform filters.

    Empty filters → entire hierarchy under root.
    """
    try:
        stack = [root] + list(getattr(root, "children_recursive", []) or [])
    except Exception:
        stack = [root]
    if not transforms and not base_transforms:
        return list(stack)
    wanted = set()
    tset = {str(t).lower() for t in (transforms or [])}
    bset = {str(t).lower() for t in (base_transforms or [])}
    by_key = {}
    for o in stack:
        try:
            key = _object_key_plain(o.name).lower()
            by_key.setdefault(key, []).append(o)
            # Blender may suffix .001
            base = key.split(".", 1)[0]
            by_key.setdefault(base, []).append(o)
        except Exception:
            pass
    for t in tset:
        for o in by_key.get(t, []):
            wanted.add(o)
    for t in bset:
        for o in by_key.get(t, []):
            wanted.add(o)
            try:
                for ch in getattr(o, "children_recursive", []) or []:
                    wanted.add(ch)
            except Exception:
                pass
    return list(wanted) if wanted else list(stack)


def _materials_on_objects(objs):
    mats = []
    seen = set()
    for o in objs or []:
        for slot in getattr(o, "material_slots", []) or []:
            mat = slot.material
            if mat is None:
                continue
            k = mat.as_pointer()
            if k in seen:
                continue
            seen.add(k)
            mats.append(mat)
    return mats


def _image_name_stem(img):
    if img is None:
        return ""
    try:
        n = img.name or ""
    except Exception:
        n = ""
    if not n:
        try:
            n = os.path.basename(bpy.path.abspath(img.filepath) or "")
        except Exception:
            n = ""
    return os.path.splitext(n)[0].lower()


def _apply_b9_textures(root, data, chosen):
    """Full B9PartSwitch TEXTURE node application (viewport).

    Supports:
      texture, currentTexture, isNormalMap, shaderProperty,
      transform (multi), baseTransform (multi, +children).
    """
    entries = chosen.get("b9_textures") or []
    if not entries:
        return
    _ensure_default_textures_cached(root, data)
    for td in entries:
        url = td.get("texture")
        if not url:
            continue
        path = _resolve_texture_url(root, url)
        if not path:
            # try as-is relative path with extensions
            continue
        img = _load_variant_image(
            path, os.path.splitext(os.path.basename(path))[0]
        )
        if img is None:
            continue
        try:
            if td.get("isNormalMap"):
                img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        slot = td.get("shaderProperty") or (
            "_BumpMap" if td.get("isNormalMap") else "_MainTex"
        )
        objs = _collect_objects_for_b9_tex(
            root, td.get("transforms") or [], td.get("baseTransforms") or []
        )
        mats = _materials_on_objects(objs)
        cur_want = (td.get("currentTexture") or "").strip().lower()
        for mat in mats:
            if cur_want:
                # Only replace if the material currently uses this texture name
                node = _maintex_image_node(mat) if slot == "_MainTex" else None
                if node is None:
                    try:
                        from . import materials as _mats  # optional
                    except Exception:
                        pass
                    # fallback: any image texture node for this slot
                    try:
                        nt = mat.node_tree
                        if nt:
                            for n in nt.nodes:
                                if n.type == "TEX_IMAGE" and n.image:
                                    if slot.lower() in (n.name or "").lower() or slot == "_MainTex":
                                        node = n
                                        break
                    except Exception:
                        pass
                stem = _image_name_stem(node.image if node else None)
                if stem and stem != cur_want and cur_want not in stem:
                    continue
            if slot == "_MainTex" and not _mat_had_stock_slot(data, mat, slot):
                # still allow B9 swaps on mats that had a map; if stock empty skip
                # unless currentTexture matched something
                if not cur_want:
                    try:
                        if not any(
                            n.type == "TEX_IMAGE" and n.image
                            for n in (mat.node_tree.nodes if mat.node_tree else [])
                        ):
                            continue
                    except Exception:
                        continue
            _set_material_slot_image_viewport(mat, slot, img, data=data)


def parse_broken_transform_names(cfg_text):
    """Collect mesh/transform names that represent the *damaged* visual.

    Stock / mods:
      - object names containing .broken / _broken / broken / busted
      - ModuleWheelDamage: damagedTransformName / bustedWheelName
      - ModuleDeployable*: damagedObjectName (when present)
    """
    names = set()
    if not cfg_text:
        return []
    for key in (
        "damagedTransformName",
        "damagedObjectName",
        "bustedWheelName",
        "brokenObjectName",
        "brokenTransformName",
    ):
        for m in re.finditer(
            rf"^\s*{key}\s*=\s*(.+)$", cfg_text, re.M | re.I
        ):
            v = m.group(1).strip().strip('"').split("//")[0].strip()
            if v:
                names.add(v)
    return sorted(names)


def is_broken_object_name(name, cfg_broken_names=None):
    """True if this Blender object is a damaged/broken visual mesh."""
    n = (name or "").lower()
    if not n:
        return False
    wedge = "\u2227"
    key = n.split(wedge, 1)[0].strip()
    if cfg_broken_names:
        for bn in cfg_broken_names:
            bl = str(bn).lower()
            if key == bl or key.startswith(bl + ".") or bl in key:
                return True
    if ".broken" in n or n.endswith("broken") or "_broken" in n:
        return True
    if "busted" in n or n.startswith("damaged") or ".damaged" in n:
        return True
    return False


def _hide_object_branch(obj, hide=True):
    """Hide *obj* and every descendant (meshes, colliders, empties)."""
    n = 0
    if obj is None:
        return 0
    try:
        branch = [obj] + list(getattr(obj, "children_recursive", []) or [])
    except Exception:
        branch = [obj]
    for o in branch:
        try:
            o.hide_viewport = bool(hide)
            o.hide_render = bool(hide)
            n += 1
        except Exception:
            pass
    return n


def apply_broken_default_hide(root, cfg_text=None, hide=True):
    """Hide broken meshes under root (viewport). Keep visible if *only* broken.

    Debris / scrap parts that are entirely damaged geometry stay shown.
    Hides the whole branch under each broken root (children colliders/meshes).
    """
    if root is None:
        return 0
    cfg_names = parse_broken_transform_names(cfg_text or "")
    try:
        stack = [root] + list(getattr(root, "children_recursive", []) or [])
    except Exception:
        stack = [root]
    broken_roots = []
    non_broken_mesh = 0
    for obj in stack:
        try:
            if is_broken_object_name(obj.name, cfg_names):
                # Only top-most broken in this branch (skip if parent already broken)
                parent = getattr(obj, "parent", None)
                skip = False
                while parent is not None:
                    if is_broken_object_name(getattr(parent, "name", ""), cfg_names):
                        skip = True
                        break
                    parent = getattr(parent, "parent", None)
                if not skip:
                    broken_roots.append(obj)
            elif getattr(obj, "type", None) == "MESH":
                on = (obj.name or "").lower()
                if "collider" in on or on.startswith("col"):
                    continue
                if is_broken_object_name(obj.name, cfg_names):
                    continue
                non_broken_mesh += 1
        except Exception:
            pass
    if not broken_roots:
        return 0
    if non_broken_mesh == 0:
        return 0
    n = 0
    for obj in broken_roots:
        n += _hide_object_branch(obj, hide=hide)
    try:
        root[MU_BROKEN_KEY] = json.dumps(
            [o.name for o in broken_roots if o and o.name]
        )
    except Exception:
        pass
    return n


def _attach_suspension_preview(root, cfg_text):
    """Synthesize a short location Action for ModuleWheelSuspension travel.

    Moves ``suspensionTransformName`` along local +Z (Blender) by
    ``suspensionDistance`` so amortyzatory / gear legs scrub in the Timeline.
    Tagged ``mu_fx_preview`` / cfg_preview so export skips it.
    """
    if root is None or not cfg_text or "ModuleWheelSuspension" not in cfg_text:
        return 0
    try:
        from ..utils.action_compat import fcurve_new, push_action_to_nla
    except Exception:
        return 0
    import math

    # Index objects under root once
    named = {}
    try:
        stack = [root] + list(getattr(root, "children_recursive", []) or [])
    except Exception:
        stack = [root]
    for o in stack:
        try:
            n = (o.name or "").split("\u2227", 1)[0].strip()
            if n and n not in named:
                named[n] = o
            # also bare name
            if o.name and o.name not in named:
                named[o.name] = o
        except Exception:
            pass

    count = 0
    for m in re.finditer(r"MODULE\s*\{", cfg_text, re.I):
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        if not re.search(r"^\s*name\s*=\s*ModuleWheelSuspension\b", body, re.M | re.I):
            continue
        tr = re.search(r"^\s*suspensionTransformName\s*=\s*(.+)$", body, re.M | re.I)
        if not tr:
            continue
        tname = tr.group(1).strip().strip('"').split("//")[0].strip()
        dist_m = re.search(r"^\s*suspensionDistance\s*=\s*([-\d.]+)", body, re.M | re.I)
        try:
            dist = float(dist_m.group(1)) if dist_m else 0.2
        except Exception:
            dist = 0.2
        if abs(dist) < 1e-6:
            continue
        obj = named.get(tname)
        if obj is None:
            # fuzzy
            tl = tname.lower()
            for k, v in named.items():
                if k.lower() == tl or k.lower().startswith(tl + "."):
                    obj = v
                    break
        if obj is None:
            continue
        # Axis: Unity suspension is typically local Y → Blender local Z after import
        axis = (0.0, 0.0, 1.0)
        ax_m = re.search(
            r"^\s*suspensionAxis\s*=\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)",
            body, re.M | re.I,
        )
        if ax_m:
            try:
                # Unity Y-up → Blender Z-up: (x,y,z)_u → (x,z,y)_b
                ux, uy, uz = (float(ax_m.group(i)) for i in (1, 2, 3))
                axis = (ux, uz, uy)
            except Exception:
                pass
        try:
            rest = list(obj.location)
        except Exception:
            continue
        # Visible travel on the timeline: ≥10 frames (user request), default 24.
        f0 = 1
        f1 = max(f0 + 10, f0 + 24)
        # Ensure motion is visible even when cfg distance is tiny
        travel = float(dist)
        if abs(travel) < 0.05:
            travel = 0.15 if travel >= 0 else -0.15
        act_name = "Suspension_%s" % (obj.name.split("\u2227")[0][:40])
        # Replace prior preview action of same name
        try:
            old = bpy.data.actions.get(act_name)
            if old is not None:
                bpy.data.actions.remove(old)
        except Exception:
            pass
        act = bpy.data.actions.new(act_name)
        try:
            act["mu_fx_preview"] = 1
            act["mu_cfg_preview"] = 1
        except Exception:
            pass
        try:
            ad = obj.animation_data
            if ad and ad.nla_tracks:
                for t in list(ad.nla_tracks):
                    if (t.name or "").startswith("Suspension"):
                        ad.nla_tracks.remove(t)
        except Exception:
            pass
        # Intermediate keys so the strip is clearly multi-frame in the editor
        frames = [f0]
        steps = max(10, f1 - f0)
        for s in range(1, steps):
            frames.append(f0 + s)
        frames.append(f1)
        frames = sorted(set(int(x) for x in frames))
        for i in range(3):
            delta = float(axis[i]) * travel
            try:
                # Blender 4/5: fcurve_new(action, datablock, data_path, index)
                fc = fcurve_new(act, obj, "location", index=i)
            except TypeError:
                try:
                    fc = fcurve_new(act, "location", index=i)
                except Exception:
                    continue
            except Exception:
                continue
            try:
                while fc.keyframe_points:
                    fc.keyframe_points.remove(fc.keyframe_points[0])
            except Exception:
                pass
            try:
                fc.keyframe_points.add(len(frames))
            except Exception:
                continue
            for ki, fr in enumerate(frames):
                t = 0.0 if f1 == f0 else (float(fr) - f0) / float(f1 - f0)
                val = float(rest[i]) + delta * t
                try:
                    fc.keyframe_points[ki].co = (float(fr), val)
                    fc.keyframe_points[ki].interpolation = "LINEAR"
                except Exception:
                    pass
            try:
                fc.update()
            except Exception:
                pass
        try:
            scene = bpy.context.scene
            if scene and int(scene.frame_end) < f1:
                scene.frame_end = int(f1)
        except Exception:
            pass
        try:
            track, strip = push_action_to_nla(obj, act, "SuspensionTravel")
            if track is not None:
                try:
                    track.mute = False
                except Exception:
                    pass
            if strip is not None:
                try:
                    strip.frame_start = float(f0)
                    strip.frame_end = float(f1)
                    strip.action_frame_start = float(f0)
                    strip.action_frame_end = float(f1)
                except Exception:
                    pass
            # Keep action assigned so Dope Sheet / timeline scrub shows motion
            try:
                if not obj.animation_data:
                    obj.animation_data_create()
                obj.animation_data.action = act
            except Exception:
                pass
            count += 1
        except Exception:
            try:
                if not obj.animation_data:
                    obj.animation_data_create()
                obj.animation_data.action = act
                count += 1
            except Exception:
                pass
    return count


def parse_part_variants(cfg_text):
    """Return {base, variants:[{name, objects, textures, …}], kind}.

    ``kind`` is ``model``, ``texture``, or ``both``.

    When the cfg lists VARIANT blocks but omits ``baseVariant``, KSP keeps the
    .mu stock materials as the default look (Serenity robotics: color stripes
    vs Gray/plain TEXTURE). We insert a synthetic ``Stock`` entry for that.
    """
    if not cfg_text or "ModulePartVariants" not in cfg_text:
        return None
    m = re.search(r"^\s*baseVariant\s*=\s*(.+)$", cfg_text, re.M | re.I)
    base = m.group(1).strip().strip('"') if m else None
    base_display = None
    bd = re.search(r"^\s*baseDisplayName\s*=\s*(.+)$", cfg_text, re.M | re.I)
    if bd:
        base_display = bd.group(1).strip().strip('"').split("//")[0].strip()
    bt = re.search(r"^\s*baseThemeName\s*=\s*(.+)$", cfg_text, re.M | re.I)
    base_theme = None
    if bt:
        base_theme = bt.group(1).strip().strip('"').split("//")[0].strip()
    variants = []
    # Split on VARIANT { ... } blocks (brace depth)
    for block in re.finditer(
        r"VARIANT\s*\{", cfg_text, re.I
    ):
        start = block.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        nm = re.search(r"^\s*name\s*=\s*(.+)$", body, re.M | re.I)
        if not nm:
            continue
        vname = nm.group(1).strip().strip('"')
        objects = {}
        go = re.search(r"GAMEOBJECTS\s*\{([^}]*)\}", body, re.I | re.S)
        if go:
            for line in go.group(1).splitlines():
                line = line.strip()
                if "=" not in line or line.startswith("//"):
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().split("//")[0].strip().lower()
                if k:
                    objects[k] = v in ("true", "1", "yes")
        textures = {}
        tex = re.search(r"TEXTURE\s*\{([^}]*)\}", body, re.I | re.S)
        if tex:
            for line in tex.group(1).splitlines():
                line = line.strip()
                if "=" not in line or line.startswith("//"):
                    continue
                k, _, v = line.partition("=")
                textures[k.strip()] = v.strip().split("//")[0].strip()
        # EXTRA_INFO: procedural fairing base/shell maps (FairingsMat is runtime-only)
        extra = re.search(r"EXTRA_INFO\s*\{([^}]*)\}", body, re.I | re.S)
        if extra:
            for line in extra.group(1).splitlines():
                line = line.strip()
                if "=" not in line or line.startswith("//"):
                    continue
                k, _, v = line.partition("=")
                textures[k.strip()] = v.strip().split("//")[0].strip()
        disabled = []
        dm = re.search(
            r"^\s*disabledAnimations\s*=\s*(.+)$", body, re.M | re.I
        )
        if dm:
            raw = dm.group(1).strip().strip('"')
            disabled = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
        variants.append({
            "name": vname,
            "objects": objects,
            "textures": textures,
            "disabledAnimations": disabled,
        })
    if not variants:
        return None
    # No baseVariant → stock .mu materials are the default (not VARIANT[0]).
    # Serenity robotics: only VARIANT is Gray/plain TEXTURE; stripes live in .mu.
    if base is None:
        stock_label = base_display or base_theme or STOCK_VARIANT_NAME
        # Keep identifier enum-safe (no spaces); label carries theme name.
        stock_name = STOCK_VARIANT_NAME
        if any(v.get("name") == stock_name for v in variants):
            stock_name = "_Stock"
        variants.insert(0, {
            "name": stock_name,
            "objects": {},
            "textures": {},
            "disabledAnimations": [],
            "stock": True,
            "displayName": stock_label,
        })
        base = stock_name
    elif not any(v.get("name") == base for v in variants):
        # Squad sometimes leaves baseVariant as an old alias (TwoBell vs
        # DoubleBell on liquidEngine2-2_v2). Prefer a real VARIANT — never
        # invent empty stock GAMEOBJECTS (that shows every bell at once).
        resolved = _resolve_base_variant_alias(base, variants)
        if resolved:
            base = resolved
        else:
            base = variants[0]["name"]
    has_obj = any(v["objects"] for v in variants)
    has_tex = any(v["textures"] for v in variants)
    if has_obj and has_tex:
        kind = "both"
    elif has_tex:
        kind = "texture"
    else:
        kind = "model"
    out = {
        "base": base,
        "active": base,
        "kind": kind,
        "variants": variants,
    }
    if base_display:
        out["baseDisplayName"] = base_display
    if base_theme:
        out["baseThemeName"] = base_theme
    return out


def _resolve_base_variant_alias(base, variants):
    """Map stale baseVariant names onto a real VARIANT block."""
    if not base:
        return None
    names = [v.get("name") for v in variants if v.get("name")]
    bl = base.lower()
    for n in names:
        if n.lower() == bl:
            return n
    # Common Squad renames
    aliases = {
        "twobell": "doublebell",
        "doublebell": "twobell",
        "onebell": "singlebell",
        "singlebell": "onebell",
    }
    want = aliases.get(bl)
    if want:
        for n in names:
            if n.lower() == want:
                return n
    # Conservative containment (TwoBell ⊂ … only when unique)
    hits = []
    for n in names:
        nl = n.lower()
        if len(bl) >= 5 and (bl in nl or nl in bl):
            hits.append(n)
    if len(hits) == 1:
        return hits[0]
    return None


def _iter_meshes_under(root):
    """Depth-first mesh objects under root (including root)."""
    if root is None:
        return
    stack = [root]
    while stack:
        o = stack.pop()
        if o.type == "MESH":
            yield o
        stack.extend(list(o.children))


def _is_collider_mesh(obj):
    if obj is None:
        return False
    if obj.get("mu_collider"):
        return True
    key = _variant_object_key(obj.name or "").lower()
    return key == "collider"


def has_visible_preview_mesh(root, skip_colliders=True):
    """True when at least one non-hidden render mesh remains under root."""
    for o in _iter_meshes_under(root):
        if o.hide_render or o.hide_viewport:
            continue
        if (o.name or "").startswith("mesh:"):
            continue
        if skip_colliders and _is_collider_mesh(o):
            continue
        data = getattr(o, "data", None)
        if data and getattr(data, "vertices", None) and len(data.vertices) > 0:
            return True
    return False


def pick_viable_base_variant(root, info):
    """Pick baseVariant or first variant that leaves visible geometry in this .mu."""
    if root is None or not info:
        return None
    variants = info.get("variants") or []
    order = []
    base = info.get("base")
    if base:
        order.append(base)
    for v in variants:
        n = v.get("name")
        if n and n not in order:
            order.append(n)
    for name in order:
        if apply_variant(root, name, skip_textures=True) and has_visible_preview_mesh(
            root
        ):
            return name
    return base or (variants[0].get("name") if variants else None)


def variant_has_visible_geometry(root, variant_name):
    """True when applying variant_name would show at least one preview mesh."""
    if root is None or not variant_name:
        return False
    if apply_variant(root, variant_name, skip_textures=True):
        return has_visible_preview_mesh(root)
    return False


def find_cfg_for_part_name(game_data, part_name, mu_path=None):
    """Locate best PART/INTERNAL .cfg for a test ``part_name`` (+ optional .mu path)."""
    if not part_name or not game_data:
        return None
    gd = Path(game_data)
    if not gd.is_dir():
        return None
    mu_rel = None
    mu_stem = None
    if mu_path:
        try:
            mu_abs = Path(mu_path).resolve()
            mu_stem = mu_abs.stem.lower()
            mu_rel = str(mu_abs.relative_to(gd.resolve())).replace("\\", "/").lower()
        except Exception:
            mu_stem = Path(mu_path).stem.lower()
            mu_rel = None
    pn = str(part_name)
    pn_low = pn.lower()
    name_re = re.compile(
        rf"^\s*name\s*=\s*{re.escape(pn)}\s*(?://|$)",
        re.M | re.I,
    )
    best_path = None
    best_score = -1
    for cfg_path in gd.rglob("*.cfg"):
        try:
            text = cfg_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        low = text.lower().replace("\\", "/")
        score = 0
        if name_re.search(text):
            score += 40
            if re.search(r"^\s*INTERNAL\s*\{", text, re.M | re.I):
                score += 8
            elif "PART" in text[:400]:
                score += 5
        if mu_rel and mu_rel.replace(".mu", "") in low:
            score += 35
        elif mu_stem and mu_stem in low:
            score += 22
        if pn_low in low:
            score += 6
        if "modulepartvariants" in low:
            score += 12
        if score < 18:
            continue
        if score > best_score:
            best_score = score
            best_path = cfg_path
    return str(best_path) if best_path else None


def apply_part_cfg_preview(root, cfg_path, mudir=None, apply_base=True):
    """Apply ModulePartVariants / jettison from a specific PART .cfg (test harness)."""
    if root is None or not cfg_path or not os.path.isfile(cfg_path):
        return False
    try:
        text = open(cfg_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return False
    if mudir:
        try:
            root["mu_dirname"] = mudir
        except Exception:
            pass
    attach_path = (root.get("mu_cfg_path") or "").strip()
    try:
        root["mu_cfg_path"] = cfg_path
    except Exception:
        pass
    info = parse_part_variants(text)
    if info is None:
        # B9PartSwitch mesh/resource switches → same Options dropdown
        try:
            b9mods = parse_b9_part_switch(text)
            info = b9_to_variant_info(b9mods)
        except Exception as e:
            print("WARNING: B9PartSwitch parse:", e)
            info = None
    elif "ModuleB9PartSwitch" in text:
        # Merge B9 subtypes as extra variants (prefixed when needed)
        try:
            b9mods = parse_b9_part_switch(text)
            b9info = b9_to_variant_info(b9mods)
            if b9info and b9info.get("variants"):
                existing = {v.get("name") for v in info.get("variants") or []}
                for v in b9info["variants"]:
                    if v.get("name") not in existing:
                        info.setdefault("variants", []).append(v)
        except Exception as e:
            print("WARNING: B9PartSwitch merge:", e)
    if info:
        # Keep stock texture snapshot from import_mu.attach_cfg (must not
        # re-cache after a variant TEXTURE swap — that poisons restore/export).
        try:
            prev_data = json.loads(root.get(MU_VARIANTS_KEY) or "{}")
        except Exception:
            prev_data = {}
        # attach_cfg may have picked a sibling PART in shared Assets/ folders.
        if (
            attach_path
            and os.path.normcase(attach_path) != os.path.normcase(cfg_path)
        ):
            for key in ("_default_maintex", "_default_texprops", "_default_texmapping"):
                prev_data.pop(key, None)
        for key in ("_default_maintex", "_default_texprops", "_default_texmapping"):
            if prev_data.get(key):
                info[key] = prev_data[key]
        root[MU_VARIANTS_KEY] = json.dumps(info)
        try:
            data = json.loads(root[MU_VARIANTS_KEY])
            if not data.get("_default_maintex") or not data.get("_default_texmapping"):
                _ensure_default_textures_cached(root, data)
            root[MU_VARIANTS_KEY] = json.dumps(data)
        except Exception:
            pass
        if apply_base:
            base = pick_viable_base_variant(root, info) or info.get("base")
            if not base and info.get("variants"):
                base = info["variants"][0].get("name")
            if base:
                apply_variant(root, base)
    else:
        # Harness cfg has no ModulePartVariants — drop stale attach_cfg variants
        # (shared .mu e.g. rocketNoseConeSize4 vs protectiveRocketNoseMk7_v2).
        try:
            prev_data = json.loads(root.get(MU_VARIANTS_KEY) or "{}")
        except Exception:
            prev_data = {}
        keep = {}
        for key in ("_default_maintex", "_default_texprops", "_default_texmapping"):
            if prev_data.get(key):
                keep[key] = prev_data[key]
        if keep:
            root[MU_VARIANTS_KEY] = json.dumps(keep)
        elif MU_VARIANTS_KEY in root:
            del root[MU_VARIANTS_KEY]
    jettison = parse_module_jettison_names(text)
    if jettison:
        try:
            root[MU_JETTISON_KEY] = json.dumps([str(n).lower() for n in jettison])
        except Exception:
            pass
        try:
            if MU_VARIANTS_KEY in root:
                data = json.loads(root[MU_VARIANTS_KEY])
                data["jettisonNames"] = [str(n).lower() for n in jettison]
                root[MU_VARIANTS_KEY] = json.dumps(data)
        except Exception:
            pass
    return True


def apply_variant(root, variant_name, skip_textures=False):
    """Show/hide GAMEOBJECTS + swap TEXTURE main maps for the named variant."""
    if root is None or MU_VARIANTS_KEY not in root:
        return False
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return False
    variants = data.get("variants") or []
    chosen = None
    for v in variants:
        if v.get("name") == variant_name:
            chosen = v
            break
    if chosen is None:
        return False
    # Cache stock maps before the first TEXTURE swap (never after Gray/plain).
    _ensure_default_textures_cached(root, data)
    named = _index_named_objects(root)
    all_keys = set()
    for v in variants:
        all_keys.update((v.get("objects") or {}).keys())
    obj_map = chosen.get("objects") or {}
    is_stock = bool(chosen.get("stock"))
    present_keys = {k for k in all_keys if k in named}
    if present_keys and not (is_stock and not obj_map):
        for key in present_keys:
            if key in obj_map:
                visible = bool(obj_map[key])
            else:
                # Owned by other variants — hide when not listed here
                visible = False
            for o in named.get(key, []):
                # Skip keyed children so Orthogonal=true does not unhide thruster5
                # when thruster5=false (4Horn / 3Horn / 2Horn).
                _set_branch_hide(o, not visible, keyed_names=present_keys)
    elif present_keys and is_stock and not obj_map:
        # Synthetic Stock: show every GAMEOBJECTS key (imported mesh set).
        for key in present_keys:
            for o in named.get(key, []):
                _set_branch_hide(o, False, keyed_names=present_keys)
    if not skip_textures:
        _apply_variant_textures(root, data, chosen)
    data["active"] = variant_name
    root[MU_VARIANTS_KEY] = json.dumps(data)
    return True


def _game_data_root(mudir):
    """Walk up from part folder to …/GameData."""
    cur = mudir
    for _ in range(12):
        if not cur:
            return None
        if os.path.basename(cur).lower() == "gamedata":
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def _resolve_texture_url(root, url):
    """Resolve KSP ``mainTextureURL`` (GameData-relative, no extension) to a file."""
    if not url:
        return None
    url = url.replace("\\", "/").strip().strip('"')
    mudir = None
    try:
        mudir = root.get("mu_dirname") if root is not None else None
    except Exception:
        mudir = None
    if not mudir:
        # Infer from any image filepath under this import
        for mat in _iter_materials_under(root):
            node = _maintex_image_node(mat)
            if node is None or node.image is None:
                continue
            try:
                fp = bpy.path.abspath(node.image.filepath)
            except Exception:
                fp = node.image.filepath
            if fp and os.path.isfile(fp):
                mudir = os.path.dirname(fp)
                break
    candidates = []
    gd = _game_data_root(mudir) if mudir else None
    if gd:
        base = os.path.join(gd, *url.split("/"))
        for ext in (".dds", ".png", ".tga", ".mbm"):
            candidates.append(base + ext)
    if mudir:
        leaf = url.split("/")[-1]
        for ext in (".dds", ".png", ".tga", ".mbm"):
            candidates.append(os.path.join(mudir, leaf + ext))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _iter_materials_under(root):
    seen = set()
    stack = [root] if root is not None else []
    while stack:
        o = stack.pop()
        if o.type == "MESH" and getattr(o, "data", None):
            for slot in o.material_slots:
                mat = slot.material
                if mat is not None and mat.name not in seen:
                    seen.add(mat.name)
                    yield mat
        stack.extend(list(o.children))


def _maintex_image_node(mat):
    nt = getattr(mat, "node_tree", None)
    if not nt:
        return None
    node = nt.nodes.get("_MainTex")
    if node is not None and getattr(node, "image", None) is not None:
        return node
    # Fallback: first image node feeding Principled Base Color / SpecPBR
    for n in nt.nodes:
        if n.type == "TEX_IMAGE" and n.image is not None:
            return n
    return None


def _curve_end_value(curve_body, default=0.0):
    """Last ``key = t value …`` sample from a ModuleColorChanger *Curve block."""
    if not curve_body:
        return float(default)
    last = default
    for m in re.finditer(
        r"^\s*key\s*=\s*([-\d.]+)\s+([-\d.]+)", curve_body, re.M | re.I
    ):
        try:
            last = float(m.group(2))
        except Exception:
            pass
    return float(last)


def apply_color_changer_preview(root, info):
    """Viewport NLA: ModuleColorChanger lights off→on (Cupola / cabin windows).

    Stock ``_EmissiveColor`` stays at cfg OFF in mumatprop (export/reimport safe).
    A preview-only material Action (``mu_fx_preview``) drives the ON colour so
    scrubbing / GIF matches other glow clips. Export skips that Action.
    """
    if not root or not info:
        return 0
    from ..utils.action_compat import (
        ensure_action_assigned,
        fcurve_new,
        push_action_to_nla,
    )
    try:
        from ..shader.colorprops import apply_color_prop_to_nodes
    except Exception:
        apply_color_prop_to_nodes = None
    try:
        from ..shader.shader import ensure_heat_emissive_map_visible
    except Exception:
        ensure_heat_emissive_map_visible = None

    prop_name = info.get("shaderProperty") or "_EmissiveColor"
    off = list(info.get("off") or (0.0, 0.0, 0.0, 1.0))
    on = list(info.get("on") or (1.0, 1.0, 0.7, 1.0))
    while len(off) < 4:
        off.append(1.0)
    while len(on) < 4:
        on.append(1.0)
    off = tuple(float(x) for x in off[:4])
    on = tuple(float(x) for x in on[:4])
    mat_filter = None
    if info.get("useMaterialsList") or info.get("materialsNames"):
        names = info.get("materialsNames") or []
        mat_filter = {n.lower() for n in names if n}
    included = info.get("includedRenderers") or []
    included_set = {n.lower() for n in included if n} if included else None

    scene = bpy.context.scene
    try:
        fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
    except Exception:
        fps = 24.0
    fps = max(1.0, fps)
    # ~1s fade (ModuleColorChanger animRate≈0.8 → similar feel)
    f0 = int(scene.frame_start) if scene else 1
    f1 = f0 + max(12, int(round(fps * 1.0)))

    n = 0
    for mat in _iter_materials_under(root):
        if mat_filter is not None:
            stem = (mat.name or "").split("\u2227")[0]
            if "." in stem:
                b, s = stem.rsplit(".", 1)
                if s.isdigit():
                    stem = b
            if stem.lower() not in mat_filter and (mat.name or "").lower() not in mat_filter:
                continue
        if included_set is not None:
            # includedRenderers names mesh/renderer hosts — keep mats used by them
            used = False
            for o in bpy.data.objects:
                if not o.data or not hasattr(o.data, "materials"):
                    continue
                oname = (o.name or "").split("\u2227")[0]
                if "." in oname:
                    b, s = oname.rsplit(".", 1)
                    if s.isdigit():
                        oname = b
                if oname.lower() not in included_set:
                    continue
                if mat.name in (m.name for m in o.data.materials if m):
                    used = True
                    break
            if not used:
                continue
        mp = getattr(mat, "mumatprop", None)
        if not mp:
            continue
        prop = None
        prop_idx = -1
        try:
            for i, p in enumerate(mp.color.properties):
                if p.name == prop_name:
                    prop = p
                    prop_idx = i
                    break
        except Exception:
            continue
        if prop is None or prop_idx < 0:
            continue
        # Keep stock OFF for export; nodes follow via sync handler
        try:
            prop.value = off
        except Exception:
            pass
        if apply_color_prop_to_nodes is not None:
            try:
                apply_color_prop_to_nodes(mat, prop)
            except Exception:
                pass
        if ensure_heat_emissive_map_visible is not None:
            try:
                ensure_heat_emissive_map_visible(mat)
            except Exception:
                pass
        nt = getattr(mat, "node_tree", None)
        if nt:
            mul = nt.nodes.get("_MuEmStrengthScale")
            if mul is not None and mul.inputs:
                try:
                    mul.inputs[1].default_value = max(
                        float(mul.inputs[1].default_value), 1.1
                    )
                except Exception:
                    pass

        # Already have a preview ColorChanger track?
        ad = getattr(mat, "animation_data", None)
        if ad and ad.nla_tracks:
            skip = False
            for t in ad.nla_tracks:
                tn = (t.name or "")
                if tn.startswith("ColorChangerLights"):
                    skip = True
                    break
            if skip:
                n += 1
                continue

        act_name = f"ColorChangerLights.{mat.name}"[:60]
        act = bpy.data.actions.get(act_name)
        if act is None:
            act = bpy.data.actions.new(name=act_name)
        try:
            act["mu_fx_preview"] = 1
        except Exception:
            pass
        try:
            act["mu_color_changer_preview"] = 1
        except Exception:
            pass
        ensure_action_assigned(mat, act)
        path = f"mumatprop.color.properties[{prop_idx}].value"
        for i in range(4):
            try:
                fc = fcurve_new(act, mat, path, i)
            except Exception:
                continue
            try:
                while fc.keyframe_points:
                    fc.keyframe_points.remove(fc.keyframe_points[0])
            except Exception:
                pass
            try:
                fc.keyframe_points.add(2)
                fc.keyframe_points[0].co = (float(f0), float(off[i]))
                fc.keyframe_points[0].interpolation = "LINEAR"
                fc.keyframe_points[1].co = (float(f1), float(on[i]))
                fc.keyframe_points[1].interpolation = "LINEAR"
            except Exception:
                pass
        track, _strip = push_action_to_nla(mat, act, "ColorChangerLights")
        # NLATrack may not support IDProperties in Blender 5 — name is the marker
        try:
            if track is not None and hasattr(track, "name"):
                track.name = "ColorChangerLights"
        except Exception:
            pass
        try:
            mat["mu_color_changer_preview"] = 1
        except Exception:
            pass
        n += 1
    return n


def parse_module_color_changer(cfg_text):
    """Parse first ModuleColorChanger → emissive window-light preview targets.

    Skips heat-shield burn modules (``_BurnColor``, editor-off without light flags).
    Returns dict with shaderProperty + off/on RGBA (+ optional materialsNames), or None.
    """
    if not cfg_text or "ModuleColorChanger" not in cfg_text:
        return None
    for block in re.finditer(
        r"MODULE\s*\{", cfg_text, re.I
    ):
        start = block.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        nm = re.search(r"^\s*name\s*=\s*(.+)$", body, re.M | re.I)
        if not nm or nm.group(1).strip().strip('"') != "ModuleColorChanger":
            continue
        prop = "_EmissiveColor"
        pm = re.search(r"^\s*shaderProperty\s*=\s*(.+)$", body, re.M | re.I)
        if pm:
            prop = pm.group(1).strip().strip('"')
        # Heat shields use _BurnColor — not cabin lights
        if "burn" in prop.lower():
            continue

        def _bool_field(name, default=False):
            m = re.search(rf"^\s*{name}\s*=\s*(.+)$", body, re.M | re.I)
            if not m:
                return default
            v = m.group(1).strip().strip('"').lower()
            return v in ("true", "1", "yes")

        toggle_ed = _bool_field("toggleInEditor", False)
        use_rate = _bool_field("useRate", False)
        use_mats = _bool_field("useMaterialsList", False)
        mats = []
        mm = re.search(r"^\s*materialsNames\s*=\s*(.+)$", body, re.M | re.I)
        if mm:
            mats = [
                x.strip()
                for x in mm.group(1).strip().strip('"').split(",")
                if x.strip()
            ]
            if mats:
                use_mats = True
        included = []
        ir = re.search(r"^\s*includedRenderers\s*=\s*(.+)$", body, re.M | re.I)
        if ir:
            included = [
                x.strip()
                for x in ir.group(1).strip().strip('"').split(",")
                if x.strip()
            ]
        # Lights: emissive + editor toggle / rate / materials list (EVA)
        if prop != "_EmissiveColor" and not mats and not included:
            continue
        if not (toggle_ed or use_rate or use_mats or included):
            continue

        def _curve(name):
            m = re.search(
                rf"{name}\s*\{{([^}}]*)\}}", body, re.I | re.S
            )
            return m.group(1) if m else ""

        on = (
            _curve_end_value(_curve("redCurve"), 1.0),
            _curve_end_value(_curve("greenCurve"), 1.0),
            _curve_end_value(_curve("blueCurve"), 1.0),
            _curve_end_value(_curve("alphaCurve"), 1.0),
        )
        # EVA-style solid tint when curves missing but RGB colors set
        if not _curve("redCurve"):
            def _f(n, d):
                m = re.search(rf"^\s*{n}\s*=\s*(.+)$", body, re.M | re.I)
                if not m:
                    return d
                try:
                    return float(m.group(1).strip().strip('"'))
                except Exception:
                    return d
            on = (
                _f("redColor", on[0]),
                _f("greenColor", on[1]),
                _f("blueColor", on[2]),
                _curve_end_value(_curve("alphaCurve"), on[3]),
            )
        out = {
            "shaderProperty": prop,
            "off": (0.0, 0.0, 0.0, 1.0),
            "on": on,
        }
        if mats:
            out["materialsNames"] = mats
            out["useMaterialsList"] = True
        if included:
            out["includedRenderers"] = included
        return out
    return None


def _ensure_default_textures_cached(root, data):
    """Remember per-material stock texture names (before any TEXTURE preview swap).

    Includes empty ``tex`` slots (flag placeholders) so Stock restore can clear
    a Dark/Gray leak off materials that never had a stock map.
    Also caches node ``texture_mapping`` per slot (stock UV baseline for variants).
    """
    need_maintex = data.get("_default_maintex") is None
    need_texprops = data.get("_default_texprops") is None
    need_mapping = data.get("_default_texmapping") is None
    if not need_maintex and not need_texprops and not need_mapping:
        return
    if need_maintex or need_texprops:
        defaults = {}
        texprops = {}
        for mat in _iter_materials_under(root):
            node = _maintex_image_node(mat)
            if node is not None and node.image is not None:
                defaults[mat.name] = node.image.name
            try:
                slots = {}
                for tp in mat.mumatprop.texture.properties:
                    name = getattr(tp, "name", None)
                    if not name:
                        continue
                    slots[name] = getattr(tp, "tex", None) or ""
                if slots:
                    texprops[mat.name] = slots
            except Exception:
                pass
        if need_maintex:
            data["_default_maintex"] = defaults
        if need_texprops:
            data["_default_texprops"] = texprops
    if need_mapping:
        mapping = {}
        for mat in _iter_materials_under(root):
            nt = getattr(mat, "node_tree", None)
            if not nt:
                continue
            mat_map = {}
            try:
                for tp in mat.mumatprop.texture.properties:
                    name = getattr(tp, "name", None)
                    if not name:
                        continue
                    node = _shader_slot_image_node(mat, name)
                    if node is None:
                        continue
                    mat_map[name] = {
                        "scale": (
                            float(node.texture_mapping.scale[0]),
                            float(node.texture_mapping.scale[1]),
                        ),
                        "translation": (
                            float(node.texture_mapping.translation[0]),
                            float(node.texture_mapping.translation[1]),
                        ),
                    }
            except Exception:
                pass
            if mat_map:
                mapping[mat.name] = mat_map
        data["_default_texmapping"] = mapping


def _mat_had_stock_slot(data, mat, slot):
    """True when this material had a non-empty stock map for ``slot``.

    TEXTURE variants must not paint Dark/Gray onto flag / empty placeholders
    (smallClaw / GrapplingDevice ``flagMat``) — those leak into .mu export.
    """
    if mat is None or not slot:
        return False
    texprops = (data.get("_default_texprops") or {}).get(mat.name) or {}
    if slot in texprops:
        return bool(texprops.get(slot))
    if slot == "_MainTex":
        return bool((data.get("_default_maintex") or {}).get(mat.name))
    return False


def _clear_material_slot_image(mat, slot_name):
    """Clear Image Texture node + mumatprop.tex for a slot (stock was empty)."""
    if mat is None or not slot_name:
        return False
    cleared = False
    nt = getattr(mat, "node_tree", None)
    if nt:
        node = _shader_slot_image_node(mat, slot_name)
        if node is not None:
            node.image = None
            cleared = True
    try:
        for tp in mat.mumatprop.texture.properties:
            if tp.name != slot_name:
                continue
            tp.tex = ""
            cleared = True
            break
    except Exception:
        pass
    return cleared


def restore_default_textures(root):
    """Undo ModulePartVariants TEXTURE preview swaps (export-safe stock maps).

    Viewport may still show a paint variant; call this before .mu export so
    ``mumatprop.tex`` matches the imported stock names (Serenity Gray/plain).
    Keeps MassiveBooster ESA preview intact until export restore.
    """
    if root is None or MU_VARIANTS_KEY not in root:
        return False
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return False
    _ensure_default_textures_cached(root, data)
    defaults = data.get("_default_maintex") or {}
    texprops = data.get("_default_texprops") or {}
    restored = False
    for mat in _iter_materials_under(root):
        slots = dict(texprops.get(mat.name) or {})
        if not slots and defaults.get(mat.name):
            slots["_MainTex"] = defaults[mat.name]
        for slot, dname in slots.items():
            if not dname:
                if _clear_material_slot_image(mat, slot):
                    restored = True
                continue
            if dname not in bpy.data.images:
                continue
            img = bpy.data.images[dname]
            _configure_variant_image(img, getattr(img, "filepath", "") or "")
            if _set_material_slot_image(mat, slot, img):
                restored = True
    base = data.get("base")
    if base:
        data["active"] = base
    root[MU_VARIANTS_KEY] = json.dumps(data)
    return restored


def get_active_variant_name(root):
    """Return currently active ModulePartVariants name, or None."""
    if root is None or MU_VARIANTS_KEY not in root:
        return None
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return None
    return data.get("active") or data.get("base") or None


def add_variant_duplicate(root, new_name=None):
    """Duplicate active (or Stock/base) variant; return new name or None.

    Only updates ``mu_variants`` JSON (cfg ModulePartVariants). Does **not**
    create Blender Collection / Outliner entries — GAMEOBJECTS/TEXTURE state
    is applied via ``apply_variant``; no meshes are invented.
    """
    if root is None or MU_VARIANTS_KEY not in root:
        return None
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return None
    variants = list(data.get("variants") or [])
    if not variants:
        return None
    active = data.get("active") or data.get("base")
    src = None
    for v in variants:
        if v.get("name") == active:
            src = v
            break
    if src is None:
        src = variants[0]
    base_name = new_name or f"{src.get('name') or 'Variant'}_copy"
    existing = {v.get("name") for v in variants}
    name = base_name
    n = 2
    while name in existing:
        name = f"{base_name}_{n}"
        n += 1
    import copy
    dup = copy.deepcopy(src)
    dup["name"] = name
    dup["displayName"] = name
    dup["themeName"] = name
    dup.pop("stock", None)
    variants.append(dup)
    data["variants"] = variants
    data["active"] = name
    root[MU_VARIANTS_KEY] = json.dumps(data)
    apply_variant(root, name)
    return name


def remove_active_variant(root):
    """Remove active variant unless it is Stock or the only remaining entry.

    Only removes the name from ``mu_variants`` JSON and re-applies the new
    active via ``apply_variant``. Does **not** delete imported meshes or
    Outliner collections (those are .mu hierarchy / vis groups, not cfg
    VARIANT entries).

    Returns True on success.
    """
    if root is None or MU_VARIANTS_KEY not in root:
        return False
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return False
    variants = list(data.get("variants") or [])
    if len(variants) <= 1:
        return False
    active = data.get("active") or data.get("base")
    chosen = next((v for v in variants if v.get("name") == active), None)
    if chosen is None:
        return False
    # Never delete synthetic Stock / sole base
    if chosen.get("stock") or chosen.get("name") == STOCK_VARIANT_NAME:
        return False
    if chosen.get("name") == data.get("base"):
        # Reassign base to first remaining non-removed variant later
        pass
    variants = [v for v in variants if v.get("name") != active]
    if not variants:
        return False
    data["variants"] = variants
    if data.get("base") == active:
        data["base"] = variants[0].get("name")
    new_active = data.get("base") or variants[0].get("name")
    if not any(v.get("name") == new_active for v in variants):
        new_active = variants[0].get("name")
    data["active"] = new_active
    root[MU_VARIANTS_KEY] = json.dumps(data)
    apply_variant(root, new_active)
    return True


def prepare_stock_for_export(root):
    """Switch hierarchy to stock/base materials + GAMEOBJECTS for normal export.

    Returns the previous active variant name (for ``finish_stock_export``),
    or None when the root has no ``mu_variants``.
    """
    if root is None or MU_VARIANTS_KEY not in root:
        return None
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return None
    prev = data.get("active") or data.get("base")
    base = data.get("base")
    if base:
        # GAMEOBJECTS only — TEXTURE swaps are viewport-only and restored below.
        apply_variant(root, base, skip_textures=True)
    restore_default_textures(root)
    return prev


def finish_stock_export(root, previous_variant):
    """Re-apply the viewport preview variant after a stock .mu export."""
    if root is None or not previous_variant:
        return False
    ok = bool(apply_variant(root, previous_variant))
    try:
        scene = bpy.context.scene
        if hasattr(scene, "mu_active_variant"):
            scene["mu_active_variant"] = previous_variant
    except Exception:
        pass
    return ok


def _configure_variant_image(img, path):
    """Match import_mu.textures.load_image so EEVEE does not premul-black DDS."""
    if img is None:
        return
    try:
        # Squad diffuse DDS often has near-zero alpha; STRAIGHT → black in EEVEE.
        img.alpha_mode = "CHANNEL_PACKED"
    except Exception:
        pass
    try:
        img.colorspace_settings.is_data = False
    except Exception:
        pass
    if path and str(path).lower().endswith(".dds"):
        try:
            img.muimageprop.invertY = True
        except Exception:
            pass


def _load_variant_image(path, name_hint):
    """Load variant DDS/PNG from GameData."""
    if not path or not os.path.isfile(path):
        return None
    path = os.path.normpath(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    name = name_hint or stem
    if name in bpy.data.images:
        img = bpy.data.images[name]
        try:
            if os.path.normpath(bpy.path.abspath(img.filepath)) != path:
                img.filepath = path
                img.reload()
        except Exception:
            pass
        _configure_variant_image(img, path)
        return img
    try:
        img = bpy.data.images.load(path)
        img.name = name
        _configure_variant_image(img, path)
        return img
    except Exception:
        return None


def _shader_slot_image_node(mat, slot_name):
    """Image Texture node for a mumatprop slot (handles _MainTex alias)."""
    if slot_name == "_MainTex":
        return _maintex_image_node(mat)
    nt = getattr(mat, "node_tree", None)
    if not nt or not slot_name:
        return None
    node = nt.nodes.get(slot_name)
    if node is None or not hasattr(node, "image"):
        return None
    return node


def _apply_texprop_mapping(mat, texprop):
    """Push scale/offset (+ DDS invertY) onto the Image Texture node."""
    nt = getattr(mat, "node_tree", None)
    if not nt or not texprop or not getattr(texprop, "name", None):
        return
    node = _shader_slot_image_node(mat, texprop.name)
    if node is None:
        return
    try:
        scale = Vector((float(texprop.scale[0]), float(texprop.scale[1])))
        offset = Vector((float(texprop.offset[0]), float(texprop.offset[1])))
    except Exception:
        scale = Vector((1.0, 1.0))
        offset = Vector((0.0, 0.0))
    tex_name = getattr(texprop, "tex", "") or ""
    if tex_name in bpy.data.images:
        img = bpy.data.images[tex_name]
        try:
            if img.muimageprop.invertY:
                scale = Vector((scale.x, -scale.y))
                offset = Vector((offset.x, 1.0 - offset.y))
        except Exception:
            pass
    try:
        node.texture_mapping.translation.xy = offset
        node.texture_mapping.scale.xy = scale
    except Exception:
        pass


def _set_material_slot_image_viewport(mat, slot_name, img, data=None):
    """Viewport-only variant swap: change node image only (UV stays from import).

    ModulePartVariants TEXTURE maps share the same UV layout as stock .mu;
    re-applying scale/offset/invertY scrambles mapping (adapter zebra, etc.).
    ``data`` is ignored (kept for call-site compatibility).
    """
    del data
    if mat is None or img is None or not slot_name:
        return False
    node = _shader_slot_image_node(mat, slot_name)
    if node is None:
        return False
    is_bump = slot_name == "_BumpMap"
    try:
        for tp in mat.mumatprop.texture.properties:
            if tp.name == slot_name:
                is_bump = bool(getattr(tp, "type", False))
                break
    except Exception:
        pass
    node.image = img
    try:
        node.image.colorspace_settings.is_data = is_bump
    except Exception:
        pass
    return True


def _set_material_slot_image(mat, slot_name, img):
    """Assign image to shader slot node + keep mumatprop.tex / UV mapping in sync."""
    if mat is None or img is None or not slot_name:
        return False
    node = _shader_slot_image_node(mat, slot_name)
    if node is None:
        return False
    node.image = img
    try:
        for tp in mat.mumatprop.texture.properties:
            if tp.name != slot_name:
                continue
            tp.tex = img.name
            _apply_texprop_mapping(mat, tp)
            return True
    except Exception:
        pass
    try:
        if img.muimageprop.invertY:
            node.texture_mapping.scale.xy = (1.0, -1.0)
            node.texture_mapping.translation.xy = (0.0, 1.0)
    except Exception:
        pass
    return True


def _apply_variant_textures(root, data, chosen):
    """Apply ModulePartVariants TEXTURE + B9PartSwitch TEXTURE switches."""
    _ensure_default_textures_cached(root, data)
    # Full B9 TEXTURE nodes (transform-scoped, currentTexture, normals…)
    if chosen.get("b9_textures"):
        try:
            _apply_b9_textures(root, data, chosen)
        except Exception as e:
            print("WARNING: B9 TEXTURE apply:", e)
        # Still fall through for any flat slot map on the same subtype
    defaults = data.get("_default_maintex") or {}
    tex_map = chosen.get("textures") or {}
    mats = list(_iter_materials_under(root))
    if not mats:
        return

    # Map shader property → GameData URL (mainTextureURL aliases _MainTex)
    slot_urls = {}
    material_filter = None
    for k, v in (tex_map or {}).items():
        if not v:
            continue
        key = (k or "").strip()
        if key in ("mainTextureURL", "mainTexture", "FairingsTextureURL"):
            slot_urls["_MainTex"] = v
        elif key in ("TextureNormalURL", "FairingsNormalURL", "_BumpMap"):
            slot_urls["_BumpMap"] = v
        elif key.startswith("_"):
            slot_urls[key] = v
        elif key == "materialName":
            material_filter = v

    # Procedural fairing: FairingsMat is runtime-only — map to shell preview mat
    def _mat_matches(mat):
        if not material_filter:
            return True
        mn = (mat.name or "").split(".")[0]
        want = material_filter
        if want == "FairingsMat":
            return mn in ("FairingIconShell", "FairingsMat", "FairingShell")
        return mn == want or want in mn

    if slot_urls:
        for slot, url in slot_urls.items():
            path = _resolve_texture_url(root, url)
            if not path:
                continue
            img = _load_variant_image(
                path, os.path.splitext(os.path.basename(path))[0]
            )
            if img is None:
                continue
            for mat in mats:
                if not _mat_matches(mat):
                    continue
                # Skip flag / empty placeholders (stock had no map for this slot).
                if not _mat_had_stock_slot(data, mat, slot):
                    continue
                _set_material_slot_image_viewport(mat, slot, img, data=data)

    # EXTRA_INFO BaseTextureName → FairingBase (and BaseNormalsName → bump)
    base_tex = (tex_map or {}).get("BaseTextureName") or (tex_map or {}).get(
        "DefaultBaseTextureURL"
    )
    base_nm = (tex_map or {}).get("BaseNormalsName") or (tex_map or {}).get(
        "DefaultBaseNormalsURL"
    )
    base_mat_name = (tex_map or {}).get("BaseMaterialName") or "FairingBase"
    if base_tex or base_nm:
        for mat in mats:
            mn = (mat.name or "").split(".")[0]
            if mn != base_mat_name and base_mat_name not in mn:
                continue
            if base_tex:
                path = _resolve_texture_url(root, base_tex)
                if path:
                    img = _load_variant_image(
                        path, os.path.splitext(os.path.basename(path))[0]
                    )
                    if img is not None:
                        _set_material_slot_image_viewport(mat, "_MainTex", img, data=data)
            if base_nm:
                path = _resolve_texture_url(root, base_nm)
                if path:
                    img = _load_variant_image(
                        path, os.path.splitext(os.path.basename(path))[0]
                    )
                    if img is not None:
                        _set_material_slot_image_viewport(mat, "_BumpMap", img, data=data)

    if not slot_urls and not base_tex:
        # Base / White / Stock: restore every cached slot (hinge multi-mat).
        texprops = data.get("_default_texprops") or {}
        for mat in mats:
            slots = dict(texprops.get(mat.name) or {})
            if not slots and defaults.get(mat.name):
                slots["_MainTex"] = defaults[mat.name]
            for slot, dname in slots.items():
                if not dname:
                    _clear_material_slot_image(mat, slot)
                    continue
                if dname not in bpy.data.images:
                    continue
                img = bpy.data.images[dname]
                _configure_variant_image(img, getattr(img, "filepath", "") or "")
                _set_material_slot_image(mat, slot, img)


def find_variant_root(obj=None, context=None):
    """Prefer active object / active collection hierarchy for mu_variants.

    When multiple imported .mu with variants share a scene, Options must
    target the selection's import root — not the first root in bpy.data.
    """
    def _walk_up(o):
        cur = o
        while cur is not None:
            if MU_VARIANTS_KEY in cur:
                return cur
            cur = cur.parent
        return None

    def _scan_objects(objects):
        # Prefer roots (no parent) that carry variants, else any match
        roots = []
        any_hit = []
        for o in objects:
            if MU_VARIANTS_KEY not in o:
                continue
            any_hit.append(o)
            if o.parent is None:
                roots.append(o)
        if roots:
            return roots[0]
        return any_hit[0] if any_hit else None

    if obj is not None:
        hit = _walk_up(obj)
        if hit is not None:
            return hit

    ctx = context
    if ctx is None:
        try:
            ctx = bpy.context
        except Exception:
            ctx = None

    # Active collection: if user selected a collection in the outliner
    if ctx is not None:
        col = getattr(ctx, "collection", None)
        if col is not None:
            # Collection itself may be the import root name — scan its objects
            hit = _scan_objects(list(getattr(col, "all_objects", []) or []))
            if hit is not None:
                # Prefer variant root that belongs to this collection
                return hit
        # Selected objects (multi-select): first that resolves
        for o in list(getattr(ctx, "selected_objects", []) or []):
            hit = _walk_up(o)
            if hit is not None:
                return hit
        ao = getattr(ctx, "active_object", None)
        if ao is not None:
            hit = _walk_up(ao)
            if hit is not None:
                return hit

    for o in bpy.data.objects:
        if o.parent is None and MU_VARIANTS_KEY in o:
            return o
    for o in bpy.data.objects:
        if MU_VARIANTS_KEY in o:
            return o
    return None


def get_variant_items(scene, context):
    """Enum items callback for Options → Part Variant dropdown."""
    root = find_variant_root(
        getattr(context, "active_object", None) if context else None,
        context=context,
    )
    items = [("NONE", "(no variants)", "No ModulePartVariants on import")]
    if root is None:
        return items
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return items
    items = []
    base = data.get("base") or ""
    for v in data.get("variants") or []:
        name = v.get("name") or "?"
        if v.get("stock") or name == STOCK_VARIANT_NAME:
            label = (
                v.get("displayName")
                or data.get("baseDisplayName")
                or data.get("baseThemeName")
                or "Stock"
            )
        else:
            label = name
        if name == base:
            label = f"{label} (default)"
        items.append((name, label, f"Activate variant '{name}'"))
    return items or [("NONE", "(no variants)", "")]


def _cfg_mu_identity_certain(cfg_path, text, muname, filepath_stem=None):
    """True when this .cfg is confidently the sibling of the imported .mu."""
    stems = []
    for src in (filepath_stem, muname):
        for s in _muname_match_stems(src):
            if s and s.lower() not in stems:
                stems.append(s.lower())
    if not stems or not text:
        return False
    cfg_stem = os.path.splitext(os.path.basename(cfg_path or ""))[0].lower()
    # Exact cfg basename ↔ mu stem (e.g. fuelTank.cfg + fuelTank.mu)
    if cfg_stem and cfg_stem != "part":
        for s in stems:
            sn = s.replace(".", "_").replace("p", "_")
            bn = cfg_stem.replace(".", "_").replace("p", "_")
            if sn == bn or sn == cfg_stem or cfg_stem == s:
                return True
    # Explicit mesh= / model= reference to this .mu stem
    for s in stems:
        esc = re.escape(s)
        if re.search(rf"^\s*mesh\s*=\s*.*{esc}\b", text, re.M | re.I):
            return True
        if re.search(rf"^\s*model\s*=\s*.*{esc}", text, re.M | re.I):
            return True
        # mesh = model when file is model.mu — still "certain" only if unique
        # handled via basename / PART name below when stems include "model"
    return False


def _cfg_collection_name(cfg_path, text):
    """Preferred Outliner collection name from a confidently matched .cfg."""
    cfg_stem = os.path.splitext(os.path.basename(cfg_path or ""))[0]
    if cfg_stem and cfg_stem.lower() != "part":
        return cfg_stem
    # Generic part.cfg — use PART { name = ... }
    part_m = re.search(r"PART\s*\{", text or "", re.I)
    if not part_m:
        return None
    rest = text[part_m.end():]
    nm = re.search(r"^\s*name\s*=\s*([^\r\n]+)", rest, re.M | re.I)
    if not nm:
        return None
    name = nm.group(1).strip().strip('"').split("//")[0].strip()
    return name or None


def _rename_import_collections(root, new_base):
    """Rename main / .vis / .collider hierarchy when name differs from .mu stem."""
    if root is None or not new_base:
        return
    leaf = None
    try:
        leaf = root.users_collection[0] if root.users_collection else None
    except Exception:
        leaf = None
    if leaf is None:
        return
    main = None
    old_base = None
    leaf_name = leaf.name or ""
    for suffix in (".vis", ".collider"):
        if leaf_name.endswith(suffix):
            old_base = leaf_name[: -len(suffix)]
            main = bpy.data.collections.get(old_base)
            break
    if main is None:
        # Walk parents of leaf in the collection tree
        for col in bpy.data.collections:
            if leaf.name in [c.name for c in col.children]:
                main = col
                old_base = col.name
                break
    if main is None:
        if leaf_name != new_base and new_base not in bpy.data.collections:
            try:
                leaf.name = new_base
            except Exception:
                pass
        return
    if old_base == new_base:
        return
    target = new_base
    n = 0
    while target in bpy.data.collections and bpy.data.collections[target] != main:
        n += 1
        target = f"{new_base}.{n:03d}"
    for child in list(main.children):
        cname = child.name or ""
        if old_base and cname == old_base + ".vis":
            try:
                child.name = target + ".vis"
            except Exception:
                pass
        elif old_base and cname == old_base + ".collider":
            try:
                child.name = target + ".collider"
            except Exception:
                pass
    try:
        main.name = target
    except Exception:
        pass


def attach_cfg_viewport_markers(root, mudir, muname, filepath_stem=None):
    """Add CoM / CoP / CoL empties + apply default part variant."""
    cfg = _find_part_cfg(mudir, muname, filepath_stem=filepath_stem)
    if not cfg or root is None:
        return
    try:
        root["mu_dirname"] = mudir or os.path.dirname(cfg)
        root["mu_cfg_path"] = cfg
    except Exception:
        pass
    try:
        text = open(cfg, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return
    # When cfg↔mu identity is certain, rename Outliner collections from the
    # .mu stem (often "model") to the cfg basename / PART name.
    try:
        if _cfg_mu_identity_certain(cfg, text, muname, filepath_stem):
            col_name = _cfg_collection_name(cfg, text)
            if col_name:
                _rename_import_collections(root, col_name)
    except Exception as e:
        print(f"WARNING: cfg collection rename: {e}")
    col = root.users_collection[0] if root.users_collection else bpy.context.scene.collection
    for key, disp, size, rgba in (
        ("CoMOffset", "SPHERE", 0.12, (1.0, 0.35, 0.1, 1.0)),
        ("CoPOffset", "CIRCLE", 0.12, (0.2, 0.6, 1.0, 1.0)),
        ("CoLOffset", "CONE", 0.12, (0.3, 1.0, 0.4, 1.0)),
    ):
        m = re.search(rf"^\s*{key}\s*=\s*(.+)$", text, re.M | re.I)
        if not m:
            continue
        loc = _parse_vec3(m.group(1))
        if loc is None:
            continue
        _make_marker(
            col, root, f"{root.name}.{key}{_CFG_PREVIEW_SUFFIX}",
            loc, display=disp, size=size, color=rgba,
        )
    for o in list(bpy.data.objects):
        n = (o.name or "").lower()
        if any(k in n for k in ("thrusttransform", "fxtransform", "thrust_transform")):
            try:
                if o.type == "EMPTY":
                    o.empty_display_type = "SINGLE_ARROW"
                    o.empty_display_size = max(0.2, o.empty_display_size)
            except Exception:
                pass
    info = parse_part_variants(text)
    if info:
        root[MU_VARIANTS_KEY] = json.dumps(info)
        # Snapshot stock _MainTex names before any VARIANT TEXTURE apply.
        try:
            data = json.loads(root[MU_VARIANTS_KEY])
            _ensure_default_textures_cached(root, data)
            root[MU_VARIANTS_KEY] = json.dumps(data)
        except Exception:
            pass
        base = pick_viable_base_variant(root, info) or info.get("base")
        if not base and info.get("variants"):
            base = info["variants"][0]["name"]
        if base:
            apply_variant(root, base)
        # Sync Options enum to default variant
        try:
            scene = bpy.context.scene
            if hasattr(scene, "mu_active_variant"):
                scene.mu_active_variant = base
        except Exception:
            pass
    jettison = parse_module_jettison_names(text)
    if jettison:
        try:
            root[MU_JETTISON_KEY] = json.dumps(
                [str(n).lower() for n in jettison]
            )
        except Exception:
            pass
        # Also embed in mu_variants so GIF/PNG helpers that only read that
        # blob still see ModuleJettison covers.
        try:
            if MU_VARIANTS_KEY in root:
                data = json.loads(root[MU_VARIANTS_KEY])
                data["jettisonNames"] = [str(n).lower() for n in jettison]
                root[MU_VARIANTS_KEY] = json.dumps(data)
        except Exception:
            pass
    cc = parse_module_color_changer(text)
    if cc:
        try:
            root[MU_COLOR_CHANGER_KEY] = json.dumps(cc)
        except Exception:
            pass
        try:
            apply_color_changer_preview(root, cc)
        except Exception as e:
            print(f"WARNING: color changer preview: {e}")
    # ModuleProceduralFairing: base mesh should use FairingBase, not panel diff
    try:
        _apply_procedural_fairing_textures(root, text)
    except Exception as e:
        print(f"WARNING: fairing textures: {e}")
    # ModuleAeroSurface (airbrake): synthesize Flap deploy NLA
    try:
        _attach_aero_surface_preview(root, text)
    except Exception as e:
        print(f"WARNING: aero surface preview: {e}")
    # Damaged / .broken meshes — hidden by default (Tools → Broken to show)
    try:
        apply_broken_default_hide(root, text, hide=True)
    except Exception as e:
        print(f"WARNING: broken hide: {e}")
    # ModuleWheelSuspension travel preview (amortyzatory)
    try:
        _attach_suspension_preview(root, text)
    except Exception as e:
        print(f"WARNING: suspension preview: {e}")
    # Serenity / Breaking Ground: synthesize ServoOperate from ModuleRobotic*
    try:
        from .robotics_preview import attach_robotics_preview
        attach_robotics_preview(root, text)
    except Exception as e:
        print(f"WARNING: robotics preview: {e}")
    # ModuleLight Flare (gear / lamp additive halo) — no shadows + soft glow
    try:
        from .flare_preview import attach_flare_preview
        attach_flare_preview(root, text)
    except Exception as e:
        print(f"WARNING: flare preview: {e}")
    # FlagDecal viewport preview (flags/ + Options enum)
    # Corrupt UTF-16/null-byte modules warn once, then skip.
    global _flag_preview_import_failed
    if not _flag_preview_import_failed:
        try:
            fp_path = os.path.join(os.path.dirname(__file__), 'flag_preview.py')
            with open(fp_path, 'rb') as _fh:
                if b'\x00' in _fh.read(512):
                    raise SyntaxError('source code string cannot contain null bytes')
            from .flag_preview import attach_flag_preview
            attach_flag_preview(root, text, mudir=mudir or os.path.dirname(cfg))
        except SyntaxError as e:
            _flag_preview_import_failed = True
            print(f"WARNING: flag preview: {e} (further imports suppressed)")
        except Exception as e:
            print(f"WARNING: flag preview: {e}")


def _find_child_named(root, name):
    want = (name or "").lower()
    if not want or root is None:
        return None
    stack = [root]
    while stack:
        o = stack.pop()
        n = (o.name or "").split("\u2227")[0]
        if "." in n:
            b, s = n.rsplit(".", 1)
            if s.isdigit():
                n = b
        if n.lower() == want:
            return o
        stack.extend(list(o.children))
    return None


def _apply_procedural_fairing_textures(root, cfg_text):
    """Apply ModuleProceduralFairing DefaultBaseTextureURL / TextureURL to mats."""
    if not cfg_text or "ModuleProceduralFairing" not in cfg_text:
        return
    m = re.search(
        r"MODULE\s*\{[^}]*name\s*=\s*ModuleProceduralFairing[^}]*\}",
        cfg_text,
        re.I | re.S,
    )
    # Broader: find the MODULE block properly
    body = None
    for block in re.finditer(r"MODULE\s*\{", cfg_text, re.I):
        start = block.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        nm = re.search(r"^\s*name\s*=\s*(.+)$", body, re.M | re.I)
        if nm and nm.group(1).strip().strip('"') == "ModuleProceduralFairing":
            break
        body = None
    if not body:
        return

    def _val(key):
        mm = re.search(rf"^\s*{key}\s*=\s*(.+)$", body, re.M | re.I)
        if not mm:
            return None
        return mm.group(1).strip().strip('"').split("//")[0].strip()

    base_url = _val("DefaultBaseTextureURL")
    base_nm = _val("DefaultBaseNormalsURL")
    shell_url = _val("TextureURL")
    shell_nm = _val("TextureNormalURL")
    vdata = None
    try:
        if root is not None and MU_VARIANTS_KEY in root:
            vdata = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        vdata = None
    for mat in _iter_materials_under(root):
        mn = (mat.name or "").split(".")[0]
        if mn == "FairingBase" and base_url:
            path = _resolve_texture_url(root, base_url)
            if path:
                img = _load_variant_image(
                    path, os.path.splitext(os.path.basename(path))[0]
                )
                if img is not None:
                    _set_material_slot_image_viewport(
                        mat, "_MainTex", img, data=vdata
                    )
            if base_nm:
                path = _resolve_texture_url(root, base_nm)
                if path:
                    img = _load_variant_image(
                        path, os.path.splitext(os.path.basename(path))[0]
                    )
                    if img is not None:
                        _set_material_slot_image_viewport(
                            mat, "_BumpMap", img, data=vdata
                        )
        elif mn in ("FairingIconShell", "FairingsMat") and shell_url:
            path = _resolve_texture_url(root, shell_url)
            if path:
                img = _load_variant_image(
                    path, os.path.splitext(os.path.basename(path))[0]
                )
                if img is not None:
                    _set_material_slot_image_viewport(
                        mat, "_MainTex", img, data=vdata
                    )


def _attach_aero_surface_preview(root, cfg_text):
    """Synthesize Deploy NLA for ModuleAeroSurface (airbrake Flap)."""
    if not cfg_text or "ModuleAeroSurface" not in cfg_text:
        return 0
    import math
    from ..utils.action_compat import fcurve_new, push_action_to_nla

    made = 0
    for block in re.finditer(r"MODULE\s*\{", cfg_text, re.I):
        start = block.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        nm = re.search(r"^\s*name\s*=\s*(.+)$", body, re.M | re.I)
        if not nm or nm.group(1).strip().strip('"') != "ModuleAeroSurface":
            continue
        tname = "Flap"
        tm = re.search(r"^\s*transformName\s*=\s*(.+)$", body, re.M | re.I)
        if tm:
            tname = tm.group(1).strip().strip('"').split("//")[0].strip()
        rng = 70.0
        rm = re.search(r"^\s*ctrlSurfaceRange\s*=\s*([0-9.+-eE]+)", body, re.M | re.I)
        if rm:
            try:
                rng = float(rm.group(1))
            except Exception:
                pass
        flap = _find_child_named(root, tname)
        if flap is None:
            print(f"WARNING: aero surface: missing transform {tname!r}")
            continue
        # Already has real animation?
        ad = getattr(flap, "animation_data", None)
        if ad and ad.nla_tracks:
            has_real = False
            for t in ad.nla_tracks:
                if t.get("mu_fx_preview") or t.get("mu_robotic_preview"):
                    continue
                if t.name:
                    has_real = True
                    break
            if has_real:
                continue
        scene = bpy.context.scene
        try:
            fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
        except Exception:
            fps = 24.0
        fps = max(1.0, fps)
        f0 = float(scene.frame_start)
        # Unity/KSP aero surface typically pitches about local X
        flap.rotation_mode = "XYZ"
        rest = list(flap.rotation_euler)
        act = bpy.data.actions.new(name=f"AeroSurfaceDeploy.{flap.name}")
        try:
            act["mu_fx_preview"] = 1
        except Exception:
            pass
        fc = fcurve_new(act, flap, "rotation_euler", index=0)
        fc.keyframe_points.add(2)
        fc.keyframe_points[0].co = f0, float(rest[0])
        fc.keyframe_points[0].interpolation = "LINEAR"
        fc.keyframe_points[1].co = f0 + fps * 1.0, float(rest[0]) + math.radians(rng)
        fc.keyframe_points[1].interpolation = "LINEAR"
        track, _strip = push_action_to_nla(flap, act, "AeroSurfaceDeploy")
        made += 1
        print(f"INFO: aero surface preview -> {flap.name} (range {rng} deg)")
        # FXModuleLookAtConstraint piston pair
        for cm in re.finditer(
            r"CONSTRAINLOOKFX\s*\{([^}]*)\}", body, re.I | re.S
        ):
            pass  # LookAts are in a separate MODULE
        break
    # Separate FXModuleLookAtConstraint blocks
    for block in re.finditer(r"MODULE\s*\{", cfg_text, re.I):
        start = block.end() - 1
        depth = 0
        i = start
        while i < len(cfg_text):
            if cfg_text[i] == "{":
                depth += 1
            elif cfg_text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = cfg_text[start + 1:i]
                    break
            i += 1
        else:
            continue
        nm = re.search(r"^\s*name\s*=\s*(.+)$", body, re.M | re.I)
        if not nm or nm.group(1).strip().strip('"') != "FXModuleLookAtConstraint":
            continue
        for cm in re.finditer(
            r"CONSTRAINLOOKFX\s*\{([^}]*)\}", body, re.I | re.S
        ):
            cbody = cm.group(1)
            tgt = re.search(r"^\s*targetName\s*=\s*(.+)$", cbody, re.M | re.I)
            rot = re.search(r"^\s*rotatorsName\s*=\s*(.+)$", cbody, re.M | re.I)
            if not tgt or not rot:
                continue
            t_obj = _find_child_named(root, tgt.group(1).strip().strip('"'))
            r_obj = _find_child_named(root, rot.group(1).strip().strip('"'))
            if t_obj is None or r_obj is None:
                continue
            try:
                for c in list(r_obj.constraints):
                    if (c.name or "").startswith("AeroLookAt"):
                        r_obj.constraints.remove(c)
                # One-way only — mutual LookAts cycle the depsgraph.
                if any(
                    (c.name or "").startswith("AeroLookAt")
                    for c in t_obj.constraints
                ):
                    continue
                c = r_obj.constraints.new("DAMPED_TRACK")
                c.name = "AeroLookAt"
                c.target = t_obj
                c.track_axis = "TRACK_Y"
            except Exception as e:
                print(f"WARNING: aero lookat: {e}")
    return made
