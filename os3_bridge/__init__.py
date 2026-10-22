"""Compatibility shim: the package is now ``nexus``, in ``src/nexus``.

An already installed LaunchAgent or systemd unit runs ``python -m os3_bridge.cli``
from the checkout. This puts ``src/`` on ``sys.path`` and makes every
``os3_bridge.*`` name the same module object as its ``nexus.*`` twin, so that
command and ``import os3_bridge`` keep working.
"""

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_OLD = __name__
_NEW = "nexus"
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _real_name(fullname: str) -> str:
    return _NEW + fullname[len(_OLD) :]


class _AliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve ``os3_bridge.x`` to the already loaded (or freshly loaded) ``nexus.x``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        real = importlib.util.find_spec(_real_name(fullname))
        if real is None:
            return None
        spec = importlib.machinery.ModuleSpec(
            fullname,
            self,
            origin=real.origin,
            is_package=real.submodule_search_locations is not None,
        )
        return spec

    def create_module(self, spec):
        module = importlib.import_module(_real_name(spec.name))
        spec.loader_state = module.__spec__
        return module

    def exec_module(self, module):
        # The import system set the alias spec on the shared module; put the real one back.
        module.__spec__ = module.__spec__.loader_state

    def get_code(self, fullname):
        # `python -m os3_bridge.cli` runs nexus.cli's code as __main__.
        real = importlib.util.find_spec(_real_name(fullname))
        return real.loader.get_code(real.name)


sys.meta_path.insert(0, _AliasFinder())

import nexus  # noqa: E402

sys.modules[_OLD] = nexus
