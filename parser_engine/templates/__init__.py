"""Real report templates live here: one module per layout (era).

Importing this package imports every module in it, so each @register runs.
See docs/adding_a_template.md.
"""
import importlib
import pkgutil


def load_all() -> list[str]:
    loaded = []
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")
            loaded.append(mod.name)
    return loaded
