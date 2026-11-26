"""MAC-keyed, non-destructive inventory merging."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.registry import Machine, Registry, normalize_mac

LAPTOP = "20:bd:1d:86:f3:30"
ALIEN = "cc:5e:f8:07:36:13"


def _registry(*machines) -> Registry:
    reg = Registry(Path("<memory>"))
    reg.machines = list(machines)
    return reg


def test_merge_adds_unknown_and_normalises_backend():
    reg = _registry(Machine(mac=ALIEN, name="alienware", ssh_user="ruoyu"))
    report = reg.merge([Machine(mac=LAPTOP, name="laptop", backend="local")], own_mac=ALIEN)
    assert report["added"] == [LAPTOP]
    added = reg.get_by_mac(LAPTOP)
    assert added.backend == "ssh"          # backend is host-relative, not copied


def test_merge_marks_own_record_local():
    reg = _registry()
    report = reg.merge([Machine(mac=ALIEN, name="alienware")], own_mac=ALIEN)
    assert report["added"] == [ALIEN]
    assert reg.get_by_mac(ALIEN).backend == "local"


def test_merge_fills_empty_fields_only():
    reg = _registry(Machine(mac=ALIEN, name="alienware"))
    report = reg.merge([Machine(mac=ALIEN, name="other-name", ssh_user="ruoyu",
                                os="ubuntu")])
    mine = reg.get_by_mac(ALIEN)
    assert mine.name == "alienware"        # never overwritten
    assert mine.ssh_user == "ruoyu"        # empty -> filled
    assert ("%s" % ALIEN, "ssh_user") in [(m, f) for m, f in report["filled"]]


def test_merge_reports_conflicts_and_keeps_local():
    reg = _registry(Machine(mac=ALIEN, name="alienware", ssh_user="ruoyu"))
    report = reg.merge([Machine(mac=ALIEN, name="alienware", ssh_user="someone_else")])
    assert report["conflicts"][0][0] == ALIEN
    assert report["conflicts"][0][1] == "ssh_user"
    assert reg.get_by_mac(ALIEN).ssh_user == "ruoyu"


def test_merge_never_deletes():
    reg = _registry(Machine(mac=ALIEN, name="alienware"), Machine(mac=LAPTOP, name="laptop"))
    reg.merge([Machine(mac=ALIEN, name="alienware")])
    assert len(reg.machines) == 2


def test_dumps_roundtrip():
    reg = _registry(Machine(mac=ALIEN, name="alienware", ssh_user="ruoyu", backend="ssh"),
                    Machine(mac=LAPTOP, name="laptop", backend="local"))
    text = reg.dumps()
    assert "cc:5e:f8:07:36:13" in text
    again = Registry.from_text(text)
    assert {m.mac for m in again.machines} == {normalize_mac(ALIEN), normalize_mac(LAPTOP)}
    assert again.get_by_mac(LAPTOP).name == "laptop"


def test_from_text_tolerates_garbage():
    assert Registry.from_text("not: a manifest").machines == []
    assert Registry.from_text("").machines == []
