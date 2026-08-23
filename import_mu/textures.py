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

from struct import unpack
import os.path

import bpy
from mathutils import Vector

def load_mbm(mbmpath):
    mbmfile = open(mbmpath, "rb")
    header = mbmfile.read(20)
    magic, width, height, bump, bpp = unpack("<5i", header)
    if magic != 0x50534b03: # "\x03KSP" as little endian
        return 0, 0, []
    if bpp == 32:
        pixels = mbmfile.read(width * height * 4)
    elif bpp == 24:
        pixels = [0, 0, 0, 255] * width * height
        for i in range(width * height):
            p = mbmfile.read(3)
            l = i * 4
            pixels[l:l+3] = list(p)
    else:
        return 0, 0, []
    return width, height, pixels

def _bump_type_from_name(base, type):
    # Squad often uses "..._Normal" / "..._Bump"; mu type flag is frequently 0.
    bl = base.lower()
    if (bl.endswith("_n") or bl.endswith("nrm") or bl.endswith("_normal")
            or bl.endswith("normal") or bl.endswith("_bump") or bl.endswith("bump")):
        return 1
    return type


def load_image(base, ext, path, type):
    """Load image; return resolved type (0/1) on success, False if missing/failed."""
    name = base + ext
    full = os.path.join(path, name)
    if not os.path.isfile(full):
        return False
    # Reuse only when the existing image is the same file on disk.
    # Name-only reuse breaks sequential imports / thumbnails (shared basenames).
    if base in bpy.data.images:
        img = bpy.data.images[base]
        try:
            existing = bpy.path.abspath(img.filepath) if img.filepath else ""
        except Exception:
            existing = ""
        want = os.path.normcase(os.path.normpath(os.path.abspath(full)))
        have = os.path.normcase(os.path.normpath(existing)) if existing else ""
        if have and have == want:
            return _bump_type_from_name(base, type)
        # Packed / empty filepath, or different file: free the name for the new load.
        # Export of objects still using this block keeps the datablock (just renamed).
        try:
            img.name = base + ".stale"
        except Exception:
            pass
    if ext.lower() in [".dds", ".png", ".tga"]:
        img = bpy.data.images.load(full)
        img.name = base
        img.muimageprop.invertY = False
        if ext.lower() == ".dds":
            img.muimageprop.invertY = True
        pixels = img.pixels[:1024]  # 256 pixels
        type = _bump_type_from_name(base, type)
    elif ext.lower() == ".mbm":
        w, h, pixels = load_mbm(full)
        if not pixels:
            return False
        img = bpy.data.images.new(base, w, h, alpha=True)
        img.pixels[:] = map(lambda x: x / 255.0, pixels)
        img.pack()
    else:
        return False
    # CHANNEL_PACKED: keep gloss/GA in alpha without premultiplying RGB to ~0
    # (Squad diffuse DDS often has near-zero alpha; STRAIGHT/premul looks black in EEVEE Next)
    img.alpha_mode = 'CHANNEL_PACKED'
    img.muimageprop.invertY = (ext.lower() == ".dds")
    img.muimageprop.convertNorm = False
    img.colorspace_settings.is_data = False
    if type == 1:
        img.colorspace_settings.is_data = True
        # Sample from the Blender image (0..1), not raw MBM bytes.
        img.muimageprop.convertNorm = _detect_ga_normal(img.pixels)
    return type


def _detect_ga_normal(pixels, n=512):
    """True only when the map looks DXT5nm (GA), not RGB tangent normals.

    Old heuristic tripped on a few non-unit RGB texels (compression) and then
    forced GA unpack → gray 'ghost shadow' shading on stock parts.

    InflatableAirlock ``airlock_N`` is GA with R≈1, B≈0.5 (flat blue-ish RGB
    decode) — that used to look RGB-unit and was mis-classified.
    """
    tot = min(int(len(pixels) / 4), n)
    if tot <= 0:
        return False
    rgb_unit = 0
    b_high = 0
    r_high = 0
    b_mid = 0
    for i in range(tot):
        r, g, b = pixels[i * 4], pixels[i * 4 + 1], pixels[i * 4 + 2]
        nx, ny, nz = 2 * r - 1, 2 * g - 1, 2 * b - 1
        if abs(nx * nx + ny * ny + nz * nz - 1.0) <= 0.15:
            rgb_unit += 1
        if b > 0.85:
            b_high += 1
        if r > 0.85:
            r_high += 1
        if 0.35 <= b <= 0.65:
            b_mid += 1
    # DXT5nm / GA: R channel often ~1 (unused) and B sits mid-gray, not Z≈1
    if (r_high / tot) >= 0.55 and (b_mid / tot) >= 0.40 and (b_high / tot) < 0.25:
        return True
    # RGB normals: mostly unit-length and B≈1 (Z) on flat / mild areas
    if rgb_unit / tot >= 0.65 and b_high / tot >= 0.45:
        return False
    # GA-packed: RGB decode is rarely unit-length
    return (rgb_unit / tot) < 0.45

def create_textures(mu, path):
    extensions = [".dds", ".mbm", ".tga", ".png"]
    #texture info is in the top level object
    try:
        from .progress_util import mu_tick_items
    except Exception:
        mu_tick_items = None
    texes = list(getattr(mu, "textures", None) or [])
    n = max(len(texes), 1)
    for i, tex in enumerate(texes):
        if mu_tick_items is not None:
            try:
                mu_tick_items(i, n, 15, 38, text="Loading textures…")
            except Exception:
                pass
        base, ext = os.path.splitext(tex.name)
        ind = 0
        if ext in extensions:
            ind = extensions.index(ext)
        lst = extensions[ind:] + extensions[:ind]
        for e in lst:
            resolved = load_image(base, e, path, tex.type)
            if resolved is not False:
                tex.type = resolved
                break
    pass
