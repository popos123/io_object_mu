# vim:ts=4:et
# <pep8 compliant>

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
    CollectionProperty,
)
from bpy.types import PropertyGroup, WindowManager


# ============================================================
# CATEGORY
# ============================================================

def _category_items(self, context):
    try:
        from . import catalog
        cats = catalog.categories()
    except Exception:
        cats = []

    if not cats:
        return [("Inne", "Inne", "", 0)]

    return [
        (c, c, "", i)
        for i, c in enumerate(cats)
    ]


def _update_category(self, context):
    try:
        from . import operators
        operators.refresh_part_list(context)

        br = getattr(
            context.window_manager,
            "ksp_mu_browser",
            None,
        )

        if br is not None and len(br.parts):
            index = max(
                0,
                min(
                    br.parts_index,
                    len(br.parts) - 1,
                ),
            )

            br.parts_index = index
            br.part_preview = "PART_%d" % index

    except Exception:
        pass


# ============================================================
# GRID - ZACHOWANE DLA KOMPATYBILNOŚCI
# ============================================================

def _update_grid_row(self, context):
    try:
        row = self.grid_rows[
            self.grid_rows_index
        ]
    except Exception:
        return

    idxs = [
        i
        for i in (
            row.i0,
            row.i1,
            row.i2,
        )
        if i >= 0
    ]

    if not idxs:
        return

    if self.parts_index not in idxs:
        self.parts_index = idxs[0]


# ============================================================
# PREVIEW ENUM
# ============================================================

def _safe_text(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    try:
        return str(value)
    except Exception:
        return ""

def _preview_items(self, context):
    """
    Dynamiczne elementy dla template_icon_view().
    """
    items = []

    try:
        parts = self.parts
    except Exception:
        return [("NONE", "No parts", "", 0, 0)]

    for i, item in enumerate(parts):
        identifier = "PART_%d" % i

        name = _safe_text(
            item.name or identifier
        )

        description = _safe_text(
            item.title or item.name or ""
        )

        icon_id = 0
        try:
            if item.mu_path:
                from . import thumbnails
                icon_id = thumbnails.icon_id_for_part(
                    item.mu_path,
                    ensure=False,
                )
        except Exception:
            icon_id = 0
        # ALWAYS an int (0 = no custom icon)
        items.append((
            identifier,        # identifier
            name,              # label shown under thumb
            description,       # description / tooltip
            int(icon_id or 0), # icon – MUST be int
            i,                 # number
        ))

    if not items:
        return [("NONE", "No parts", "", 0, 0)]

    return items


def _update_preview(self, context):
    """
    Kliknięcie miniatury.

    template_icon_view() zmienia part_preview.
    Tutaj synchronizujemy to z istniejącym parts_index,
    żeby reszta addonu nadal działała tak jak wcześniej.
    """

    value = self.part_preview

    if not value:
        return

    if not value.startswith("PART_"):
        return

    try:
        index = int(
            value[5:]
        )
    except (TypeError, ValueError):
        return

    if index < 0:
        return

    if index >= len(self.parts):
        return

    if self.parts_index == index:
        return

    self.parts_index = index


# ============================================================
# PART
# ============================================================

class KSPMU_PG_MuPartItem(PropertyGroup):
    name: StringProperty(
        name="Name"
    )

    title: StringProperty(
        name="Title"
    )

    category: StringProperty(
        name="Category"
    )

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


# ============================================================
# GRID ROW
# ============================================================

class KSPMU_PG_MuGridRow(PropertyGroup):
    i0: IntProperty(
        default=-1
    )

    i1: IntProperty(
        default=-1
    )

    i2: IntProperty(
        default=-1
    )


# ============================================================
# BROWSER
# ============================================================

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

    # Istniejący indeks — pozostaje głównym indeksem
    # używanym przez resztę addonu.
    parts_index: IntProperty(
        name="Part Index",
        default=0,
    )

    # --------------------------------------------------------
    # NOWE:
    #
    # Enum używany przez template_icon_view().
    # Kliknięcie miniatury zmienia tę właściwość.
    # --------------------------------------------------------

    part_preview: EnumProperty(
        name="Part Preview",
        description="Select KSP part",
        items=_preview_items,
        update=_update_preview,
    )

    # --------------------------------------------------------
    # STARE GRID ROWS
    #
    # Zostawiamy, żeby nie rozwalić istniejącego kodu,
    # który może je nadal generować.
    # --------------------------------------------------------

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


# ============================================================
# REGISTER
# ============================================================

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