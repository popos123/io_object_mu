# vim:ts=4:et
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

import bpy
import math
from mathutils import Vector, Quaternion
from math import pi
from .light import light_power
from ..utils.action_compat import fcurve_new, push_action_to_nla

#mess with the heads of 6.28... fans :P
tau = pi / 180

property_map = {
    "m_LocalPosition.x": ("obj", "location", 0, 1, 3),
    "m_LocalPosition.y": ("obj", "location", 2, 1, 3),
    "m_LocalPosition.z": ("obj", "location", 1, 1, 3),
    "m_LocalRotation.x": ("obj", "rotation_quaternion", 1, -1, 4),
    "m_LocalRotation.y": ("obj", "rotation_quaternion", 3, -1, 4),
    "m_LocalRotation.z": ("obj", "rotation_quaternion", 2, -1, 4),
    "m_LocalRotation.w": ("obj", "rotation_quaternion", 0, 1, 4),
    "m_LocalScale.x": ("obj", "scale", 0, 1, 3),
    "m_LocalScale.y": ("obj", "scale", 2, 1, 3),
    "m_LocalScale.z": ("obj", "scale", 1, 1, 3),
    "localEulerAnglesRaw.x": ("obj", "rotation_euler", 0, -tau, 3),
    "localEulerAnglesRaw.y": ("obj", "rotation_euler", 2, -tau, 3),
    "localEulerAnglesRaw.z": ("obj", "rotation_euler", 1, -tau, 3),
    "m_Intensity": ("data", "energy", 0, light_power),
    "m_Color.r": ("data", "color", 0, 1),
    "m_Color.g": ("data", "color", 1, 1),
    "m_Color.b": ("data", "color", 2, 1),
    "m_Color.a": ("data", "color", 3, 1),
    # Unity Light.enabled — stored as custom prop for round-trip
    "m_Enabled": ("obj", '["mu_light_enabled"]', 0, 1),
}

# Unity AudioSource properties commonly animated in PartTools clips (AT_AUDIO_SOURCE=3)
_AUDIO_PROPS = {
    "m_Volume", "m_Pitch", "m_SpatialBlend", "panLevelCustomCurve",
    "m_Pan2D", "minDistance", "maxDistance", "dopplerLevel", "spread",
    "m_PlayOnAwake", "m_Loop", "m_Mute", "m_BypassEffects",
    "m_BypassListenerEffects", "m_BypassReverbZones", "reverbZoneMixCustomCurve",
    "volume", "pitch", "spatialBlend", "minVolume", "maxVolume", "rolloffFactor",
}


def _audio_prop_key(unity_prop: str) -> str:
    safe = unity_prop.replace(".", "_").replace("[", "_").replace("]", "_")
    return "mu_audio_" + safe


def audio_property(obj, prop):
    """Map AudioSource curve → Object ID property for lossless round-trip."""
    if not obj or not prop:
        return None
    base = prop.split(".")[0]
    if prop not in _AUDIO_PROPS and base not in _AUDIO_PROPS:
        # Accept unknown m_* floats that are not transform/light/material
        if not (prop.startswith("m_") or prop in ("volume", "pitch")):
            return None
        if prop.startswith("m_Local") or prop.startswith("m_Color") or prop.startswith("m_Intensity"):
            return None
    key = _audio_prop_key(prop)
    if key not in obj:
        try:
            obj[key] = 0.0
        except Exception:
            return None
    # RNA path for custom properties on Object
    return obj, f'["{key}"]', 0

vector_map = {
    "r": 0, "g": 1, "b": 2, "a":3,
    "x": 0, "y": 1, "z": 2, "w":3,  # shader props not read as quaternions
}

def property_index(properties, prop):
    for i, p in enumerate(properties):
        if p.name == prop:
            return i
    return None

def _parse_texture_anim_prop(prop):
    """Map Unity texture anim names → (tex_name, attr, rna_index).

    Supported forms:
      _MainTex.offset.x / .offset.y / .scale.x / .scale.y
      _MainTex_ST.r/.g/.b/.a  (scale.xy, offset.xy)
    ``_TexelSize`` is Unity texture metadata — not stored on MuTextureProperties.
    """
    if not prop:
        return None
    if len(prop) == 3 and prop[1] in ("offset", "scale") and prop[2] in ("x", "y"):
        return prop[0], prop[1], 0 if prop[2] == "x" else 1
    if len(prop) == 2 and prop[0].endswith("_ST") and prop[1] in vector_map:
        base = prop[0][:-3]  # strip "_ST"
        idx = vector_map[prop[1]]
        if idx < 2:
            return base, "scale", idx
        return base, "offset", idx - 2
    if len(prop) == 2 and prop[0].endswith("_TexelSize"):
        return None
    return None

def _ensure_texture_prop(mat, tex_name):
    props = mat.mumatprop.texture.properties
    if tex_name in props:
        return property_index(props, tex_name)
    item = props.add()
    item.name = tex_name
    try:
        item.scale = (1.0, 1.0)
        item.offset = (0.0, 0.0)
    except Exception:
        pass
    return property_index(props, tex_name)

def _shader_property_on_mat(mat, prop):
    if not mat or not hasattr(mat, "mumatprop"):
        return None
    mumat = mat.mumatprop
    # Texture offset/scale / _ST (before generic color/vector lookup)
    tex_parsed = _parse_texture_anim_prop(prop)
    if tex_parsed is not None:
        tex_name, attr, rna_index = tex_parsed
        prop_index = _ensure_texture_prop(mat, tex_name)
        if prop_index is None:
            return None
        path = "mumatprop.texture.properties[%d].%s" % (prop_index, attr)
        return mat, path, rna_index
    if len(prop) >= 1 and (prop[0].endswith("_TexelSize")
                           or (len(prop) >= 2 and prop[0].endswith("_ST"))):
        # Unmapped texture metadata (e.g. TexelSize) — quiet skip
        return None
    for subpath in ["color", "vector", "float2", "float3", "texture"]:
        propset = getattr(mumat, subpath)
        if prop[0] not in propset.properties:
            continue
        if subpath == "texture":
            # Bare texture name without .offset/.scale/_ST — nothing to animate
            return None
        if subpath[:5] == "float":
            rnaIndex = 0
        else:
            if len(prop) < 2:
                return None
            rnaIndex = vector_map[prop[1]]
        propIndex = property_index(propset.properties, prop[0])
        path = "mumatprop.%s.properties[%d].value" % (subpath, propIndex)
        return mat, path, rnaIndex
    return None

def shader_property(obj, prop):
    """Find animated shader prop on obj, its mesh children, or any loaded material."""
    prop = prop.split(".")
    if not obj:
        return None

    def materials_on(o):
        if o and type(getattr(o, "data", None)) == bpy.types.Mesh and o.data.materials:
            for mat in o.data.materials:
                if mat:
                    yield mat

    def search_obj(o, depth=0):
        for mat in materials_on(o):
            hit = _shader_property_on_mat(mat, prop)
            if hit:
                return hit
        if depth < 4 and o:
            for c in o.children:
                hit = search_obj(c, depth + 1)
                if hit:
                    return hit
            # Also look at siblings via parent (gimbal anim → body material)
            if depth == 0 and o.parent:
                for s in o.parent.children:
                    if s is o:
                        continue
                    hit = search_obj(s, depth + 1)
                    if hit:
                        return hit
        return None

    hit = search_obj(obj)
    if hit:
        return hit
    # Fallback: any material in the file that has this property (emissive
    # curves often target a transform whose mesh is a sibling/cousin).
    for mat in bpy.data.materials:
        hit = _shader_property_on_mat(mat, prop)
        if hit:
            return hit
    # Last resort: synthesize the color/texture prop on the nearest mesh
    # material so export keeps the curves (KSP animates _EmissiveColor even
    # when the material chunk did not list it; same for texture offset/_ST).
    if prop and prop[0].startswith("_"):
        tex_parsed = _parse_texture_anim_prop(prop)

        def _synth_and_hit(mat):
            if not mat or not hasattr(mat, "mumatprop"):
                return None
            if tex_parsed is not None:
                _ensure_texture_prop(mat, tex_parsed[0])
            elif prop[0] not in mat.mumatprop.color.properties:
                item = mat.mumatprop.color.properties.add()
                item.name = prop[0]
                try:
                    item.value = (1.0, 1.0, 1.0, 1.0)
                except Exception:
                    pass
            return _shader_property_on_mat(mat, prop)

        for mat in list(materials_on(obj)):
            hit = _synth_and_hit(mat)
            if hit:
                return hit
        if obj and obj.parent:
            for s in obj.parent.children:
                for mat in materials_on(s):
                    hit = _synth_and_hit(mat)
                    if hit:
                        return hit
        for mat in bpy.data.materials:
            hit = _synth_and_hit(mat)
            if hit:
                return hit
    return None

def _convert_empty_to_light(obj, light_type='SPOT'):
    """Blender Empties cannot hold a Light datablock (only Image/None).
    Replace the Empty with a LIGHT object, preserving hierarchy + props.
    Returns (light_datablock, new_object).
    """
    if obj.type == 'LIGHT' and obj.data:
        return obj.data, obj
    light = bpy.data.lights.new(obj.name + ".light", light_type)
    name = obj.name
    collections = list(obj.users_collection)
    parent = obj.parent
    parent_type = obj.parent_type
    parent_bone = obj.parent_bone
    matrix_local = obj.matrix_local.copy()
    children = [(c, c.matrix_local.copy()) for c in obj.children]
    custom = {}
    for k in obj.keys():
        if k == "_RNA_UI":
            continue
        try:
            custom[k] = obj[k]
        except Exception:
            pass
    # Preserve KSP object props when present
    tag = layer = None
    try:
        tag = obj.muproperties.tag
        layer = obj.muproperties.layer
    except Exception:
        pass

    new_obj = bpy.data.objects.new(name, light)
    for col in collections:
        try:
            col.objects.link(new_obj)
        except Exception:
            pass
    new_obj.parent = parent
    new_obj.parent_type = parent_type
    if parent_type == 'BONE' and parent_bone:
        new_obj.parent_bone = parent_bone
    new_obj.matrix_local = matrix_local
    for c, ml in children:
        c.parent = new_obj
        c.matrix_local = ml
    for k, v in custom.items():
        try:
            new_obj[k] = v
        except Exception:
            pass
    if tag is not None:
        try:
            new_obj.muproperties.tag = tag
            new_obj.muproperties.layer = layer
        except Exception:
            pass
    # Created only so Blender can host light anim curves — source .mu had no
    # Light component. Export must not emit MuLight (CockpitStandard etc.).
    new_obj["mu_synth_light"] = 1
    # Remove old empty (unlink first)
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:
        pass
    return light, new_obj


def create_fcurve(action, curve, propmap, datablock, rest_value=None):
    if datablock is None:
        print(f"WARNING: skip fcurve {propmap[0]!r} — no datablock")
        return None
    dp, ind, mult = propmap
    fps = bpy.context.scene.render.fps
    fc = fcurve_new(action, datablock, dp, index=ind)
    keys = list(curve.keys)
    # Unity AnimationCurve Clamp: before the first key the value is the *first
    # key's value*, not the GO bind pose. Padding with bind (deployed gear /
    # open hatch) made LandingGear Large/Medium/Small start with the wheel out
    # on a closed bay, then snap shut at the first key. Prefer first-key pad
    # (Unity Clamp). ``rest_value`` (bind) is only a fallback when the first
    # key is unreadable.
    frame0 = float(bpy.context.scene.frame_start)
    pad_value = None
    if keys and float(keys[0].time) > 1e-4:
        try:
            pad_value = float(keys[0].value) * float(mult)
        except Exception:
            pad_value = None
        if pad_value is None and rest_value is not None:
            try:
                pad_value = float(rest_value)
            except Exception:
                pad_value = None
    need_rest = pad_value is not None and math.isfinite(float(pad_value))
    fc.keyframe_points.add(len(keys) + (1 if need_rest else 0))

    def _safe_handle_delta(tangent, dist):
        """Unity stepped/constant keys often store ±Inf tangents — keep Blender finite."""
        if dist == 0 or not math.isfinite(dist):
            return 10.0, 0.0
        dx = dist * fps
        raw = tangent * dist * mult
        if not math.isfinite(raw) or abs(raw) > 1e6:
            return dx, 0.0
        return dx, raw

    def _write_kp(i, x, y, tan_in, tan_out, dist_in, dist_out):
        if not math.isfinite(x):
            x = float(i)
        if not math.isfinite(y):
            y = 0.0
        fc.keyframe_points[i].co = x, y
        fc.keyframe_points[i].handle_left_type = 'FREE'
        fc.keyframe_points[i].handle_right_type = 'FREE'
        dx, dy = _safe_handle_delta(tan_in, dist_in)
        fc.keyframe_points[i].handle_left = x - dx, y - dy
        dx, dy = _safe_handle_delta(tan_out, dist_out)
        fc.keyframe_points[i].handle_right = x + dx, y + dy

    off = 0
    if need_rest:
        _write_kp(0, frame0, float(pad_value), 0.0, 0.0, 0.0, 0.0)
        off = 1

    for i, key in enumerate(keys):
        x = key.time * fps + frame0
        y = key.value * mult
        dist_in = 0.0
        dist_out = 0.0
        tan_in = 0.0
        tan_out = 0.0
        if i > 0:
            dist_in = (key.time - keys[i - 1].time) / 3
            tan_in = key.tangent[0]
        elif need_rest:
            dist_in = key.time / 3
            tan_in = key.tangent[0]
        if i < len(keys) - 1:
            dist_out = (keys[i + 1].time - key.time) / 3
            tan_out = key.tangent[1]
        _write_kp(i + off, x, y, tan_in, tan_out, dist_in, dist_out)
    return fc

def _ensure_path_object(mu, mu_path):
    """Create stub MuObjects + Blender empties for clip paths missing in the .mu hierarchy."""
    if mu_path in mu.object_paths:
        return mu.object_paths[mu_path]
    parts = mu_path.split("/")
    # Find longest existing prefix
    parent = None
    parent_path = ""
    built = []
    for i, part in enumerate(parts):
        built.append(part)
        p = "/".join(built)
        if p in mu.object_paths:
            parent = mu.object_paths[p]
            parent_path = p
            continue
        # Need a new node under parent
        class _T:
            pass
        class _O:
            pass
        stub = _O()
        stub.transform = _T()
        stub.transform.name = part
        stub.transform.localPosition = Vector((0, 0, 0))
        stub.transform.localRotation = Quaternion((1, 0, 0, 0))
        stub.transform.localScale = Vector((1, 1, 1))
        stub.children = []
        stub.components = []
        stub.path = p
        stub.parent = parent
        stub.force_import = True
        stub.mu = mu
        col = getattr(mu, "collection", None) or bpy.context.scene.collection
        bobj = bpy.data.objects.new(part, None)
        bobj.empty_display_type = 'PLAIN_AXES'
        col.objects.link(bobj)
        if parent is not None and getattr(parent, "bobj", None):
            bobj.parent = parent.bobj
        elif parent is not None and getattr(parent, "armature_obj", None):
            bobj.parent = parent.armature_obj
        stub.bobj = bobj
        try:
            # Stubs keep clip curves for round-trip but must not affect
            # viewport framing / GIF motion bounds (stale capsule/hatch).
            bobj["mu_anim_stub"] = 1
            bobj.hide_render = True
            bobj.hide_viewport = True
        except Exception:
            pass
        mu.object_paths[p] = stub
        mu.objects.setdefault(part, stub)
        if parent is not None:
            if not hasattr(parent, "children") or parent.children is None:
                parent.children = []
            parent.children.append(stub)
        print(f"INFO: Created missing anim path node: {p}")
        parent = stub
        parent_path = p
    return mu.object_paths.get(mu_path)

def create_action(mu, path, clip, host=None):
    #print(clip.name)
    actions = {}
    bones = set()
    retargets = set()
    for curve in clip.curves:
        if not curve.keys:
            # Unity empty AnimationCurves evaluate to 0 and are common in
            # partial PartTools clips — nothing useful to put in Blender.
            # Keeping them would require sidecar metadata; skipping is safe.
            print("INFO: Curve has no keys (skipped)")
            continue
        path_retarget = False
        if not curve.path:
            mu_path = path
            # Self-targeted curves (path="") must use the Animation host.
            # Duplicate sibling names (RCSBlock Angled×4 RCSthruster) collide
            # in object_paths — looking up by path alone binds every clip to
            # the last sibling and drops glow on the other nozzles.
            if host is not None:
                muobj = host
            elif mu_path in mu.object_paths:
                muobj = mu.object_paths[mu_path]
            else:
                muobj = _ensure_path_object(mu, mu_path)
                if muobj is None:
                    if not hasattr(mu, "bad_paths"):
                        mu.bad_paths = set()
                    if mu_path not in mu.bad_paths:
                        mu.bad_paths.add(mu_path)
                        print("Unknown path: %s" % (mu_path))
                    continue
        else:
            # Curves are usually relative to the Animation host, but some
            # Squad parts store root-absolute paths — never double-prefix.
            rel = "/".join([path, curve.path]) if path else curve.path
            if rel in mu.object_paths:
                mu_path = rel
            elif curve.path in mu.object_paths:
                mu_path = curve.path
            else:
                mu_path = rel
            resolved = None
            base = curve.path.rstrip("/").split("/")[-1] if curve.path else ""
            if mu_path not in mu.object_paths:
                # Unity often uses a short relative name (e.g. "spotlight") while
                # the GO lives deeper (…/Lamp/spotlight). Resolve by unique
                # basename under the anim host before synthesizing stubs.
                # Multi-segment paths (capsule/hatch) must match as a path
                # suffix — basename-only binding retargets IVA hatch clips onto
                # the wrong GO (90° + huge mid-clip position keys).
                cpath = (curve.path or "").rstrip("/")
                multi = "/" in cpath
                if base:
                    under = [p for p in mu.object_paths
                             if (p == base or p.endswith("/" + base))
                             and (not path or p == path or p.startswith(path + "/"))]
                    if multi:
                        under = [p for p in under
                                 if p == cpath or p.endswith("/" + cpath)]
                    if len(under) == 1:
                        resolved = under[0]
                    else:
                        any_match = [p for p in mu.object_paths
                                     if p == base or p.endswith("/" + base)]
                        if multi:
                            any_match = [p for p in any_match
                                         if p == cpath or p.endswith("/" + cpath)]
                        if len(any_match) == 1:
                            resolved = any_match[0]
                if resolved:
                    mu_path = resolved
            if mu_path not in mu.object_paths:
                # Intermediate parents often missing in IVA .mu (clip path
                # capsule/hatch vs real GO mk1PodInternal/…/hatch). Fall back
                # to a unique leaf under the anim host before synthesizing a
                # hidden stub (stubs eat hatch rotation while the mesh sits
                # still).
                if not resolved and base:
                    leaf = [p for p in mu.object_paths
                            if (p == base or p.endswith("/" + base))
                            and (not path or p == path or p.startswith(path + "/"))]
                    if len(leaf) == 1:
                        resolved = leaf[0]
                    elif len(leaf) > 1:
                        meshish = []
                        for p in leaf:
                            mo = mu.object_paths.get(p)
                            if mo is None:
                                continue
                            if (hasattr(mo, "shared_mesh")
                                    or hasattr(mo, "renderer")
                                    or (getattr(mo, "bobj", None) is not None
                                        and getattr(mo.bobj, "type", None) == "MESH")):
                                meshish.append(p)
                        if len(meshish) == 1:
                            resolved = meshish[0]
                    if resolved:
                        mu_path = resolved
                        # Alias so later curves / stubs find the real GO
                        try:
                            mu.object_paths[rel] = mu.object_paths[resolved]
                        except Exception:
                            pass
                if resolved:
                    # Clip authored under a missing parent (capsule/…). Leaf
                    # mesh has a different bind pose — rebase rotation later.
                    path_retarget = True
            if mu_path not in mu.object_paths:
                # Synthesize missing hierarchy nodes referenced by clips (lights,
                # shake empties, colliders with odd names, etc.)
                muobj = _ensure_path_object(mu, mu_path)
                if muobj is None:
                    if not hasattr(mu, "bad_paths"):
                        mu.bad_paths = set()
                    if mu_path not in mu.bad_paths:
                        mu.bad_paths.add(mu_path)
                        print("Unknown path: %s" % (mu_path))
                    continue
            else:
                muobj = mu.object_paths[mu_path]
            # Multi-segment clip path bound to a GO whose hierarchy path does
            # not end with that clip path (capsule/hatch → …/hatch): retarget.
            cpath = (curve.path or "").rstrip("/")
            if "/" in cpath and muobj is not None:
                real = (getattr(muobj, "path", None) or mu_path or "").rstrip("/")
                if real != cpath and not real.endswith("/" + cpath):
                    path_retarget = True
            if path_retarget:
                # Loc/scale keys are in the missing parent's local space
                # (IVA hatch loc ≈ (−4,−1,0.7) vs mesh (0,0.66,0.43)). Applying
                # them teleports the door; keep mesh bind for those channels.
                prop = curve.property or ""
                if prop.startswith("m_LocalPosition") or prop.startswith(
                        "m_LocalScale"):
                    continue
                try:
                    muobj._mu_anim_retarget = True
                except Exception:
                    pass
        dppref = ""
        use_pose_bone = False
        if hasattr(muobj, "bone"):
            arm = getattr(muobj, "armature", None)
            arm_obj = getattr(arm, "armature_obj", None) if arm else None
            if not isinstance(arm_obj, bpy.types.Object):
                # owner.armature_obj path (set after create_armature)
                owner = getattr(muobj, "owner", None)
                arm_obj = getattr(owner, "armature_obj", None) if owner else None
            if isinstance(arm_obj, bpy.types.Object):
                obj = arm_obj
                dppref = f'pose.bones["{muobj.bone}"].'
                use_pose_bone = True
            elif hasattr(muobj, "bobj") and muobj.bobj:
                # Fallback: animate the hierarchy object when no armature exists
                obj = muobj.bobj
            else:
                # Last resort: create a temporary empty so curves are not dropped
                print("Warning: No armature_obj for bone at path: %s "
                      "(creating hierarchy empty)" % (mu_path))
                col = getattr(mu, "collection", None) or bpy.context.scene.collection
                obj = bpy.data.objects.new(muobj.transform.name, None)
                col.objects.link(obj)
                muobj.bobj = obj
                muobj.force_import = True
        elif hasattr(muobj, "bobj") and muobj.bobj:
            obj = muobj.bobj
        else:
            # Animated node without mesh/collider — synthesize an empty host
            print("INFO: No blender object at path: %s (creating empty)" % (mu_path))
            col = getattr(mu, "collection", None) or bpy.context.scene.collection
            obj = bpy.data.objects.new(muobj.transform.name, None)
            col.objects.link(obj)
            muobj.bobj = obj
        if curve.property[:-2] == "localEulerAnglesRaw":
            obj.rotation_mode = 'YXZ'
        is_shader_curve = False
        is_audio_curve = False
        if curve.property not in property_map:
            sp = shader_property(obj, curve.property)
            if not sp:
                ap = audio_property(obj, curve.property)
                if not ap:
                    print("%s: Unknown property: %s" % (mu_path, curve.property))
                    continue
                obj, dp, rnaIndex = ap
                propmap = dp, rnaIndex, 1
                subpath = "obj"
                is_audio_curve = True
                use_pose_bone = False
                dppref = ""
            else:
                obj, dp, rnaIndex = sp
                propmap = dp, rnaIndex, 1
                subpath = "obj"
                is_shader_curve = True
                # Material ID fcurves must NOT use pose.bones[] prefixes
                use_pose_bone = False
                dppref = ""
        else:
            propmap = property_map[curve.property]
            subpath, propmap = propmap[0], propmap[1:]
        fullpropmap = (dppref + propmap[0],) + propmap[1:3]
        if subpath != "obj":
            data = getattr(obj, subpath, None)
            # Light curves need a Light datablock. Blender Empties cannot hold
            # lights (Object.data only accepts Image/None for EMPTY).
            if subpath == "data" and obj.type == 'EMPTY':
                light, new_obj = _convert_empty_to_light(obj, 'SPOT')
                if getattr(muobj, "bobj", None) is obj:
                    muobj.bobj = new_obj
                obj = new_obj
                if "mu_light_enabled" not in obj:
                    obj["mu_light_enabled"] = 1.0
                data = light
            obj = data
        if obj is None:
            print(f"{mu_path}: skip curve {curve.property} — no datablock")
            continue
        if curve.property == "m_Enabled":
            # Do NOT assign to `host` — that parameter is the MuAnimation owner
            # MuObject for path="" curves (spotLightMk1 emissive). Overwriting
            # it with a Blender Object caused AttributeError on .transform.
            try:
                light_obj = muobj.bobj if getattr(muobj, "bobj", None) else None
                if light_obj is not None and "mu_light_enabled" not in light_obj:
                    light_obj["mu_light_enabled"] = 1.0
            except Exception:
                pass
        host_name = (muobj.bobj.name if getattr(muobj, "bobj", None)
                     else getattr(obj, "name", "obj"))
        objname = ".".join([host_name, subpath])
        name = objname
        actpath = "/".join([curve.path, name])
        if actpath not in actions:
            actions[actpath] = bpy.data.actions.new(name), obj
        act, obj = actions[actpath]
        if is_shader_curve or is_audio_curve:
            # Preserve Unity renderer-relative path (shared materials may be
            # attached to a different mesh than the clip path, e.g. obj_gimbal).
            try:
                act["mu_unity_curve_path"] = curve.path or ""
            except Exception:
                pass
            # Preserve exact Unity property name (offset.x vs _ST.r, etc.)
            try:
                act["mu_uprop:%s:%d" % (fullpropmap[0], fullpropmap[1])] = (
                    curve.property)
            except Exception:
                pass
            if is_audio_curve:
                try:
                    act["mu_atype:%s:%d" % (fullpropmap[0], fullpropmap[1])] = 3
                except Exception:
                    pass
        rest_value = None
        if (not use_pose_bone and not is_shader_curve and not is_audio_curve
                and hasattr(muobj, "transform") and muobj.transform is not None):
            try:
                dp0 = propmap[0]
                idx = int(propmap[1])
                xform = muobj.transform
                if dp0 == "location":
                    rest_value = float(xform.localPosition[idx])
                elif dp0 == "rotation_quaternion":
                    rest_value = float(xform.localRotation[idx])
                elif dp0 == "scale":
                    rest_value = float(xform.localScale[idx])
            except Exception:
                rest_value = None
        fcurve = create_fcurve(act, curve, fullpropmap, obj, rest_value=rest_value)
        if fcurve is None:
            continue
        # Only transform channels (location/rotation/scale) carry a 4th
        # "component count" entry. Light/material props are shorter tuples —
        # never feed them into bone-space fcurve rebasing (turboJet crash).
        if use_pose_bone and len(propmap) >= 4:
            if not hasattr(muobj, "fcurves"):
                muobj.fcurves = {}
            if propmap[0] not in muobj.fcurves:
                muobj.fcurves[propmap[0]] = [None] * propmap[3]
            muobj.fcurves[propmap[0]][propmap[1]] = fcurve
            bones.add(muobj)
        elif (getattr(muobj, "_mu_anim_retarget", False)
                and not use_pose_bone and not is_shader_curve
                and not is_audio_curve and len(propmap) >= 4
                and propmap[0] == "rotation_quaternion"):
            if not hasattr(muobj, "fcurves"):
                muobj.fcurves = {}
            if propmap[0] not in muobj.fcurves:
                muobj.fcurves[propmap[0]] = [None] * propmap[3]
            muobj.fcurves[propmap[0]][propmap[1]] = fcurve
            retargets.add(muobj)
    for muobj in bones:
        xform = muobj.transform
        rrot = muobj.relRotation
        if "location" in muobj.fcurves:
            location = muobj.fcurves["location"]
            lloc = Vector(muobj.transform.localPosition)
            if None in location:
                print("INFO: Skipping incomplete location fcurve set")
            else:
                n = min(len(location[i].keyframe_points) for i in range(3))
                if n == 0:
                    print("INFO: Skipping empty location fcurve set")
                else:
                    if (len(location[0].keyframe_points) != n
                            or len(location[1].keyframe_points) != n
                            or len(location[2].keyframe_points) != n):
                        print("INFO: Location fcurve key counts differ — "
                              f"converting first {n} keys")
                    for i in range(n):
                        def transformkey(kval, i=i):
                            xk = getattr(location[0].keyframe_points[i], kval)
                            yk = getattr(location[1].keyframe_points[i], kval)
                            zk = getattr(location[2].keyframe_points[i], kval)
                            loc = Vector((xk.y, yk.y, zk.y))
                            loc = rrot @ (loc - lloc)
                            (xk.y, yk.y, zk.y) = loc
                        transformkey("co")
                        transformkey("handle_left")
                        transformkey("handle_right")
        if "rotation_quaternion" in muobj.fcurves:
            rotation = muobj.fcurves["rotation_quaternion"]
            lrot = Quaternion(muobj.transform.localRotation).inverted()
            if None in rotation:
                print("INFO: Skipping incomplete rotation fcurve set")
            elif ((len(rotation[0].keyframe_points)
                  != len(rotation[1].keyframe_points))
                  or (len(rotation[0].keyframe_points)
                      != len(rotation[2].keyframe_points))
                  or (len(rotation[0].keyframe_points)
                      != len(rotation[3].keyframe_points))):
                print("INFO: Skipping mismatched rotation fcurve set")
            else:
                for i in range(len(rotation[0].keyframe_points)):
                    def rotkey(kval):
                        wk = getattr(rotation[0].keyframe_points[i], kval)
                        xk = getattr(rotation[1].keyframe_points[i], kval)
                        yk = getattr(rotation[2].keyframe_points[i], kval)
                        zk = getattr(rotation[3].keyframe_points[i], kval)
                        q = Quaternion((wk.y, xk.y, yk.y, zk.y))
                        q = lrot @ q
                        (wk.y, xk.y, yk.y, zk.y) = q
                    rotkey("co")
                    rotkey("handle_left")
                    rotkey("handle_right")
    # IVA hatch etc.: clip rest is identity under missing capsule/; mesh bind
    # is ~90°. Rebase so t=0 keeps the flush door and motion stays relative.
    for muobj in retargets:
        rotation = (getattr(muobj, "fcurves", None) or {}).get(
            "rotation_quaternion")
        if not rotation or None in rotation:
            continue
        if ((len(rotation[0].keyframe_points)
              != len(rotation[1].keyframe_points))
              or (len(rotation[0].keyframe_points)
                  != len(rotation[2].keyframe_points))
              or (len(rotation[0].keyframe_points)
                  != len(rotation[3].keyframe_points))):
            continue
        try:
            mesh_rest = Quaternion(muobj.transform.localRotation)
        except Exception:
            continue
        try:
            w0 = rotation[0].keyframe_points[0].co.y
            x0 = rotation[1].keyframe_points[0].co.y
            y0 = rotation[2].keyframe_points[0].co.y
            z0 = rotation[3].keyframe_points[0].co.y
            clip_rest = Quaternion((w0, x0, y0, z0))
            if clip_rest.magnitude < 1e-8:
                continue
            clip_rest.normalize()
            delta = mesh_rest @ clip_rest.inverted()
        except Exception:
            continue
        for i in range(len(rotation[0].keyframe_points)):
            def retarget_rotkey(kval, i=i, delta=delta):
                wk = getattr(rotation[0].keyframe_points[i], kval)
                xk = getattr(rotation[1].keyframe_points[i], kval)
                yk = getattr(rotation[2].keyframe_points[i], kval)
                zk = getattr(rotation[3].keyframe_points[i], kval)
                q = Quaternion((wk.y, xk.y, yk.y, zk.y))
                q = delta @ q
                (wk.y, xk.y, yk.y, zk.y) = q
            retarget_rotkey("co")
            retarget_rotkey("handle_left")
            retarget_rotkey("handle_right")
        try:
            print("INFO: Rebased retargeted rotation onto mesh bind (%s)"
                  % (getattr(muobj, "path", "?"),))
        except Exception:
            pass
    for name in actions:
        act, obj = actions[name]
        # Remember which Mu hierarchy node owned this Animation component so
        # export can restore multiple MuAnimation hosts (nested clips).
        try:
            act["mu_anim_host"] = path
            act["mu_clip_name"] = clip.name
        except Exception:
            pass
        # Unique Blender object name — required when siblings share a Unity
        # path (RCSBlock Angled×4 RCSthruster). Export groups by this.
        try:
            bobj = getattr(host, "bobj", None) if host is not None else None
            if bobj is not None and getattr(bobj, "name", None):
                act["mu_anim_bobj"] = bobj.name
        except Exception:
            pass
        track, _strip = push_action_to_nla(obj, act, clip.name)
        # NlaTrack may not support IDProperties (Blender 5); Action holds host.

def create_object_paths(mu):
    def recurse (mu, obj, parent_names, parent):
        obj.parent = parent
        obj.mu = mu
        name = obj.transform.name
        parent_names.append(name)
        obj.path = "/".join(parent_names)
        mu.objects[name] = obj
        mu.object_paths[obj.path] = obj
        for child in obj.children:
            recurse(mu, child, parent_names, obj)
        parent_names.pop()
    mu.objects = {}
    mu.object_paths = {}
    mu.bad_paths = set()
    recurse(mu, mu.obj, [], None)

def _preview_clip_score(name):
    """Lower = better default viewport clip (stowed / closed pose).

    KSP often ships Deploy then Running/Drill/Grab. NLA push order is not
    reliable (MiniDrill pushes Drill before Deploy), so prefer deploy/extend
    over operate / grab / glow clips.
    """
    n = (name or "").lower().replace(" ", "_")
    if any(k in n for k in ("deploy", "extend", "retract", "unfold", "open", "close")):
        return 0
    if any(k in n for k in ("glow", "heat", "emissive", "light", "noozle", "nozzle")):
        return 1
    if "servo" in n or n in ("servooperate", "servo_operate"):
        return 2
    if any(k in n for k in ("grab", "operate", "running", "spin", "rotate", "loop")):
        return 3
    # Bare "Drill" / "*_drill" without deploy → treat as secondary (operate/FX)
    if n == "drill" or n.endswith("_drill") or n.startswith("drill_"):
        return 3
    return 1


def ordered_clip_names(names):
    """Stable Deploy → glow/heat → operate/grab order for GIF / NLA playback."""
    uniq = []
    seen = set()
    for n in names:
        if not n or n in seen:
            continue
        seen.add(n)
        uniq.append(n)
    return sorted(uniq, key=lambda n: (_preview_clip_score(n), uniq.index(n)))


def finalize_animation_preview():
    """Blender 5 keeps the last pushed Action as ``ad.action``.

    Clear the active Action and unmute the best preview clip (prefer Deploy /
    stowed at ``frame_start``), mute the rest (e.g. Drill_Running).
    """
    try:
        bpy.context.scene.frame_set(int(bpy.context.scene.frame_start))
    except Exception:
        pass

    def _finalize(id_data):
        ad = getattr(id_data, "animation_data", None)
        if not ad or not ad.nla_tracks:
            return
        names = [t.name for t in ad.nla_tracks if t.name]
        if not names:
            return
        keep = ordered_clip_names(names)[0]
        for track in ad.nla_tracks:
            track.mute = (track.name != keep)
        try:
            ad.action = None
        except Exception:
            pass

    for obj in bpy.data.objects:
        _finalize(obj)
    for mat in bpy.data.materials:
        _finalize(mat)
    # Engine / heat: restore EmissionMap (undo white-bypass) + soft blackbody
    try:
        from ..shader.shader import (
            ensure_heat_emissive_map_visible,
            enhance_heat_blackbody_preview,
        )
    except Exception:
        ensure_heat_emissive_map_visible = None
        enhance_heat_blackbody_preview = None
    if ensure_heat_emissive_map_visible:
        for mat in bpy.data.materials:
            ad = getattr(mat, "animation_data", None)
            if not ad or not ad.nla_tracks:
                continue
            heatish = False
            for track in ad.nla_tracks:
                n = (track.name or "").lower()
                if any(k in n for k in ("heat", "emissive", "glow", "noozle", "nozzle")):
                    heatish = True
                    break
            if not heatish:
                continue
            try:
                ensure_heat_emissive_map_visible(mat)
            except Exception:
                pass
            if enhance_heat_blackbody_preview:
                try:
                    enhance_heat_blackbody_preview(mat)
                except Exception:
                    pass
