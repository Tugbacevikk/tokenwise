"""Shared by the check scripts. They run inside a copy of the project (cwd = its root)."""

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock


def use_src(path: str = "src") -> None:
    sys.path.insert(0, str(Path.cwd() / path))
    sys.path.insert(0, str(Path.cwd()))


def stub_missing(*names: str) -> None:
    """Third-party packages the checked code imports but the check never exercises."""
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                sys.modules.setdefault(".".join(parts[:i]), MagicMock())


def load_file(path: str, name: str = "checked"):
    spec = importlib.util.spec_from_file_location(name, Path.cwd() / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(path: str) -> str:
    return (Path.cwd() / path).read_text(encoding="utf-8")


def raises(error, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except error:
        return True
    return False
