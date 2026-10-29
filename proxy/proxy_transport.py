"""Explicit outbound HTTPX clients; no environment proxy or direct fallback."""
import hashlib
import inspect
import json

import httpx

from .outbound_proxy import proxy_url


def proxy_fingerprint(profile):
    """Opaque pool/cache identity including rotated credentials; never log it."""
    if profile is None:
        return 'direct'
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class _PinnedProxyTransport(httpx.AsyncHTTPTransport):
    """Retain original TLS hostname while CONNECT targets an SSRF-checked IP.

    httpcore 1.0.x honors sni_hostname for direct/SOCKS connections but not its
    HTTP CONNECT tunnel. Its trace hook receives the actual start_tls kwargs;
    override only that tunnel's server_hostname before TLS starts. Certificate
    validation remains enabled using HTTPX's default SSLContext. Covered by a
    real local CONNECT/TLS test; keep this regression test when upgrading HTTPX.
    No connection reuse across pinned virtual hosts (keepalive disabled).
    """
    async def handle_async_request(self, request):
        hostname = request.extensions.get('sni_hostname')
        if not hostname:
            return await super().handle_async_request(request)
        original = request.extensions.get('trace')

        async def trace(event, info):
            if event == 'proxy.start_tls.started' or event == 'http_proxy.start_tls.started':
                info['server_hostname'] = hostname
            if original is not None:
                result = original(event, info)
                if inspect.isawaitable(result):
                    await result

        request.extensions['trace'] = trace
        try:
            return await super().handle_async_request(request)
        finally:
            if original is None:
                request.extensions.pop('trace', None)
            else:
                request.extensions['trace'] = original


def proxy_client_options(profile=None, transport=None, *, limits=None):
    """Only an explicitly injected MockTransport suppresses the actual proxy.

    HTTPX proxy= creates a mount that bypasses the injected MockTransport. Use
    one explicit transport instead, so production always proxies, tests stay
    wholly intercepted, and the pinned-IP TLS hostname remains intact.
    """
    options = {'transport': transport, 'trust_env': False, 'follow_redirects': False}
    if profile is not None:
        url = proxy_url(profile)
        if not isinstance(transport, httpx.MockTransport):
            options['transport'] = _PinnedProxyTransport(proxy=url, trust_env=False,
                limits=limits or httpx.Limits(max_keepalive_connections=0))
    return options


def async_client(profile=None, transport=None, **options):
    """Construct with fixed error messages; never leak credential-bearing URLs."""
    try:
        return httpx.AsyncClient(**proxy_client_options(profile, transport, limits=options.get('limits')), **options)
    except (ImportError, ValueError, TypeError):
        raise ValueError('outbound_proxy_unavailable') from None
