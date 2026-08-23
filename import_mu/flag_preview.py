# vim:ts=4:et
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

"""Viewport FlagDecal preview: copy GameData flags into addon ``flags/`` and
swap ``_MainTex`` on the flagged quad (export skips via ``mu_flag_preview``).
"""

from __future__ import annotations

import json
import os
import re
import shutil

import bpy
from bpy.utils import previews

MU_FLAG_KEY = "mu_flag_decal"
MU_FLAG_PREVIEW = "mu_flag_preview"
# Viewport-only: FlagDecal quad hidden for "No Flag" (not written to .mu).
MU_FLAG_NO_FLAG_HIDDEN = "mu_flag_no_flag_hidden"
MU_FLAG_PREV_HIDE_VP = "mu_flag_prev_hide_viewport"
MU_FLAG_PREV_HIDE_RENDER = "mu_flag_prev_hide_render"
NO_FLAG = "NONE"

# bpy.utils.previews collection for Flag Decal enum thumbnails.
_flag_previews = None
# Blender keeps references to enum callback strings — cache the last items list.
_flag_enum_items_cache = []


def addon_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def flags_dir():
    return os.path.join(addon_root(), "flags")


def _ensure_flag_previews():
    """Return the PreviewCollection used for Flag Decal enum icons."""
    global _flag_previews
    if _flag_previews is None:
        _flag_previews = previews.new()
    return _flag_previews


def clear_flag_previews():
    """Remove loaded flag thumbnails (call after sync / on unregister)."""
    global _flag_previews, _flag_enum_items_cache
    if _flag_previews is not None:
        try:
            previews.remove(_flag_previews)
        except Exception:
            pass
        _flag_previews = None
    _flag_enum_items_cache = []


def register_flag_previews():
    """Create an empty preview collection (images load on demand)."""
    _ensure_flag_previews()


def unregister_flag_previews():
    clear_flag_previews()


def _flag_preview_image_path(stem, path=None):
    """Prefer PNG in flags/ over DDS/TGA for enum thumbnails."""
    dest = flags_dir()
    for ext in (".png", ".tga", ".dds"):
        candidate = os.path.join(dest, stem + ext)
        if os.path.isfile(candidate):
            return candidate
    if path and os.path.isfile(path):
        return path
    return None


def _flag_preview_icon_id(stem, path=None):
    """Load (or reuse) a small preview; return icon_id, or 0 on failure."""
    key = "flag_" + (stem or "").lower()
    if not key or key == "flag_":
        return 0
    pcoll = _ensure_flag_previews()
    if key in pcoll:
        return pcoll[key].icon_id
    img_path = _flag_preview_image_path(stem, path)
    if not img_path:
        return 0
    low = img_path.lower()
    # Prefer PNG; skip very large DDS so the enum stays responsive.
    if low.endswith(".dds"):
        try:
            if os.path.getsize(img_path) > 2 * 1024 * 1024:
                return 0
        except Exception:
            return 0
    try:
        pcoll.load(key, img_path, "IMAGE")
        return pcoll[key].icon_id
    except Exception:
        return 0


def flags_manifest_path():
    return os.path.join(flags_dir(), "manifest.json")


def _walk_gamedata_roots(mudir=None):
    roots = []
    try:
        prefs = bpy.context.preferences.addons
        for mod in prefs:
            if not mod or not getattr(mod, "preferences", None):
                continue
            gd = getattr(mod.preferences, "GameData", None)
            if gd and os.path.isdir(gd) and gd not in roots:
                roots.append(gd)
    except Exception:
        pass
    if mudir:
        cur = os.path.abspath(mudir)
        for _ in range(12):
            if os.path.basename(cur).lower() == "gamedata":
                if cur not in roots:
                    roots.append(cur)
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    test_gd = os.path.join(addon_root(), "test tools v2", "GameData")
    if os.path.isdir(test_gd) and test_gd not in roots:
        roots.append(test_gd)
    return roots


def _flag_source_dirs(mudir=None):
    """GameData/**/Flags* directories that hold agency/player flag PNGs (not part meshes)."""
    out = []
    for gd in _walk_gamedata_roots(mudir):
        if not os.path.isdir(gd):
            continue
        for root, dirs, _files in os.walk(gd):
            base = os.path.basename(root)
            low = base.lower()
            norm = root.replace("\\", "/").lower()
            # Squad/Flags, FlagsAgency, FlagsOrganization — not Parts/.../flags/
            if low.startswith("flags") and "/parts/" not in norm:
                out.append(root)
            # Prune deep part trees a bit
            if "parts" in low and low != "parts":
                dirs[:] = [d for d in dirs if not d.lower().startswith("flag")]
    # Prefer shallow Squad/Flags* first
    def rank(p):
        n = os.path.basename(p).lower()
        if n == "flags":
            return 0
        if "agency" in n:
            return 1
        if "organization" in n:
            return 2
        return 3
    return sorted(set(out), key=rank)


def sync_flags_from_gamedata(mudir=None, force=False):
    """Copy PNG/DDS from GameData Flags* into addon ``flags/``; write manifest.

    Returns number of files copied/updated.
    """
    dest = flags_dir()
    os.makedirs(dest, exist_ok=True)
    manifest = {"flags": []}
    seen = set()
    copied = 0
    for src_dir in _flag_source_dirs(mudir):
        try:
            names = os.listdir(src_dir)
        except Exception:
            continue
        for fn in names:
            low = fn.lower()
            if not low.endswith((".png", ".dds", ".tga")):
                continue
            stem = os.path.splitext(fn)[0]
            key = stem.lower()
            if key in seen:
                continue
            seen.add(key)
            src = os.path.join(src_dir, fn)
            dst_name = stem + os.path.splitext(fn)[1].lower()
            dst = os.path.join(dest, dst_name)
            try:
                need = force or (not os.path.isfile(dst)) or (
                    os.path.getmtime(src) > os.path.getmtime(dst)
                )
            except Exception:
                need = True
            if need:
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except Exception:
                    continue
            if not os.path.isfile(dst):
                continue
            manifest["flags"].append({
                "name": stem,
                "file": dst_name,
                "source": src.replace("\\", "/"),
            })
    manifest["flags"].sort(key=lambda e: e["name"].lower())
    try:
        with open(flags_manifest_path(), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass
    # Thumbnails may be stale after copy/update.
    clear_flag_previews()
    return copied


def list_flag_files():
    """Return [(stem, filepath), ...] from addon flags/ (sync if empty).

    One entry per stem; prefers PNG over TGA/DDS when several formats exist.
    """
    dest = flags_dir()
    if not os.path.isdir(dest) or not any(
        n.lower().endswith((".png", ".dds", ".tga"))
        for n in (os.listdir(dest) if os.path.isdir(dest) else [])
    ):
        sync_flags_from_gamedata()
    out = []
    if not os.path.isdir(dest):
        return out
    # rank: lower wins (PNG best for previews and viewport)
    rank = {".png": 0, ".tga": 1, ".dds": 2}
    by_stem = {}
    for fn in sorted(os.listdir(dest), key=str.lower):
        low = fn.lower()
        ext = os.path.splitext(low)[1]
        if ext not in rank:
            continue
        stem = os.path.splitext(fn)[0]
        key = stem.lower()
        path = os.path.join(dest, fn)
        prev = by_stem.get(key)
        if prev is None or rank[ext] < prev[0]:
            by_stem[key] = (rank[ext], stem, path)
    for _r, stem, path in sorted(by_stem.values(), key=lambda t: t[1].lower()):
        out.append((stem, path))
    return out


def parse_flag_decal(cfg_text):
    """Return first FlagDecal {textureQuadName} or None."""
    if not cfg_text or "FlagDecal" not in cfg_text:
        return None
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
        if not nm or nm.group(1).strip().strip('"') != "FlagDecal":
            continue
        tq = re.search(r"^\s*textureQuadName\s*=\s*(.+)$", body, re.M | re.I)
        if not tq:
            continue
        return {
            "textureQuadName": tq.group(1).strip().strip('"').split("//")[0].strip(),
        }
    return None


def _object_stem(name):
    n = (name or "").split("\u2227")[0]
    if "." in n:
        b, s = n.rsplit(".", 1)
        if s.isdigit():
            n = b
    return n


def _find_named(root, want):
    want_l = (want or "").lower()
    if not want_l or root is None:
        return None
    stack = [root]
    while stack:
        o = stack.pop()
        if _object_stem(o.name).lower() == want_l:
            return o
        stack.extend(list(o.children))
    return None


def find_flag_quad(root, texture_quad_name=None):
    """Locate FlagDecal mesh/empty under root."""
    if root is None:
        return None
    if texture_quad_name:
        hit = _find_named(root, texture_quad_name)
        if hit is not None:
            return hit
    # Heuristic: flag / flagTransform / FLAG / flagMat host
    stack = [root]
    prefer = []
    while stack:
        o = stack.pop()
        stem = _object_stem(o.name).lower()
        if stem in ("flag", "flagtransform", "texturequad", "flagdecal"):
            prefer.append(o)
        elif "flag" in stem and o.type == "MESH":
            prefer.append(o)
        stack.extend(list(o.children))
    for o in prefer:
        if o.type == "MESH":
            return o
    for o in prefer:
        for ch in o.children:
            if ch.type == "MESH":
                return ch
        return o
    # Material name heuristic
    for o in bpy.data.objects:
        if o.type != "MESH" or not o.data:
            continue
        # Must be under root
        cur = o
        under = False
        while cur:
            if cur == root:
                under = True
                break
            cur = cur.parent
        if not under:
            continue
        for mat in o.data.materials:
            if not mat:
                continue
            mn = (mat.name or "").lower()
            if "flag" in mn:
                return o
    return None


def _configure_flag_image(img, path):
    try:
        img.alpha_mode = "STRAIGHT"
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


def load_flag_image(stem_or_path):
    """Load flag image by stem (from flags/) or absolute path."""
    path = stem_or_path
    stem = os.path.splitext(os.path.basename(stem_or_path))[0]
    if not os.path.isfile(path):
        for s, p in list_flag_files():
            if s.lower() == stem.lower() or s.lower() == str(stem_or_path).lower():
                path = p
                stem = s
                break
        else:
            return None
    img_name = f"mu_flag_{stem}"[:60]
    if img_name in bpy.data.images:
        img = bpy.data.images[img_name]
        try:
            if os.path.normpath(img.filepath_from_user() or "") != os.path.normpath(path):
                img.filepath = path
                img.reload()
        except Exception:
            pass
        _configure_flag_image(img, path)
        return img
    try:
        img = bpy.data.images.load(path)
        img.name = img_name
        _configure_flag_image(img, path)
        return img
    except Exception:
        return None


def _set_maintex_image(mat, img):
    """Viewport-only: swap Image Texture node; leave mumatprop.tex for .mu export."""
    if mat is None or img is None:
        return False
    nt = getattr(mat, "node_tree", None)
    if not nt:
        return False
    node = nt.nodes.get("_MainTex")
    if node is None:
        for n in nt.nodes:
            if n.type == "TEX_IMAGE":
                node = n
                break
    if node is None:
        return False
    node.image = img
    return True


def _resolve_flag_mesh(quad):
    """Return FlagDecal MESH object from quad empty/mesh, or None."""
    if quad is None:
        return None
    if quad.type == "MESH" and quad.data:
        return quad
    for ch in quad.children:
        if ch.type == "MESH" and ch.data:
            return ch
    return None


def _flag_targets(mesh_obj):
    """Objects to hide for No Flag (mesh + empty parent when parent is Flag*)."""
    if mesh_obj is None:
        return []
    out = [mesh_obj]
    parent = mesh_obj.parent
    if parent is not None and parent.type == "EMPTY":
        stem = _object_stem(parent.name).lower()
        if "flag" in stem:
            out.append(parent)
    return out


def _set_no_flag_hidden(mesh_obj, hide):
    """Hide/show FlagDecal for No Flag preview (viewport-only, export-safe).

    Uses ``hide_viewport`` so the quad disappears in the 3D View. Does **not**
    rely on ``hide_render`` alone: ``export_mu.empty.export_collection`` skips
    ``hide_render`` roots, which would drop the flag mesh from .mu. We keep
    ``hide_render`` False while No Flag is active; ``prepare_flag_for_export``
    also forces a temporary unhide before export as a safety net.
    """
    for obj in _flag_targets(mesh_obj):
        try:
            if hide:
                if not obj.get(MU_FLAG_NO_FLAG_HIDDEN):
                    obj[MU_FLAG_PREV_HIDE_VP] = int(bool(obj.hide_viewport))
                    obj[MU_FLAG_PREV_HIDE_RENDER] = int(bool(obj.hide_render))
                    obj[MU_FLAG_NO_FLAG_HIDDEN] = 1
                obj.hide_viewport = True
                # Keep exportable: do not leave hide_render True for No Flag.
                obj.hide_render = False
            else:
                if obj.get(MU_FLAG_NO_FLAG_HIDDEN):
                    prev_vp = bool(obj.get(MU_FLAG_PREV_HIDE_VP, 0))
                    prev_r = bool(obj.get(MU_FLAG_PREV_HIDE_RENDER, 0))
                    obj.hide_viewport = prev_vp
                    obj.hide_render = prev_r
                    for key in (
                        MU_FLAG_NO_FLAG_HIDDEN,
                        MU_FLAG_PREV_HIDE_VP,
                        MU_FLAG_PREV_HIDE_RENDER,
                    ):
                        if key in obj:
                            del obj[key]
                else:
                    obj.hide_viewport = False
                    obj.hide_render = False
        except Exception:
            pass


def prepare_flag_for_export(root):
    """Temporarily unhide No Flag preview so .mu keeps the FlagDecal mesh.

    Returns a restore token for ``finish_flag_export`` (list of objects that
    were unhidden), or None.
    """
    if root is None:
        return None
    restored = []
    stack = [root]
    while stack:
        o = stack.pop()
        if o.get(MU_FLAG_NO_FLAG_HIDDEN):
            try:
                o.hide_viewport = False
                o.hide_render = False
                restored.append(o)
            except Exception:
                pass
        stack.extend(list(o.children))
    return restored or None


def finish_flag_export(restore_token):
    """Re-hide FlagDecal objects after export if No Flag was active."""
    if not restore_token:
        return
    for o in restore_token:
        try:
            if o.get(MU_FLAG_NO_FLAG_HIDDEN):
                o.hide_viewport = True
                o.hide_render = False
        except Exception:
            pass


def apply_flag_texture(root, flag_stem, info=None):
    """Swap _MainTex on FlagDecal quad (viewport only; never mumatprop.tex).

    ``flag_stem`` NONE / No Flag hides the quad in the viewport (reversible).
    Real flag stems unhide and apply the preview image.
    """
    if root is None:
        return False
    info = info or {}
    try:
        raw = root.get(MU_FLAG_KEY)
        if raw and not info:
            info = json.loads(raw)
    except Exception:
        pass
    quad_name = (info or {}).get("textureQuadName")
    quad = find_flag_quad(root, quad_name)
    if quad is None:
        return False
    mesh_obj = _resolve_flag_mesh(quad)
    if mesh_obj is None:
        return False
    if not flag_stem or flag_stem == NO_FLAG:
        _set_no_flag_hidden(mesh_obj, True)
        try:
            root["mu_active_flag"] = NO_FLAG
            root[MU_FLAG_KEY] = json.dumps({
                "textureQuadName": quad_name or _object_stem(quad.name),
                "active": NO_FLAG,
            })
        except Exception:
            pass
        return True
    # Real flag: show again, then swap viewport image (not mumatprop).
    _set_no_flag_hidden(mesh_obj, False)
    img = load_flag_image(flag_stem)
    if img is None:
        return False
    ok = False
    for mat in mesh_obj.data.materials:
        if mat is None:
            continue
        if _set_maintex_image(mat, img):
            try:
                mat[MU_FLAG_PREVIEW] = 1
            except Exception:
                pass
            ok = True
    if ok:
        try:
            mesh_obj[MU_FLAG_PREVIEW] = 1
            root["mu_active_flag"] = flag_stem
            root[MU_FLAG_KEY] = json.dumps({
                "textureQuadName": quad_name or _object_stem(quad.name),
                "active": flag_stem,
            })
        except Exception:
            pass
    return ok


def attach_flag_preview(root, cfg_text, mudir=None):
    """Parse FlagDecal, sync flags, apply default.png (or first available)."""
    info = parse_flag_decal(cfg_text)
    if not info:
        return 0
    try:
        sync_flags_from_gamedata(mudir)
    except Exception:
        pass
    try:
        root[MU_FLAG_KEY] = json.dumps(info)
    except Exception:
        pass
    default = "default"
    stems = {s.lower(): s for s, _p in list_flag_files()}
    if default not in stems:
        default = next(iter(stems.values()), None) if stems else None
    else:
        default = stems[default]
    if default:
        apply_flag_texture(root, default, info)
        try:
            scene = bpy.context.scene
            if hasattr(scene, "mu_active_flag"):
                scene.mu_active_flag = default
        except Exception:
            pass
        return 1
    return 0


def find_flag_root(obj=None, context=None):
    """Walk up / search scene for object with mu_flag_decal."""
    if obj is None and context is not None:
        obj = getattr(context, "active_object", None)
    cur = obj
    while cur is not None:
        if cur.get(MU_FLAG_KEY):
            return cur
        cur = cur.parent
    for o in bpy.data.objects:
        if o.get(MU_FLAG_KEY):
            return o
    return None


def get_flag_enum_items(self, context):
    """Enum items for Scene.mu_active_flag with flag PNG thumbnails.

    Returns 5-tuples ``(id, name, desc, icon, index)`` so Blender shows
    ``preview.icon_id`` beside each name. ``No Flag`` uses built-in ``BLANK1``.
    """
    global _flag_enum_items_cache
    # Built-in icon name (string) for No Flag; custom icon_id (int) for flags.
    items = [
        (NO_FLAG, "No Flag", "No FlagDecal / clear preview", "BLANK1", 0),
    ]
    root = find_flag_root(
        getattr(context, "active_object", None) if context else None,
        context=context,
    )
    if root is None or not root.get(MU_FLAG_KEY):
        _flag_enum_items_cache = items
        return _flag_enum_items_cache
    idx = 1
    for stem, path in list_flag_files():
        icon = _flag_preview_icon_id(stem, path)
        if not icon:
            icon = "BLANK1"
        items.append((stem, stem, f"Flag texture {stem}", icon, idx))
        idx += 1
    if len(items) == 1:
        items.append(("default", "default", "Default flag", "BLANK1", 1))
    _flag_enum_items_cache = items
    return _flag_enum_items_cache
