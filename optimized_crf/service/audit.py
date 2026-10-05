"""The audit trail of one job: who did what, when, and what changed.

Regulated work needs every action on a submission deliverable to be
attributable and reconstructable (21 CFR Part 11 / EU Annex 11 style):

* **Append-only.** Entries are only ever added, one JSON object per line.
* **Attributable.** Each entry names the signed-in user from the job ticket.
* **Before and after.** An edit records the old and the new annotation.
* **Tamper-evident.** Each entry carries the SHA-256 of the entry before it,
  so removing or altering any line breaks the chain, and `verify` says where.

Times are UTC, ISO 8601, to the millisecond.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

AUDIT_FILE = "audit.ndjson"
GENESIS = "0" * 64

_LOCK = threading.Lock()


def actor_of(ticket: Mapping[str, Any]) -> dict[str, str]:
    """The person behind a request, as the signed ticket names them."""
    return {
        "user_id": str(ticket.get("user") or ""),
        "email": str(ticket.get("email") or ""),
        "name": str(ticket.get("name") or ""),
    }


def _digest(entry: Mapping[str, Any]) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def entries(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / AUDIT_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record(root: Path, actor: Mapping[str, str], action: str, **details: Any) -> dict[str, Any]:
    """Append one entry and return it."""
    path = Path(root) / AUDIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        previous = entries(root)
        last = previous[-1] if previous else None
        entry: dict[str, Any] = {
            "seq": (last["seq"] + 1) if last else 1,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": dict(actor),
            "action": action,
            **details,
            "prev": last["hash"] if last else GENESIS,
        }
        entry["hash"] = _digest(entry)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def verify(root: Path) -> tuple[bool, Optional[int]]:
    """(True, None) when the chain is intact; else (False, first bad seq)."""
    previous = GENESIS
    for index, entry in enumerate(entries(root), start=1):
        if entry.get("seq") != index or entry.get("prev") != previous or entry.get("hash") != _digest(entry):
            return False, index
        previous = entry["hash"]
    return True, None


_CSV_COLUMNS = ("seq", "at_utc", "user_email", "user_name", "user_id", "action", "page", "crf_text",
                "before", "after", "details", "hash")
_CSV_KNOWN = {"seq", "at", "actor", "action", "page", "crf_text", "before", "after", "prev", "hash"}


def to_csv(root: Path) -> bytes:
    """The trail as a spreadsheet a reviewer can read, with the chain status."""
    intact, broken_at = verify(root)
    buffer = io.StringIO()
    buffer.write(f"# Audit trail. Hash chain: {'intact' if intact else f'BROKEN at entry {broken_at}'}\n")
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)
    for entry in entries(root):
        actor = entry.get("actor") or {}
        extra = {key: value for key, value in entry.items() if key not in _CSV_KNOWN}
        writer.writerow([
            entry.get("seq"), entry.get("at"), actor.get("email", ""), actor.get("name", ""),
            actor.get("user_id", ""), entry.get("action"), entry.get("page", ""), entry.get("crf_text", ""),
            entry.get("before", ""), entry.get("after", ""),
            json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else "", entry.get("hash"),
        ])
    return buffer.getvalue().encode("utf-8")
