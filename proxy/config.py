"""Strict config validation; capability metadata does not rewrite request bodies."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
BOUNDS = {
    "request_timeout_s": (5, 900), "connect_timeout_s": (1, 60),
    "read_timeout_s": (1, 600), "cooldown_s": (1, 3600),
    "failure_threshold": (1, 20), "max_request_bytes": (1024, 67108864),
    "max_response_bytes": (1024, 67108864), "max_buffer_events": (16, 131072),
    "max_concurrent_requests": (1, 64), "max_attempts": (1, 10),
}


def text(value, field, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(x) < 32 for x in value):
        raise ValueError(f"invalid_{field}")
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("invalid_id")
    return value


def public_ip(value):
    addr = ipaddress.ip_address(value)
    return addr.is_global and not addr.is_multicast and not addr.is_unspecified


def base_url(value, private=False):
    value = text(value, "base_url", 1000)
    p = urlsplit(value)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("invalid_base_url")
    try:
        p.port
    except ValueError:
        raise ValueError("invalid_base_url") from None
    host = p.hostname.lower()
    if not private:
        if p.scheme != "https":
            raise ValueError("upstream_https_required")
        if host in ("localhost", "metadata.google.internal") or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("private_upstream_forbidden")
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not public_ip(str(addr)):
                raise ValueError("private_upstream_forbidden")
    return value.rstrip("/")


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("invalid_config")
    settings = value.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("invalid_settings")
    if set(settings) - set(BOUNDS) - {"trust_private_upstreams"}:
        raise ValueError("unknown_settings")
    clean = {}
    for key, (lo, hi) in BOUNDS.items():
        v = settings.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or int(v) != v:
            raise ValueError("invalid_" + key)
        clean[key] = int(v)
    private = settings.get("trust_private_upstreams", False)
    if not isinstance(private, bool):
        raise ValueError("invalid_trust_private_upstreams")
    clean["trust_private_upstreams"] = private
    from .outbound_proxy import normalize_profiles
    profiles = normalize_profiles(value.get('outbound_proxies', []))
    proxy_ids = {profile['id'] for profile in profiles}
    providers, models = value.get("providers"), value.get("models")
    if not isinstance(providers, list) or len(providers) > 50 or not isinstance(models, list) or len(models) > 100:
        raise ValueError("invalid_providers_or_models")
    ids, output = set(), []
    for row in providers:
        if not isinstance(row, dict):
            raise ValueError('invalid_provider')
        id = identifier(row.get("id"))
        if id in ids:
            raise ValueError("duplicate_provider")
        ids.add(id)
        priority = row.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 1000:
            raise ValueError("invalid_priority")
        if not isinstance(row.get("enabled"), bool):
            raise ValueError("invalid_enabled")
        p = {"id": id, "name": text(row.get("name"), "provider_name", 100),
             "base_url": base_url(row.get("base_url"), private), "priority": priority, "enabled": row["enabled"]}
        from .provider_policy import SERVICE_TIERS,PIN
        mode=row.get('routing_mode','native');tier=row.get('service_tier') or None;pin=row.get('provider_pin') or None
        if mode not in ('native','openrouter'):raise ValueError('invalid_routing_mode')
        if tier is not None and (not isinstance(tier,str) or tier not in SERVICE_TIERS):raise ValueError('invalid_service_tier')
        if pin is not None and (not isinstance(pin,str) or not PIN.fullmatch(pin)):raise ValueError('invalid_provider_pin')
        if mode=='native' and (tier or pin):raise ValueError('routing_policy_requires_openrouter_mode')
        if mode=='openrouter':p.update(routing_mode=mode,service_tier=tier,provider_pin=pin)
        if "api_key" in row:
            key = row["api_key"]
            if not isinstance(key, str) or len(key) > 8192 or any(ord(x) < 32 for x in key):
                raise ValueError("invalid_api_key")
            p["api_key"] = key
        if 'proxy_id' in row:
            proxy_id = row['proxy_id']
            if proxy_id is not None and (not isinstance(proxy_id, str) or proxy_id not in proxy_ids):
                raise ValueError('invalid_proxy_reference')
            p['proxy_id'] = proxy_id
        output.append(p)
    mids, result_models = set(), []
    for row in models:
        id = identifier(row.get("id"))
        name = text(row.get("name"), "model_name", 200)
        if id in mids or any(x["name"] == name for x in result_models):
            raise ValueError("duplicate_model")
        mids.add(id)
        routes = row.get("routes")
        if not isinstance(row.get("enabled"), bool) or not isinstance(row.get("supports_vision"), bool) or not isinstance(routes, list):
            raise ValueError("invalid_model")
        seen, rout = set(), []
        for r in routes:
            pid = r.get("provider_id")
            if pid not in ids or pid in seen or not isinstance(r.get("enabled"), bool):
                raise ValueError("invalid_route")
            seen.add(pid)
            rout.append({"provider_id": pid, "upstream_model": text(r.get("upstream_model"), "upstream_model", 200), "enabled": r["enabled"]})
        model = {"id": id, "name": name, "enabled": row["enabled"], "supports_vision": row["supports_vision"], "routes": rout}
        for key, lo in (("context_length", 1000), ("max_output_tokens", 1)):
            if key not in row:
                continue
            v = row[key]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= 10000000):
                raise ValueError("invalid_" + key)
            model[key] = v
        if 'context_preflight' in row:
            if row['context_preflight'] not in ('off','observe'):raise ValueError('invalid_context_preflight')
            model['context_preflight']=row['context_preflight']
        result_models.append(model)
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError("invalid_revision")
    result = {"revision": revision, "settings": clean, "providers": output, "models": result_models}
    if 'outbound_proxies' in value:
        result['outbound_proxies'] = profiles
    if 'api_access' in value:
        from .ip_allowlist import normalize_access
        result['api_access'] = normalize_access(value['api_access'])
    return result
