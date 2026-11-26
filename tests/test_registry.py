"""Two-layer inventory: curated shared file + per-host overlay."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.registry import Machine, Registry

LAPTOP = "20:bd:1d:86:f3:30"
ALIEN = "cc:5e:f8:07:36:13"


def _shared(tmp_path, entries) -> Path:
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "machines": entries}, sort_keys=False))
    return path


def _registry(tmp_path, shared_entries=()) -> Registry:
    shared = _shared(tmp_path, list(shared_entries)) if shared_entries else tmp_path / "inventory.yaml"
    return Registry(tmp_path / "machines.yaml", shared)


def test_shared_entries_are_visible_without_an_overlay(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware", "ssh_user": "ruoyu"}])
    machine = reg.find("alienware")
    assert machine.mac == ALIEN
    assert reg.overlay == {}                      # reading does not create overlay rows


def test_overlay_overrides_shared_fields(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware", "ssh_user": "ruoyu"}])
    reg.upsert(Machine(mac=ALIEN, project_root="/home/ruoyu/project"))   # overlay
    merged = reg.get_by_mac(ALIEN)
    assert merged.name == "alienware"             # from shared
    assert merged.project_root == "/home/ruoyu/project"  # from overlay


def test_overlay_only_machine_appears_in_the_view(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware"}])
    reg.upsert(Machine(mac=LAPTOP, name="laptop", backend="local"))
    assert {m.mac for m in reg.machines} == {ALIEN, LAPTOP}
    assert reg.get_by_mac(LAPTOP).backend == "local"


def test_upsert_never_creates_duplicates(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert(Machine(mac=ALIEN, name="one"))
    reg.upsert(Machine(mac="CC:5E:F8:07:36:13", name="two"))
    assert len(reg.machines) == 1
    assert reg.get_by_mac(ALIEN).name == "two"


def test_shared_layer_has_no_local_fields(tmp_path):
    reg = _registry(tmp_path)
    reg.upsert(Machine(mac=ALIEN, name="alienware", backend="local"), shared=True)
    text = (tmp_path / "inventory.yaml").read_text()
    data = yaml.safe_load(text)
    entry = data["machines"][0]
    assert entry["name"] == "alienware"
    assert "backend" not in entry                  # curated file stays host-neutral
    assert "last_seen" not in entry


def test_touch_updates_overlay_not_shared(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware"}])
    before = (tmp_path / "inventory.yaml").read_text()
    reg.touch(reg.find("alienware"))
    assert (tmp_path / "inventory.yaml").read_text() == before
    assert reg.get_by_mac(ALIEN).last_seen


def test_find_by_name_mac_and_prefix(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware"},
                               {"mac": LAPTOP, "name": "laptop"}])
    assert reg.find("alienware").mac == ALIEN
    assert reg.find("cc:5e").mac == ALIEN
    assert reg.find("lapt").mac == LAPTOP
    try:
        reg.find("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_remove_drops_shared_row_too(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware"}])
    reg.remove(reg.find("alienware"))
    assert reg.machines == []
    assert yaml.safe_load((tmp_path / "inventory.yaml").read_text())["machines"] == []


def test_corrupt_shared_file_warns_but_still_loads_overlay(tmp_path):
    shared = tmp_path / "inventory.yaml"
    shared.write_text("{ nope: [\n")
    reg = Registry(tmp_path / "machines.yaml", shared)
    assert reg.warnings
    reg.upsert(Machine(mac=ALIEN, name="alienware"))
    assert reg.get_by_mac(ALIEN).name == "alienware"


def test_curated_name_wins_over_stale_overlay_name(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware"}])
    reg.upsert(Machine(mac=ALIEN, name="host-3613"))      # old auto-name in overlay
    assert reg.get_by_mac(ALIEN).name == "alienware"
    assert reg.find("alienware").mac == ALIEN


def test_overlay_still_overrides_reachability_fields(tmp_path):
    reg = _registry(tmp_path, [{"mac": ALIEN, "name": "alienware", "ssh_user": "curated",
                                "project_root": "/srv/project"}])
    reg.upsert(Machine(mac=ALIEN, ssh_user="localuser", project_root="/home/local/project"))
    merged = reg.get_by_mac(ALIEN)
    assert merged.name == "alienware"
    assert merged.ssh_user == "localuser"
    assert merged.project_root == "/home/local/project"
