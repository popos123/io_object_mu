# vim:ts=4:et
# <pep8 compliant>


import unicodedata
import re
import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
    CollectionProperty,
)
from bpy.types import PropertyGroup, WindowManager

def _category_items(self, context):
    try:
        from . import catalog
        cats = catalog.categories()
    except Exception:
        cats = []
    if not cats:
        return [("Other", "Other", "", 0)]
    return [(c, c, "", i) for i, c in enumerate(cats)]

def _reset_part_selection(br):
    if br is None or len(br.parts) == 0:
        return
    br.parts_index = 0
    try:
        br.part_preview = ""
    except Exception:
        pass
    try:
        br.part_preview = _enum_cache_string("PART_0")
    except Exception:
        pass

def _update_category(self, context):
    try:
        from . import operators
        operators.refresh_part_list(context)
        from . import thumbnails
        thumbnails.invalidate_ksp_thumbs_index()
        try:
            thumbnails.build_ksp_thumbs_index(
                gd,
                force=True,
            )
        except Exception:
            pass
        br = getattr(
            context.window_manager,
            "ksp_mu_browser",
            None,
        )
        if br is None:
            return
        _reset_part_selection(br)
        if context.area:
            context.area.tag_redraw()
    except Exception:
        pass

def _update_grid_row(self, context):
    try:
        row = self.grid_rows[self.grid_rows_index]
    except Exception:
        return
    idxs = [i for i in (row.i0, row.i1, row.i2,) if i >= 0]
    if not idxs:
        return
    if self.parts_index not in idxs:
        self.parts_index = idxs[0]

def _safe_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    value = (
        value
        .replace("\u2018", "'")   # ‘
        .replace("\u2019", "'")   # ’
        .replace("\u201c", '"')   # “
        .replace("\u201d", '"')   # ”
        .replace("\u2013", "-")   # –
        .replace("\u2014", "-")   # —
        .replace("\u2026", "...") # …
        .replace("\xa0", " ")     # non-breaking space
    )
    value = unicodedata.normalize("NFKC", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in "\t\n")
    if len(value) > 80:
        value = value[:77] + "..."
    return value.strip()

_ENUM_STRING_CACHE = {}
def _enum_cache_string(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    cached = _ENUM_STRING_CACHE.get(value)
    if cached is None:
        cached = "{}".format(value)
        _ENUM_STRING_CACHE[value] = cached
    return cached

def _preview_items(self, context):
    items = []
    try:
        parts = self.parts
    except Exception:
        return [
            (
                _enum_cache_string("NONE"),
                _enum_cache_string("No parts"),
                _enum_cache_string(""),
                0,
                0,
            )
        ]
    for i, item in enumerate(parts):
        identifier = _enum_cache_string("PART_%d" % i)
        title = _safe_text(item.title)
        title = _enum_cache_string(title)
        name = _safe_text(item.name)
        name = _enum_cache_string(name)
        icon_id = 0
        try:
            if item.mu_path:
                from . import thumbnails
                icon_id = (
                    thumbnails.icon_id_for_part(
                        item.mu_path,
                        item.name,
                        ensure=False,
                    )
                )
        except Exception:
            icon_id = 0
        items.append(
            (
                identifier,
                title,
                name,
                int(icon_id or 0),
                i,
            )
        )
    if not items:
        return [
            (
                _enum_cache_string("NONE"),
                _enum_cache_string("No parts"),
                _enum_cache_string(""),
                0,
                0,
            )
        ]
    return items

def _update_preview(self, context):
    value = self.part_preview
    if not value:
        return
    if not value.startswith("PART_"):
        return
    try:
        index = int(value[5:])
    except (TypeError, ValueError):
        return
    if index < 0:
        return
    if index >= len(self.parts):
        return
    self.parts_index = index

class KSPMU_PG_MuPartItem(PropertyGroup):
    name: StringProperty(name="Name")
    title: StringProperty(name="Title")
    category: StringProperty(name="Category")
    mu_path: StringProperty(
        name="Mu Path",
        subtype="FILE_PATH",
    )
    cfg_path: StringProperty(
        name="Cfg Path",
        subtype="FILE_PATH",
    )
    attach_rules: StringProperty(
        name="Attach Rules",
        default="",
    )

class KSPMU_PG_MuGridRow(PropertyGroup):
    i0: IntProperty(default=-1)
    i1: IntProperty(default=-1)
    i2: IntProperty(default=-1)

class KSPMU_PG_MuBrowser(PropertyGroup):
    like_ksp: BoolProperty(
        name="like KSP",
        description=(
            "Snap parts on attach nodes "
            "(VAB-like). Off = normal "
            "Blender import"
        ),
        default=False,
    )
    category: EnumProperty(
        name="Category",
        items=_category_items,
        update=_update_category,
    )
    filter: StringProperty(
        name="Filter",
        default="",
        update=_update_category,
    )
    parts: CollectionProperty(
        type=KSPMU_PG_MuPartItem
    )
    parts_index: IntProperty(
        name="Part Index",
        default=0,
    )
    part_preview: EnumProperty(
        name="Part Preview",
        description="Select KSP part",
        items=_preview_items,
        update=_update_preview,
    )
    grid_rows: CollectionProperty(
        type=KSPMU_PG_MuGridRow
    )
    grid_rows_index: IntProperty(
        name="Grid Row",
        default=0,
        update=_update_grid_row,
    )
    thumbs_pending: BoolProperty(
        name="Lazy Thumbnails",
        description=(
            "Load cached preview icons; "
            "use Generate for missing ones"
        ),
        default=True,
    )

classes_to_register = (
    KSPMU_PG_MuPartItem,
    KSPMU_PG_MuGridRow,
    KSPMU_PG_MuBrowser,
)

custom_properties_to_register = (
    (
        WindowManager,
        "ksp_mu_browser",
        KSPMU_PG_MuBrowser,
    ),
)