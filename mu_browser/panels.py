# vim:ts=4:et
# <pep8 compliant>

import os
import bpy
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    CollectionProperty,
)
from bpy.types import PropertyGroup, WindowManager

from . import thumbnails
from ..utils.action_compat import (
    iter_action_fcurves,
    apply_mu_clip_wrap_to_action,
    apply_mu_curve_wrap,
    get_mu_curve_wrap,
    mu_wrap_name,
)

_THUMB_SCALE = 7.5 # Main thumb (7.5X20px)
_THUMB_SCALE_POPUP = 5.0 # Menu thumbs (5x20px)

# Module-level collapse state (like shader property sets)
_anim_section_expanded = True
# UIList default / hard cap (same idea as import_ksp panels)
_ANIM_LIST_DEFAULT_ROWS = 2
_ANIM_LIST_MAX_ROWS = 10


def _mu_int(val, default=0):
    try:
        return int(val)
    except Exception:
        return default


def _normalize_wrap(mode):
    """Unity Default(0) → Once(1) for button highlight."""
    mode = _mu_int(mode, 1)
    return 1 if mode == 0 else mode


def _find_nla_owner(action):
    """Return Blender object name that owns this Action on an NLA track."""
    if action is None:
        return ""
    try:
        stored = action.get("mu_nla_owner")
        if stored:
            return str(stored)
    except Exception:
        pass
    try:
        stored = action.get("mu_anim_bobj")
        if stored and stored in bpy.data.objects:
            return str(stored)
    except Exception:
        pass
    # Walk scene — prefer exact Action pointer match on NLA strips
    for obj in bpy.data.objects:
        ad = getattr(obj, "animation_data", None)
        if not ad:
            continue
        for track in ad.nla_tracks:
            for strip in track.strips:
                if strip.action == action:
                    return obj.name
    return ""


def _mu_animation_entries(context):
    """List MU clips around selection in NLA visual order (top track first).

    Each entry: dict(action, clip_name, owner_name, wrap, autoplay)
    """
    obj = context.object
    if obj is None:
        return []
    roots = []
    current = obj
    while current is not None:
        if current not in roots:
            roots.append(current)
        current = current.parent
    for child in getattr(obj, "children_recursive", []):
        if child not in roots:
            roots.append(child)

    entries = []
    seen = set()

    def _add_from_id(id_data):
        ad = getattr(id_data, "animation_data", None)
        if not ad:
            return
        # NLA editor shows last track at the top — reverse for UI list
        for track in reversed(list(ad.nla_tracks)):
            for strip in track.strips:
                action = strip.action
                if action is None:
                    continue
                ptr = action.as_pointer()
                if ptr in seen:
                    continue
                has_mu = False
                try:
                    has_mu = (
                        "mu_clip_name" in action
                        or "mu_clip_wrap_mode" in action
                        or "mu_anim_host" in action
                        or "mu_nla_owner" in action
                    )
                except Exception:
                    has_mu = False
                # Also accept plain NLA under MU hierarchy (props may have failed)
                if not has_mu and not track.name:
                    continue
                if not has_mu:
                    # Require at least a track name that looks like a clip
                    if not track.name:
                        continue
                seen.add(ptr)
                try:
                    clip_name = action.get("mu_clip_name") or track.name or action.name
                except Exception:
                    clip_name = track.name or action.name
                owner = _find_nla_owner(action) or getattr(id_data, "name", "")
                try:
                    wrap = _normalize_wrap(action.get("mu_clip_wrap_mode", 1))
                except Exception:
                    wrap = 1
                try:
                    autoplay = bool(_mu_int(action.get("mu_auto_play", 0), 0))
                except Exception:
                    autoplay = False
                entries.append({
                    "action": action,
                    "clip_name": str(clip_name),
                    "owner": str(owner),
                    "wrap": wrap,
                    "autoplay": autoplay,
                })

    for root in roots:
        _add_from_id(root)
        for slot in getattr(root, "material_slots", []) or []:
            mat = getattr(slot, "material", None)
            if mat:
                _add_from_id(mat)
    return entries


def _wrap_short(mode):
    """Compact wrap label for the UIList status column."""
    try:
        mode = int(mode)
    except Exception:
        mode = 1
    return {0: "?", 1: "Once", 2: "Loop", 4: "PP", 8: "Clamp"}.get(mode, "?")


def _host_key_of_action(action, owner=""):
    """Stable host id for grouping clips under one Animation component."""
    if action is not None:
        try:
            for prop in ("mu_anim_bobj", "mu_anim_host", "mu_nla_owner"):
                v = action.get(prop) if hasattr(action, "get") else None
                if not v and prop in action:
                    v = action[prop]
                if v:
                    return str(v)
        except Exception:
            pass
    return str(owner or "") or "_default"


def _curve_wrap_of_action(action):
    """Dominant curve wrap mode (0 = mixed/unknown)."""
    if action is None:
        return 8
    try:
        wraps = []
        for fc in iter_action_fcurves(action):
            try:
                pre, post = get_mu_curve_wrap(action, fc)
                wraps.append((int(pre), int(post)))
            except Exception:
                pass
        if wraps and len(set(wraps)) == 1 and wraps[0][0] == wraps[0][1]:
            return int(wraps[0][0])
        if wraps:
            return 0
    except Exception:
        pass
    return 8


def _group_entries_by_host(entries):
    """Ordered list of (host_key, display_name, owner, autoplay, [clip dicts])."""
    from collections import OrderedDict
    groups = OrderedDict()
    for ent in entries:
        act = ent.get("action")
        owner = ent.get("owner") or ""
        hk = _host_key_of_action(act, owner)
        if hk not in groups:
            # Display: prefer bobj/owner name; show path host in paren if different
            disp = owner or hk
            try:
                if act is not None:
                    bobj = act.get("mu_anim_bobj") or ""
                    path = act.get("mu_anim_host") or ""
                    if bobj:
                        disp = str(bobj)
                        if path and str(path) != str(bobj) and "/" in str(path):
                            # keep short — owner empty name is enough
                            pass
                    elif path:
                        disp = str(path).rstrip("/").split("/")[-1] or str(path)
            except Exception:
                pass
            ap = bool(ent.get("autoplay"))
            groups[hk] = {
                "host_key": hk,
                "name": str(disp),
                "owner": str(owner),
                "autoplay": ap,
                "clips": [],
            }
        g = groups[hk]
        if ent.get("autoplay"):
            g["autoplay"] = True
        g["clips"].append(ent)
    return list(groups.values())


class KSPMU_PG_AnimRowItem(PropertyGroup):
    """Flat tree row: host header or clip child (KSPedia-style)."""
    kind: StringProperty(name="Kind", default="clip")  # "host" | "clip"
    host_key: StringProperty(name="Host Key", default="")
    name: StringProperty(name="Name", default="")
    owner: StringProperty(name="Owner", default="")
    action_name: StringProperty(name="Action", default="")
    wrap: IntProperty(name="Wrap", default=1)
    curve_wrap: IntProperty(name="Curve Wrap", default=8)
    autoplay: BoolProperty(name="Auto Play", default=False)
    expanded: BoolProperty(name="Expanded", default=True)
    depth: IntProperty(name="Depth", default=0)


class KSPMU_PG_AnimClipsHost(PropertyGroup):
    """Host for the scrollable MU animation tree (on WindowManager)."""
    rows: CollectionProperty(type=KSPMU_PG_AnimRowItem)
    rows_index: IntProperty(name="MU Anim Row", default=0)


class KSPMU_UL_AnimClipList(bpy.types.UIList):
    """Collapsible host → clips tree (scrollable, drag edge to resize)."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon='ACTION')
            return
        row = layout.row(align=True)
        # Indent clip rows
        if int(item.depth) > 0:
            pad = row.row(align=True)
            pad.ui_units_x = 0.8
            pad.label(text="")
        if item.kind == "host":
            icon_tri = 'TRIA_DOWN' if item.expanded else 'TRIA_RIGHT'
            tri = row.row(align=True)
            tri.scale_x = 0.85
            tri.prop(item, "expanded", text="", icon=icon_tri, emboss=False)
            # Host IS the empty / Animation GO — name only (no redundant paren)
            title = item.name or item.host_key or "Host"
            row.label(text=title, icon='OUTLINER_OB_EMPTY')
            # Auto Play — label button, depressed when ON, slightly shorter
            ap = row.row(align=True)
            ap.alignment = 'RIGHT'
            ap.ui_units_x = 4.0
            # Lower Button
            ap.scale_y = 1
            op = ap.operator(
                "object.ksp_mu_set_animation_autoplay",
                text="Auto Play",
                depress=bool(item.autoplay)
            )
            op.host_key = item.host_key
            op.action_name = item.action_name
            op.value = not bool(item.autoplay)
        else:
            # Clip: name (owner empty) so sibling antennas stay distinguishable
            title = item.name or "?"
            if item.owner:
                title = "%s (%s)" % (title, item.owner)
            row.label(text=title, icon='ACTION')
            st = row.row(align=True)
            st.alignment = 'RIGHT'
            st.ui_units_x = 4.2
            st.label(text="%s/%s" % (
                _wrap_short(item.wrap), _wrap_short(item.curve_wrap)))

    def filter_items(self, context, data, propname):
        """Hide clip rows under collapsed hosts (KSPedia TOC pattern)."""
        nodes = getattr(data, propname)
        flt_flags = [self.bitflag_filter_item] * len(nodes)
        flt_neworder = list(range(len(nodes)))
        collapse_depth = None
        for i, node in enumerate(nodes):
            depth = int(node.depth)
            if collapse_depth is not None:
                if depth > collapse_depth:
                    flt_flags[i] = 0
                    continue
                collapse_depth = None
            if node.kind == "host" and not node.expanded:
                collapse_depth = depth
        return flt_flags, flt_neworder


def _anim_clips_host(context):
    wm = context.window_manager
    return getattr(wm, "ksp_mu_anim_ui", None)


def _entries_signature(entries):
    """Stable signature so we only rebuild the tree when data changes."""
    parts = []
    for ent in entries:
        act = ent.get("action")
        an = act.name if act is not None else ""
        parts.append("%s\x00%s\x00%s\x00%d\x00%d" % (
            ent.get("clip_name") or "",
            ent.get("owner") or "",
            an,
            int(ent.get("wrap") or 1),
            1 if ent.get("autoplay") else 0,
        ))
    return "\n".join(parts)


# Last built signature (module-level) — avoids clear() every redraw which
# steals UIList selection (could not click clip rows under a host).
_anim_tree_sig = ""


def _sync_anim_clip_collection(context, entries):
    """Rebuild tree rows only when clip/host data actually changed."""
    global _anim_tree_sig
    ui = _anim_clips_host(context)
    if ui is None:
        return None
    coll = ui.rows
    sig = _entries_signature(entries)

    # Lightweight refresh of autoplay / wrap flags without clearing selection
    if sig == _anim_tree_sig and len(coll) > 0:
        by_action = {}
        for ent in entries:
            act = ent.get("action")
            if act is not None:
                by_action[act.name] = ent
        for r in coll:
            if r.kind == "clip" and r.action_name in by_action:
                ent = by_action[r.action_name]
                r.wrap = int(ent.get("wrap") or 1)
                r.autoplay = bool(ent.get("autoplay"))
                r.curve_wrap = _curve_wrap_of_action(ent.get("action"))
            elif r.kind == "host":
                # Any clip under host with autoplay
                ap = False
                for r2 in coll:
                    if r2.kind == "clip" and r2.host_key == r.host_key:
                        if r2.action_name in by_action and by_action[r2.action_name].get("autoplay"):
                            ap = True
                            break
                r.autoplay = ap
        return ui

    # Full rebuild
    expand_map = {}
    prev_key = ""
    try:
        for r in coll:
            if r.kind == "host":
                expand_map[r.host_key] = bool(r.expanded)
        idx = int(ui.rows_index)
        if 0 <= idx < len(coll):
            # Prefer clip selection key (kind|host|action)
            prev_key = "%s|%s|%s" % (coll[idx].kind, coll[idx].host_key, coll[idx].action_name)
    except Exception:
        pass

    coll.clear()
    groups = _group_entries_by_host(entries)
    for g in groups:
        hk = g["host_key"]
        hrow = coll.add()
        hrow.kind = "host"
        hrow.host_key = hk
        hrow.name = g["name"]
        hrow.owner = g["owner"]
        hrow.autoplay = bool(g["autoplay"])
        hrow.depth = 0
        hrow.expanded = expand_map.get(hk, True)
        hrow.wrap = 1
        hrow.curve_wrap = 8
        if g["clips"]:
            act0 = g["clips"][0].get("action")
            hrow.action_name = act0.name if act0 is not None else ""
        for ent in g["clips"]:
            act = ent.get("action")
            crow = coll.add()
            crow.kind = "clip"
            crow.host_key = hk
            crow.name = ent.get("clip_name") or (act.name if act else "?")
            crow.owner = ent.get("owner") or ""
            crow.action_name = act.name if act is not None else ""
            crow.wrap = int(ent.get("wrap") or 1)
            crow.curve_wrap = _curve_wrap_of_action(act)
            crow.autoplay = bool(ent.get("autoplay"))
            crow.depth = 1
            crow.expanded = True

    new_idx = 0
    if prev_key:
        for i, r in enumerate(coll):
            key = "%s|%s|%s" % (r.kind, r.host_key, r.action_name)
            if key == prev_key:
                new_idx = i
                break
    try:
        ui.rows_index = min(new_idx, max(0, len(coll) - 1))
    except Exception:
        pass
    _anim_tree_sig = sig
    return ui


def _ensure_parts_populated(context, br):
    """Fill br.parts for the current category when the list is still empty.

    First open of the MU tab often has category enum set but parts never
    scanned — category change triggers refresh, plain tab open does not.
    """
    if br is None:
        return
    try:
        if len(br.parts) > 0:
            return
    except Exception:
        return
    try:
        from . import operators
        operators.refresh_part_list(context)
    except Exception:
        pass


def _ensure_first_part_preview(br):
    """Force first category part into the icon view (parts_index + part_preview).

    template_icon_view binds to ``part_preview`` EnumProperty; setting only
    ``parts_index`` leaves the thumbnail blank until the user changes category.
    Always re-assert PART_<index> via the enum string cache used by properties.
    """
    if br is None:
        return
    try:
        n = len(br.parts)
    except Exception:
        return
    if n <= 0:
        return
    try:
        idx = int(br.parts_index)
    except Exception:
        idx = -1
    if idx < 0 or idx >= n:
        idx = 0
        try:
            br.parts_index = 0
        except Exception:
            pass
    # Always sync part_preview → PART_<idx> (enum identity must match cache)
    try:
        from .properties import _enum_cache_string
        target = _enum_cache_string("PART_%d" % idx)
    except Exception:
        target = "PART_%d" % idx
    try:
        cur = str(br.part_preview or "")
    except Exception:
        cur = ""
    if cur != target and cur != ("PART_%d" % idx):
        try:
            br.part_preview = target
        except Exception:
            try:
                from .properties import _reset_part_selection
                _reset_part_selection(br)
            except Exception:
                try:
                    br.part_preview = "PART_%d" % idx
                except Exception:
                    pass
    # If still empty/invalid after assign, hard reset to first item
    try:
        cur2 = str(br.part_preview or "")
        if not cur2.startswith("PART_"):
            br.parts_index = 0
            try:
                from .properties import _reset_part_selection
                _reset_part_selection(br)
            except Exception:
                try:
                    from .properties import _enum_cache_string
                    br.part_preview = _enum_cache_string("PART_0")
                except Exception:
                    br.part_preview = "PART_0"
    except Exception:
        pass


class KSPMU_OT_toggle_anim_section(bpy.types.Operator):
    bl_idname = "object.ksp_mu_toggle_anim_section"
    bl_label = "Toggle Animation Section"
    bl_description = "Expand or collapse the MU Animation clip list"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _anim_section_expanded
        _anim_section_expanded = not _anim_section_expanded
        return {'FINISHED'}


class KSPMU_OT_set_animation_wrap(bpy.types.Operator):
    bl_idname = "object.ksp_mu_set_animation_wrap"
    bl_label = "Set MU Clip Wrap Mode"
    bl_description = "Set Unity AnimationClip.wrapMode (Once / Loop / Ping-Pong / Clamp) for this MU clip"
    bl_options = {'UNDO'}

    action_name: StringProperty(name="Action", default="")
    wrap_mode: IntProperty(name="Wrap Mode", default=1)

    def execute(self, context):
        action = bpy.data.actions.get(self.action_name)
        if action is None:
            self.report({'WARNING'}, "Action not found")
            return {'CANCELLED'}
        mode = _normalize_wrap(self.wrap_mode)
        try:
            action["mu_clip_wrap_mode"] = int(mode)
        except Exception as e:
            self.report({'WARNING'}, "Could not set wrap: %s" % e)
            return {'CANCELLED'}
        apply_mu_clip_wrap_to_action(action, mode)
        return {'FINISHED'}


class KSPMU_OT_set_curve_wrap(bpy.types.Operator):
    bl_idname = "object.ksp_mu_set_curve_wrap"
    bl_label = "Set MU Curve Wrap Mode"
    bl_description = "Set pre/post wrap on all F-Curves of this Action (Once / Loop / Ping-Pong / Clamp)"
    bl_options = {'UNDO'}

    action_name: StringProperty(name="Action", default="")
    wrap_mode: IntProperty(name="Wrap Mode", default=8)

    def execute(self, context):
        action = bpy.data.actions.get(self.action_name)
        if action is None:
            self.report({'WARNING'}, "Action not found")
            return {'CANCELLED'}
        # Blender 5.x layered Actions: iter_action_fcurves walks channelbags;
        # still guard each fcurve so a single bad slot cannot abort the rest.
        curves = list(iter_action_fcurves(action))
        if not curves:
            self.report({'WARNING'}, "No F-Curves in Action")
            return {'CANCELLED'}
        mode = int(self.wrap_mode)
        if mode not in (1, 2, 4, 8):
            mode = 8
        ok = 0
        for fcurve in curves:
            try:
                if fcurve is None:
                    continue
                apply_mu_curve_wrap(action, fcurve, mode, mode)
                ok += 1
            except Exception:
                continue
        if ok == 0:
            self.report({'WARNING'}, "Could not set curve wrap on any F-Curve")
            return {'CANCELLED'}
        return {'FINISHED'}


class KSPMU_OT_set_animation_autoplay(bpy.types.Operator):
    bl_idname = "object.ksp_mu_set_animation_autoplay"
    bl_label = "Set MU Auto Play"
    bl_description = "Toggle MuAnimation.autoPlay for this Animation host only (Unity: one flag per host)"
    bl_options = {'UNDO'}

    host_key: StringProperty(name="Host Key", default="")
    action_name: StringProperty(name="Action", default="")
    value: BoolProperty(name="Auto Play", default=False)

    def execute(self, context):
        value = int(bool(self.value))
        host_key = (self.host_key or "").strip()
        # Resolve host from explicit key or from the representative action
        if not host_key and self.action_name:
            act = bpy.data.actions.get(self.action_name)
            if act is not None:
                host_key = _host_key_of_action(act, "")
        if not host_key and self.action_name:
            # Fallback: only the named action
            act = bpy.data.actions.get(self.action_name)
            if act is not None:
                try:
                    act["mu_auto_play"] = value
                except Exception:
                    pass
            return {'FINISHED'}

        matched = 0
        for other in bpy.data.actions:
            try:
                oh = _host_key_of_action(other, "")
                if oh and oh == host_key:
                    other["mu_auto_play"] = value
                    matched += 1
            except Exception:
                continue
        if matched == 0 and self.action_name:
            act = bpy.data.actions.get(self.action_name)
            if act is not None:
                try:
                    act["mu_auto_play"] = value
                except Exception:
                    pass
        return {'FINISHED'}


class VIEW3D_PT_mu_part_browser(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MU"
    bl_label = "Parts (.mu)"
    bl_order = 10

    def draw(self, context):
        layout = self.layout
        try:
            from ..import_mu.progress_util import draw_mu_panel_progress
            draw_mu_panel_progress(layout)
        except Exception:
            pass
        wm = context.window_manager
        br = getattr(wm, "ksp_mu_browser", None)

        if br is None:
            layout.label(text="Browser not registered", icon="ERROR")
            return
        layout.prop(br, "like_ksp", text="like KSP", icon="SNAP_ON")

        if br.like_ksp:
            box = layout.box()
            box.label(text="Nodes snap + attachRules", icon="INFO")
            box.label(text="Import places a ghost under the cursor")
        try:
            from ..preferences.preferences import Preferences
            gd = (Preferences().GameData or "").strip()
        except Exception:
            gd = ""
        if not gd:
            layout.label(text="Set GameData in Tool > Options", icon="ERROR")
        else:
            layout.label(text=(os.path.basename(gd.rstrip("/\\")) or gd), icon="FILE_FOLDER")
        row = layout.row(align=True)
        row.operator("object.ksp_mu_browser_refresh", text="Refresh", icon="FILE_REFRESH")
        row.operator("object.ksp_mu_browser_gen_thumbs", text="Thumbs", icon="IMAGE_DATA")
        row.operator("object.ksp_mu_browser_regen_thumbs", text="Regen", icon="FILE_REFRESH")
        layout.prop(br, "category", text="")
        layout.prop(br, "filter", text="", icon="VIEWZOOM")
        # First MU-tab open: category may be set while parts are still empty.
        _ensure_parts_populated(context, br)
        # Sync icon-view enum to the current parts_index (usually 0).
        _ensure_first_part_preview(br)
        parts = br.parts
        if not parts:
            layout.label(text="No parts in category", icon="INFO")
        else:
            preview_box = layout.box()
            preview_box.template_icon_view(
                br, "part_preview", show_labels=True,
                scale=_THUMB_SCALE, scale_popup=_THUMB_SCALE_POPUP)
        row = layout.row(align=True)
        op = row.operator("object.ksp_mu_browser_import_part", text="Import", icon="IMPORT")
        if 0 <= br.parts_index < len(br.parts):
            op.part_name = br.parts[br.parts_index].name
        if 0 <= br.parts_index < len(br.parts):
            item = br.parts[br.parts_index]
            box = layout.box()
            row = box.row(align=True)
            row.scale_y = 0.5
            row.label(text="Part title:   %s" % item.title)
            row = box.row(align=True)
            row.scale_y = 0.5
            row.label(text="File name: %s" % item.name)
            if item.attach_rules:
                row = box.row(align=True)
                row.scale_y = 0.5
                row.label(text="Attach rules: %s" % item.attach_rules)

        # ---- MU Animation (host → clips tree) ----
        entries = _mu_animation_entries(context)
        if not entries:
            return

        box = layout.box()
        header = box.row(align=True)
        header.operator(
            "object.ksp_mu_toggle_anim_section", text="", emboss=False,
            icon='TRIA_DOWN' if _anim_section_expanded else 'TRIA_RIGHT')
        n_hosts = len(_group_entries_by_host(entries))
        n_clips = len(entries)
        if _anim_section_expanded:
            header.label(text="Animation", icon="ANIM")
        else:
            header.label(
                text="Animation (%d host / %d clip)" % (n_hosts, n_clips),
                icon="ANIM")
            return

        ui = _sync_anim_clip_collection(context, entries)
        if ui is not None and len(ui.rows) > 0:
            # Count visible rows (expanded tree) for default height
            vis = 0
            collapse = None
            for r in ui.rows:
                d = int(r.depth)
                if collapse is not None:
                    if d > collapse:
                        continue
                    collapse = None
                vis += 1
                if r.kind == "host" and not r.expanded:
                    collapse = d
            rows = min(max(_ANIM_LIST_DEFAULT_ROWS, 1), _ANIM_LIST_MAX_ROWS)
            rows = min(rows, max(1, vis))
            box.template_list(
                "KSPMU_UL_AnimClipList", "",
                ui, "rows",
                ui, "rows_index",
                rows=rows,
                maxrows=_ANIM_LIST_MAX_ROWS,
            )
            # Detail: Clip / Curve Wrap ONLY when a CLIP row is selected.
            # Host row = Animation component (empty) — no clip wrap UI.
            try:
                aidx = int(ui.rows_index)
            except Exception:
                aidx = 0
            if 0 <= aidx < len(ui.rows):
                item = ui.rows[aidx]
                if item.kind == "clip" and item.action_name:
                    action = bpy.data.actions.get(item.action_name)
                    if action is not None:
                        detail_name = item.name or action.name
                        if item.owner:
                            detail_name = "%s (%s)" % (detail_name, item.owner)
                        clip_wrap = 1
                        try:
                            clip_wrap = _normalize_wrap(
                                action.get("mu_clip_wrap_mode", 1))
                        except Exception:
                            pass
                        clip_box = box.box()
                        clip_box.label(text=detail_name, icon="ACTION")
                        _WRAP_BTNS = (
                            (1, "Once"), (2, "Loop"),
                            (4, "Ping-Pong"), (8, "Clamp"),
                        )
                        row = clip_box.row(align=True)
                        row.label(text="Clip Wrap")
                        for mode, label in _WRAP_BTNS:
                            op = row.operator(
                                "object.ksp_mu_set_animation_wrap",
                                text=label, depress=(clip_wrap == mode))
                            op.action_name = action.name
                            op.wrap_mode = mode

                        active_curve_mode = None
                        try:
                            cw = _curve_wrap_of_action(action)
                            if cw in (1, 2, 4, 8):
                                active_curve_mode = cw
                        except Exception:
                            pass
                        row = clip_box.row(align=True)
                        row.label(text="Curve Wrap")
                        for mode, label in _WRAP_BTNS:
                            op = row.operator(
                                "object.ksp_mu_set_curve_wrap",
                                text=label,
                                depress=(active_curve_mode == mode))
                            op.action_name = action.name
                            op.wrap_mode = mode
                elif item.kind == "host":
                    box.label(
                        text="Select a clip under the host to edit wrap",
                        icon="INFO")
        else:
            box.label(
                text="Reload addon to enable animation list",
                icon="INFO")


classes_to_register = (
    KSPMU_PG_AnimRowItem,
    KSPMU_PG_AnimClipsHost,
    KSPMU_UL_AnimClipList,
    KSPMU_OT_toggle_anim_section,
    KSPMU_OT_set_animation_wrap,
    KSPMU_OT_set_curve_wrap,
    KSPMU_OT_set_animation_autoplay,
    VIEW3D_PT_mu_part_browser,
)

# Registered by main __init__ via:
#   setattr(prop[0], prop[1], PointerProperty(type=prop[2]))
custom_properties_to_register = (
    (WindowManager, "ksp_mu_anim_ui", KSPMU_PG_AnimClipsHost),
)
