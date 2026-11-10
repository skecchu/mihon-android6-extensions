"""Read the types, methods and fields a dex file references (its id tables)."""
import struct


def _uleb_skip(d, off):
    while d[off] & 0x80:
        off += 1
    return off + 1


def _strings(d):
    size, off = struct.unpack_from("<II", d, 0x38)
    out = []
    for i in range(size):
        s = struct.unpack_from("<I", d, off + 4 * i)[0]
        s = _uleb_skip(d, s)
        e = d.index(b"\0", s)
        out.append(d[s:e].decode("utf-8", "replace"))
    return out


def refs(path):
    """Return (types, members): types is a set of descriptors, members a set of
    (class_descriptor, kind, name) with kind 'm' (method) or 'f' (field)."""
    d = open(path, "rb").read()
    if d[:4] != b"dex\n":
        raise ValueError("not a dex file: %s" % path)
    strs = _strings(d)
    tsize, toff = struct.unpack_from("<II", d, 0x40)
    types = [strs[struct.unpack_from("<I", d, toff + 4 * i)[0]] for i in range(tsize)]
    fsize, foff = struct.unpack_from("<II", d, 0x50)
    msize, moff = struct.unpack_from("<II", d, 0x58)
    members = set()
    for i in range(fsize):
        cls, _typ, name = struct.unpack_from("<HHI", d, foff + 8 * i)
        members.add((types[cls], "f", strs[name]))
    for i in range(msize):
        cls, _proto, name = struct.unpack_from("<HHI", d, moff + 8 * i)
        members.add((types[cls], "m", strs[name]))
    return set(types), members


def desc_to_class(desc):
    """'[Lj$/util/Foo$Bar;' -> 'j$.util.Foo$Bar' (None for primitives)."""
    desc = desc.lstrip("[")
    if not (desc.startswith("L") and desc.endswith(";")):
        return None
    return desc[1:-1].replace("/", ".")
