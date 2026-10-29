"""Validated explicit outbound proxies. Never include secrets in validation errors.

Proxy endpoints are administrator-controlled: private/loopback hosts are allowed.
Keep the hostname intact so HTTPS proxies retain certificate validation and SNI.
The transport resolves proxy DNS; destination SSRF checks remain independent.
"""
import ipaddress
import re
from urllib.parse import quote

_SCHEMES = {'http', 'https', 'socks5', 'socks5h'}
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
_LABEL = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z')


def normalize_profile(value):
    if not isinstance(value, dict) or set(value) - {'id', 'name', 'scheme', 'host', 'port', 'username', 'password', 'has_password', 'clear_password', 'enabled'}:
        raise ValueError('invalid_outbound_proxy')
    id = value.get('id')
    if not isinstance(id, str) or not _ID.fullmatch(id):
        raise ValueError('invalid_proxy_id')
    name = value.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError('invalid_proxy_name')
    scheme = value.get('scheme')
    if not isinstance(scheme, str) or scheme not in _SCHEMES:
        raise ValueError('invalid_proxy_scheme')
    host = value.get('host')
    if not isinstance(host, str) or not host or len(host) > 253 or host != host.strip() or '%' in host:
        raise ValueError('invalid_proxy_host')
    if host.startswith('[') and host.endswith(']'):
        try:
            host = str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:
            raise ValueError('invalid_proxy_host') from None
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('invalid_proxy_host') from None
        dns_name = host[:-1] if host.endswith('.') else host
        if not host or len(host) > 253 or not all(_LABEL.fullmatch(part) for part in dns_name.split('.')):
            raise ValueError('invalid_proxy_host')
    port = value.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid_proxy_port')
    result = {'id': id, 'name': name.strip(), 'scheme': scheme, 'host': host, 'port': port}
    for key in ('username', 'password'):
        if key not in value and key == 'password':
            continue
        secret = value.get(key, '')
        if not isinstance(secret, str) or len(secret) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            raise ValueError('invalid_proxy_' + key)
        try:
            secret.encode('utf-8')
        except UnicodeError:
            raise ValueError('invalid_proxy_' + key) from None
        result[key] = secret
    for key in ('has_password', 'clear_password', 'enabled'):
        if key in value:
            if not isinstance(value[key], bool):
                raise ValueError('invalid_proxy_' + key)
            result[key] = value[key]
    if result.get('clear_password') and result.get('password'):
        raise ValueError('conflicting_proxy_password')
    return result


def normalize_profiles(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('invalid_outbound_proxies')
    result, ids = [], set()
    for item in value:
        profile = normalize_profile(item)
        if profile['id'] in ids:
            raise ValueError('duplicate_proxy')
        ids.add(profile['id'])
        result.append(profile)
    return result


def profile_for(config, proxy_id):
    """Resolve an optional selection. Missing/invalid selections never go direct."""
    if proxy_id is None or proxy_id == '':
        return None
    if not isinstance(proxy_id, str) or not _ID.fullmatch(proxy_id) or not isinstance(config, dict):
        raise ValueError('invalid_proxy_reference')
    profiles = normalize_profiles(config.get('outbound_proxies', []))
    matches = [profile for profile in profiles if profile['id'] == proxy_id]
    if len(matches) != 1:
        raise ValueError('unknown_proxy')
    profile = matches[0]
    if profile.get('enabled') is False:
        raise ValueError('proxy_disabled')
    if profile.get('has_password') and not profile.get('password'):
        raise ValueError('proxy_password_unavailable')
    return profile


resolve_proxy = profile_for


def proxy_url(profile):
    """Credential-bearing transport input only; never log or return via admin API."""
    profile = normalize_profile(profile)
    if profile.get('enabled') is False:
        raise ValueError('proxy_disabled')
    if profile.get('has_password') and not profile.get('password'):
        raise ValueError('proxy_password_unavailable')
    host = profile['host']
    if ':' in host:
        host = '[' + host + ']'
    username, password = profile['username'], profile.get('password', '')
    auth = ''
    if username or password:
        auth = quote(username, safe='') + ':' + quote(password, safe='') + '@'
    return f"{profile['scheme']}://{auth}{host}:{profile['port']}"


def client_kwargs(config, proxy_id, transport=None):
    """Compatibility wrapper; transport module owns HTTPX construction/pooling."""
    from .proxy_transport import proxy_client_options
    return proxy_client_options(profile_for(config, proxy_id), transport)
