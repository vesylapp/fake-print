"""Minimal IPP/1.1-2.x wire format encoder/decoder (RFC 8010)."""

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Delimiter (group) tags
OPERATION = 0x01
JOB = 0x02
END = 0x03
PRINTER = 0x04
UNSUPPORTED_GROUP = 0x05

# Out-of-band value tags
UNSUPPORTED = 0x10
UNKNOWN = 0x12
NO_VALUE = 0x13

# Value tags
INTEGER = 0x21
BOOLEAN = 0x22
ENUM = 0x23
OCTET_STRING = 0x30
DATETIME = 0x31
RESOLUTION = 0x32
RANGE = 0x33
BEG_COLLECTION = 0x34
TEXT_LANG = 0x35
NAME_LANG = 0x36
END_COLLECTION = 0x37
TEXT = 0x41
NAME = 0x42
KEYWORD = 0x44
URI = 0x45
URI_SCHEME = 0x46
CHARSET = 0x47
LANGUAGE = 0x48
MIME = 0x49
MEMBER_NAME = 0x4A

# Operations
PRINT_JOB = 0x0002
VALIDATE_JOB = 0x0004
CREATE_JOB = 0x0005
SEND_DOCUMENT = 0x0006
CANCEL_JOB = 0x0008
GET_JOB_ATTRIBUTES = 0x0009
GET_JOBS = 0x000A
GET_PRINTER_ATTRIBUTES = 0x000B
CANCEL_MY_JOBS = 0x0039
CLOSE_JOB = 0x003B
IDENTIFY_PRINTER = 0x003C

# Status codes
OK = 0x0000
OK_IGNORED = 0x0001
BAD_REQUEST = 0x0400
NOT_FOUND = 0x0406
NOT_POSSIBLE = 0x0404
DOCUMENT_FORMAT_NOT_SUPPORTED = 0x040A
COMPRESSION_NOT_SUPPORTED = 0x040F
INTERNAL_ERROR = 0x0500
OPERATION_NOT_SUPPORTED = 0x0501
VERSION_NOT_SUPPORTED = 0x0503


@dataclass
class Request:
    version: tuple
    operation: int
    request_id: int
    groups: list = field(default_factory=list)  # [(group_tag, {name: [values]})]
    data: bytes = b""

    def group(self, tag):
        for t, attrs in self.groups:
            if t == tag:
                return attrs
        return {}

    def get(self, tag, name, default=None):
        values = self.group(tag).get(name)
        return values[0] if values else default


def _read_raw(buf, pos):
    tag = buf[pos]
    (name_len,) = struct.unpack_from(">H", buf, pos + 1)
    pos += 3
    name = buf[pos:pos + name_len].decode("utf-8", "replace")
    pos += name_len
    (value_len,) = struct.unpack_from(">H", buf, pos)
    pos += 2
    raw = buf[pos:pos + value_len]
    if len(raw) != value_len:
        raise ValueError("truncated attribute value")
    return tag, name, raw, pos + value_len


def _decode_simple(tag, raw):
    if tag in (INTEGER, ENUM):
        return struct.unpack(">i", raw)[0]
    if tag == BOOLEAN:
        return raw != b"\x00"
    if tag == RESOLUTION:
        return struct.unpack(">iib", raw)
    if tag == RANGE:
        return struct.unpack(">ii", raw)
    if tag in (TEXT_LANG, NAME_LANG):
        (lang_len,) = struct.unpack_from(">H", raw, 0)
        (text_len,) = struct.unpack_from(">H", raw, 2 + lang_len)
        return raw[4 + lang_len:4 + lang_len + text_len].decode("utf-8", "replace")
    if tag in (UNSUPPORTED, UNKNOWN, NO_VALUE):
        return None
    if 0x40 <= tag <= 0x5F:
        return raw.decode("utf-8", "replace")
    return bytes(raw)


def _read_value(buf, tag, raw, pos):
    if tag != BEG_COLLECTION:
        return _decode_simple(tag, raw), pos
    collection = {}
    member = None
    while True:
        t, _, r, pos = _read_raw(buf, pos)
        if t == END_COLLECTION:
            return collection, pos
        if t == MEMBER_NAME:
            member = r.decode("utf-8", "replace")
            collection.setdefault(member, [])
            continue
        value, pos = _read_value(buf, t, r, pos)
        collection.setdefault(member, []).append(value)


def parse(buf):
    if len(buf) < 9:
        raise ValueError("IPP message too short")
    major, minor, operation, request_id = struct.unpack_from(">bbHi", buf, 0)
    req = Request((major, minor), operation, request_id)
    pos = 8
    current = None
    last_name = None
    while True:
        if pos >= len(buf):
            raise ValueError("missing end-of-attributes tag")
        tag = buf[pos]
        if tag == END:
            pos += 1
            break
        if tag < 0x10:
            current = {}
            req.groups.append((tag, current))
            last_name = None
            pos += 1
            continue
        if current is None:
            raise ValueError("attribute outside of group")
        t, name, raw, pos = _read_raw(buf, pos)
        value, pos = _read_value(buf, t, raw, pos)
        if name:
            last_name = name
            current[name] = [value]
        elif last_name:
            current[last_name].append(value)
    req.data = bytes(buf[pos:])
    return req


# ---------------------------------------------------------------- encoding

def datetime_value(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return struct.pack(">HBBBBBBcBB", d.year, d.month, d.day, d.hour, d.minute,
                       d.second, d.microsecond // 100000, b"+", 0, 0)


def _encode_value(tag, value):
    if tag in (INTEGER, ENUM):
        return struct.pack(">i", value)
    if tag == BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == RESOLUTION:
        x, y, *units = value
        return struct.pack(">iib", x, y, units[0] if units else 3)
    if tag == RANGE:
        return struct.pack(">ii", *value)
    if tag in (UNSUPPORTED, UNKNOWN, NO_VALUE):
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _encode_attr(out, name, tag, values):
    if not isinstance(values, (list, tuple)) or tag in (RESOLUTION, RANGE) and values and isinstance(values[0], int):
        values = [values]
    if tag in (NO_VALUE, UNKNOWN, UNSUPPORTED) and not values:
        values = [None]
    for i, value in enumerate(values):
        n = name.encode() if i == 0 else b""
        if tag == BEG_COLLECTION:
            out += bytes([BEG_COLLECTION]) + struct.pack(">H", len(n)) + n + b"\x00\x00"
            for member_name, member_tag, member_values in value:
                mn = member_name.encode()
                out += bytes([MEMBER_NAME]) + b"\x00\x00" + struct.pack(">H", len(mn)) + mn
                _encode_attr(out, "", member_tag, member_values)
            out += bytes([END_COLLECTION]) + b"\x00\x00\x00\x00"
        else:
            v = _encode_value(tag, value)
            out += bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(v)) + v


def encode_response(version, status, request_id, groups):
    """groups: [(group_tag, [(name, value_tag, values), ...]), ...]"""
    out = bytearray(struct.pack(">bbHi", version[0], version[1], status, request_id))
    for group_tag, attrs in groups:
        out.append(group_tag)
        for name, tag, values in attrs:
            _encode_attr(out, name, tag, values)
    out.append(END)
    return bytes(out)
