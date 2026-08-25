# vim:ts=4:et
# <pep8 compliant>

import os
import bpy

from . import thumbnails

_THUMB_SCALE = 7.5 # Main thumb (7.5X20px)
_THUMB_SCALE_POPUP = 5.0 # Menu thumbs (5x20px)

class VIEW3D_PT_mu_part_browser(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MU"
    bl_label = "Parts (.mu)"
    bl_order = 10

    def draw(self,context,):
        layout = self.layout
        try:
            from ..import_mu.progress_util import (draw_mu_panel_progress,)
            draw_mu_panel_progress(layout)
        except Exception:
            pass
        wm = context.window_manager
        br = getattr(wm, "ksp_mu_browser", None,)

        if br is None:
            layout.label(
                text="Browser not registered", 
                icon="ERROR",
            )
            return
        layout.prop(
            br, 
            "like_ksp", 
            text="like KSP", 
            icon="SNAP_ON",
        )

        if br.like_ksp:
            box = layout.box()
            box.label(
                text="Nodes snap + attachRules", 
                icon="INFO",
            )

            box.label(
                text=("Import places a ghost under the cursor")
            )
        try:
            from ..preferences.preferences import (Preferences)

            gd = (Preferences().GameData or "").strip()

        except Exception:
            gd = ""
        if not gd:
            layout.label(
                text=("Set GameData in Tool > Options"),
                icon="ERROR",
            )
        else:
            layout.label(
                text=(os.path.basename(gd.rstrip("/\\")) or gd),
                icon="FILE_FOLDER",
            )
        row = layout.row(align=True)

        row.operator(
            "object.ksp_mu_browser_refresh",
            text="Refresh",
            icon="FILE_REFRESH",
        )
        row.operator(
            "object.ksp_mu_browser_gen_thumbs",
            text="Thumbs",
            icon="IMAGE_DATA",
        )
        row.operator(
            "object.ksp_mu_browser_regen_thumbs",
            text="Regen",
            icon="FILE_REFRESH",
        )
        layout.prop(
            br,
            "category",
            text="",
        )
        layout.prop(
            br,
            "filter",
            text="",
            icon="VIEWZOOM",
        )
        parts = br.parts
        if not parts:
            layout.label(
                text="No parts in category",
                icon="INFO",
            )
        else:
            preview_box = layout.box()
            preview_box.template_icon_view(
                br,
                "part_preview",
                show_labels=True,
                scale=_THUMB_SCALE,
                scale_popup=_THUMB_SCALE_POPUP,
            )
        row = layout.row(align=True)
        op = row.operator(
            "object.ksp_mu_browser_import_part",
            text="Import",
            icon="IMPORT",
        )
        if (0 <= br.parts_index < len(br.parts)):
            op.part_name = (
                br.parts[
                    br.parts_index
                ].name
            )
        if (0 <= br.parts_index < len(br.parts)):
            item = br.parts[br.parts_index]
            box = layout.box()
            row = box.row(align=True)
            row.scale_y = 0.5
            row.label(
                text=(
                    "Part title:   %s"
                    % item.title
                )
            )
            row = box.row(align=True)
            row.scale_y = 0.5
            row.label(
                text=(
                    "File name: %s"
                    % item.name
                )
            )
            if item.attach_rules:
                row = box.row(align=True)
                row.scale_y = 0.5
                row.label(
                    text=(
                        "Attach rules: %s"
                        % item.attach_rules
                    )
                )

classes_to_register = (
    VIEW3D_PT_mu_part_browser,
)