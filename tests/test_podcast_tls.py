"""Exercise the real podcast transport against a private, test-only TLS authority."""

from __future__ import annotations

import socket
import ssl
import sys
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from src.podcasts import feeds
from src.podcasts.service import PodcastService

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform != "win32", reason="Windows native trust contract")]


def _certificates(tmp_path, *, expired: bool):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Podcast test authority")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Older managed TLS certificates can be trusted by Windows without an AKI.
    # Python's strict OpenSSL defaults reject this exact shape with verify code 85.
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "podcast.example")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("podcast.example")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    return server, ca.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.mark.parametrize("failure", [None, "untrusted", "hostname", "expired"])
async def test_subscription_uses_native_trust_and_rejects_invalid_certificates(tmp_path, monkeypatch, failure):
    server_ssl, authority = _certificates(tmp_path, expired=failure == "expired")

    async def feed(_request):
        return web.Response(
            body=b'<rss><channel><title>TLS fixture</title><item><guid>one</guid><title>Episode</title><enclosure url="https://podcast.example/episode.mp3" type="audio/mpeg"/></item></channel></rss>'
        )

    app = web.Application()
    app.router.add_get("/feed", feed)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=server_ssl)
    async with server:
        # Only this test resolver maps the public fixture hostname to its local server.
        class FixtureResolver(AbstractResolver):
            async def resolve(self, host, port=0, family=socket.AF_INET):
                assert host in {"podcast.example", "wrong.example"}
                return [
                    {
                        "hostname": host,
                        "host": "127.0.0.1",
                        "port": server.port,
                        "family": socket.AF_INET,
                        "proto": socket.IPPROTO_TCP,
                        "flags": 0,
                    }
                ]

            async def close(self):
                pass

        monkeypatch.setattr(feeds, "PublicResolver", FixtureResolver)
        transport = feeds.PodcastHttp()
        session = transport._client()
        context = session.connector._ssl
        if context is True:
            # Preserve old production defaults to make this regression red before the fix.
            context = ssl.create_default_context()
            session.connector._ssl = context
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        if failure != "untrusted":
            context.load_verify_locations(cadata=authority)
        service = PodcastService(tmp_path / "podcasts", None, transport=transport)
        try:
            url = "https://wrong.example/feed" if failure == "hostname" else "https://podcast.example/feed"
            if failure:
                with pytest.raises(aiohttp.ClientConnectorCertificateError):
                    await service.subscribe(url, auto_process=False)
                assert (await service.library())["subscriptions"] == []
            else:
                identifier = await service.subscribe(url, auto_process=False)
                assert (await service.episodes(identifier))["total"] == 1
        finally:
            await service.close()
