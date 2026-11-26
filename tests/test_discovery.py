"""Discovery helpers that can be tested without touching the network."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_machines.discovery import ScanCache, ScanResult, is_local_mac, normalize_mac


def test_normalize_mac_accepts_common_spellings():
    canonical = "cc:5e:f8:07:36:13"
    for spelling in ("CC:5E:F8:07:36:13", "cc-5e-f8-07-36-13", "cc5ef8073613",
                     " cc:5e:f8:07:36:13 "):
        assert normalize_mac(spelling) == canonical


def test_normalize_mac_rejects_garbage():
    for bad in ("", "zz:5e:f8:07:36:13", "cc:5e:f8:07:36", "not-a-mac"):
        try:
            normalize_mac(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_is_local_mac_flags_multicast_bit():
    assert is_local_mac("01:00:5e:00:00:01") is True    # multicast
    assert is_local_mac("cc:5e:f8:07:36:13") is False   # unicast


def test_scan_cache_ttl(tmp_path):
    cache = ScanCache(tmp_path / "cache.json", ttl=2)
    result = ScanResult("wlan0", "192.168.5.0/24", 254, ["192.168.5.101"],
                        {"cc:5e:f8:07:36:13": "192.168.5.101"},
                        {"192.168.5.101": "cc:5e:f8:07:36:13"}, 0.1)
    cache.store(result)
    loaded = cache.load()
    assert loaded["mac_to_ip"]["cc:5e:f8:07:36:13"] == "192.168.5.101"

    # Expired cache must not be trusted.
    import json
    data = json.loads(cache.path.read_text())
    data["ts"] = time.time() - 10
    cache.path.write_text(json.dumps(data))
    assert cache.load() is None

    cache.clear()
    assert cache.load() is None


def test_entries_sorted_by_mac():
    result = ScanResult("wlan0", "192.168.5.0/24", 0, [], 
                        {"cc:5e:f8:07:36:13": "192.168.5.101", "aa:bb:cc:dd:ee:ff": "192.168.5.50"},
                        {}, 0.0)
    assert [mac for mac, _ in result.entries] == ["aa:bb:cc:dd:ee:ff", "cc:5e:f8:07:36:13"]


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
