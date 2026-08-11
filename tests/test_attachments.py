"""SSRF regression tests for meshapi.attachments._fetch_url.

The threat: a user-supplied image URL that is public on the first hop but
302-redirects to a private/link-local target (e.g. the cloud-metadata IP
169.254.169.254). The defense is (a) follow_redirects=False and (b)
re-validating EVERY hop with safety.is_url_safe_for_fetch before issuing the
request.

We inject an httpx.MockTransport (no live network) by swapping
attachments.httpx for a shim whose Client() attaches the transport, and we
stub safety.socket.getaddrinfo so DNS resolution is fully offline too. The
handler asserts it is never invoked for a blocked host — proving the block
lands at validation, before the request goes out.
"""
import socket
import types

import httpx
import pytest

from meshapi import attachments, safety

PUBLIC_IP = "93.184.216.34"        # a literal public IP (documentation range)
METADATA_IP = "169.254.169.254"    # link-local cloud metadata endpoint
LOOPBACK_IP = "127.0.0.1"


@pytest.fixture
def offline_dns(monkeypatch):
    """Resolve numeric hosts to themselves; never touch the network."""
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, 0))]
    monkeypatch.setattr(safety.socket, "getaddrinfo", fake_getaddrinfo)


def _install_transport(monkeypatch, handler):
    """Swap attachments.httpx for a shim that injects a MockTransport into
    every Client(). Returns a dict capturing the kwargs the code passed to
    httpx.Client so the test can assert follow_redirects=False."""
    captured = {}
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        captured.update(kwargs)
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    shim = types.SimpleNamespace(
        Client=client_factory,
        HTTPError=httpx.HTTPError,
    )
    monkeypatch.setattr(attachments, "httpx", shim)
    return captured


def test_direct_blocked_ip_raises(monkeypatch, offline_dns):
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("request issued to a blocked host")

    _install_transport(monkeypatch, handler)

    with pytest.raises(attachments.AttachmentError) as ei:
        attachments._fetch_url(f"http://{METADATA_IP}/latest/meta-data")
    assert "refusing to fetch" in str(ei.value)


def test_direct_loopback_raises(monkeypatch, offline_dns):
    def handler(request):  # pragma: no cover
        raise AssertionError("request issued to loopback")

    _install_transport(monkeypatch, handler)
    with pytest.raises(attachments.AttachmentError):
        attachments._fetch_url(f"http://{LOOPBACK_IP}/x.png")


def test_redirect_to_metadata_is_blocked_at_the_hop(monkeypatch, offline_dns):
    """Public first hop 302s to the metadata IP; the second hop must be
    blocked by per-hop re-validation BEFORE any request is issued to it."""
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == PUBLIC_IP:
            return httpx.Response(
                302, headers={"Location": f"http://{METADATA_IP}/"}
            )
        # Reaching the metadata host would mean the guard failed.
        raise AssertionError(f"request reached blocked host {request.url.host}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(attachments.AttachmentError) as ei:
        attachments._fetch_url(f"http://{PUBLIC_IP}/redir.png")

    assert str(METADATA_IP) in str(ei.value)
    # The redirect target was validated and rejected before the request went
    # out: only the first (public) hop ever hit the transport.
    assert seen_hosts == [PUBLIC_IP]


def test_normal_png_returns_data_mime_name(monkeypatch, offline_dns):
    png = b"\x89PNG\r\n\x1a\n" + b"payload-bytes"

    def handler(request):
        assert request.url.host == PUBLIC_IP
        return httpx.Response(
            200, content=png, headers={"content-type": "image/png"}
        )

    captured = _install_transport(monkeypatch, handler)

    data, mime, name = attachments._fetch_url(f"http://{PUBLIC_IP}/photo.png")

    assert data == png
    assert mime == "image/png"
    assert name == "photo.png"
    # Confirm the code opened the client with redirect-following DISABLED —
    # the whole SSRF defense hinges on manual, re-validated hops.
    assert captured.get("follow_redirects") is False


def test_non_image_mime_is_rejected(monkeypatch, offline_dns):
    def handler(request):
        return httpx.Response(
            200, content=b"<html>", headers={"content-type": "text/html"}
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(attachments.AttachmentError) as ei:
        attachments._fetch_url(f"http://{PUBLIC_IP}/not-image.png")
    assert "image" in str(ei.value).lower()
