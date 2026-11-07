"""File mirror: folder snapshot, sync diff, stability gate, state round-trip.

Covers `services/file_sync.py` (scan → diff → upload/delete → state) and
the `files_dirs` parsing/validation of `TopicConfig`. Telegram traffic goes
through a fake bot that records sends and deletes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_bot.core.services.file_sync import (
    FileSyncService,
    count_mirror_files,
    scan_mirror_files,
)
from telegram_bot.core.services.topic_config import TopicConfig


class FakeBot:
    """Records document uploads and message deletions."""

    def __init__(self, fail_send: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[tuple[int, int]] = []
        self._next_id = 100
        self.fail_send = fail_send

    async def send_document(
        self, chat_id: int, document: Any, caption: str | None = None, **kwargs: Any
    ) -> SimpleNamespace:
        if self.fail_send:
            raise RuntimeError("telegram is down")
        self.sent.append(
            {
                "chat_id": chat_id,
                "path": str(document.path),
                "caption": caption,
                "thread_id": kwargs.get("message_thread_id"),
            }
        )
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


def _make_service(tmp_path: Path, bot: FakeBot | None = None) -> FileSyncService:
    bot = bot or FakeBot()
    config = TopicConfig(str(tmp_path / "topic_config.json"))
    return FileSyncService(bot=bot, topic_config=config, state_dir=tmp_path / "state")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # Guarantee distinct mtimes across writes inside one test.
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))


# --- scan -------------------------------------------------------------------


def test_scan_skips_hidden_and_junk(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _write(root / "plan.md", "# plan")
    _write(root / "plan.pdf", "pdf")
    _write(root / ".DS_Store", "junk")
    _write(root / ".git" / "config", "git")
    _write(root / "__pycache__" / "m.py", "x")

    snapshot = scan_mirror_files([{"dir": str(root), "pattern": "**/*"}])
    names = sorted(Path(p).name for p in snapshot)
    assert names == ["plan.md", "plan.pdf"]
    assert count_mirror_files([{"dir": str(root), "pattern": "**/*"}]) == 2


def test_scan_pattern_selects_files(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    _write(root / "note.md", "md")
    _write(root / "note.pdf", "pdf")
    _write(root / "sub" / "deep.pdf", "pdf")
    _write(root / "sub" / "skip.md", "md")

    # Flat glob: only the folder's own matching files.
    flat = scan_mirror_files([{"dir": str(root), "pattern": "*.md"}])
    assert sorted(Path(p).name for p in flat) == ["note.md"]

    # Recursive glob: matching files at any depth.
    deep = scan_mirror_files([{"dir": str(root), "pattern": "**/*.pdf"}])
    assert sorted(str(Path(p).relative_to(root)) for p in deep) == [
        "note.pdf",
        "sub/deep.pdf",
    ]


def test_scan_signature_distinguishes_content(tmp_path: Path) -> None:
    f = tmp_path / "note.md"
    _write(f, "one")
    first = scan_mirror_files([{"dir": str(tmp_path), "pattern": "*.md"}])[str(f)]
    _write(f, "two")
    second = scan_mirror_files([{"dir": str(tmp_path), "pattern": "*.md"}])[str(f)]
    assert first != second


# --- sync -------------------------------------------------------------------


async def test_new_file_needs_two_ticks_then_uploads(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs / "a.md", "# a")

    service = _make_service(tmp_path)
    bot = service._bot
    entries = [{"dir": str(docs), "pattern": "**/*"}]

    # First sighting: held back (stability gate).
    r1 = await service.sync_topic(1, 42, entries)
    assert (r1.posted, r1.updated, r1.removed) == (0, 0, 0)
    assert bot.sent == []

    # Second sighting with the same signature: uploaded.
    r2 = await service.sync_topic(1, 42, entries)
    assert r2.posted == 1
    assert [Path(s["path"]).name for s in bot.sent] == ["a.md"]
    assert bot.sent[0]["thread_id"] == 42
    assert bot.sent[0]["chat_id"] == 1

    # State persisted — a third tick is a no-op.
    r3 = await service.sync_topic(1, 42, entries)
    assert (r3.posted, r3.updated, r3.removed) == (0, 0, 0)
    assert len(bot.sent) == 1


async def test_force_uploads_without_stability_wait(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs / "a.md", "# a")

    service = _make_service(tmp_path)
    r = await service.sync_topic(1, 42, [{"dir": str(docs), "pattern": "**/*"}], force=True)
    assert r.posted == 1
    assert len(service._bot.sent) == 1


async def test_changed_file_sends_new_then_deletes_old(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "plan.md"
    _write(f, "v1")

    service = _make_service(tmp_path)
    bot = service._bot
    entries = [{"dir": str(docs), "pattern": "*.md"}]
    await service.sync_topic(1, 42, entries, force=True)
    old_msg_id = json.loads((tmp_path / "state" / "42.json").read_text())["files"][str(f)]["msg_id"]

    _write(f, "v2")
    r = await service.sync_topic(1, 42, entries, force=True)
    assert (r.posted, r.updated, r.removed) == (0, 1, 0)
    assert len(bot.sent) == 2
    # Send happened before the delete — a failed send loses nothing.
    assert bot.deleted == [(1, old_msg_id)]
    state = json.loads((tmp_path / "state" / "42.json").read_text())
    assert state["files"][str(f)]["msg_id"] != old_msg_id


async def test_deleted_file_removes_message(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "gone.md"
    _write(f, "x")

    service = _make_service(tmp_path)
    bot = service._bot
    entries = [{"dir": str(docs), "pattern": "*.md"}]
    await service.sync_topic(1, 42, entries, force=True)
    msg_id = json.loads((tmp_path / "state" / "42.json").read_text())["files"][str(f)]["msg_id"]

    f.unlink()
    r = await service.sync_topic(1, 42, entries, force=True)
    assert r.removed == 1
    assert bot.deleted == [(1, msg_id)]
    state = json.loads((tmp_path / "state" / "42.json").read_text())
    assert str(f) not in state["files"]


async def test_failed_send_keeps_previous_copy_and_state(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    f = docs / "plan.md"
    _write(f, "v1")
    service = _make_service(tmp_path)
    entries = [{"dir": str(docs), "pattern": "*.md"}]
    await service.sync_topic(1, 42, entries, force=True)
    state_before = (tmp_path / "state" / "42.json").read_text()

    _write(f, "v2")
    # Bot goes down before the update: the old message must survive.
    service._bot.fail_send = True
    r = await service.sync_topic(1, 42, entries, force=True)
    assert (r.posted, r.updated, r.removed) == (0, 0, 0)
    service._bot.fail_send = False
    assert service._bot.deleted == []
    # State not advanced past the failed update — the next tick retries.
    r2 = await service.sync_topic(1, 42, entries, force=True)
    assert r2.updated == 1
    assert state_before != (tmp_path / "state" / "42.json").read_text()


async def test_sync_all_learns_chat_id_from_state(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs / "a.md", "a")
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps({"topics": {"42": {"files_dirs": [{"dir": str(docs), "pattern": "*.md"}]}}}),
        encoding="utf-8",
    )
    bot = FakeBot()
    config = TopicConfig(str(config_path))
    service = FileSyncService(bot=bot, topic_config=config, state_dir=tmp_path / "state")

    # No chat_id in state yet: the watchdog cannot route, nothing is sent.
    await service.sync_all()
    assert bot.sent == []

    # One manual sync records the chat id; the watchdog now posts updates
    # (after the stability gate holds the new file for one tick).
    await service.sync_topic(7, 42, [{"dir": str(docs), "pattern": "*.md"}], force=True)
    _write(docs / "b.md", "b")
    await service.sync_all()
    await service.sync_all()
    assert sorted(Path(s["path"]).name for s in bot.sent) == ["a.md", "b.md"]
    assert all(s["chat_id"] == 7 for s in bot.sent)


async def test_stop_cancels_watchdog(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    service.start(interval_sec=0.01)
    assert service._watch_task is not None
    await service.stop()
    assert service._watch_task is None


# --- topic config -----------------------------------------------------------


def test_files_dirs_parse_validation(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "7": {
                        "files_dirs": [
                            # Shorthand string → default pattern.
                            str(good),
                            {"dir": str(good), "pattern": "*.pdf"},
                            # Invalid: relative dir, missing dir, bad type,
                            # absolute pattern, pattern-only entry.
                            {"dir": "relative/path"},
                            {"dir": str(tmp_path / "missing")},
                            {"pattern": "*.md"},
                            {"dir": str(good), "pattern": "/etc/*"},
                            42,
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = TopicConfig(str(config_path))
    assert config.get_topic(7).files_dirs == [
        {"dir": str(good), "pattern": "**/*"},
        {"dir": str(good), "pattern": "*.pdf"},
    ]
    assert config.get_topic(8).files_dirs is None


async def test_update_files_dirs_roundtrip(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    config = TopicConfig(str(tmp_path / "topic_config.json"))

    # Shorthand strings are accepted and normalized.
    assert await config.update_files_dirs(7, [str(good)])
    assert config.get_topic(7).files_dirs == [{"dir": str(good), "pattern": "**/*"}]

    # Invalid input is refused loudly, the previous value survives.
    assert not await config.update_files_dirs(7, ["relative"])
    assert not await config.update_files_dirs(7, [str(tmp_path / "missing")])
    assert not await config.update_files_dirs(7, [{"dir": str(good), "pattern": "/etc/*"}])
    assert config.get_topic(7).files_dirs == [{"dir": str(good), "pattern": "**/*"}]

    # Empty list switches the mirror off (persisted as null).
    assert await config.update_files_dirs(7, [])
    assert config.get_topic(7).files_dirs is None
