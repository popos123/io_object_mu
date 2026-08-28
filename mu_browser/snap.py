# vim:ts=4:et
# <pep8 compliant>
"""like KSP: node snap, attachRules filter, ghost place modal."""

from __future__ import annotations

import math

import bpy
from mathutils import Matrix, Vector

_snap_handler = None
SNAP_DIST = 0.35  # meters-ish Blender units


def prepare_part_for_editor(root):
    """Hide clutter; keep node_* visible for snapping."""
    if root is None:
        return
    stack = [root] + list(getattr(root, "children_recursive", []) or [])
    for obj in stack:
        try:
            name = (obj.name or "").lower()
        except Exception:
            continue
        hide = False
        if "fairing" in name or "shroud" in name:
            hide = True
        try:
            from ..utils.utils import is_hideable_collider_name
            if is_hideable_collider_name(obj.name or ""):
                hide = True
        except Exception:
            if ".collider" in name or (
                "collider" in name and name.split("\u2227", 1)[0].strip() != "collider"
            ):
                hide = True
        if "cfg_preview" in name or "fx_preview" in name:
            hide = True
        # Keep attach nodes visible in like-KSP mode
        if name.startswith("node_"):
            try:
                obj.hide_set(False)
                obj.hide_viewport = False
                obj.empty_display_type = "SINGLE_ARROW"
            except Exception:
                pass
            continue
        if hide:
            try:
                obj.hide_set(True)
                obj.hide_render = True
            except Exception:
                pass


def _part_root_of(obj):
    cur = obj
    while cur is not None:
        try:
            if "ksp_part_name" in cur.keys():
                return cur
        except Exception:
            pass
        # Heuristic: top empty of an imported .mu often has children node_*
        try:
            cur = cur.parent
        except Exception:
            break
    return obj


def _attach_rules(obj):
    root = _part_root_of(obj)
    raw = ""
    try:
        raw = str(root.get("ksp_attach_rules", "") or "")
    except Exception:
        raw = ""
    vals = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            vals.append(int(p))
        except Exception:
            vals.append(0)
    # KSP: stack, srfAttach, allowStack, allowSrfAttach, allowCollision
    while len(vals) < 5:
        vals.append(1)
    return vals


def _nodes_under(root):
    out = []
    if root is None:
        return out
    stack = [root] + list(getattr(root, "children_recursive", []) or [])
    for o in stack:
        try:
            if (o.name or "").startswith("node_"):
                out.append(o)
        except Exception:
            continue
    return out


def _node_kind(name: str) -> str:
    n = (name or "").lower()
    if n.startswith("node_stack"):
        return "stack"
    if n.startswith("node_attach"):
        return "attach"
    return "other"


def _rules_allow(src_root, src_node, dst_root, dst_node) -> bool:
    """Rough attachRules gate (stack vs surface)."""
    sk = _node_kind(src_node.name)
    dk = _node_kind(dst_node.name)
    sr = _attach_rules(src_root)
    dr = _attach_rules(dst_root)
    # indices: 0 stackAllowedOnParent?, 1 srfAttach, 2 allowStack, 3 allowSrfAttach
    if sk == "stack" and dk == "stack":
        return bool(sr[2]) and bool(dr[2] if len(dr) > 2 else 1)
    if sk == "attach" or dk == "attach":
        return bool(sr[3] if len(sr) > 3 else 1) and bool(dr[1] if len(dr) > 1 else 1)
    return True


def _snap_active_to_nearest(context):
    br = getattr(context.window_manager, "ksp_mu_browser", None)
    if br is None or not br.like_ksp:
        return False
    ao = context.view_layer.objects.active
    if ao is None:
        return False
    moving = _part_root_of(ao)
    src_nodes = _nodes_under(moving)
    if not src_nodes:
        return False

    # Candidate targets: other part roots in scene
    others = []
    for obj in context.scene.objects:
        try:
            if obj == moving or obj.parent is not None:
                # only consider roots tagged as parts, or empties with node children
                pass
        except Exception:
            continue
        try:
            if "ksp_part_name" in obj.keys() and obj != moving:
                others.append(obj)
        except Exception:
            continue

    best = None  # (dist, src_node, dst_node, dst_root)
    for dst in others:
        for dn in _nodes_under(dst):
            dw = dn.matrix_world.translation
            for sn in src_nodes:
                if not _rules_allow(moving, sn, dst, dn):
                    continue
                sw = sn.matrix_world.translation
                dist = (sw - dw).length
                if dist > SNAP_DIST:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, sn, dn, dst)
    if best is None:
        return False
    _, sn, dn, dst = best
    # Align moving so src node lands on dst node, facing opposite
    try:
        # World matrices
        sn_mw = sn.matrix_world.copy()
        dn_mw = dn.matrix_world.copy()
        # Desired: sn world == dn world with 180 deg around local X of node (stack)
        target = dn_mw.copy()
        # Flip stack nodes so arrows oppose
        flip = Matrix.Rotation(math.pi, 4, "X")
        target = target @ flip
        # moving * sn_local = target => moving = target * inv(sn_local)
        sn_local = moving.matrix_world.inverted() @ sn_mw
        new_mw = target @ sn_local.inverted()
        moving.matrix_world = new_mw
        return True
    except Exception:
        return False


def _on_depsgraph(scene, depsgraph=None):
    try:
        ctx = bpy.context
        br = getattr(ctx.window_manager, "ksp_mu_browser", None)
        if br is None or not br.like_ksp:
            return
        if getattr(ctx, "mode", "") != "OBJECT":
            return
        # Only while transforming roughly: if active moved
        _snap_active_to_nearest(ctx)
    except Exception:
        pass


def ensure_snap_handler():
    global _snap_handler
    if _snap_handler is not None:
        return
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    _snap_handler = _on_depsgraph


def remove_snap_handler():
    global _snap_handler
    try:
        if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    except Exception:
        pass
    _snap_handler = None


class KSPMU_OT_LikeKspPlace(bpy.types.Operator):
    """Ghost-follow cursor until LMB; then snap if near a node."""
    bl_idname = "object.ksp_mu_like_ksp_place"
    bl_label = "Place Part (like KSP)"
    bl_description = "Place the imported part under the cursor (LMB snap, RMB/Esc cancel)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def description(cls, context, properties):
        return cls.bl_description

    def modal(self, context, event):
        root = context.view_layer.objects.active
        if root is None:
            return {"CANCELLED"}
        if event.type in {"RIGHTMOUSE", "ESC"}:
            return {"CANCELLED"}
        if event.type == "MOUSEMOVE":
            # Project mouse to view
            try:
                region = context.region
                rv3d = context.region_data
                from bpy_extras import view3d_utils
                coord = (event.mouse_region_x, event.mouse_region_y)
                view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
                loc = view3d_utils.region_2d_to_location_3d(region, rv3d, coord, view_vector)
                root.location = loc
                # Ghost: semi-transparent
                for o in [root] + list(getattr(root, "children_recursive", []) or []):
                    try:
                        o.display_type = "WIRE"
                    except Exception:
                        pass
                _snap_active_to_nearest(context)
            except Exception:
                pass
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            for o in [root] + list(getattr(root, "children_recursive", []) or []):
                try:
                    o.display_type = "TEXTURED"
                except Exception:
                    pass
            _snap_active_to_nearest(context)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def invoke(self, context, event):
        br = context.window_manager.ksp_mu_browser
        if not br.like_ksp:
            return {"CANCELLED"}
        ensure_snap_handler()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}


classes_to_register = (
    KSPMU_OT_LikeKspPlace,
)
