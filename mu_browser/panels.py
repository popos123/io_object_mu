# vim:ts=4:et
# <pep8 compliant>

import os
import bpy
from . import thumbnails

# ============================================================
# USTAWIENIA
# ============================================================

# Maksymalna liczba wierszy rysowanych przy jednym odświeżeniu.
MAX_VISIBLE_ROWS = 3

# Liczba kolumn.
GRID_COLUMNS = 3

# 1.0 ~= 20 px
# 5.0 ~= 100 px
_THUMB_SCALE = 5.0
_THUMB_SCALE_BIG = 8.0

_COL_PX = 100
_COL_UI_UNITS = 10.0

_MAX_TITLE_LINES = 1
_MAX_NAME_LINES = 1

_PX_PER_CHAR = 5.0

# Interlinia tekstu.
_TEXT_LEADING = 0.75


# ============================================================
# TEKST
# ============================================================

def _max_chars(max_px=_COL_PX):
    try:
        scale = float(
            bpy.context.preferences.system.ui_scale
        ) or 1.0
    except Exception:
        scale = 1.0

    return max(
        8,
        int(max_px / (_PX_PER_CHAR * scale)),
    )


def _wrap_lines(
    text,
    max_px=_COL_PX,
    max_lines=_MAX_TITLE_LINES,
):
    text = " ".join((text or "").split())

    if not text:
        return [""]

    max_chars = _max_chars(max_px)

    words = text.split(" ")
    lines = []
    cur = ""
    leftover = False

    def commit():
        nonlocal cur

        if cur:
            lines.append(cur)
            cur = ""

    for w in words:
        if len(lines) >= max_lines:
            leftover = True
            break

        if not cur:
            if len(w) <= max_chars:
                cur = w
            else:
                while (
                    len(w) > max_chars
                    and len(lines) < max_lines
                ):
                    lines.append(
                        w[:max_chars]
                    )
                    w = w[max_chars:]

                if len(lines) >= max_lines:
                    leftover = True
                    cur = ""
                else:
                    cur = w

            continue

        trial = cur + " " + w

        if len(trial) <= max_chars:
            cur = trial
        else:
            commit()

            if len(lines) >= max_lines:
                leftover = True
                cur = ""
                break

            if len(w) <= max_chars:
                cur = w
            else:
                while (
                    len(w) > max_chars
                    and len(lines) < max_lines
                ):
                    lines.append(
                        w[:max_chars]
                    )
                    w = w[max_chars:]

                if len(lines) >= max_lines:
                    leftover = True
                    cur = ""
                else:
                    cur = w

    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur:
        leftover = True

    lines = [
        ln
        for ln in lines[:max_lines]
        if ln
    ]

    if leftover and lines:
        last = lines[-1]

        cut = max(
            1,
            max_chars - 1,
        )

        lines[-1] = (
            last[:cut].rstrip()
            + "…"
        )

    return lines or [""]


# ============================================================
# POJEDYNCZA KOMÓRKA
# ============================================================

def _draw_cell(layout, br, parts, idx):
    """Draw one browser cell."""

    # --------------------------------------------------------
    # PUSTA KOMÓRKA
    # --------------------------------------------------------

    cell = layout.column(
        align=True
    )

    try:
        cell.ui_units_x = _COL_UI_UNITS
    except Exception:
        pass

    if idx < 0 or idx >= len(parts):
        cell.separator()
        return

    item = parts[idx]

    selected = (
        idx == br.parts_index
    )

    # --------------------------------------------------------
    # BOX
    # --------------------------------------------------------

    # Nie używamy:
    #
    #     inner.active = selected
    #
    # ponieważ active=False powodowałoby zablokowanie
    # niezaznaczonych elementów.
    #
    # Zamiast tego zaznaczenie wizualne robimy przez
    # emboss operatora/ramkę przy miniaturze i tekst.
    inner = cell.box()

    # --------------------------------------------------------
    # MINIATURA
    # --------------------------------------------------------

    icon_id = 0

    try:
        if item.mu_path:
            icon_id = (
                thumbnails.icon_id_for_part(
                    item.mu_path,
                    ensure=False,
                )
            )
    except Exception:
        icon_id = 0

    if icon_id:
        # ----------------------------------------------------
        # WAŻNE:
        #
        # template_icon daje prawdziwe ~100 px.
        #
        # Nie używamy operator(icon_value=...), ponieważ
        # wtedy sama ikona pozostaje ~20 px.
        # ----------------------------------------------------

        inner.template_icon(
            icon_value=icon_id,
            scale=_THUMB_SCALE,
        )

    else:
        inner.label(
            text="",
            icon="MESH_DATA",
        )

    # --------------------------------------------------------
    # KLIKALNY OBSZAR MINIATURY
    # --------------------------------------------------------
    #
    # template_icon() samo nie jest przyciskiem.
    #
    # Dodajemy więc przezroczysty operator jako osobny
    # element. Blender nie pozwala nam jednak nałożyć go
    # fizycznie na template_icon().
    #
    # Dlatego przycisk znajduje się bezpośrednio pod
    # miniaturą.
    #
    # Tekst i miniatura mają wspólną funkcję wyboru.
    # --------------------------------------------------------

    if icon_id:
        select_row = inner.row(
            align=True
        )

        select_row.alignment = "LEFT"

        op = select_row.operator(
            "object.ksp_mu_browser_select",
            text="Select",
            icon="RESTRICT_SELECT_OFF",
            emboss=selected,
            depress=selected,
        )

        op.index = idx

    # --------------------------------------------------------
    # TYTUŁ
    # --------------------------------------------------------

    textcol = inner.column(
        align=True
    )

    textcol.alignment = "LEFT"

    textcol.scale_y = _TEXT_LEADING

    title = (
        item.title
        or item.name
    )

    for line in _wrap_lines(
        title,
        max_lines=_MAX_TITLE_LINES,
    ):
        op = textcol.operator(
            "object.ksp_mu_browser_select",
            text=line,
            icon="NONE",
            emboss=selected,
            depress=selected,
        )

        op.index = idx

    # --------------------------------------------------------
    # NAZWA PLIKU
    # --------------------------------------------------------

    gray = textcol.column(
        align=True
    )

    gray.alignment = "LEFT"

    gray.enabled = False

    for line in _wrap_lines(
        item.name or "",
        max_lines=_MAX_NAME_LINES,
    ):
        gray.label(
            text=line
        )


# ============================================================
# PANEL
# ============================================================

class VIEW3D_PT_mu_part_browser(
    bpy.types.Panel
):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MU"
    bl_label = "Parts (.mu)"
    bl_order = 10

    def draw(self, context):
        layout = self.layout

        # ----------------------------------------------------
        # PROGRESS
        # ----------------------------------------------------

        try:
            from ..import_mu.progress_util import (
                draw_mu_panel_progress,
            )

            draw_mu_panel_progress(
                layout
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # BROWSER
        # ----------------------------------------------------

        wm = context.window_manager

        br = getattr(
            wm,
            "ksp_mu_browser",
            None,
        )

        if br is None:
            layout.label(
                text="Browser not registered",
                icon="ERROR",
            )
            return

        # ----------------------------------------------------
        # LIKE KSP
        # ----------------------------------------------------

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
                text=(
                    "Import places a ghost "
                    "under the cursor"
                )
            )

        # ----------------------------------------------------
        # GAMEDATA
        # ----------------------------------------------------

        try:
            from ..preferences.preferences import (
                Preferences
            )

            gd = (
                Preferences().GameData
                or ""
            ).strip()

        except Exception:
            gd = ""

        if not gd:
            layout.label(
                text=(
                    "Set GameData "
                    "in Tool > Options"
                ),
                icon="ERROR",
            )

        else:
            layout.label(
                text=(
                    os.path.basename(
                        gd.rstrip("/\\")
                    )
                    or gd
                ),
                icon="FILE_FOLDER",
            )

        # ----------------------------------------------------
        # BUTTONS
        # ----------------------------------------------------

        row = layout.row(
            align=True
        )

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

        # ----------------------------------------------------
        # CATEGORY
        # ----------------------------------------------------

        layout.prop(
            br,
            "category",
            text="",
        )

        # ----------------------------------------------------
        # FILTER
        # ----------------------------------------------------

        layout.prop(
            br,
            "filter",
            text="",
            icon="VIEWZOOM",
        )

        # ====================================================
        # GRID
        # ====================================================
        # -----------------------------------------------------
        # PART PREVIEW
        # -----------------------------------------------------
        #
        # Blender 5.2:
        #
        # template_icon_view() daje duże, klikalne preview.
        #
        # Kliknięcie miniatury:
        #     -> zmienia br.part_preview
        #     -> _update_preview()
        #     -> ustawia br.parts_index
        #
        # Nie ma przycisku "Select".
        # -----------------------------------------------------

        if hasattr(br, "part_preview"):

            preview = layout.column(
                align=True
            )

            # Zawsze od lewej.
            preview.alignment = "LEFT"

            preview.template_icon_view(
                br,
                "part_preview",
                show_labels=True,
                scale=_THUMB_SCALE_BIG,
                scale_popup=_THUMB_SCALE,
            )

        else:
            layout.label(
                text=(
                    "Reload add-on "
                    "(preview property missing)"
                ),
                icon="ERROR",
            )

        # ----------------------------------------------------
        # IMPORT
        # ----------------------------------------------------

        row = layout.row(
            align=True
        )

        op = row.operator(
            "object.ksp_mu_browser_import_part",
            text="Import",
            icon="IMPORT",
        )

        if (
            0 <= br.parts_index
            < len(br.parts)
        ):
            op.part_name = (
                br.parts[
                    br.parts_index
                ].name
            )

        # ----------------------------------------------------
        # INFORMACJE O ZAZNACZONYM
        # ----------------------------------------------------

        if (0 <= br.parts_index < len(br.parts)):
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


# ============================================================
# REJESTRACJA
# ============================================================

classes_to_register = (
    VIEW3D_PT_mu_part_browser,
)