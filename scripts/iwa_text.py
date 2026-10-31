#!/usr/bin/env python3
"""Extract readable text strings from an Apple iWork (.pages/.numbers) IWA bundle.

No iWork app required and no third-party deps: IWA is snappy-compressed protobuf
chunks, so this decodes the framing + raw snappy blocks itself and then pulls the
printable UTF-8 runs out of the protobuf payloads.

    python3 iwa_text.py <file.pages> [--raw]

--raw prints every string in file order, including style/identifier noise.
Default filters to strings that look like document prose (contain a letter, are
not iWork type names, not hex/UUID blobs).
"""
import re
import sys
import zipfile


def snappy_decompress(src: bytes) -> bytes:
    """Raw snappy block decompression (not the framed format)."""
    pos = 0
    # preamble: uncompressed length, varint
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

        if kind == 1:  # copy, 1-byte offset
            ln = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | src[pos]
            pos += 1
        elif kind == 2:  # copy, 2-byte offset
            ln = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 2], "little")
            pos += 2
        else:  # copy, 4-byte offset
            ln = (tag >> 2) + 1
            offset = int.from_bytes(src[pos:pos + 4], "little")
            pos += 4

        if offset == 0 or offset > len(out):
            break
        start = len(out) - offset
        for i in range(ln):  # overlapping copies are legal in snappy
            out.append(out[start + i])

    return bytes(out[:length])


def iwa_payloads(path: str):
    """Yield decompressed payloads for each .iwa file in the bundle."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith(".iwa"):
                continue
            raw = zf.read(name)
            pos = 0
            while pos + 4 <= len(raw):
                chunk_type = raw[pos]
                ln = int.from_bytes(raw[pos + 1:pos + 4], "little")
                body = raw[pos + 4:pos + 4 + ln]
                pos += 4 + ln
                if len(body) != ln:
                    break
                if chunk_type == 0:
                    try:
                        body = snappy_decompress(body)
                    except Exception:
                        continue
                yield name, body


STRING_RE = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{2,}")


def strings_in(payload: bytes):
    for m in STRING_RE.finditer(payload):
        try:
            s = m.group().decode("utf-8", "strict").strip()
        except UnicodeDecodeError:
            continue
        if s:
            yield s


# iWork internals that are never document prose
NOISE = re.compile(
    r"^(TSS\.|TSWP\.|TSCH\.|TSD\.|TSA\.|KN\.|SFU\.|com\.apple\.|__|_Tt|NS|PK)",
)


def looks_like_prose(s: str) -> bool:
    if len(s) < 3 or NOISE.match(s):
        return False
    if not re.search(r"[A-Za-z]{2}", s):
        return False
    if re.fullmatch(r"[0-9a-fA-F\-]{8,}", s):
        return False
    if re.fullmatch(r"[A-Za-z0-9+/=]{40,}", s):  # base64 archives
        return False
    return True


def main():
    path = sys.argv[1]
    raw = "--raw" in sys.argv
    for name, payload in iwa_payloads(path):
        for s in strings_in(payload):
            if raw or looks_like_prose(s):
                print(f"{name}\t{s}")


if __name__ == "__main__":
    main()
