# vim:ts=4:et
# <pep8 compliant>
"""Lazy OpenGL thumbnails for MU browser parts (disk-cached)."""

from __future__ import annotations

import hashlib
import os
from typing import Optional

import bpy
from bpy.utils import previews

import shutil

_CACHE_CLEARED = True # temporary turn off cache clearing (for future auto import)

_pcoll = None
_ICON_SIZE = 128
_THUMB_VER = "v2"  # bump when lighting/framing changes so old black PNGs are ignored
_DEFAULT_THUMB_NAME = "_default_thumbnail.png"
_DEFAULT_PREVIEW_KEY = "__MU_DEFAULT_THUMB__"

def _cache_dir() -> str:
    global _CACHE_CLEARED
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(addon_dir, "cache", "thumbnails")
    if not _CACHE_CLEARED:
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
        os.makedirs(cache_dir, exist_ok=True)
        _CACHE_CLEARED = True
    return cache_dir


def _cache_path(mu_path: str) -> str:
    key = hashlib.sha1(mu_path.encode("utf-8", errors="replace")).hexdigest()[:16]
    base = os.path.splitext(os.path.basename(mu_path))[0]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)[:40]
    return os.path.join(_cache_dir(), "%s_%s_%s.png" % (safe, key, _THUMB_VER))


def ensure_previews():
    global _pcoll
    if _pcoll is None:
        _pcoll = previews.new()
    return _pcoll


def clear_previews():
    global _pcoll
    if _pcoll is not None:
        try:
            previews.remove(_pcoll)
        except Exception:
            pass
        _pcoll = None


def _hide_for_thumbnail(root):
    stack = [root] + list(getattr(root, "children_recursive", []) or [])
    for obj in stack:
        try:
            name = (obj.name or "").lower()
        except Exception:
            continue
        hide = False
        if name.startswith("node_"):
            hide = True
        if "fairing" in name or "shroud" in name:
            hide = True
        try:
            from ..utils.utils import is_hideable_collider_name
            if is_hideable_collider_name(obj.name or ""):
                hide = True
        except Exception:
            if ".collider" in name or (
                "collider" in name
                and name.split("\u2227", 1)[0].strip() != "collider"
            ):
                hide = True
        if "cfg_preview" in name or "fx_preview" in name:
            hide = True
        if hide:
            try:
                obj.hide_set(True)
                obj.hide_render = True
            except Exception:
                pass


def _add_sun(col, name, location, target, energy, color=(1.0, 1.0, 1.0)):
    from mathutils import Vector
    data = bpy.data.lights.new(name, "SUN")
    data.energy = energy
    data.color = color
    try:
        data.angle = 0.15
    except Exception:
        pass
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    direction = Vector(target) - Vector(location)
    if direction.length > 1e-8:
        obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    col.objects.link(obj)
    return obj


def _setup_world(scene):
    world = bpy.data.worlds.new("_mu_thumb_world")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = None
    for n in nt.nodes:
        if n.type == "BACKGROUND":
            bg = n
            break
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
        out = None
        for n in nt.nodes:
            if n.type == "OUTPUT_WORLD":
                out = n
                break
        if out is not None:
            nt.links.new(bg.outputs[0], out.inputs[0])
    bg.inputs[0].default_value = (0.75, 0.80, 0.88, 1.0)
    bg.inputs[1].default_value = 0.8
    return world


def generate_thumbnail(mu_path: str, *, force: bool = False) -> Optional[str]:
    """Render a small OpenGL preview PNG; return path or None."""
    print("[mu_thumb] generate_thumbnail:", os.path.basename(mu_path))
    if not mu_path or not os.path.isfile(mu_path):
        print("[mu_thumb] FAIL: path missing or not a file")
        return None
    out = _cache_path(mu_path)
    print("[mu_thumb] cache:", os.path.basename(out))
    if (not force) and os.path.isfile(out) and os.path.getsize(out) > 64:
        print("[mu_thumb] cache hit, size=", os.path.getsize(out))
        return out

    from ..import_mu import import_mu
    from mathutils import Vector

    # Snapshot bpy.data before import so we can purge everything this
    # thumbnail run created (objects, mats, images, …). Without this,
    # sequential thumbs pollute global images/materials by name and the
    # next browser import / thumb gets the wrong textures and variants.
    before = {
        "objects": set(bpy.data.objects.keys()),
        "meshes": set(bpy.data.meshes.keys()),
        "materials": set(bpy.data.materials.keys()),
        "images": set(bpy.data.images.keys()),
        "armatures": set(bpy.data.armatures.keys()),
        "actions": set(bpy.data.actions.keys()),
        "collections": set(bpy.data.collections.keys()),
        "cameras": set(bpy.data.cameras.keys()),
        "lights": set(bpy.data.lights.keys()),
        "worlds": set(bpy.data.worlds.keys()),
        "node_groups": set(bpy.data.node_groups.keys()),
    }

    def _purge_new():
        """Remove data-blocks created since the snapshot (import + thumb helpers)."""
        # Objects first so data-blocks lose users
        for name in list(bpy.data.objects.keys()):
            if name not in before["objects"]:
                o = bpy.data.objects.get(name)
                if o is not None:
                    try:
                        bpy.data.objects.remove(o, do_unlink=True)
                    except Exception:
                        pass
        for name in list(bpy.data.collections.keys()):
            if name not in before["collections"]:
                c = bpy.data.collections.get(name)
                if c is not None:
                    try:
                        bpy.data.collections.remove(c)
                    except Exception:
                        pass
        for key, coll in (
            ("meshes", bpy.data.meshes),
            ("materials", bpy.data.materials),
            ("images", bpy.data.images),
            ("armatures", bpy.data.armatures),
            ("actions", bpy.data.actions),
            ("cameras", bpy.data.cameras),
            ("lights", bpy.data.lights),
            ("worlds", bpy.data.worlds),
            ("node_groups", bpy.data.node_groups),
        ):
            for name in list(coll.keys()):
                if name not in before[key]:
                    block = coll.get(name)
                    if block is not None:
                        try:
                            coll.remove(block)
                        except Exception:
                            pass

    scene = bpy.data.scenes.new("_mu_thumb_tmp")
    world = None
    try:
        col = bpy.data.collections.new("_mu_thumb_col")
        scene.collection.children.link(col)
        try:
            ret = import_mu(col, mu_path, False, False)
            root = ret[0] if isinstance(ret, (tuple, list)) else ret
            print("[mu_thumb] import_mu returned type=%s root=%s" % (type(ret).__name__, root))
        except Exception as e:
            print("[mu_thumb] FAIL import_mu:", type(e).__name__, e)
            import traceback
            traceback.print_exc()
            return None
        if root is None:
            print("[mu_thumb] FAIL: import returned None")
            return None
        _hide_for_thumbnail(root)

        cam_data = bpy.data.cameras.new("_mu_thumb_cam")
        cam_data.type = "ORTHO"
        cam_obj = bpy.data.objects.new("_mu_thumb_cam", cam_data)
        col.objects.link(cam_obj)
        scene.camera = cam_obj

        try:
            for vl in scene.view_layers:
                vl.update()
        except Exception:
            pass
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        objs = [root] + list(getattr(root, "children_recursive", []) or [])
        print("[mu_thumb] objects under root:", len(objs), [getattr(o, "name", "?") for o in objs[:8]])
        coords = []
        for o in objs:
            try:
                if o.hide_get():
                    continue
            except Exception:
                pass
            try:
                for corner in o.bound_box:
                    coords.append(o.matrix_world @ Vector(corner))
            except Exception as e:
                print("[mu_thumb] bound_box skip %s: %s" % (getattr(o, "name", "?"), e))
                continue
        if not coords:
            print("[mu_thumb] FAIL: no bound_box coords (all hidden or empty?)")
            return None
        mn = Vector((min(v.x for v in coords), min(v.y for v in coords), min(v.z for v in coords)))
        mx = Vector((max(v.x for v in coords), max(v.y for v in coords), max(v.z for v in coords)))
        center = (mn + mx) * 0.5
        size = max((mx - mn).length, 0.05)
        print("[mu_thumb] bbox center=%s size=%.4f" % (tuple(center), size))
        cam_obj.location = (
            center.x + size * 0.9,
            center.y - size * 1.1,
            center.z + size * 0.7,
        )
        direction = center - cam_obj.location
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        cam_data.ortho_scale = size * 1.35

        world = _setup_world(scene)
        key_loc = cam_obj.location.copy()
        _add_sun(col, "_mu_thumb_key", key_loc, center, energy=2.0, color=(1.0, 0.98, 0.94))
        fill_loc = Vector((
            center.x - size * 1.1,
            center.y + size * 0.8,
            center.z + size * 0.4,
        ))
        _add_sun(col, "_mu_thumb_fill", fill_loc, center, energy=0.8, color=(0.75, 0.82, 1.0))
        rim_loc = Vector((
            center.x - size * 0.4,
            center.y + size * 1.2,
            center.z + size * 1.0,
        ))
        _add_sun(col, "_mu_thumb_rim", rim_loc, center, energy=1.0, color=(1.0, 1.0, 1.0))
        print("[mu_thumb] lights+world set")

        scene.render.resolution_x = _ICON_SIZE
        scene.render.resolution_y = _ICON_SIZE
        scene.render.filepath = out
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = True
        try:
            scene.render.engine = "BLENDER_EEVEE_NEXT"
        except Exception:
            try:
                scene.render.engine = "BLENDER_EEVEE"
            except Exception:
                pass
        try:
            scene.view_settings.view_transform = "Standard"
            scene.view_settings.look = "None"
            scene.view_settings.exposure = 0.2
        except Exception:
            pass

        rendered = False
        try:
            with bpy.context.temp_override(scene=scene):
                bpy.ops.render.render(write_still=True)
            rendered = True
            print("[mu_thumb] render.render OK (temp_override)")
        except Exception as e1:
            print("[mu_thumb] render.render failed:", type(e1).__name__, e1)
            try:
                bpy.ops.render.render(write_still=True, scene=scene.name)
                rendered = True
                print("[mu_thumb] render.render OK (scene=)")
            except Exception as e2:
                print("[mu_thumb] render.render(scene=) failed:", type(e2).__name__, e2)
                try:
                    bpy.ops.render.opengl(write_still=True)
                    rendered = True
                    print("[mu_thumb] render.opengl OK")
                except Exception as e3:
                    print("[mu_thumb] FAIL all render paths:", type(e3).__name__, e3)
                    import traceback
                    traceback.print_exc()
                    return None
        if os.path.isfile(out) and os.path.getsize(out) > 64:
            print("[mu_thumb] OK wrote", os.path.basename(out), "size=", os.path.getsize(out))
            return out
        print(
            "[mu_thumb] FAIL: no valid output file after render "
            "(exists=%s size=%s rendered=%s)"
            % (
                os.path.isfile(out),
                os.path.getsize(out) if os.path.isfile(out) else 0,
                rendered,
            )
        )
        return None
    finally:
        # Always purge import leftovers + thumb helpers so the next thumb
        # and browser import do not reuse wrong textures / variant mats.
        try:
            _purge_new()
        except Exception as e:
            print("[mu_thumb] purge warning:", type(e).__name__, e)
        try:
            if scene.name in bpy.data.scenes:
                bpy.data.scenes.remove(scene, do_unlink=True)
        except Exception:
            pass


def _default_thumb_path() -> str:
    return os.path.join(_cache_dir(), _DEFAULT_THUMB_NAME)


def _ensure_default_thumbnail() -> str:
    path = _default_thumb_path()

    if os.path.isfile(path) and os.path.getsize(path) > 64:
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
            # szare tło
            pixels.extend((0.30, 0.30, 0.30, 1.0))

    img.pixels = pixels
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()

    bpy.data.images.remove(img)

    return path


def icon_id_for_part(mu_path: str, *, ensure: bool = True) -> int:
    """Return preview icon_id. Use default thumbnail when no generated preview exists."""

    pcoll = ensure_previews()

    key = hashlib.sha1(
        mu_path.encode("utf-8", errors="replace")
    ).hexdigest()[:20]

    # Already loaded preview for this MU.
    if key in pcoll:
        return pcoll[key].icon_id

    path = _cache_path(mu_path)

    # Existing generated thumbnail.
    if not (
        os.path.isfile(path)
        and os.path.getsize(path) >= 64
    ):
        path = None

    # Generate only when explicitly requested.
    if path is None and ensure:
        path = generate_thumbnail(mu_path)

    # No generated thumbnail -> default.
    if not path or not os.path.isfile(path):
        default_path = _ensure_default_thumbnail()

        try:
            if _DEFAULT_PREVIEW_KEY not in pcoll:
                pcoll.load(
                    _DEFAULT_PREVIEW_KEY,
                    default_path,
                    "IMAGE",
                )

            return pcoll[_DEFAULT_PREVIEW_KEY].icon_id

        except Exception:
            return 0

    # Load actual MU thumbnail.
    try:
        pcoll.load(key, path, "IMAGE")
        return pcoll[key].icon_id
    except Exception:
        return 0