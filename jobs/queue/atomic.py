"""Durable file writes: never leave a half-written queue behind.

Protocol
--------
1. serialise the payload into ``<name>`` (with an integrity header),
2. write it to ``<name>.tmp`` and fsync,
3. copy the current file to ``<name>.bak``,
4. ``os.replace`` the temp over the real file (atomic on POSIX).

Readers therefore see either the complete old file or the complete new one.
``load_document`` additionally validates the embedded count/sha256, because a
truncated YAML can still parse into a *valid but shorter* document — the hash is
what catches "it loaded, but jobs are missing".
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import yaml


def checksum(entries: list[dict]) -> str:
    """Stable sha256 over a list of entries."""
    canonical = yaml.safe_dump(entries, sort_keys=True, allow_unicode=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_document(path: Path, payload: dict, header: str = "") -> None:
    text = header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    backup = path.with_name(path.name + ".bak")

    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass                      # a missing backup must not block a write

    os.replace(tmp, path)             # atomic: readers never see a partial file
    try:                              # make the rename durable too
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def load_document(path: Path, *, count_key: str = "entries_count",
                  hash_key: str = "entries_sha256",
                  list_key: str = "entries") -> tuple[dict | None, Path | None, str]:
    """Load the main file, falling back to ``.bak`` when integrity fails.

    Returns ``(payload, source_path, note)``. ``payload`` is None only when
    nothing readable *and* valid exists.
    """
    backup = path.with_name(path.name + ".bak")
    for candidate in (path, backup):
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            note = f"{candidate.name}: invalid YAML ({exc})"
            continue
        items = data.get(list_key)
        if count_key in data or hash_key in data:
            if not isinstance(items, list):
                note = f"{candidate.name}: missing '{list_key}'"
                continue
            if data.get(count_key) != len(items):
                note = (f"{candidate.name}: count mismatch "
                        f"({data.get(count_key)} != {len(items)})")
                continue
            if data.get(hash_key) and data[hash_key] != checksum(items):
                note = f"{candidate.name}: checksum mismatch"
                continue
        return data, candidate, ""
    return None, None, "no readable queue file (main and .bak both unusable)"
