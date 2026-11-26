"""Auto-naming: slugging and collision suffixes."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.registry import Machine, slugify, unique_name


def test_slugify_basic():
    assert slugify("Ruoyu-Alienware m17 R5 AMD") == "ruoyu-alienware-m17-r5"
    assert slugify("  __weird__  name!! ") == "weird-name"
    assert slugify("") == ""


def test_slugify_truncates_on_token_boundary():
    out = slugify("a" * 30, max_len=10)
    assert len(out) <= 10
    assert not out.endswith("-")


def test_unique_name_appends_counter():
    machines = [Machine(mac="cc:5e:f8:07:36:13", name="alienware-m17"),
                Machine(mac="aa:bb:cc:dd:ee:ff", name="alienware-m17-2")]
    assert unique_name("alienware-m17", machines) == "alienware-m17-3"
    assert unique_name("fresh-name", machines) == "fresh-name"


def test_unique_name_ignores_self():
    me = Machine(mac="cc:5e:f8:07:36:13", name="alienware-m17")
    assert unique_name("alienware-m17", [me], ignore_mac=me.mac) == "alienware-m17"


def test_vendor_lookup():
    from local_machines.discovery import vendor_for
    assert vendor_for("b8:27:eb:12:34:56") == "raspberrypi"
    assert vendor_for("zz:zz:zz:zz:zz:zz") == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
