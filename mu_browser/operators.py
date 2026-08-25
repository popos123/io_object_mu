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
                thumbnails.icon_id_for_part(
                    entry.mu_path, entry.name, ensure=False
                )
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
        from . import thumbnails
        from ..preferences.preferences import Preferences

        gd = (Preferences().GameData or "").strip()
        if not gd or not os.path.isdir(gd):
            self.report({"ERROR"}, "Set GameData path in Tool > Options")
            return {"CANCELLED"}
        catalog.invalidate_cache()
        catalog.scan_gamedata(gd, force=True)
        try:
            thumbnails.invalidate_ksp_thumbs_index()
            thumbnails.build_ksp_thumbs_index(gd, force=True)
        except Exception:
            pass
        refresh_part_list(context)
        n = len(context.window_manager.ksp_mu_browser.parts)
        self.report({"INFO"}, "Parts in category: %d" % n)
        return {"FINISHED"}


def _thumb_queue(br, *, force: bool, max_count: int):
    """All parts in category (up to max_count).

    Each entry: (mu_path, part_name, need_generate)
    need_generate=False → only load icon (still advances progress).
    """
    from . import thumbnails

    queue = []
    for item in br.parts:
        path = item.mu_path
        if not path:
            continue
        need = True
        if not force:
            existing = None
            try:
                existing = thumbnails._resolve_cache_path(path, item.name)
            except Exception:
                existing = None
            if existing:
                need = False
        queue.append((path, item.name or "", need))
        if len(queue) >= int(max_count):
            break
    return queue


def _progress_begin(context, total, title):
    """Returns (cm, handle) — MUST keep cm alive until end."""
    try:
        from ..import_mu.progress_util import mu_progress_bar
        cm = mu_progress_bar(context, total=total, title=title)
        if hasattr(cm, "__enter__"):
            handle = cm.__enter__()
            return cm, handle
        return cm, cm
    except Exception:
        return None, None


def _progress_update(handle, current, total, title="Thumbnails"):
    if handle is None:
        return
    for name in ("update", "step", "set", "set_progress", "tick"):
        fn = getattr(handle, name, None)
        if callable(fn):
            try:
                fn(current)
                return
            except TypeError:
                try:
                    fn(current, total)
                    return
                except Exception:
                    pass
            except Exception:
                pass
    for attr, val in (("current", current), ("progress", current), ("value", current)):
        if hasattr(handle, attr):
            try:
                setattr(handle, attr, val)
            except Exception:
                pass


def context_areas_safe():
    try:
        return list(bpy.context.screen.areas)
    except Exception:
        return []


def _progress_end(cm, handle=None):
    if cm is None:
        return
    if hasattr(cm, "__exit__"):
        try:
            cm.__exit__(None, None, None)
            return
        except Exception:
            pass
    for name in ("finish", "close", "done", "end"):
        fn = getattr(cm, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


class _ThumbBatchBase(bpy.types.Operator):
    """Shared modal batch thumbnail generator with progress bar."""
    bl_options = {"REGISTER"}

    max_count: IntProperty(name="Max", default=200, min=1, max=500)
    force: BoolProperty(default=False)

    _queue = None
    _done = 0
    _index = 0
    _timer = None
    _progress = None
    _progress_cm = None
    _title = "Thumbnails"

    def invoke(self, context, event):
        br = context.window_manager.ksp_mu_browser
        self._queue = _thumb_queue(
            br, force=bool(self.force), max_count=int(self.max_count)
        )
        self._index = 0
        self._done = 0

        if not self._queue:
            self.report({"INFO"}, "No parts in category")
            return {"FINISHED"}

        need_any = any(t[2] for t in self._queue)
        if not need_any and not self.force:
            from . import thumbnails
            for path, name, _need in self._queue:
                try:
                    thumbnails.icon_id_for_part(path, name, ensure=False)
                except Exception:
                    pass
            self.report({"INFO"}, "No thumbnails to generate")
            return {"FINISHED"}

        self._title = "Regen Thumbnails" if self.force else "Generate Thumbnails"
        self._progress_cm, self._progress = _progress_begin(context, len(self._queue), self._title)

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._cleanup(context)
            self.report({"WARNING"}, "Cancelled (%d done)" % self._done)
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        from . import thumbnails

        if self._index >= len(self._queue):
            self._cleanup(context)
            br = context.window_manager.ksp_mu_browser
            for item in br.parts:
                if item.mu_path:
                    try:
                        thumbnails.icon_id_for_part(
                            item.mu_path, item.name, ensure=False
                        )
                    except Exception:
                        pass
            try:
                for area in context_areas_safe():
                    area.tag_redraw()
            except Exception:
                pass
            self.report({"INFO"}, "%s: %d" % (self._title, self._done))
            return {"FINISHED"}

        path, name, need = self._queue[self._index]
        self._index += 1
        try:
            if need or self.force:
                result = thumbnails.generate_thumbnail(path, name, force=bool(self.force))
                if result:
                    thumbnails.icon_id_for_part(path, name, ensure=False)
                    self._done += 1
            else:
                thumbnails.icon_id_for_part(path, name, ensure=False)
        except Exception as e:
            print("[mu_thumb] batch error:", e)

        _progress_update(self._progress, self._index, len(self._queue), self._title)
        try:
            for area in context_areas_safe():
                area.tag_redraw()
        except Exception:
            pass
        return {"RUNNING_MODAL"}

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer is not None:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        _progress_end(self._progress_cm, self._progress)
        self._progress_cm = None
        self._progress = None


class KSPMU_OT_MuBrowserGenThumbs(_ThumbBatchBase):
    """Generate only missing thumbnails for the current category."""
    bl_idname = "object.ksp_mu_browser_gen_thumbs"
    bl_label = "Generate Thumbnails"

    force: BoolProperty(default=False)


class KSPMU_OT_MuBrowserRegenThumbs(_ThumbBatchBase):
    """Force-regenerate thumbnails for the current category."""
    bl_idname = "object.ksp_mu_browser_regen_thumbs"
    bl_label = "Regenerate Thumbnails"

    force: BoolProperty(default=True)


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
        try:
            for area in context_areas_safe():
                area.tag_redraw()
        except Exception:
            pass
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
            try:
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
    KSPMU_OT_MuBrowserRegenThumbs,
    KSPMU_OT_MuBrowserSelect,
    KSPMU_OT_MuBrowserImportPart,
)