"""Manifest round-trip and lookup tests (no network access required)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.registry import Machine, Registry, dump_minimal_yaml, parse_minimal_yaml


def test_roundtrip_and_no_ip_persisted(tmp_path):
    path = tmp_path / "machines.yaml"
    reg = Registry(path)
    reg.add(Machine(mac="CC:5E:F8:07:36:13", name="dell", ssh_user="sry"))
    reg.add(Machine(mac="aa-bb-cc-dd-ee-ff", name="pi"))
    reg.save()

    text = path.read_text()
    assert "cc:5e:f8:07:36:13" in text
    assert "aa:bb:cc:dd:ee:ff" in text
    assert "ip:" not in text, "the manifest must never persist a DHCP address"

    reloaded = Registry(path)
    assert len(reloaded.machines) == 2
    assert reloaded.find("cc:5e:f8:07:36:13").name == "dell"


def test_lookup_by_name_prefix_and_ambiguity(tmp_path):
    path = tmp_path / "machines.yaml"
    reg = Registry(path)
    reg.add(Machine(mac="cc:5e:f8:07:36:13", name="dell-ubuntu"))
    reg.add(Machine(mac="aa:bb:cc:dd:ee:ff", name="pi-node"))
    reg.save()

    reg = Registry(path)
    assert reg.find("dell-ubuntu").mac == "cc:5e:f8:07:36:13"
    assert reg.find("cc:5e").mac == "cc:5e:f8:07:36:13"      # unique mac prefix
    assert reg.find("pi").name == "pi-node"                  # unique name prefix
    try:
        reg.find("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown machine")


def test_duplicate_mac_is_idempotent(tmp_path):
    path = tmp_path / "machines.yaml"
    reg = Registry(path)
    first = reg.add(Machine(mac="cc:5e:f8:07:36:13", name="a"))
    second = reg.add(Machine(mac="CC:5E:F8:07:36:13", name="b"))
    assert first is second
    assert len(reg.machines) == 1


def test_remove_and_first_seen(tmp_path):
    path = tmp_path / "machines.yaml"
    reg = Registry(path)
    m = reg.add(Machine(mac="cc:5e:f8:07:36:13"))
    assert m.first_seen, "first_seen should be stamped on add"
    reg.remove(m)
    reg.save()
    assert Registry(path).machines == []


def test_minimal_yaml_fallback_matches_schema():
    payload = {"version": 1, "updated": "2026-11-26T00:00:00+00:00",
               "machines": [{"mac": "cc:5e:f8:07:36:13", "name": "dell", "tags": ["ubuntu"]}]}
    text = dump_minimal_yaml(payload)
    parsed = parse_minimal_yaml(text)
    entry = parsed["machines"][0]
    assert entry["mac"] == "cc:5e:f8:07:36:13"
    assert entry["name"] == "dell"
    assert entry["tags"] == ["ubuntu"]


def test_ubuntu_detection():
    assert Machine(mac="cc:5e:f8:07:36:13", os="ubuntu").is_ubuntu
    assert Machine(mac="cc:5e:f8:07:36:13", os="debian").is_ubuntu is False


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(Path(tmp)) if fn.__code__.co_argcount else fn()
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
