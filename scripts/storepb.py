"""Read a Mihon extension store index in protobuf form (index.pb) into the JSON shape.

Keiyoushi publishes the same store as index.json and as index.pb (gzip-compressed
protobuf). build_store.py reads index.json; this is the fallback if that ever goes away.
Field numbers follow Mihon's NetworkExtensionStore (data/.../NetworkExtensionStore.kt);
jarUrl is Keiyoushi's own extra field 501 in Resources, which Mihon ignores.

The result matches Keiyoushi's index.json: versionCode and source ids are strings,
contentWarning is "CONTENT_WARNING_*", absent optional fields are left out.

usage (check): python storepb.py index.pb [index.json]
"""
import gzip
import json
import sys

WARNINGS = {0: "CONTENT_WARNING_UNSPECIFIED", 1: "CONTENT_WARNING_SAFE",
            2: "CONTENT_WARNING_MIXED", 3: "CONTENT_WARNING_NSFW"}


def _varint(b, i):
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if c < 0x80:
            return n, i
        shift += 7


def _fields(b):
    """Yield (field number, value): int for varints, bytes for length-delimited."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wire == 1:
            v, i = b[i:i + 8], i + 8
        elif wire == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire)
        yield num, v


def _int64(n):
    return n - (1 << 64) if n >= 1 << 63 else n


def _msg(b, spec):
    """spec: {num: (name, kind)}; kind is 'str', 'int', 'warn', a nested spec (dict),
    or a 1-item list of one of those for repeated fields."""
    out = {}
    for num, v in _fields(b):
        if num not in spec:
            continue
        name, kind = spec[num]
        rep = isinstance(kind, list)
        k = kind[0] if rep else kind
        if isinstance(k, dict):
            val = _msg(v, k)
        elif k == "str":
            val = v.decode("utf-8")
        elif k == "int":
            val = str(_int64(v))
        elif k == "warn":
            val = WARNINGS.get(v, "CONTENT_WARNING_UNSPECIFIED")
        else:
            raise AssertionError(k)
        if rep:
            out.setdefault(name, []).append(val)
        else:
            out[name] = val
    return out


SOURCE = {1: ("id", "int"), 2: ("name", "str"), 3: ("language", "str"), 4: ("homeUrl", "str"),
          5: ("mirrorUrls", ["str"]), 7: ("message", "str")}
RESOURCES = {1: ("apkUrl", "str"), 2: ("iconUrl", "str"), 501: ("jarUrl", "str")}
EXTENSION = {1: ("name", "str"), 2: ("packageName", "str"), 3: ("resources", RESOURCES),
             4: ("extensionLib", "str"), 5: ("versionCode", "int"), 6: ("versionName", "str"),
             7: ("contentWarning", "warn"), 8: ("sources", [SOURCE])}
STORE = {1: ("name", "str"), 2: ("badgeLabel", "str"), 3: ("signingKey", "str"),
         4: ("contact", {1: ("website", "str"), 2: ("discord", "str")}),
         101: ("extensionList", {1: ("extensions", [EXTENSION])}),
         102: ("extensionListUrl", "str")}


def parse(data):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    store = _msg(data, STORE)
    for e in store.get("extensionList", {}).get("extensions", []):
        e.setdefault("sources", [])
        # proto3 leaves out zero values: an unset enum means UNSPECIFIED.
        e.setdefault("contentWarning", WARNINGS[0])
    return store


def main():
    store = parse(open(sys.argv[1], "rb").read())
    exts = store.get("extensionList", {}).get("extensions", [])
    print("extensions:", len(exts))
    if len(sys.argv) > 2:
        ref = json.load(open(sys.argv[2], encoding="utf-8"))
        a = {e["packageName"]: e for e in exts}
        b = {e["packageName"]: e for e in ref["extensionList"]["extensions"]}
        diff = [p for p in sorted(set(a) | set(b)) if a.get(p) != b.get(p)]
        print("differ from json:", len(diff))
        for p in diff[:5]:
            print(" ", p, "\n   pb:  ", json.dumps(a.get(p))[:400], "\n   json:", json.dumps(b.get(p))[:400])
        top = {k: v for k, v in store.items() if k != "extensionList"}
        rtop = {k: v for k, v in ref.items() if k != "extensionList"}
        print("top-level match:", top == rtop, "" if top == rtop else (top, rtop))
        sys.exit(1 if diff else 0)


if __name__ == "__main__":
    main()
