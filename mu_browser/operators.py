# vim:ts=4:et
# <pep8 compliant>

from __future__ import annotations

import os

import bpy
from bpy.props import StringProperty, BoolProperty, IntProperty
from bpy_extras.io_utils import ImportHelper


def refresh_part_list(context):
    from . import catalog
    from . import thumbnails

    wm = context.window_manager
    br = wm.ksp_mu_browser
    catalog.scan_gamedata()
    cat = br.category or "Inne"
    filt = (br.filter or "").strip().lower()
    br.parts.clear()
    for entry in catalog.parts_in_category(cat):
        if filt:
            blob = "%s %s %s" % (entry.name, entry.title, entry.manufacturer)
            if filt not in blob.lower():
                continue
        item = br.parts.add()
        item.name = entry.name
        item.title = entry.title
        item.category = entry.category
        item.mu_path = entry.mu_path
        item.cfg_path = entry.cfg_path
        item.attach_rules = ",".join(str(x) for x in (entry.attach_rules or ()))
        if br.thumbs_pending and entry.mu_path:
            try:
                thumbnails.icon_id_for_part(entry.mu_path, ensure=False)
            except Exception:
                pass

    if hasattr(br, "grid_rows"):
        br.grid_rows.clear()
        n = len(br.parts)
        for start in range(0, n, 3):
            grow = br.grid_rows.add()
            grow.i0 = start
            grow.i1 = start + 1 if start + 1 < n else -1
            grow.i2 = start + 2 if start + 2 < n else -1
        if n <= 0:
            br.grid_rows_index = 0
            br.parts_index = 0
        else:
            if br.parts_index >= n:
                br.parts_index = n - 1
            br.grid_rows_index = min(br.parts_index // 3, max(0, len(br.grid_rows) - 1))


class KSPMU_OT_MuBrowserRefresh(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_refresh"
    bl_label = "Refresh Parts"
    bl_options = {"REGISTER"}

    force: BoolProperty(default=True)

    def execute(self, context):
        from . import catalog
        from ..preferences.preferences import Preferences

        gd = (Preferences().GameData or "").strip()
        if not gd or not os.path.isdir(gd):
            self.report({"ERROR"}, "Set GameData path in Tool > Options")
            return {"CANCELLED"}
        catalog.invalidate_cache()
        catalog.scan_gamedata(gd, force=True)
        refresh_part_list(context)
        n = len(context.window_manager.ksp_mu_browser.parts)
        self.report({"INFO"}, "Parts in category: %d" % n)
        return {"FINISHED"}


class KSPMU_OT_MuBrowserGenThumbs(bpy.types.Operator):
    """Generate missing thumbnails for the current category (lazy batch)."""
    bl_idname = "object.ksp_mu_browser_gen_thumbs"
    bl_label = "Generate Thumbnails"
    bl_options = {"REGISTER"}

    max_count: IntProperty(name="Max", default=200, min=1, max=300)

    def execute(self, context):
        from . import thumbnails

        br = context.window_manager.ksp_mu_browser
        done = 0
        for item in br.parts:
            if done >= int(self.max_count):
                break
            path = item.mu_path
            if not path:
                continue
            cache = thumbnails._cache_path(path)
            if os.path.isfile(cache) and os.path.getsize(cache) > 64:
                thumbnails.icon_id_for_part(path, ensure=False)
                continue
            if thumbnails.generate_thumbnail(path):
                thumbnails.icon_id_for_part(path, ensure=False)
                done += 1
        self.report({"INFO"}, "Generated %d thumbnails" % done)
        return {"FINISHED"}


class KSPMU_OT_MuBrowserSelect(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_select"
    bl_label = "Select Part"
    bl_description = "Select this part in the browser"
    bl_options = {"INTERNAL"}

    index: IntProperty(default=0)

    def execute(self, context):
        br = context.window_manager.ksp_mu_browser
        n = len(br.parts)
        if n <= 0:
            return {"CANCELLED"}
        i = max(0, min(int(self.index), n - 1))
        br.parts_index = i
        if hasattr(br, "grid_rows_index"):
            br.grid_rows_index = i // 3
        return {"FINISHED"}


class KSPMU_OT_MuBrowserImportPart(bpy.types.Operator):
    bl_idname = "object.ksp_mu_browser_import_part"
    bl_label = "Import Part"
    bl_options = {"REGISTER", "UNDO"}

    part_name: StringProperty(default="")

    def execute(self, context):
        from . import catalog
        from . import snap

        br = context.window_manager.ksp_mu_browser
        name = self.part_name
        if not name:
            if 0 <= br.parts_index < len(br.parts):
                name = br.parts[br.parts_index].name
        entry = catalog.find_part(name)
        if entry is None or not entry.mu_path:
            self.report({"ERROR"}, "Part not found")
            return {"CANCELLED"}

        collection = context.view_layer.active_layer_collection.collection
        try:
            from ..import_mu.progress_util import mu_progress_bar
            from ..import_mu.import_mu import import_mu as import_mu_file
            with mu_progress_bar(context, total=100, title="MU Import"):
                ret = import_mu_file(collection, entry.mu_path, False, False)
            root = ret[0] if isinstance(ret, tuple) else ret
        except Exception as e:
            self.report({"ERROR"}, "Import failed: %s" % e)
            return {"CANCELLED"}
        # Force the entire imported hierarchy to the world origin.
        try:
            import mathutils
            root.matrix_world = mathutils.Matrix.Identity(4)
        except Exception as e:
            print("[MU Browser] Failed to reset root:", e)

        try:
            root["ksp_part_name"] = entry.name
            root["ksp_part_category"] = entry.category
            root["ksp_attach_rules"] = ",".join(str(x) for x in entry.attach_rules)
            root["ksp_mu_path"] = entry.mu_path
        except Exception:
            pass

        if br.like_ksp:
            snap.prepare_part_for_editor(root)
            #snap.ensure_snap_handler()
            try:
                #cursor = context.scene.cursor.location
                #root.location = cursor.copy()
                root.location = (0.0, 0.0, 0.0)
            except Exception:
                pass
            try:
                bpy.ops.object.ksp_mu_like_ksp_place("INVOKE_DEFAULT")
            except Exception:
                pass

        for o in context.scene.objects:
            try:
                o.select_set(False)
            except Exception:
                pass
        try:
            context.view_layer.objects.active = root
            root.select_set(True)
        except Exception:
            pass
        self.report({"INFO"}, "Imported %s" % (entry.title or entry.name))
        return {"FINISHED"}


classes_to_register = (
    KSPMU_OT_MuBrowserRefresh,
    KSPMU_OT_MuBrowserGenThumbs,
    KSPMU_OT_MuBrowserSelect,
    KSPMU_OT_MuBrowserImportPart,
)