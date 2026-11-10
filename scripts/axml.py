"""Patch integer attributes in a binary AndroidManifest.xml.

Only touches attributes found by their android: resource id, so string pools and
offsets never change. Used to lower minSdkVersion and to bump versionCode.
"""
import struct

MIN_SDK_VERSION = 0x0101020C
VERSION_CODE = 0x0101021B

TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11


def _walk_attrs(data):
    """Yield (offset_of_attr, res_id) for every attribute of every start element."""
    typ, hsz, _total = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0003:
        raise ValueError("not a binary XML file")
    res_ids = []
    off = hsz
    while off < len(data):
        ctype, chsz, csize = struct.unpack_from("<HHI", data, off)
        if ctype == 0x0180:  # resource map
            n = (csize - chsz) // 4
            res_ids = list(struct.unpack_from("<%dI" % n, data, off + chsz))
        elif ctype == 0x0102:  # start element
            ext = off + chsz
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, ext + 8)
            a = ext + attr_start
            for i in range(attr_count):
                p = a + i * attr_size
                name_idx = struct.unpack_from("<I", data, p + 4)[0]
                if name_idx < len(res_ids):
                    yield p, res_ids[name_idx]
        if csize <= 0:
            raise ValueError("bad chunk size")
        off += csize


def get_int(data, res_id):
    vals = []
    for p, rid in _walk_attrs(data):
        if rid == res_id:
            if data[p + 15] not in (TYPE_INT_DEC, TYPE_INT_HEX):
                raise ValueError("attribute %#x is not an int (type %#x)" % (res_id, data[p + 15]))
            vals.append(struct.unpack_from("<I", data, p + 16)[0])
    if len(vals) != 1:
        raise ValueError("expected one attribute %#x, found %d" % (res_id, len(vals)))
    return vals[0]


def set_int(data, res_id, value):
    """Set an int attribute in place (data must be a bytearray). Returns the old value."""
    old = None
    count = 0
    for p, rid in _walk_attrs(data):
        if rid == res_id:
            if data[p + 15] not in (TYPE_INT_DEC, TYPE_INT_HEX):
                raise ValueError("attribute %#x is not an int (type %#x)" % (res_id, data[p + 15]))
            old = struct.unpack_from("<I", data, p + 16)[0]
            struct.pack_into("<I", data, p + 16, value)
            count += 1
    if count != 1:
        raise ValueError("expected one attribute %#x, found %d" % (res_id, count))
    return old
