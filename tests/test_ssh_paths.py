"""Remote path rewriting (regression test for the literal-'~' bug)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.ssh_tools import remote_path


def test_tilde_forms():
    assert remote_path("~/project") == "$HOME/project"
    assert remote_path("~") == "$HOME"
    assert remote_path("~/a/b") == "$HOME/a/b"


def test_absolute_and_relative_untouched():
    assert remote_path("/srv/data") == "/srv/data"
    assert remote_path("rel/dir") == "rel/dir"
    # only a *leading* tilde is rewritten
    assert remote_path("a/~/b") == "a/~/b"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
