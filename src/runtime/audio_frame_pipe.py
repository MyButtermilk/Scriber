from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Final

AUDIO_FRAME_MAGIC: Final[bytes] = b"SAF1"
AUDIO_FRAME_VERSION: Final[int] = 1
AUDIO_FRAME_HEADER_LEN: Final[int] = 36
AUDIO_FRAME_MAX_PAYLOAD_BYTES: Final[int] = 1024 * 1024
AUDIO_FRAME_FLAG_PREBUFFER: Final[int] = 0x0001
AUDIO_FRAME_FLAG_END_OF_STREAM: Final[int] = 0x0002

_HEADER_STRUCT = struct.Struct("<4sHHIQQIHH")


class AudioFrameProtocolError(ValueError):
    """Raised when a Rust audio-frame pipe message violates the protocol."""


@dataclass(frozen=True)
class AudioFrameHeader:
    payload_len: int
    sequence: int
    timestamp_micros: int
    frame_count: int
    channels: int
    flags: int = 0

    @property
    def expected_payload_len(self) -> int:
        return int(self.channels) * int(self.frame_count) * 2

    def validate(self) -> None:
        if not (1 <= int(self.channels) <= 16):
            raise AudioFrameProtocolError(f"invalid audio frame channel count: {self.channels}")
        if int(self.frame_count) <= 0:
            raise AudioFrameProtocolError(f"invalid audio frame count: {self.frame_count}")
        if not (0 <= int(self.payload_len) <= AUDIO_FRAME_MAX_PAYLOAD_BYTES):
            raise AudioFrameProtocolError(f"audio frame payload too large: {self.payload_len}")
        expected = self.expected_payload_len
        if int(self.payload_len) != expected:
            raise AudioFrameProtocolError(
                f"audio frame payload length mismatch: expected {expected}, got {self.payload_len}"
            )

    def encode(self) -> bytes:
        self.validate()
        return _HEADER_STRUCT.pack(
            AUDIO_FRAME_MAGIC,
            AUDIO_FRAME_HEADER_LEN,
            AUDIO_FRAME_VERSION,
            int(self.payload_len),
            int(self.sequence),
            int(self.timestamp_micros),
            int(self.frame_count),
            int(self.channels),
            int(self.flags),
        )


class AudioFrameSequenceGuard:
    def __init__(self) -> None:
        self._next_sequence = 0

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def verify_and_advance(self, header: AudioFrameHeader) -> None:
        if int(header.sequence) != self._next_sequence:
            raise AudioFrameProtocolError(
                f"audio frame sequence out of order: expected {self._next_sequence}, got {header.sequence}"
            )
        self._next_sequence += 1


def decode_audio_frame_header(data: bytes | bytearray | memoryview) -> AudioFrameHeader:
    view = memoryview(data)
    if len(view) < AUDIO_FRAME_HEADER_LEN:
        raise AudioFrameProtocolError(
            f"audio frame header too short: expected {AUDIO_FRAME_HEADER_LEN}, got {len(view)}"
        )
    (
        magic,
        header_len,
        version,
        payload_len,
        sequence,
        timestamp_micros,
        frame_count,
        channels,
        flags,
    ) = _HEADER_STRUCT.unpack(bytes(view[:AUDIO_FRAME_HEADER_LEN]))
    if magic != AUDIO_FRAME_MAGIC:
        raise AudioFrameProtocolError(f"invalid audio frame magic: {magic!r}")
    if header_len != AUDIO_FRAME_HEADER_LEN:
        raise AudioFrameProtocolError(f"invalid audio frame header length: {header_len}")
    if version != AUDIO_FRAME_VERSION:
        raise AudioFrameProtocolError(f"unsupported audio frame version: {version}")
    header = AudioFrameHeader(
        payload_len=int(payload_len),
        sequence=int(sequence),
        timestamp_micros=int(timestamp_micros),
        frame_count=int(frame_count),
        channels=int(channels),
        flags=int(flags),
    )
    header.validate()
    return header


def encode_audio_frame(header: AudioFrameHeader, payload: bytes | bytearray | memoryview) -> bytes:
    payload_bytes = bytes(payload)
    header.validate()
    if len(payload_bytes) != int(header.payload_len):
        raise AudioFrameProtocolError(
            f"audio frame payload length mismatch: expected {header.payload_len}, got {len(payload_bytes)}"
        )
    return header.encode() + payload_bytes


def decode_audio_frame(data: bytes | bytearray | memoryview) -> tuple[AudioFrameHeader, bytes]:
    view = memoryview(data)
    header = decode_audio_frame_header(view)
    expected_len = AUDIO_FRAME_HEADER_LEN + int(header.payload_len)
    if len(view) != expected_len:
        raise AudioFrameProtocolError(
            f"audio frame payload length mismatch: expected {expected_len}, got {len(view)}"
        )
    return header, bytes(view[AUDIO_FRAME_HEADER_LEN:])
