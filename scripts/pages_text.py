#!/usr/bin/env python3
"""Recover the body text of an Apple iWork (.pages/.numbers) document.

No iWork install, no third-party packages. IWA is snappy-compressed protobuf
chunks, so this decodes the framing and raw snappy blocks itself, then stitches
the text runs back together.

Why stitching rather than a clean protobuf walk: the text lives in nested
TSWP.StorageArchive messages whose field numbering moves between iWork
versions, and a bad guess makes the walker stop early and return nothing.
What is stable is the *run* structure — Pages splits a paragraph into runs at
every formatting change ("400 Ribes " + "\u2018Heart" + "\u2019s Delight"), and
the runs appear in the buffer in document order. So we take printable runs in
byte order and start a new paragraph whenever a run looks like the start of one
(a leading quantity, or a heading). Everything else appends verbatim, so the
spaces between runs survive.

    python3 pages_text.py <file.pages> [--runs]

--runs dumps the raw run list with repr(), which is what to look at when a
document's paragraph shape is unusual.
"""
import re
import sys
import zipfile


# ---------- snappy (raw block format) ----------
def snappy_decompress(src: bytes) -> bytes:
    pos = 0
    length = 0
    shift = 0
    while True:
        b = src[pos]
        pos += 1
        length |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7

    out = bytearray()
    n = len(src)
    while pos < n:
        tag = src[pos]
        pos += 1
        kind = tag & 0x03

        if kind == 0:  # literal
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                ln = int.from_bytes(src[pos:pos + extra], "little") + 1
                pos += extra
            out += src[pos:pos + ln]
            pos += ln
            continue

        if kind == 1:  # copy with 1-byte offset
            ln = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | src[pos]
            pos += 1
        elif kind == 2:  # copy with 2-byte offset
            ln = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 2], "little")
            pos += 2
        else:  # copy with 4-byte offset
            ln = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 4], "little")
            pos += 4

        if offset == 0 or offset > len(out):
            break
        start = len(out) - offset
        for i in range(ln):  # overlapping copies are legal in snappy
            out.append(out[start + i])

    return bytes(out[:length])


def document_payload(path: str) -> bytes:
    """Decompressed bytes of Index/Document.iwa (the body text lives here)."""
    with zipfile.ZipFile(path) as zf:
        raw = zf.read("Index/Document.iwa")
    out = bytearray()
    pos = 0
    while pos + 4 <= len(raw):
        chunk_type = raw[pos]
        ln = int.from_bytes(raw[pos + 1:pos + 4], "little")
        body = raw[pos + 4:pos + 4 + ln]
        pos += 4 + ln
        if len(body) != ln:
            break
        out += snappy_decompress(body) if chunk_type == 0 else body
    return bytes(out)


# Printable ASCII plus UTF-8 continuation/lead bytes; spaces are kept because
# they are what separate a run from the run that continues it.
RUN_RE = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]*")

QUOTE_START = ("\u2018", "\u2019", "\u201c", "\u201d", "'", "\"")
HEADING_RE = re.compile(
    r"^(Total:|Week of |For \d|For [A-Za-z0-9 ]{1,12}( this week)?:|\d+\s*(Gallon|gal)\b)",
    re.I,
)
QTY_LINE_RE = re.compile(r"^\s*\d[\d,]*\s+\S")


def starts_paragraph(s: str) -> bool:
    if QTY_LINE_RE.match(s):
        return True
    if HEADING_RE.match(s.strip()):
        return True
    return False


def extract_runs(payload: bytes):
    """Printable runs in document order, with spaces preserved."""
    out = []
    for m in RUN_RE.finditer(payload):
        try:
            s = m.group().decode("utf-8", "strict")
        except UnicodeDecodeError:
            continue
        if not s:
            continue
        # a run that is only a style/proto token is noise
        if len(s) < 2 and not starts_paragraph(s):
            continue
        out.append(s)
    return out


def looks_like_run(s: str) -> bool:
    """Filter proto noise: keep runs that could be document text."""
    if s.startswith(QUOTE_START) or starts_paragraph(s):
        return True
    if re.search(r"[A-Za-z]{2}", s):
        return True
    return False


def reconstruct(runs) -> str:
    paras = []
    cur = ""
    for s in runs:
        if not looks_like_run(s):
            continue
        if starts_paragraph(s) and cur:
            paras.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        paras.append(cur)
    return "\n".join(p.rstrip() for p in paras)


def trim_to_body(text: str) -> str:
    """Keep only the list: first size/week heading through the last 'Total:'.

    The recovered text otherwise starts with the document's internal locale and
    style tables and ends with a wall of UUIDs.
    """
    lines = text.split("\n")
    start_re = re.compile(
        r"^(?:For\s+)?\d+\s*(?:\"|inch\b|in\b|gallon\b|gal\b)|^Week of |^Liners?$", re.I
    )
    start = next((i for i, l in enumerate(lines) if start_re.match(l.strip())), 0)
    end = max((i for i, l in enumerate(lines) if l.strip().lower().startswith("total:")), default=len(lines) - 1)
    # the last Total line usually has the document's UUID tail glued onto it
    m = re.match(r"\s*(Total:\s*[\d,]+)", lines[end])
    if m:
        lines[end] = m.group(1)
    return "\n".join(lines[start:end + 1]).strip()


def main():
    path = sys.argv[1]
    payload = document_payload(path)
    runs = extract_runs(payload)

    if "--runs" in sys.argv:
        for r in runs:
            print(repr(r))
        return

    text = trim_to_body(reconstruct(runs))
    # collapse runs of blank lines, keep single blank separators
    out = []
    blank = 0
    for line in text.split("\n"):
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line.rstrip())
    print("\n".join(out).strip())


if __name__ == "__main__":
    main()
