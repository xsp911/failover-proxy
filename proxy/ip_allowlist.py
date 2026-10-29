"""Optional peer-IP access policy for public API routes only.

Forwarded headers are intentionally ignored. Uvicorn runs --no-proxy-headers.
Admin authentication remains independent to avoid locking owners out of recovery.
"""
import ipaddress


def normalize_access(value):
    if not isinstance(value,dict) or set(value)-{'enabled','allowed_ips'}:
        raise ValueError('invalid_api_access')
    enabled=value.get('enabled',False)
    entries=value.get('allowed_ips',[])
    if not isinstance(enabled,bool) or not isinstance(entries,list) or len(entries)>512:
        raise ValueError('invalid_api_access')
    networks=[]
    for entry in entries:
        if not isinstance(entry,str) or not entry.strip() or len(entry)>100 or '%' in entry:
            raise ValueError('invalid_allowed_ip')
        try:network=ipaddress.ip_network(entry.strip(),strict=False)
        except ValueError:raise ValueError('invalid_allowed_ip') from None
        # An IPv4-mapped IPv6 literal is canonicalized into the matching IPv4 lane.
        if isinstance(network,ipaddress.IPv6Network) and network.prefixlen>=96 and network.network_address.ipv4_mapped:
            network=ipaddress.ip_network((network.network_address.ipv4_mapped,network.prefixlen-96),strict=False)
        canonical=str(network)
        if canonical not in networks:networks.append(canonical)
    if enabled and not networks:raise ValueError('empty_ip_allowlist')
    return {'enabled':enabled,'allowed_ips':networks}


def peer_allowed(value,host):
    if value is None:return True
    if not isinstance(value,dict):return False
    if not value or value.get('enabled') is False:return True
    try:
        # Invalid/corrupt enabled policies fail closed; do not grant access on parse errors.
        policy=normalize_access(value)
        if not isinstance(host,str) or '%' in host:return False
        address=ipaddress.ip_address(host)
        if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:address=address.ipv4_mapped
        return any(address.version==network.version and address in network for network in map(ipaddress.ip_network,policy['allowed_ips']))
    except (ValueError,TypeError,AttributeError):return False


class APIIPAllowlistMiddleware:
    """Filter /v1 and all descendants before request-body read or API-key work."""
    def __init__(self,app,store):self.app,self.store=app,store
    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if scope.get('type')=='http' and (path=='/v1' or path.startswith('/v1/')):
            import asyncio
            config=await asyncio.to_thread(self.store.get_config)
            peer=scope.get('client')
            if not peer_allowed(config.get('api_access'),peer[0] if peer else None):
                from starlette.responses import JSONResponse
                response=JSONResponse({'error':{'message':'api_ip_not_allowed','type':'gateway_error','code':'api_ip_not_allowed'}},status_code=403,headers={'Cache-Control':'no-store'})
                await response(scope,receive,send);return
        await self.app(scope,receive,send)
