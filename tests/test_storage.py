from __future__ import annotations

import time

from ticket_parser.storage import Storage, safe_filename, slug_for


def test_slug_is_stable_and_filesystem_safe() -> None:
    slug = slug_for("<CAF=abc/def@mail.gmx.com>")
    assert slug == slug_for("<CAF=abc/def@mail.gmx.com>")
    assert "/" not in slug and "<" not in slug
    assert slug != slug_for("<other@mail.gmx.com>")


def test_safe_filename_strips_path_traversal() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("IMG-20240115-WA0001.jpg") == "IMG-20240115-WA0001.jpg"
    assert safe_filename("") == "attachment.bin"
    assert safe_filename("..") == "attachment.bin"


def test_partial_dir_is_invisible_until_committed(storage: Storage) -> None:
    pending = storage.new_incoming("abc")
    (pending / "raw.eml").write_bytes(b"x")
    assert not storage.inbox_dir("abc").exists()
    assert list(storage.inbox.glob("[!.]*")) == []

    storage.commit_incoming(pending, "abc")
    assert (storage.inbox_dir("abc") / "raw.eml").read_bytes() == b"x"


def test_transitions_are_idempotent_after_a_crash(storage: Storage) -> None:
    pending = storage.new_incoming("abc")
    (pending / "raw.eml").write_bytes(b"x")
    storage.commit_incoming(pending, "abc")

    staged = storage.to_staging("abc")
    assert staged == storage.to_staging("abc")  # replaying the move is a no-op
    done = storage.to_done("abc")
    assert done == storage.to_done("abc")
    assert (done / "raw.eml").read_bytes() == b"x"
    assert storage.find("abc") == done


def test_prune_respects_retention_and_never_touches_out(storage: Storage) -> None:
    old = storage.done / "old"
    old.mkdir()
    (old / "raw.eml").write_bytes(b"x")
    ancient = time.time() - 40 * 86400
    import os

    os.utime(old, (ancient, ancient))

    fresh = storage.done / "fresh"
    fresh.mkdir()
    pdf = storage.out / "report.pdf"
    pdf.write_bytes(b"%PDF")

    removed = storage.prune_done(30)
    assert removed == ["old"]
    assert fresh.exists()
    assert pdf.exists()
