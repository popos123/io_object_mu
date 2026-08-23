# vim:ts=4:et
# <pep8 compliant>

from .. import register_submodules

submodule_names = (
    "properties",
    "catalog",
    "thumbnails",
    "operators",
    "snap",
    "panels",
)
register_submodules(__name__, submodule_names)
