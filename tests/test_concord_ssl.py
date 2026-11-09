"""Windows must not combine Python's crypto DLL with another build's SSL DLL.

The native check binds the library in a fresh process but never opens a USB device.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from afterglow import concord


@pytest.mark.parametrize("platform, expected", [
    ("win32", ("ssl", "native")),
    ("linux", ("native",)),
    ("darwin", ("native",)),
])
def test_only_windows_loads_its_matching_ssl_pair_before_the_remote_library(
        monkeypatch, platform, expected):
    events = []
    library = object()
    original_import = importlib.import_module

    def import_module(name, package=None):
        if name == "ssl":
            events.append("ssl")
            return object()
        return original_import(name, package)

    def load_library(_name):
        events.append("native")
        return library

    monkeypatch.setattr(concord.sys, "platform", platform)
    monkeypatch.setenv("AFTERGLOW_LIBCONCORD", "test-only.dll")
    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.setattr(concord.ctypes, "CDLL", load_library)

    assert concord._load() is library
    assert tuple(events) == expected


def test_a_failed_python_ssl_load_is_reported_before_any_native_library_attempt(monkeypatch):
    attempts = []
    original_import = importlib.import_module

    def import_module(name, package=None):
        if name == "ssl":
            raise ImportError("matching SSL dependency is missing")
        return original_import(name, package)

    monkeypatch.setattr(concord.sys, "platform", "win32")
    monkeypatch.setenv("AFTERGLOW_LIBCONCORD", "test-only.dll")
    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.setattr(concord.ctypes, "CDLL", lambda name: attempts.append(name))

    with pytest.raises(concord.NotAvailable, match="Python's SSL runtime") as error:
        concord._load()

    assert isinstance(error.value.__cause__, ImportError)
    assert concord.LOAD_ERRORS == [str(error.value)]
    assert attempts == []


def test_a_selected_windows_library_binds_after_hashlib_without_opening_usb():
    if sys.platform != "win32":
        pytest.skip("the DLL conflict is specific to Windows")
    if not os.environ.get("AFTERGLOW_LIBCONCORD"):
        pytest.skip("no explicit native library selected")

    source = str(Path(concord.__file__).resolve().parent.parent)
    code = (
        "import hashlib, sys\n"
        f"sys.path.insert(0, {source!r})\n"
        "from afterglow import concord\n"
        "remote = concord.Remote()\n"
        "assert remote._open is False\n"
        "print('read-only bindings loaded; USB was not opened')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "USB was not opened" in result.stdout
