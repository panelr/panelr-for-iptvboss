"""Split an XMLTV guide by channel once, then assemble each customer's copy.

The split stores every channel's <channel> element and its programmes as byte
ranges in two files. A customer's guide is those ranges read back in order, so
there is no XML parsing per request. When the player accepts gzip, the output
is compressed on the fly as one gzip stream (some players reject gzip made of
several joined members). A channel is sent once even if several picked
categories contain it.
"""
import hashlib
import html
import json
import mmap
import os
import re
import shutil
import tempfile
import threading
import time
import zlib

CHANNEL_OPEN = re.compile(rb"<channel\b[^>]*?\bid=\"([^\"]+)\"")
PROGRAMME_CHANNEL = re.compile(rb"\bchannel=\"([^\"]+)\"")
TAIL = b"</tv>\n"


class GuideIndex:
    """One built guide: files on disk plus the channel -> byte range table."""

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "index.json")) as f:
            meta = json.load(f)
        self.version = meta["version"]
        self.built_at = meta["built_at"]
        self.order = meta["order"]
        self.ranges = meta["ranges"]            # channel id -> [c_off, c_len, p_off, p_len]
        with open(os.path.join(folder, "head.xml"), "rb") as f:
            self.head = f.read()

    def channels(self):
        return list(self.order)

    def chunks(self, channel_ids, chunk_size=1 << 20):
        """Yield the uncompressed guide for these channels, in the order given. Unknown ids are skipped."""
        wanted = [c for c in channel_ids if c in self.ranges]
        yield self.head
        with open(os.path.join(self.folder, "channels.xml"), "rb") as f:
            yield from _ranges(f, [(self.ranges[c][0], self.ranges[c][1]) for c in wanted], chunk_size)
        with open(os.path.join(self.folder, "programmes.xml"), "rb") as f:
            yield from _ranges(f, [(self.ranges[c][2], self.ranges[c][3]) for c in wanted], chunk_size)
        yield TAIL


def _ranges(f, spans, chunk_size):
    buf = bytearray()
    for off, length in spans:
        if not length:
            continue
        f.seek(off)
        buf += f.read(length)
        if len(buf) >= chunk_size:
            yield bytes(buf)
            buf.clear()
    if buf:
        yield bytes(buf)


def gzip_stream(chunks, level=5):
    """Compress a stream of bytes into a single gzip member."""
    c = zlib.compressobj(level, zlib.DEFLATED, 31)
    for chunk in chunks:
        out = c.compress(chunk)
        if out:
            yield out
    yield c.flush()


def build(source_path, target_folder, version):
    """Split an uncompressed XMLTV file into a GuideIndex folder. Returns the index."""
    tmp = target_folder + ".building"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with open(source_path, "rb") as src:
        if os.fstat(src.fileno()).st_size == 0:
            raise ValueError("guide is empty")
        mm = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            starts = [p for p in (mm.find(b"<channel"), mm.find(b"<programme")) if p >= 0]
            if not starts:
                raise ValueError("guide has no channels or programmes")
            head_end = min(starts)
            head = bytes(mm[:head_end])
            if b"<tv" not in head:
                raise ValueError("guide has no <tv> element")

            order, known, chan_spans, prog_spans = [], set(), {}, {}
            pos = head_end
            # Next known positions of each tag. A search is only repeated once the cursor has passed
            # the cached hit, so a guide with all channels first never rescans the file per programme.
            cs = ps = -2
            while True:
                if cs != -1 and cs < pos:
                    cs = mm.find(b"<channel", pos)
                if ps != -1 and ps < pos:
                    ps = mm.find(b"<programme", pos)
                if cs < 0 and ps < 0:
                    break
                if cs >= 0 and (ps < 0 or cs < ps):
                    tag_end = mm.find(b">", cs)
                    if tag_end < 0:
                        break
                    if mm[tag_end - 1:tag_end] == b"/":
                        end = tag_end + 1
                    else:
                        close = mm.find(b"</channel>", cs)
                        if close < 0:
                            break
                        end = close + len(b"</channel>")
                    m = CHANNEL_OPEN.match(mm, cs)
                    if m:
                        cid = html.unescape(m.group(1).decode("utf-8", "replace"))
                        if cid not in known:
                            known.add(cid)
                            order.append(cid)
                        chan_spans[cid] = (cs, end)
                    pos = end
                else:
                    close = mm.find(b"</programme>", ps)
                    if close < 0:
                        break
                    end = close + len(b"</programme>")
                    m = PROGRAMME_CHANNEL.search(mm, ps, mm.find(b">", ps))
                    if m:
                        cid = html.unescape(m.group(1).decode("utf-8", "replace"))
                        prog_spans.setdefault(cid, []).append((ps, end))
                        if cid not in known:
                            known.add(cid)
                            order.append(cid)
                    pos = end

            ranges = {}
            with open(os.path.join(tmp, "channels.xml"), "wb") as chans, \
                    open(os.path.join(tmp, "programmes.xml"), "wb") as progs:
                for cid in order:
                    c_off = chans.tell()
                    if cid in chan_spans:
                        s, e = chan_spans[cid]
                        chans.write(b"  " + mm[s:e] + b"\n")
                    p_off = progs.tell()
                    for s, e in prog_spans.get(cid, ()):
                        progs.write(b"  " + mm[s:e] + b"\n")
                    ranges[cid] = [c_off, chans.tell() - c_off, p_off, progs.tell() - p_off]
        finally:
            mm.close()

    with open(os.path.join(tmp, "head.xml"), "wb") as f:
        f.write(head.rstrip() + b"\n")
    with open(os.path.join(tmp, "index.json"), "w") as f:
        json.dump({"version": version, "built_at": int(time.time()), "order": order, "ranges": ranges}, f)
    shutil.rmtree(target_folder, ignore_errors=True)
    os.replace(tmp, target_folder)
    return GuideIndex(target_folder)


class GuideStore:
    """Guide indexes per layout, rebuilt when IPTV Boss's guide changes."""

    def __init__(self, data_dir):
        self.root = os.path.join(data_dir, "guides")
        os.makedirs(self.root, exist_ok=True)
        self._indexes = {}
        self._checked = {}
        self._guard = threading.Lock()
        for name in os.listdir(self.root):
            folder = os.path.join(self.root, name)
            if name.startswith("download-"):
                try:
                    os.unlink(folder)
                except OSError:
                    pass
                continue
            if name.endswith(".building"):
                shutil.rmtree(folder, ignore_errors=True)
                continue
            if name.isdigit() and os.path.exists(os.path.join(folder, "index.json")):
                try:
                    self._indexes[int(name)] = GuideIndex(folder)
                except (OSError, ValueError, KeyError):
                    shutil.rmtree(folder, ignore_errors=True)

    def get(self, layout):
        return self._indexes.get(layout)

    def checked_recently(self, layout, seconds):
        return time.time() - self._checked.get(layout, 0) < seconds

    def mark_checked(self, layout):
        self._checked[layout] = time.time()

    def spool(self):
        """A temp file in the data folder for downloading a guide."""
        return tempfile.NamedTemporaryFile(dir=self.root, prefix="download-", delete=False)

    def install(self, layout, source_path, version):
        with self._guard:
            index = build(source_path, os.path.join(self.root, str(layout)), version)
            self._indexes[layout] = index
            return index


def file_version(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
