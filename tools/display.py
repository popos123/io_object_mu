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

"""Viewport display helpers: collider / shroud-fairing visibility, bump, FX."""

import json
import os

import bpy


def _update_part_variant(self, context):
    """Scene enum → apply ModulePartVariants GAMEOBJECTS visibility."""
    name = getattr(self, "mu_active_variant", "") or ""
    if not name or name == "NONE":
        return
    try:
        from ..import_mu.cfg_preview import apply_variant, find_variant_root
    except Exception:
        return
    root = find_variant_root(
        getattr(context, "active_object", None), context=context
    )
    if root is None:
        return
    apply_variant(root, name)


def _update_active_flag(self, context):
    """Scene enum → swap FlagDecal quad _MainTex (viewport only)."""
    name = getattr(self, "mu_active_flag", "") or ""
    try:
        from ..import_mu.flag_preview import (
            NO_FLAG,
            apply_flag_texture,
            find_flag_root,
        )
    except Exception:
        return
    root = find_flag_root(
        getattr(context, "active_object", None), context=context
    )
    if root is None:
        return
    apply_flag_texture(root, name or NO_FLAG)


def _sync_variant_enum_to_selection(context):
    """Refresh Options dropdown to the selected model's active variant."""
    try:
        from ..import_mu.cfg_preview import find_variant_root, MU_VARIANTS_KEY
        import json
    except Exception:
        return
    root = find_variant_root(
        getattr(context, "active_object", None), context=context
    )
    if root is None or MU_VARIANTS_KEY not in root:
        return
    try:
        data = json.loads(root[MU_VARIANTS_KEY])
    except Exception:
        return
    active = data.get("active") or data.get("base") or ""
    if not active:
        return
    scene = context.scene
    if not hasattr(scene, "mu_active_variant"):
        return
    # Avoid recursive update when already matching
    if getattr(scene, "mu_active_variant", None) == active:
        return
    try:
        scene["mu_active_variant"] = active
    except Exception:
        try:
            scene.mu_active_variant = active
        except Exception:
            pass


def _sync_flag_enum_to_selection(context):
    try:
        from ..import_mu.flag_preview import NO_FLAG, find_flag_root, MU_FLAG_KEY
        import json
    except Exception:
        return
    root = find_flag_root(
        getattr(context, "active_object", None), context=context
    )
    scene = context.scene
    if not hasattr(scene, "mu_active_flag"):
        return
    if root is None:
        return
    active = root.get("mu_active_flag") or NO_FLAG
    try:
        raw = root.get(MU_FLAG_KEY)
        if raw:
            data = json.loads(raw)
            active = data.get("active") or active
    except Exception:
        pass
    if getattr(scene, "mu_active_flag", None) == active:
        return
    try:
        scene["mu_active_flag"] = active
    except Exception:
        try:
            scene.mu_active_flag = active
        except Exception:
            pass


def _variant_enum_items(self, context):
    try:
        from ..import_mu.cfg_preview import get_variant_items
        return get_variant_items(self, context)
    except Exception:
        return [("NONE", "(no variants)", "")]


def _flag_enum_items(self, context):
    try:
        from ..import_mu.flag_preview import get_flag_enum_items
        return get_flag_enum_items(self, context)
    except Exception:
        return [("NONE", "No Flag", "")]


def _preview_clip_hierarchy_roots(context):
    """Top-level import roots for the current selection (or active object)."""
    if context is None:
        return []
    sel = list(getattr(context, "selected_objects", None) or [])
    active = getattr(context, "active_object", None)
    if not sel and active is not None:
        sel = [active]
    if not sel:
        # Active collection: any object therein → its hierarchy root(s)
        col = getattr(context, "collection", None)
        if col is not None:
            sel = list(getattr(col, "all_objects", []) or [])[:32]
    roots = []
    seen = set()
    for obj in sel:
        if obj is None:
            continue
        root = obj
        while root.parent is not None:
            root = root.parent
        key = root.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _preview_clip_scope_ids(context):
    """Objects + materials for the whole selected part hierarchy.

    Walks up to each selection's import root so sibling branches (Armature
    clips + Dust/Rocks particles + light GOs) are all visible to Preview Clip.
    """
    if context is None:
        return []
    roots = _preview_clip_hierarchy_roots(context)
    objs = []
    seen_obj = set()
    for root in roots:
        stack = [root]
        while stack:
            obj = stack.pop()
            if obj is None:
                continue
            key = obj.as_pointer()
            if key in seen_obj:
                continue
            seen_obj.add(key)
            objs.append(obj)
            stack.extend(list(obj.children))
    mats = []
    seen_mat = set()
    for obj in objs:
        for slot in getattr(obj, "material_slots", []) or []:
            mat = slot.material
            if mat is None:
                continue
            key = mat.as_pointer()
            if key in seen_mat:
                continue
            seen_mat.add(key)
            mats.append(mat)
        # Particle host materials may be unassigned stock mats (DrillDust)
        if obj.get("mu_particles"):
            try:
                from ..import_mu.particles_preview import _particle_material
                mat = _particle_material(obj, None)
            except Exception:
                mat = None
            if mat is not None:
                key = mat.as_pointer()
                if key not in seen_mat:
                    seen_mat.add(key)
                    mats.append(mat)
    return objs + mats


def _strip_preview_name(name):
    try:
        from ..utils import strip_nnn
        return strip_nnn(name) or ""
    except Exception:
        raw = name or ""
        ind = raw.rfind("\u2227")
        if ind >= 0:
            raw = raw[:ind]
        return raw


def _particle_fx_hosts_in_scope(objs):
    """MuParticles hosts (Dust/Rocks) → (label, object)."""
    out = []
    seen = set()
    for obj in objs:
        if not getattr(obj, "get", lambda *_: None)("mu_particles"):
            continue
        if obj.get("mu_fx_preview"):
            continue
        label = _strip_preview_name(obj.name)
        if not label or label in seen:
            continue
        seen.add(label)
        out.append((label, obj))
    return out


def _is_fx_nla_source(id_data, track):
    """True for particle / FX preview tracks that stay unmuted during clip scrub."""
    try:
        if getattr(id_data, "get", lambda *_: None)("mu_fx_preview"):
            return True
    except Exception:
        pass
    oname = getattr(id_data, "name", "") or ""
    if ".fx_anim" in oname or ".fx_preview" in oname:
        return True
    tn = (track.name or "") if track else ""
    if tn.lower() in ("muparticles", "mu_particles") or tn.startswith("MuParticles"):
        return True
    try:
        if track is not None and track.get("mu_fx_preview"):
            return True
    except Exception:
        pass
    # Material colorAnimation baked for particle preview
    try:
        if track is not None:
            for strip in track.strips:
                act = strip.action
                if act is not None and act.get("mu_fx_preview"):
                    return True
    except Exception:
        pass
    return False


def _collect_preview_clip_names(context):
    """NLA track names + MuParticles host labels for Preview Clip enum."""
    names = []
    seen = set()
    scope = _preview_clip_scope_ids(context)
    objs = [x for x in scope if isinstance(x, bpy.types.Object)]
    for id_data in scope:
        ad = getattr(id_data, "animation_data", None)
        if not ad or not ad.nla_tracks:
            # Blender 5 may leave FX color anim as ad.action only
            act = getattr(ad, "action", None) if ad else None
            if act is not None and act.get("mu_fx_preview"):
                continue
            continue
        try:
            if getattr(id_data, "get", lambda *_: None)("mu_fx_preview"):
                continue
        except Exception:
            pass
        oname = getattr(id_data, "name", "") or ""
        if ".fx_anim" in oname or ".fx_preview" in oname:
            continue
        for track in ad.nla_tracks:
            tn = track.name or ""
            if not tn:
                continue
            if tn.lower() in ("muparticles", "mu_particles") or tn.startswith("MuParticles"):
                continue
            try:
                if track.get("mu_fx_preview") and not track.get("mu_robotic_preview"):
                    continue
            except Exception:
                pass
            if tn.startswith("ColorChangerLights"):
                label = "ColorChangerLights"
                if label not in seen:
                    seen.add(label)
                    names.append(label)
                continue
            if tn not in seen:
                seen.add(tn)
                names.append(tn)
    # Synthetic entries for hidden particle FX hosts (Dust, Rocks, …)
    for label, _host in _particle_fx_hosts_in_scope(objs):
        if label not in seen:
            seen.add(label)
            names.append(label)
    try:
        from ..import_mu.animation import ordered_clip_names
        # Keep FX host labels after real Mu clips but still ordered
        fx_labels = {lab for lab, _ in _particle_fx_hosts_in_scope(objs)}
        real = [n for n in names if n not in fx_labels]
        fx = [n for n in names if n in fx_labels]
        return ordered_clip_names(real) + fx
    except Exception:
        return names


def _refresh_preview_clip_frame(context, force_rest):
    """Force depsgraph update after NLA mute changes.

    Muting alone leaves the last evaluated mid-strip pose frozen in the
    viewport; scrubbing to frame_start (or re-setting the current frame)
    clears that hold so objects return to rest / default keys.
    """
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    try:
        if force_rest:
            target = int(scene.frame_start)
        else:
            target = int(scene.frame_current)
        scene.frame_set(target)
    except Exception:
        try:
            scene.frame_set(1)
        except Exception:
            pass


def _set_preview_clip_active(context, clip_name):
    """Apply Preview Clip selection to NLA tracks in scope.

    NONE — mute all non-FX clips and reset to rest at frame_start (not frozen).
    ALL  — unmute all non-FX clips (import-style default visibility) at frame_start.
    Dust/Rocks (MuParticles host) — force-show that FX group; mute Mu clips.
    else — unmute only the chosen clip; FX preview stays frame-gated.
    """
    mode = clip_name or "NONE"
    mute_all = mode == "NONE"
    unmute_all = mode == "ALL"
    scope = _preview_clip_scope_ids(context)
    objs = [x for x in scope if isinstance(x, bpy.types.Object)]
    fx_hosts = dict(_particle_fx_hosts_in_scope(objs))
    fx_mode = mode in fx_hosts

    for id_data in scope:
        ad = getattr(id_data, "animation_data", None)
        if not ad or not ad.nla_tracks:
            continue
        for track in ad.nla_tracks:
            tn = track.name or ""
            is_fx = _is_fx_nla_source(id_data, track)
            if is_fx:
                if tn.startswith("ColorChangerLights"):
                    if mute_all or fx_mode:
                        track.mute = True
                    elif unmute_all:
                        track.mute = False
                    else:
                        track.mute = not (
                            mode == "ColorChangerLights"
                            or mode.startswith("ColorChangerLights")
                        )
                else:
                    # MuParticles swarm tracks: unmute only for selected host
                    if mute_all:
                        track.mute = True
                    elif fx_mode:
                        host = fx_hosts.get(mode)
                        oname = getattr(id_data, "name", "") or ""
                        belong = bool(
                            host is not None and (
                                oname.startswith(host.name or "")
                                or (host.name or "") in oname
                            )
                        )
                        track.mute = not belong
                    else:
                        track.mute = False
            else:
                if mute_all or fx_mode:
                    track.mute = True
                elif unmute_all:
                    track.mute = False
                elif mode == "ColorChangerLights":
                    track.mute = not tn.startswith("ColorChangerLights")
                else:
                    track.mute = (tn != mode)
        try:
            ad.action = None
        except Exception:
            pass

    # Particle FX host visibility (Dust / Rocks are hide_viewport until deploy)
    scene = getattr(context, "scene", None)
    try:
        if scene is not None:
            scene["mu_preview_fx_force"] = mode if fx_mode else ""
    except Exception:
        pass

    _refresh_preview_clip_frame(
        context, force_rest=(mute_all or unmute_all or fx_mode)
    )

    try:
        from ..import_mu.particles_preview import (
            set_particles_preview_for_host,
            sync_particles_preview_for_frame,
        )
        if mute_all:
            for _lab, host in fx_hosts.items():
                set_particles_preview_for_host(host, False, force=True)
        elif fx_mode:
            for lab, host in fx_hosts.items():
                set_particles_preview_for_host(
                    host, visible=(lab == mode), force=True
                )
        else:
            # Regular Mu clip / ALL — restore deploy-gated FX visibility
            sync_particles_preview_for_frame(
                getattr(context.scene, "frame_current", 1)
            )
    except Exception:
        pass


def _update_preview_clip(self, context):
    name = getattr(self, "mu_preview_clip", "") or "NONE"
    _set_preview_clip_active(context, name)


def _preview_clip_enum_items(self, context):
    names = _collect_preview_clip_names(context)
    items = [
        (
            "NONE",
            "(none / muted)",
            "Mute all non-FX clips and reset pose to rest (frame start)",
        ),
        (
            "ALL",
            "(all / import default)",
            "Unmute all non-FX clips and reset to frame start (import default pose)",
        ),
    ]
    if not names:
        return [
            ("NONE", "(no clips)", "No NLA clips on selected hierarchy"),
            items[1],
        ]
    fx_labels = set()
    try:
        objs = [
            x for x in _preview_clip_scope_ids(context)
            if isinstance(x, bpy.types.Object)
        ]
        fx_labels = {lab for lab, _ in _particle_fx_hosts_in_scope(objs)}
    except Exception:
        pass
    for n in names:
        if n in fx_labels:
            items.append((n, f"{n} (FX)", f"Show particle FX host '{n}'"))
        else:
            items.append((n, n, f"Activate NLA clip '{n}'"))
    return items


def _is_collider_object(obj):
    if not obj:
        return False
    if obj.name.startswith("mesh:"):
        return True
    muprops = getattr(obj, "muproperties", None)
    if muprops and muprops.collider and muprops.collider != 'MU_COL_NONE':
        return True
    for col in obj.users_collection:
        if col.name.endswith(".collider") or ".collider." in col.name:
            return True
    if "collider" in obj.name.lower():
        return True
    return False


def _iter_scope_objects(context, scope):
    if scope == 'SELECTED':
        sel = list(context.selected_objects)
        if not sel and context.active_object:
            sel = [context.active_object]
        out = []
        for o in sel:
            out.append(o)
            out.extend(o.children_recursive)
        return out
    if scope == 'COLLECTION':
        col = context.collection
        if not col:
            return list(context.scene.objects)
        return list(col.all_objects)
    return list(context.scene.objects)


def _set_collider_hide(objects, hide):
    n = 0
    for obj in objects:
        if not _is_collider_object(obj):
            continue
        obj.hide_viewport = hide
        n += 1
    return n


def _is_shroud_or_fairing_object(obj):
    """Engine ModuleJettison covers: fairing, Shroud, ShortShroud, nShroud…

    Variant-only GAMEOBJECTS (LV-1 chamber named Shroud) are skipped. Names
    that are also ModuleJettison ``jettisonName`` (Poodle Shroud1, Terrier
    ShortShroud) still count as covers.
    """
    if not obj:
        return False
    name = (obj.name or "").lower()
    if not name:
        return False
    wedge = "\u2227"
    key = name.split(wedge, 1)[0].strip()
    jettison = set()
    variant_keys = set()
    try:
        cur = obj
        while cur is not None:
            raw_j = cur.get("mu_jettison_names")
            if raw_j:
                try:
                    jettison.update(
                        str(n).lower() for n in json.loads(raw_j) if n
                    )
                except Exception:
                    pass
            raw = cur.get("mu_variants")
            if raw:
                data = json.loads(raw)
                for v in data.get("variants") or []:
                    variant_keys.update(
                        str(k).lower() for k in (v.get("objects") or {}) if k
                    )
                for n in data.get("jettisonNames") or []:
                    if n:
                        jettison.add(str(n).lower())
                break
            cur = cur.parent
    except Exception:
        pass
    if key in jettison:
        return True
    if key in variant_keys and key not in jettison:
        return False
    return ("fairing" in name) or ("shroud" in name)


def _set_shroud_fairing_hide(objects, hide):
    n = 0
    for obj in objects:
        if not _is_shroud_or_fairing_object(obj):
            continue
        obj.hide_viewport = hide
        n += 1
    return n


def _mute_normal_nodes(node_tree, mute):
    if not node_tree:
        return 0
    n = 0
    for node in node_tree.nodes:
        if node.type in {'NORMAL_MAP', 'BUMP'}:
            node.mute = mute
            n += 1
        if node.type == 'GROUP' and getattr(node, "node_tree", None):
            n += _mute_normal_nodes(node.node_tree, mute)
        if node.type == 'TEX_IMAGE':
            lname = node.name.lower()
            if (lname.startswith('_bump') or 'bump' in lname
                    or 'normalmap' in lname or lname == '_normalmap'):
                if node.name not in {'_MainTex', '_Emissive', '_Emission'}:
                    node.mute = mute
                    n += 1
    return n


class KSPMU_OT_ToggleColliders(bpy.types.Operator):
    bl_idname = "object.mu_toggle_colliders"
    bl_label = "Toggle Colliders"
    bl_description = "Show or hide Mu colliders in the viewport"
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=(
            ('ALL', "All in Scene", "All collider objects in the scene"),
            ('SELECTED', "Selected Hierarchy",
             "Active/selected object and children"),
            ('COLLECTION', "Active Collection",
             "Objects in the active collection"),
        ),
        default='ALL',
    )
    force: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('TOGGLE', "Toggle", "Invert current visibility"),
            ('HIDE', "Hide", "Hide colliders"),
            ('SHOW', "Show", "Show colliders"),
        ),
        default='TOGGLE',
    )

    def execute(self, context):
        objs = [o for o in _iter_scope_objects(context, self.scope)
                if _is_collider_object(o)]
        if not objs:
            self.report({'INFO'}, "No colliders in scope")
            return {'CANCELLED'}
        if self.force == 'HIDE':
            hide = True
        elif self.force == 'SHOW':
            hide = False
        else:
            hidden = sum(1 for o in objs if o.hide_viewport)
            hide = hidden < len(objs) / 2
        n = _set_collider_hide(objs, hide)
        self.report({'INFO'},
                    f"{'Hidden' if hide else 'Shown'} {n} collider(s)")
        return {'FINISHED'}


class KSPMU_OT_ToggleShroudFairing(bpy.types.Operator):
    bl_idname = "object.mu_toggle_shroud_fairing"
    bl_label = "Toggle Shroud/Fairing"
    bl_description = (
        "Show or hide engine shroud / fairing meshes in the viewport "
        "(names containing shroud or fairing)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=(
            ('ALL', "All in Scene", "All shroud/fairing objects in the scene"),
            ('SELECTED', "Selected Hierarchy",
             "Active/selected object and children"),
            ('COLLECTION', "Active Collection",
             "Objects in the active collection"),
        ),
        default='ALL',
    )
    force: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('TOGGLE', "Toggle", "Invert current visibility"),
            ('HIDE', "Hide", "Hide shrouds/fairings"),
            ('SHOW', "Show", "Show shrouds/fairings"),
        ),
        default='TOGGLE',
    )

    def execute(self, context):
        objs = [o for o in _iter_scope_objects(context, self.scope)
                if _is_shroud_or_fairing_object(o)]
        if not objs:
            self.report({'INFO'}, "No shroud/fairing objects in scope")
            return {'CANCELLED'}
        if self.force == 'HIDE':
            hide = True
        elif self.force == 'SHOW':
            hide = False
        else:
            hidden = sum(1 for o in objs if o.hide_viewport)
            hide = hidden < len(objs) / 2
        n = _set_shroud_fairing_hide(objs, hide)
        self.report(
            {'INFO'},
            f"{'Hidden' if hide else 'Shown'} {n} shroud/fairing object(s)",
        )
        return {'FINISHED'}


class KSPMU_OT_ToggleBumpPreview(bpy.types.Operator):
    bl_idname = "object.mu_toggle_bump_preview"
    bl_label = "Toggle Bump/Normal Preview"
    bl_description = (
        "Mute or unmute Normal Map / Bump nodes in materials "
        "(viewport only; export unchanged)")
    bl_options = {'REGISTER', 'UNDO'}

    force: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('TOGGLE', "Toggle", "Invert mute state"),
            ('HIDE', "Hide", "Mute bump/normal nodes"),
            ('SHOW', "Show", "Unmute bump/normal nodes"),
        ),
        default='TOGGLE',
    )

    def execute(self, context):
        mats = [m for m in bpy.data.materials
                if m.node_tree and hasattr(m, "mumatprop")
                and m.mumatprop.shaderName]
        if not mats:
            self.report({'INFO'}, "No Mu materials")
            return {'CANCELLED'}
        muted = 0
        total = 0
        for m in mats:
            for n in m.node_tree.nodes:
                if n.type in {'NORMAL_MAP', 'BUMP'}:
                    total += 1
                    if n.mute:
                        muted += 1
                if n.type == 'GROUP' and n.node_tree:
                    for gn in n.node_tree.nodes:
                        if gn.type in {'NORMAL_MAP', 'BUMP'}:
                            total += 1
                            if gn.mute:
                                muted += 1
        if self.force == 'HIDE':
            mute = True
        elif self.force == 'SHOW':
            mute = False
        else:
            mute = muted < max(1, total) / 2
        n = 0
        for m in mats:
            n += _mute_normal_nodes(m.node_tree, mute)
        self.report({'INFO'},
                    f"{'Muted' if mute else 'Unmuted'} {n} bump/normal node(s)")
        return {'FINISHED'}


def _is_particle_preview_object(obj):
    """Viewport-only FX children (.fx_preview / .fx_anim / .fx_emitter)."""
    if not obj:
        return False
    name = obj.name or ""
    if (
        ".fx_preview" in name
        or ".fx_anim" in name
        or ".fx_emitter" in name
    ):
        return True
    if obj.get("mu_fx_preview"):
        return True
    if getattr(obj, "particle_systems", None) and len(obj.particle_systems):
        # Only treat as FX if tagged or named like our import preview
        if obj.get("mu_fx_preview") or "fx_" in name.lower():
            return True
    return False


def _set_particle_preview_hide(objects, hide):
    n = 0
    for obj in objects:
        if not _is_particle_preview_object(obj):
            continue
        obj.hide_viewport = hide
        try:
            obj.hide_render = hide
        except Exception:
            pass
        n += 1
    # Also mute Particle Systems (OBJECT instances ignore billboard hide alone)
    try:
        from ..import_mu.particles_preview import set_particles_preview_visible
        set_particles_preview_visible(not hide)
    except Exception:
        pass
    return n


class KSPMU_OT_ToggleParticlesPreview(bpy.types.Operator):
    bl_idname = "object.mu_toggle_particles_preview"
    bl_label = "Toggle Particles Preview"
    bl_description = (
        "Show or hide Mu particle FX preview (dust, rocks, plumes). "
        "Viewport only; export unchanged")
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=(
            ('ALL', "All in Scene", "All FX preview objects in the scene"),
            ('SELECTED', "Selected Hierarchy",
             "Active/selected object and children"),
            ('COLLECTION', "Active Collection",
             "Objects in the active collection"),
        ),
        default='ALL',
    )
    force: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('TOGGLE', "Toggle", "Invert current visibility"),
            ('HIDE', "Hide", "Hide particle FX preview"),
            ('SHOW', "Show", "Show particle FX preview"),
        ),
        default='TOGGLE',
    )

    def execute(self, context):
        objs = [o for o in _iter_scope_objects(context, self.scope)
                if _is_particle_preview_object(o)]
        if not objs:
            self.report({'INFO'}, "No particle FX preview in scope")
            return {'CANCELLED'}
        if self.force == 'HIDE':
            hide = True
        elif self.force == 'SHOW':
            hide = False
        else:
            hidden = sum(1 for o in objs if o.hide_viewport)
            hide = hidden < len(objs) / 2
        n = _set_particle_preview_hide(objs, hide)
        self.report(
            {'INFO'},
            f"{'Hidden' if hide else 'Shown'} {n} particle FX object(s)",
        )
        return {'FINISHED'}


def _set_scene_active_variant(scene, name):
    """Sync Options enum after Add/Remove (ID prop + EnumProperty)."""
    if not name or scene is None:
        return
    try:
        scene["mu_active_variant"] = name
    except Exception:
        pass
    try:
        if hasattr(scene, "mu_active_variant"):
            scene.mu_active_variant = name
    except Exception:
        pass


class KSPMU_OT_AddPartVariant(bpy.types.Operator):
    bl_idname = "object.mu_add_part_variant"
    bl_label = "Add Variant"
    bl_description = (
        "Duplicate the active ModulePartVariants cfg entry "
        "(mu_variants JSON only — not an Outliner collection)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            from ..import_mu.cfg_preview import (
                MU_VARIANTS_KEY,
                STOCK_VARIANT_NAME,
                add_variant_duplicate,
                find_variant_root,
            )
            import json
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        root = find_variant_root(
            getattr(context, "active_object", None), context=context
        )
        if root is None:
            # Seed empty variants from selection root
            obj = context.active_object
            if obj is None:
                self.report({'WARNING'}, "No active object")
                return {'CANCELLED'}
            while obj.parent:
                obj = obj.parent
            root = obj
            data = {
                "base": STOCK_VARIANT_NAME,
                "active": STOCK_VARIANT_NAME,
                "kind": "texture",
                "variants": [{
                    "name": STOCK_VARIANT_NAME,
                    "objects": {},
                    "textures": {},
                    "disabledAnimations": [],
                    "stock": True,
                    "displayName": "Stock",
                }],
            }
            root[MU_VARIANTS_KEY] = json.dumps(data)
        # JSON-only duplicate + apply_variant; never creates Outliner collections.
        name = add_variant_duplicate(root)
        if not name:
            self.report({'WARNING'}, "Could not add variant")
            return {'CANCELLED'}
        _set_scene_active_variant(context.scene, name)
        self.report(
            {'INFO'},
            f"Added cfg variant '{name}' (not an Outliner collection)",
        )
        return {'FINISHED'}


class KSPMU_OT_RemovePartVariant(bpy.types.Operator):
    bl_idname = "object.mu_remove_part_variant"
    bl_label = "Remove Variant"
    bl_description = (
        "Remove the active ModulePartVariants cfg entry from mu_variants "
        "(does not delete .mu meshes / Outliner collections)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            from ..import_mu.cfg_preview import (
                find_variant_root,
                get_active_variant_name,
                remove_active_variant,
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        root = find_variant_root(
            getattr(context, "active_object", None), context=context
        )
        if root is None:
            self.report({'WARNING'}, "No variants on selection")
            return {'CANCELLED'}
        prev = get_active_variant_name(root)
        # JSON-only remove + apply_variant; never deletes imported meshes.
        if not remove_active_variant(root):
            self.report(
                {'WARNING'},
                "Cannot remove Stock / last / base-only variant",
            )
            return {'CANCELLED'}
        active = get_active_variant_name(root)
        if active:
            _set_scene_active_variant(context.scene, active)
        self.report(
            {'INFO'},
            f"Removed cfg variant '{prev}' (meshes/collections kept)",
        )
        return {'FINISHED'}


class KSPMU_OT_SavePartCfg(bpy.types.Operator):
    bl_idname = "export_object.ksp_mu_save_part_cfg"
    bl_label = "Save Part Cfg…"
    bl_description = (
        "Write ModulePartVariants and animation EFFECTS/AUDIO into the "
        "active/selected part .cfg (confirms before overwriting the original; "
        "Save As for another path)"
    )
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filename_ext = ".cfg"
    filter_glob: bpy.props.StringProperty(default="*.cfg", options={'HIDDEN'})
    overwrite_confirmed: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    force_file_browser: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        try:
            from ..preferences import Preferences
            if not Preferences().WritePartCfg:
                self.report({'WARNING'}, "Write / Update Part Cfg is disabled")
                return {'CANCELLED'}
        except Exception:
            pass
        root = self._root(context)
        if root is None:
            self.report({'WARNING'}, "No part root with cfg / variants / sounds")
            return {'CANCELLED'}
        cfg = root.get("mu_cfg_path") or ""
        if self.force_file_browser:
            # Suggest a non-destructive default next to the original
            if cfg:
                stem, ext = os.path.splitext(cfg)
                self.filepath = stem + "_edited" + (ext or ".cfg")
            elif not self.filepath:
                mudir = root.get("mu_dirname") or ""
                self.filepath = os.path.join(mudir or "", "part_edited.cfg")
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        if cfg and not self.filepath:
            self.filepath = cfg
        # Always confirm when targeting the original mu_cfg_path
        if cfg and os.path.normpath(self.filepath or cfg) == os.path.normpath(cfg):
            return context.window_manager.invoke_props_dialog(self, width=420)
        if not self.filepath:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        path = self.filepath or "(unknown)"
        layout.label(text="Overwrite original part.cfg?")
        layout.label(text=path)
        layout.label(text="OK = overwrite · Cancel = abort")
        layout.label(text="Variants + animation sounds will be patched.")
        layout.separator()
        # Third choice (dialog footer is only OK / Cancel)
        layout.operator_context = 'INVOKE_DEFAULT'
        op = layout.operator(
            "export_object.ksp_mu_save_part_cfg",
            text="Save As…",
            icon='FILE_NEW',
        )
        op.force_file_browser = True
        op.overwrite_confirmed = True
        op.filepath = ""

    def _root(self, context):
        try:
            from ..import_mu.cfg_preview import find_variant_root
        except Exception:
            return None
        root = find_variant_root(
            getattr(context, "active_object", None), context=context
        )
        if root is not None:
            return root
        obj = context.active_object
        if obj is None:
            return None
        while obj.parent:
            obj = obj.parent
        return obj

    def execute(self, context):
        try:
            from ..preferences import Preferences
            if not Preferences().WritePartCfg:
                self.report({'WARNING'}, "Write / Update Part Cfg is disabled")
                return {'CANCELLED'}
        except Exception:
            pass
        try:
            from ..export_mu.variants_cfg import write_part_cfg
            from ..import_mu.cfg_preview import MU_VARIANTS_KEY
            from ..import_mu.sound_preview import (
                MU_SOUNDS_KEY,
                flush_sounds_from_vse,
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        root = self._root(context)
        if root is None:
            self.report({'WARNING'}, "No part root")
            return {'CANCELLED'}
        data = None
        raw = root.get(MU_VARIANTS_KEY)
        if raw:
            try:
                data = json.loads(raw)
            except Exception as e:
                self.report({'ERROR'}, f"Bad mu_variants: {e}")
                return {'CANCELLED'}
        flush_sounds_from_vse(root)
        mu_sounds = None
        sraw = root.get(MU_SOUNDS_KEY)
        if sraw:
            try:
                mu_sounds = json.loads(sraw)
            except Exception as e:
                self.report({'ERROR'}, f"Bad mu_sounds: {e}")
                return {'CANCELLED'}
        if not data and not mu_sounds:
            self.report({'WARNING'}, "No mu_variants or mu_sounds on part")
            return {'CANCELLED'}
        orig = root.get("mu_cfg_path") or ""
        path = self.filepath or orig
        if not path:
            self.report({'WARNING'}, "No cfg path")
            return {'CANCELLED'}
        # Confirm overwrite of original when dialog was used
        if orig and os.path.normpath(path) == os.path.normpath(orig):
            if not self.overwrite_confirmed:
                # Dialog OK → proceed
                self.overwrite_confirmed = True
        source = None
        if orig and os.path.isfile(orig):
            try:
                with open(orig, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
            except Exception:
                source = None
        mudir = root.get("mu_dirname") or os.path.dirname(path)
        mu_basename = None
        try:
            # Prefer .mu next to cfg
            for fn in os.listdir(mudir):
                if fn.lower().endswith(".mu"):
                    mu_basename = fn
                    break
        except Exception:
            pass
        try:
            from ..import_mu.sound_preview import log_sound_entries
            log_sound_entries("KSP sound export", mu_sounds)
        except Exception:
            pass
        try:
            write_part_cfg(
                path, data or {}, source_text=source,
                mudir=mudir, mu_basename=mu_basename,
                mu_sounds=mu_sounds,
            )
        except Exception as e:
            self.report({'ERROR'}, f"Write failed: {e}")
            return {'CANCELLED'}
        n_sfx = len((mu_sounds or {}).get("entries") or []) if mu_sounds else 0
        if n_sfx:
            self.report({'INFO'}, f"Saved {path} (+ {n_sfx} sound entr(y/ies))")
        else:
            self.report({'INFO'}, f"Saved {path}")
        return {'FINISHED'}


class KSPMU_OT_SyncAnimSounds(bpy.types.Operator):
    bl_idname = "object.ksp_mu_sync_anim_sounds"
    bl_label = "Apply Sound Timing"
    bl_description = (
        "Read VSE sound strip positions into mu_sounds (muSoundTime). "
        "Use Save Part Cfg to write them into the .cfg"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            from ..import_mu.sound_preview import flush_sounds_from_vse
            from ..import_mu.cfg_preview import find_variant_root
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        root = find_variant_root(
            getattr(context, "active_object", None), context=context
        )
        if root is None:
            obj = context.active_object
            if obj is not None:
                while obj.parent:
                    obj = obj.parent
                root = obj
        if root is None:
            self.report({'WARNING'}, "No part root")
            return {'CANCELLED'}
        data = flush_sounds_from_vse(root)
        n = len((data or {}).get("entries") or [])
        self.report({'INFO'}, f"Updated timing for {n} sound entr(y/ies)")
        return {'FINISHED'}


class KSPMU_OT_AddAnimSound(bpy.types.Operator):
    bl_idname = "object.ksp_mu_add_anim_sound"
    bl_label = "Add Animation Sound…"
    bl_description = (
        "Add a WAV/OGG under an EFFECTS name (deploy/retract/…) and create "
        "a VSE strip; Save Part Cfg writes a new AUDIO block"
    )
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(
        default="*.wav;*.ogg;*.flac", options={'HIDDEN'}
    )
    effect: bpy.props.StringProperty(name="Effect", default="deploy")
    kind: bpy.props.EnumProperty(
        name="Kind",
        items=(
            ('audio', "AUDIO", "One-shot"),
            ('loop', "AUDIO_LOOP", "Loop for clip length"),
        ),
        default='audio',
    )
    anim_clip: bpy.props.StringProperty(name="Anim clip", default="Deploy")
    sound_time: bpy.props.FloatProperty(name="Time (s)", default=0.0, min=0.0)
    sound_duration: bpy.props.FloatProperty(
        name="Duration (s)", default=1.0, min=0.05
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            from ..import_mu.sound_preview import add_sound_entry
            from ..import_mu.cfg_preview import find_variant_root
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        root = find_variant_root(
            getattr(context, "active_object", None), context=context
        )
        if root is None:
            obj = context.active_object
            if obj is not None:
                while obj.parent:
                    obj = obj.parent
                root = obj
        if root is None:
            self.report({'WARNING'}, "No part root")
            return {'CANCELLED'}
        path = self.filepath or ""
        if not path or not os.path.isfile(path):
            self.report({'WARNING'}, "No sound file")
            return {'CANCELLED'}
        try:
            add_sound_entry(
                root,
                path,
                effect=self.effect or "deploy",
                kind=self.kind,
                mu_anim_clip=self.anim_clip or "Deploy",
                mu_sound_time=float(self.sound_time),
                mu_sound_duration=float(self.sound_duration),
            )
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Added sound under EFFECTS/{self.effect}")
        return {'FINISHED'}


class WORKSPACE_PT_mu_options(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"
    bl_context = ".objectmode"
    bl_label = "Options"
    # Before Prop Tools / Mu Hierarchy / Export Mu
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        # Keep dropdown in sync with the selected import root's active variant
        _sync_variant_enum_to_selection(context)
        _sync_flag_enum_to_selection(context)
        col.label(text="Part Variant")
        col.prop(context.scene, "mu_active_variant", text="")
        row = col.row(align=True)
        row.operator("object.mu_add_part_variant", text="Add Variant")
        row.operator("object.mu_remove_part_variant", text="Remove")
        try:
            from ..preferences import Preferences
            prefs = Preferences()
            col.prop(prefs, "WritePartCfg")
            save_row = col.row()
            save_row.enabled = bool(prefs.WritePartCfg)
            save_row.operator(
                "export_object.ksp_mu_save_part_cfg", text="Save Part Cfg…"
            )
        except Exception:
            col.operator(
                "export_object.ksp_mu_save_part_cfg", text="Save Part Cfg…"
            )
        col.separator()
        col.label(text="Animation Sounds")
        col.operator("object.ksp_mu_sync_anim_sounds", text="Apply Sound Timing")
        col.operator("object.ksp_mu_add_anim_sound", text="Add Animation Sound…")
        col.separator()
        col.label(text="Flag Decal")
        col.prop(context.scene, "mu_active_flag", text="")
        col.separator()
        col.label(text="Preview Clip")
        col.prop(context.scene, "mu_preview_clip", text="")
        col.separator()
        col.label(text="Colliders")
        try:
            from ..preferences import Preferences
            prefs = Preferences()
            col.prop(prefs, "AutohideColliders")
        except Exception:
            pass
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_colliders", text="Toggle All")
        op.scope = 'ALL'
        op.force = 'TOGGLE'
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_colliders", text="Selected")
        op.scope = 'SELECTED'
        op.force = 'TOGGLE'
        op = row.operator("object.mu_toggle_colliders", text="Collection")
        op.scope = 'COLLECTION'
        op.force = 'TOGGLE'
        col.separator()
        col.label(text="Shroud / Fairing")
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_shroud_fairing", text="Toggle All")
        op.scope = 'ALL'
        op.force = 'TOGGLE'
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_shroud_fairing", text="Selected")
        op.scope = 'SELECTED'
        op.force = 'TOGGLE'
        op = row.operator("object.mu_toggle_shroud_fairing", text="Collection")
        op.scope = 'COLLECTION'
        op.force = 'TOGGLE'
        col.separator()
        col.label(text="KSP - Game Path")
        try:
            from ..preferences import Preferences
            prefs = Preferences()
            col.prop(prefs, "GameData", text="")
        except Exception:
            pass
        col.separator()
        col.label(text="Materials (preview)")
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_bump_preview",
                          text="Toggle Bump/Normal")
        op.force = 'TOGGLE'
        col.operator(
            "io_object_mu.shader_preset_selection",
            text="Apply Shader Preset…",
        )
        col.separator()
        col.label(text="Particles (preview)")
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_particles_preview",
                          text="Toggle Particles")
        op.scope = 'ALL'
        op.force = 'TOGGLE'
        row = col.row(align=True)
        op = row.operator("object.mu_toggle_particles_preview", text="Selected")
        op.scope = 'SELECTED'
        op.force = 'TOGGLE'
        op = row.operator("object.mu_toggle_particles_preview",
                          text="Collection")
        op.scope = 'COLLECTION'
        op.force = 'TOGGLE'


def register():
    if not hasattr(bpy.types.Scene, "mu_active_variant"):
        bpy.types.Scene.mu_active_variant = bpy.props.EnumProperty(
            name="Part Variant",
            description="Active ModulePartVariants preview (viewport only)",
            items=_variant_enum_items,
            update=_update_part_variant,
        )
    if not hasattr(bpy.types.Scene, "mu_active_flag"):
        bpy.types.Scene.mu_active_flag = bpy.props.EnumProperty(
            name="Flag Decal",
            description="Active FlagDecal preview texture (viewport only)",
            items=_flag_enum_items,
            update=_update_active_flag,
        )
    if not hasattr(bpy.types.Scene, "mu_preview_clip"):
        bpy.types.Scene.mu_preview_clip = bpy.props.EnumProperty(
            name="Preview Clip",
            description=(
                "NLA preview on selected hierarchy: none=mute+rest, "
                "all=unmute+rest, or a single clip (FX stays unmuted)"
            ),
            items=_preview_clip_enum_items,
            update=_update_preview_clip,
        )
    try:
        from ..import_mu.flag_preview import (
            register_flag_previews,
            sync_flags_from_gamedata,
        )
        register_flag_previews()
        sync_flags_from_gamedata()
    except Exception:
        pass
    try:
        from ..import_mu.sound_preview import register_sfx_handlers
        register_sfx_handlers()
    except Exception:
        pass


def unregister():
    if hasattr(bpy.types.Scene, "mu_preview_clip"):
        del bpy.types.Scene.mu_preview_clip
    if hasattr(bpy.types.Scene, "mu_active_variant"):
        del bpy.types.Scene.mu_active_variant
    if hasattr(bpy.types.Scene, "mu_active_flag"):
        del bpy.types.Scene.mu_active_flag
    try:
        from ..import_mu.flag_preview import unregister_flag_previews
        unregister_flag_previews()
    except Exception:
        pass
    try:
        from ..import_mu.sound_preview import unregister_sfx_handlers
        unregister_sfx_handlers()
    except Exception:
        pass


classes_to_register = (
    KSPMU_OT_ToggleColliders,
    KSPMU_OT_ToggleShroudFairing,
    KSPMU_OT_ToggleBumpPreview,
    KSPMU_OT_ToggleParticlesPreview,
    KSPMU_OT_AddPartVariant,
    KSPMU_OT_RemovePartVariant,
    KSPMU_OT_SavePartCfg,
    KSPMU_OT_SyncAnimSounds,
    KSPMU_OT_AddAnimSound,
    WORKSPACE_PT_mu_options,
)
