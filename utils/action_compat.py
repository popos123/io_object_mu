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
"""Blender 4.x / 5.x Action FCurve compatibility helpers.

Blender 5.0 removed legacy ``action.fcurves``; FCurves live in layered
channelbags. Prefer ``fcurve_ensure_for_datablock`` when creating keys.
"""

import bpy

try:
    from bpy_extras import anim_utils
except ImportError:
    anim_utils = None


def iter_action_fcurves(action):
    """Yield all FCurves on an Action (legacy or layered)."""
    if action is None:
        return
    if hasattr(action, "fcurves"):
        for fc in action.fcurves:
            yield fc
        return
    # Blender 5.x layered actions
    if anim_utils is not None:
        for slot in getattr(action, "slots", []):
            bag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if bag is not None:
                for fc in bag.fcurves:
                    yield fc
        return
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for slot in getattr(action, "slots", []):
                bag = strip.channelbag(slot) if hasattr(strip, "channelbag") else None
                if bag is not None:
                    for fc in bag.fcurves:
                        yield fc


def count_action_fcurves(action):
    return sum(1 for _ in iter_action_fcurves(action))


def ensure_action_assigned(datablock, action):
    """Assign action to datablock so fcurve_ensure_for_datablock can work."""
    if not hasattr(datablock, "animation_data"):
        return
    if not datablock.animation_data:
        datablock.animation_data_create()
    ad = datablock.animation_data
    if ad.action != action:
        ad.action = action
    # Blender 5: ensure a slot is selected when available
    if hasattr(ad, "action_slot") and hasattr(action, "slots"):
        if action.slots and getattr(ad, "action_slot", None) is None:
            try:
                ad.action_slot = action.slots[0]
            except Exception:
                pass


def fcurve_new(action, datablock, data_path, index=0):
    """Create (or ensure) an FCurve on action for datablock."""
    if datablock is None:
        raise ValueError("fcurve_new: datablock is None "
                         f"(data_path={data_path!r})")
    if hasattr(action, "fcurves"):
        return action.fcurves.new(data_path=data_path, index=index)
    # Blender 5.x
    ensure_action_assigned(datablock, action)
    return action.fcurve_ensure_for_datablock(
        datablock, data_path, index=index)


def push_action_to_nla(obj, action, track_name):
    """Create an NLA track/strip for action on obj (Blender 4/5 safe)."""
    if not obj.animation_data:
        obj.animation_data_create()
    ad = obj.animation_data
    # Ensure action is known to the ID before NLA push
    ensure_action_assigned(obj, action)
    track = ad.nla_tracks.new()
    track.name = track_name
    start = int(bpy.context.scene.frame_start)
    strip = None
    try:
        strip = track.strips.new(action.name, start, action)
    except TypeError:
        try:
            strip = track.strips.new(action.name, float(start), action)
        except Exception as e:
            print(f"WARNING: NLA strip create failed on {obj.name}: {e}")
    except Exception as e:
        print(f"WARNING: NLA strip create failed on {obj.name}: {e}")
    if strip is None:
        # Keep the action assigned so export can still find it
        ad.action = action
        return track, None
    try:
        if getattr(strip, "action", None) is None:
            strip.action = action
    except Exception:
        pass
    # Blender 5 slotted actions: clearing ad.action can drop the strip.
    # Only clear on older Blender where NLA-only playback was the norm.
    if bpy.app.version < (5, 0, 0):
        try:
            ad.action = None
        except Exception:
            pass
    return track, strip
