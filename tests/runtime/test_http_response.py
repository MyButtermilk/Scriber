import pytest

from src.runtime.http_response import (
    ResponseTooLargeError,
    read_response_json_limited,
    read_response_text_limited,
)


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _Response:
    charset = "utf-8"
    content_length = None

    def __init__(self, chunks):
        self.content = _Content(chunks)


@pytest.mark.asyncio
async def test_limited_response_reader_streams_body():
    response = _Response([b"hello", b" ", b"world"])
    assert await read_response_text_limited(response, 20) == "hello world"


@pytest.mark.asyncio
async def test_limited_response_reader_rejects_chunked_overflow():
    response = _Response([b"1234", b"5678"])
    with pytest.raises(ResponseTooLargeError):
        await read_response_text_limited(response, 7)


@pytest.mark.asyncio
async def test_limited_response_reader_rejects_declared_overflow():
    response = _Response([])
    response.content_length = 100
    with pytest.raises(ResponseTooLargeError):
        await read_response_text_limited(response, 10)


@pytest.mark.asyncio
async def test_limited_response_reader_falls_back_for_invalid_charset():
    response = _Response(["Grüße".encode()])
    response.charset = "not-a-real-codec"

    assert await read_response_text_limited(response, 20) == "Grüße"


@pytest.mark.asyncio
async def test_limited_json_reader_supports_json_only_test_double():
    class JsonOnlyResponse:
        async def json(self):
            return {"ok": True}

    assert await read_response_json_limited(JsonOnlyResponse(), 100) == {"ok": True}
