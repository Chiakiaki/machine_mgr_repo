"""Persistent queue file with atomic writes, integrity checks and backup fallback."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from . import atomic
from .model import ACTIVE, PENDING, RUNNING, QueueEntry, now

HEADER = "# machine_mgr submission queue — atomic writes, .bak fallback; safe to read anytime\n"


class QueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[QueueEntry] = []
        self.warnings: list[str] = []
        self.load()

    # ── persistence ────────────────────────────────────────────────────────
    def load(self) -> None:
        data, source, note = atomic.load_document(self.path)
        if data is None:
            if self.path.exists() or self.path.with_name(self.path.name + ".bak").exists():
                self.warnings.append(f"queue unreadable, starting empty: {note}")
            return
        if source and source != self.path:
            self.warnings.append(f"recovered queue from {source.name} ({note})")
        for raw in data.get("entries", []):
            if not isinstance(raw, dict) or not raw.get("job"):
                continue
            known = set(QueueEntry.__dataclass_fields__)
            self.entries.append(QueueEntry(**{k: v for k, v in raw.items() if k in known}))
        self._reindex()

    def _reindex(self) -> None:
        for i, entry in enumerate(sorted(self.entries, key=lambda e: (e.order, e.added)), 1):
            entry.order = i

    def save(self) -> None:
        self._reindex()
        payload = [asdict(e) for e in sorted(self.entries, key=lambda e: e.order)]
        document = {
            "version": 1,
            "updated": now(),
            "entries_count": len(payload),
            "entries_sha256": atomic.checksum(payload),
            "entries": payload,
        }
        atomic.write_document(self.path, document, header=HEADER)

    # ── queries ────────────────────────────────────────────────────────────
    def add(self, entry: QueueEntry, allow_duplicate: bool = False) -> QueueEntry:
        if not allow_duplicate:
            for existing in self.entries:
                if existing.key == entry.key and existing.status in ACTIVE:
                    return existing
        entry.order = len(self.entries) + 1
        self.entries.append(entry)
        self.save()
        return entry

    def get(self, entry_id: str) -> QueueEntry:
        exact = [e for e in self.entries if e.id == entry_id]
        if exact:
            return exact[0]
        matches = [e for e in self.entries if e.id.startswith(entry_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no queue entry matches '{entry_id}'")
        raise KeyError(f"ambiguous entry id '{entry_id}'")

    def pending(self) -> list[QueueEntry]:
        return [e for e in sorted(self.entries, key=lambda e: e.order) if e.status == PENDING]

    def running(self) -> list[QueueEntry]:
        return [e for e in self.entries if e.status == RUNNING]

    def clear(self, only_finished: bool = True) -> int:
        before = len(self.entries)
        self.entries = ([e for e in self.entries if e.status in ACTIVE]
                        if only_finished else [])
        self.save()
        return before - len(self.entries)
