"""Workspace confinement for the file tools."""

from __future__ import annotations

import os

import pytest

from app.tools.base import ToolError
from app.tools.files import MAX_WRITE_BYTES, Workspace


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "data.txt").write_text("outside data")
    return Workspace(str(root))


def test_write_read_list_roundtrip(ws):
    assert ws.write("a/b/c.txt", "hello", False) == "Wrote 5 bytes to a/b/c.txt"
    assert ws.read("a/b/c.txt", 6000) == "hello"
    assert ws.write("a/b/c.txt", " world", True) == "Appended 6 bytes to a/b/c.txt"
    assert ws.read("./a/../a/b/c.txt", 6000) == "hello world"
    assert ws.write("a/b/c.txt", "new", False) and ws.read("a/b/c.txt", 6000) == "new"
    assert ws.list("") == "a/" and ws.list("a/b") == "c.txt (3 bytes)"
    assert ws.read(os.path.join(ws.root, "a/b/c.txt"), 6000) == "new"      # absolute, but inside


def test_read_reports_truncation_binary_and_empty(ws):
    ws.write("long.txt", "x" * 10_000, False)
    out = ws.read("long.txt", 500)
    assert out.startswith("x" * 500 + "\n[file is 10000 bytes; showing the first 500 characters]")
    with open(os.path.join(ws.root, "bin.dat"), "wb") as fh:
        fh.write(b"\x00\x01\x02" * 10)
    assert ws.read("bin.dat", 500) == "[binary file, 30 bytes; not shown]"
    ws.write("empty.txt", "", False)
    assert ws.read("empty.txt", 500) == "[empty file]"


@pytest.mark.parametrize("path", ["../secret.txt", "../../etc/passwd", "/etc/passwd", "a/../../secret.txt",
                                  "..", "/", "/proc/self/environ", "bad\x00name"])
def test_paths_outside_the_workspace_are_refused(ws, path):
    for op in (lambda: ws.read(path, 100), lambda: ws.write(path, "x", False), lambda: ws.list(path)):
        with pytest.raises(ToolError):
            op()


def test_symlink_escapes_are_refused(ws, tmp_path):
    os.symlink(tmp_path / "secret.txt", os.path.join(ws.root, "link.txt"))
    os.symlink(tmp_path / "outside", os.path.join(ws.root, "linkdir"))
    os.symlink("/proc/self/environ", os.path.join(ws.root, "env"))
    for path in ("link.txt", "linkdir/data.txt", "env"):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.read(path, 100)
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.write("link.txt", "overwritten", False)
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.write("linkdir/new.txt", "planted", False)
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.list("linkdir")
    assert (tmp_path / "secret.txt").read_text() == "TOP SECRET"
    assert not (tmp_path / "outside" / "new.txt").exists()
    assert "link.txt (link)" in ws.list(".")           # listed as a link, never followed


def test_dangling_symlink_cannot_be_used_to_create_a_file_outside(ws, tmp_path):
    os.symlink(tmp_path / "outside" / "created.txt", os.path.join(ws.root, "dangling"))
    with pytest.raises(ToolError):
        ws.write("dangling", "x", False)
    assert not (tmp_path / "outside" / "created.txt").exists()


def test_symlinks_that_stay_inside_are_fine(ws):
    ws.write("real/file.txt", "inside", False)
    os.symlink("real", os.path.join(ws.root, "alias"))
    assert ws.read("alias/file.txt", 100) == "inside"


def test_link_swapped_in_after_the_check_is_not_followed(ws, tmp_path, monkeypatch):
    """The sandbox shares the workspace and can race the check; the open itself must refuse."""
    ws.write("dir/file.txt", "safe", False)
    os.rename(os.path.join(ws.root, "dir"), os.path.join(ws.root, "dir.real"))
    os.symlink(tmp_path / "outside", os.path.join(ws.root, "dir"))
    os.symlink(tmp_path / "secret.txt", os.path.join(ws.root, "swapped.txt"))
    # Pretend the path check ran before the swap and approved these names.
    monkeypatch.setattr(ws, "resolve", lambda path: path.split("/"))
    for path in ("dir/data.txt", "swapped.txt"):
        with pytest.raises(ToolError):
            ws.read(path, 100)
        with pytest.raises(ToolError):
            ws.write(path, "x", False)
    with pytest.raises(ToolError):
        ws.list("dir")
    assert (tmp_path / "secret.txt").read_text() == "TOP SECRET"
    assert (tmp_path / "outside" / "data.txt").read_text() == "outside data"


def test_size_limits(ws):
    with pytest.raises(ToolError, match="2 MB"):
        ws.write("big.txt", "x" * (MAX_WRITE_BYTES + 1), False)
    ws.write("grow.txt", "x" * (MAX_WRITE_BYTES - 10), False)
    with pytest.raises(ToolError, match="2 MB"):
        ws.write("grow.txt", "y" * 100, True)
    assert os.path.getsize(os.path.join(ws.root, "grow.txt")) == MAX_WRITE_BYTES - 10


def test_special_files_and_directories_are_not_read(ws):
    os.mkfifo(os.path.join(ws.root, "pipe"))
    os.mkdir(os.path.join(ws.root, "folder"))
    with pytest.raises(ToolError, match="not a regular file"):
        ws.read("pipe", 100)                           # must not block waiting for a writer
    with pytest.raises(ToolError, match="directory"):
        ws.read("folder", 100)
    with pytest.raises(ToolError, match="directory"):
        ws.write("folder", "x", False)
    with pytest.raises(ToolError, match="no such file"):
        ws.read("missing.txt", 100)


def test_listing_is_bounded(ws):
    for i in range(230):
        ws.write(f"many/f{i:03d}.txt", "x", False)
    lines = ws.list("many").splitlines()
    assert len(lines) == 201 and lines[-1] == "[30 more entries not shown]"
