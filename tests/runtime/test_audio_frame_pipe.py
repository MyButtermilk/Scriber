import pytest

from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AUDIO_FRAME_FLAG_PREBUFFER,
    AUDIO_FRAME_HEADER_LEN,
    AudioFrameHeader,
    AudioFrameProtocolError,
    AudioFrameSequenceGuard,
    decode_audio_frame,
    decode_audio_frame_header,
    encode_audio_frame,
)


DOCUMENTED_HEADER_HEX = (
    "5341463124000200000800002a0000000000000015cd5b07000000000002000002000100"
)


def documented_header() -> AudioFrameHeader:
    return AudioFrameHeader(
        payload_len=2048,
        sequence=42,
        timestamp_micros=123_456_789,
        frame_count=512,
        channels=2,
        flags=AUDIO_FRAME_FLAG_PREBUFFER,
    )


def test_audio_frame_header_round_trips_with_documented_layout():
    header = documented_header()
    encoded = header.encode()

    assert len(encoded) == AUDIO_FRAME_HEADER_LEN
    assert encoded.hex() == DOCUMENTED_HEADER_HEX
    assert decode_audio_frame_header(encoded) == header


def test_audio_frame_round_trip_preserves_payload():
    header = documented_header()
    payload = bytes([7]) * header.payload_len

    encoded = encode_audio_frame(header, payload)
    decoded_header, decoded_payload = decode_audio_frame(encoded)

    assert decoded_header == header
    assert decoded_payload == payload


def test_audio_frame_decode_rejects_bad_magic():
    encoded = bytearray(documented_header().encode())
    encoded[0] = ord("X")

    with pytest.raises(AudioFrameProtocolError, match="magic"):
        decode_audio_frame_header(encoded)


def test_audio_frame_decode_rejects_payload_length_mismatch():
    header = documented_header()

    with pytest.raises(AudioFrameProtocolError, match="payload length mismatch"):
        encode_audio_frame(header, b"too short")


def test_audio_frame_sequence_guard_rejects_out_of_order_frame():
    guard = AudioFrameSequenceGuard()
    first = AudioFrameHeader(
        payload_len=1024,
        sequence=0,
        timestamp_micros=1,
        frame_count=512,
        channels=1,
    )
    skipped = AudioFrameHeader(
        payload_len=1024,
        sequence=2,
        timestamp_micros=2,
        frame_count=512,
        channels=1,
    )

    guard.verify_and_advance(first)

    with pytest.raises(AudioFrameProtocolError, match="expected 1, got 2"):
        guard.verify_and_advance(skipped)


def test_zero_length_end_of_stream_control_frame_round_trips():
    header = AudioFrameHeader(
        payload_len=0,
        sequence=1,
        timestamp_micros=123,
        frame_count=0,
        channels=1,
        flags=AUDIO_FRAME_FLAG_END_OF_STREAM,
    )

    decoded_header, decoded_payload = decode_audio_frame(encode_audio_frame(header, b""))

    assert decoded_header == header
    assert decoded_payload == b""


def test_zero_length_non_eos_frame_is_rejected():
    header = AudioFrameHeader(
        payload_len=0,
        sequence=0,
        timestamp_micros=0,
        frame_count=0,
        channels=1,
    )

    with pytest.raises(AudioFrameProtocolError, match="frame count"):
        header.validate()
