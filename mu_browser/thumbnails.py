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
from typing import Dict, Optional

import bpy
from bpy.utils import previews


_CACHE_CLEARED = True

_pcoll = None
_ICON_SIZE = 128
_THUMB_VER = "v2"
_DEFAULT_THUMB_NAME = "_default_thumbnail.png"
_DEFAULT_PREVIEW_KEY = "__MU_DEFAULT_THUMB__"

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


def find_ksp_thumb(part_name: str) -> Optional[str]:
    if not part_name:
        return None
    if not _ksp_thumbs_built:
        build_ksp_thumbs_index()
    for key in _part_name_keys(part_name):
        path = _ksp_thumbs.get(key)
        if (
            path
            and os.path.isfile(path)
            and os.path.getsize(path) > 64
        ):
            return path
    return None


def _resolve_cache_path(mu_path: str, part_name: str = "") -> Optional[str]:
    ksp = find_ksp_thumb(part_name)
    if ksp:
        return ksp
    path = _cache_path(
        mu_path,
        part_name,
    )
    if (
        os.path.isfile(path)
        and os.path.getsize(path) > 64
    ):
        return path
    return None


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
        print(
            "[mu_thumb] particle operator: "
            "no FX objects detected"
        )
        return []

    print(
        "[mu_thumb] particle operator target:",
        len(fx_objects),
        [o.name for o in fx_objects],
    )

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

        print(
            "[mu_thumb] calling "
            "object.mu_toggle_particles_preview "
            "scope=ALL force=SHOW"
        )

        try:
            result = bpy.ops.object.mu_toggle_particles_preview(
                scope="ALL",
                force="SHOW",
            )

            print(
                "[mu_thumb] particle operator result:",
                result,
            )

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

    print(
        "[mu_thumb] FX after operator SHOW:",
        len(shown),
        shown,
    )

    print(
        "[mu_thumb] FX hidden after operator:",
        len(hidden),
        hidden,
    )
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


def _hide_for_thumbnail(root, keep_shroud=False):
    stack = [
        root
    ] + list(
        getattr(
            root,
            "children_recursive",
            [],
        ) or []
    )
    for obj in stack:
        if _is_fx_preview_obj(obj):
            continue
        try:
            name = (
                obj.name or ""
            ).lower()
        except Exception:
            continue
        hide = False
        if name.startswith("node_"):
            hide = True
        if "fairing" in name:
            hide = True
        if "shroud" in name and not keep_shroud:
            hide = True
        try:
            from ..utils.utils import (
                is_hideable_collider_name,
            )

            if is_hideable_collider_name(
                obj.name or ""
            ):
                hide = True
        except Exception:

            if ".collider" in name:
                hide = True

            elif (
                "collider" in name
                and name.split(
                    "\u2227",
                    1,
                )[0].strip()
                != "collider"
            ):
                hide = True

        if "cfg_preview" in name:
            hide = True

        if "fx_preview" in name:
            hide = True

        if hide:

            try:
                obj.hide_set(True)
            except Exception:
                pass

            try:
                obj.hide_render = True
            except Exception:
                pass


def _autozoom_fx_camera(cam_obj, cam_data, fx_objects, margin=1.25):
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


def generate_thumbnail(mu_path: str, part_name: str = "", *, force: bool = False) -> Optional[str]:
    print(
        "[mu_thumb] generate_thumbnail:",
        os.path.basename(mu_path),
        "part=",
        part_name or "?",
    )

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

        existing = _resolve_cache_path(
            mu_path,
            part_name,
        )

        if existing:

            print(
                "[mu_thumb] cache hit:",
                os.path.basename(existing),
            )

            return existing

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
            print("[mu_thumb] FX thumbnail mode enabled")
        else:
            print("[mu_thumb] no FX - normal thumbnail mode")
            keep_shroud = False
            try:
                keep_shroud = "shroud" in (part_name or "").lower()
            except Exception:
                pass
            _hide_for_thumbnail(root, keep_shroud=keep_shroud)

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
            if _setup_fx_camera(
                scene,
                root,
            ):
                print(
                    "[mu_thumb] FX camera setup OK"
                )
                if is_fx:
                    _autozoom_fx_camera(
                        cam_obj,
                        cam_data,
                        fx_objects,
                        margin=1.0,
                    )
            else:
                print(
                    "[mu_thumb] FX camera setup FAILED"
                )
        else:
            coords = []
            for o in objs:
                try:
                    if o.hide_get():
                        continue
                except Exception:
                    pass
                try:
                    for corner in o.bound_box:
                        coords.append(
                            o.matrix_world
                            @ Vector(corner)
                        )
                except Exception as e:
                    print(
                        "[mu_thumb] bound_box skip %s: %s"
                        % (
                            getattr(
                                o,
                                "name",
                                "?",
                            ),
                            e,
                        )
                    )
            if not coords:
                print(
                    "[mu_thumb] FAIL: "
                    "no bound_box coords"
                )
                return None
            mn = Vector(
                (
                    min(v.x for v in coords),
                    min(v.y for v in coords),
                    min(v.z for v in coords),
                )
            )
            mx = Vector(
                (
                    max(v.x for v in coords),
                    max(v.y for v in coords),
                    max(v.z for v in coords),
                )
            )
            center = (
                mn + mx
            ) * 0.5
            size = max((mx - mn).length, 0.05)
            print(
                "[mu_thumb] bbox center=%s size=%.4f"
                % (
                    tuple(center),
                    size,
                )
            )
            cam_obj.location = (
                center.x + size * 0.9,
                center.y - size * 1.1,
                center.z + size * 0.7,
            )
            direction = (
                center
                - cam_obj.location
            )

            cam_obj.rotation_euler = (
                direction
                .to_track_quat(
                    "-Z",
                    "Y",
                )
                .to_euler()
            )

            cam_data.ortho_scale = (size * 1.35)
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
        print(
            "[mu_thumb] lights+world set"
        )
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
            print(
                "[mu_thumb] FX hidden immediately "
                "before render:",
                hidden_before_render,
            )
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
        rendered = False
        try:
            bpy.ops.render.render(
                write_still=True
            )
            rendered = True
            print(
                "[mu_thumb] render.render OK"
            )
        except Exception as e1:
            print(
                "[mu_thumb] render.render failed:",
                type(e1).__name__,
                e1,
            )
            return None
        if (
            os.path.isfile(out)
            and os.path.getsize(out) > 64
        ):
            print(
                "[mu_thumb] OK wrote",
                os.path.basename(out),
                "size=",
                os.path.getsize(out),
            )
            return out
        print(
            "[mu_thumb] FAIL: "
            "no valid output file after render "
            "(exists=%s size=%s rendered=%s)"
            % (
                os.path.isfile(out),
                os.path.getsize(out)
                if os.path.isfile(out)
                else 0,
                rendered,
            )
        )
        return None

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