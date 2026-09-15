import gzip
import xml.etree.ElementTree as ET

import pytest

from conftest import build_guide
from middleware.guide import GuideStore, build, gzip_stream


def write(tmp_path, data, name="guide.xml"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def parse(chunks):
    return ET.fromstring(b"".join(chunks))


def test_subset_in_requested_order(tmp_path):
    ids = ["A.ch", "B.ch", "C.ch", "D.ch"]
    index = build(write(tmp_path, build_guide(ids)), str(tmp_path / "idx"), "v1")
    tree = parse(index.chunks(["C.ch", "A.ch", "missing.ch"]))
    assert [c.get("id") for c in tree.findall("channel")] == ["C.ch", "A.ch"]
    assert {p.get("channel") for p in tree.findall("programme")} == {"A.ch", "C.ch"}
    assert len(tree.findall("programme")) == 6
    assert tree.get("source-info-name") == "IPTVBoss"


def test_whole_guide_round_trips(tmp_path):
    data = build_guide(["A.ch", "B.ch"])
    index = build(write(tmp_path, data), str(tmp_path / "idx"), "v1")
    tree = parse(index.chunks(index.channels()))
    original = ET.fromstring(data)
    assert [c.get("id") for c in tree.findall("channel")] == [c.get("id") for c in original.findall("channel")]
    assert len(tree.findall("programme")) == len(original.findall("programme"))
    assert tree.find("programme/title").text == "Show 0 & more"


def test_gzip_output_is_a_single_member(tmp_path):
    index = build(write(tmp_path, build_guide(["A.ch", "B.ch"])), str(tmp_path / "idx"), "v1")
    raw = b"".join(gzip_stream(index.chunks(["B.ch"]), level=1))
    d = __import__("zlib").decompressobj(wbits=31)
    body = d.decompress(raw)
    assert d.eof and d.unused_data == b""
    assert ET.fromstring(body).find("channel").get("id") == "B.ch"
    assert gzip.decompress(raw) == body


def test_self_closing_channel_and_programme_only_channel(tmp_path):
    data = (b'<?xml version="1.0"?>\n<tv>\n  <channel id="A.ch"/>\n'
            b'  <programme start="1" stop="2" channel="B.ch"><title>x</title></programme>\n</tv>\n')
    index = build(write(tmp_path, data), str(tmp_path / "idx"), "v1")
    assert index.channels() == ["A.ch", "B.ch"]
    tree = parse(index.chunks(["A.ch", "B.ch"]))
    assert len(tree.findall("channel")) == 1 and len(tree.findall("programme")) == 1


def test_escaped_ids_match_player_ids(tmp_path):
    # IPTV Boss writes A&E as A&amp;E in the guide; the player API gives the plain id.
    index = build(write(tmp_path, build_guide(["A&E Crime.pluto", "Plain.ch"])), str(tmp_path / "idx"), "v1")
    assert "A&E Crime.pluto" in index.channels()
    tree = parse(index.chunks(["A&E Crime.pluto"]))
    assert [c.get("id") for c in tree.findall("channel")] == ["A&E Crime.pluto"]
    assert len(tree.findall("programme")) == 3


def test_empty_and_broken_guides_rejected(tmp_path):
    with pytest.raises(ValueError):
        build(write(tmp_path, b"", "a.xml"), str(tmp_path / "idx"), "v1")
    with pytest.raises(ValueError):
        build(write(tmp_path, b"<html>error</html>", "b.xml"), str(tmp_path / "idx2"), "v1")


def test_truncated_guide_keeps_complete_parts(tmp_path):
    data = build_guide(["A.ch", "B.ch"])
    cut = data[: data.rfind(b"</programme>") - 5]
    index = build(write(tmp_path, cut), str(tmp_path / "idx"), "v1")
    assert index.channels() == ["A.ch", "B.ch", "Orphan.ch"]


def test_store_reloads_indexes_after_restart(tmp_path):
    store = GuideStore(str(tmp_path))
    store.install(2, write(tmp_path, build_guide(["A.ch"])), "v1")
    (tmp_path / "guides" / "download-leftover").write_bytes(b"x")
    again = GuideStore(str(tmp_path))
    assert again.get(2).version == "v1"
    assert not (tmp_path / "guides" / "download-leftover").exists()
