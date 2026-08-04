"""Parsers for Loxone's websocket state-stream binary tables.

Only what this project needs: the 8-byte message header and the text-states
table (that's how nfcLearnResult arrives). Value / daytimer / weather tables
are ignored — add them if a future feature needs them.

Parser layout derived from pyloxone-api 0.2.4 (message.py), which itself
mirrors Loxone's Communicating with the Miniserver documentation.
"""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum


class MessageType(IntEnum):
    TEXT = 0
    BINARY = 1
    VALUE_STATES = 2
    TEXT_STATES = 3
    DAYTIMER_STATES = 4
    OUT_OF_SERVICE = 5
    KEEPALIVE = 6
    WEATHER_STATES = 7


@dataclass(frozen=True)
class Header:
    message_type: MessageType
    payload_length: int
    estimated: bool


def parse_header(buf: bytes) -> Header:
    if len(buf) != 8 or buf[0] != 0x03:
        raise ValueError(f"not a Loxone message header: {buf!r}")
    _, mtype, info, _reserved, length = struct.unpack("<BBBBI", buf)
    return Header(MessageType(mtype), length, bool(info >> 7))


def _loxone_uuid(raw: bytes) -> str:
    """Loxone UUIDs render as xxxxxxxx-xxxx-xxxx-xxxxYYYYYYYYYYYY — four
    hyphens, no dash between the 4-char group and the trailing 12 chars."""
    u = uuid.UUID(bytes_le=raw)
    a, b, c, d, e = str(u).split("-")
    return f"{a}-{b}-{c}-{d}{e}"


def parse_text_states(buf: bytes) -> dict[str, str]:
    """Return {state_uuid: text} for every record in a text-states table.

    Record layout (aligned to 4 bytes):
        16 bytes  state uuid (little-endian first three groups)
        16 bytes  icon uuid
         4 bytes  text length (LE uint32)
         N bytes  UTF-8 text
        padding   to next 4-byte boundary
    """
    out: dict[str, str] = {}
    off = 0
    n = len(buf)
    while off < n:
        state_uuid = _loxone_uuid(buf[off : off + 16])
        # buf[off+16:off+32] is the icon uuid — not needed
        (text_len,) = struct.unpack("<I", buf[off + 32 : off + 36])
        text = buf[off + 36 : off + 36 + text_len].decode("utf-8", errors="replace")
        out[state_uuid] = text
        record_size = 36 + text_len  # 16 + 16 + 4 + text
        off += ((record_size - 1) // 4 + 1) * 4  # round up to 4-byte boundary
    return out
