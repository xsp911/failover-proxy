"""One bounded unauthenticated IP echo request through an explicitly selected proxy."""
import asyncio
import ipaddress
import json
import time
import httpx


async def check_proxy(profile,transport=None):
    from .proxy_transport import async_client
    started=time.monotonic()
    try:
        async with asyncio.timeout(20):
            async with async_client(profile,transport,timeout=httpx.Timeout(15,connect=8),limits=httpx.Limits(max_connections=1,max_keepalive_connections=0)) as client:
                # Never send API/PAT/provider credentials to echo service; only proxy auth.
                async with client.stream('GET','https://api.ipify.org?format=json',headers={'Accept':'application/json','Accept-Encoding':'identity'}) as response:
                    if response.status_code!=200:raise ValueError('proxy_ip_check_failed')
                    if response.headers.get('content-encoding','identity').lower() not in ('identity',''):raise ValueError('proxy_ip_check_failed')
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>1024:raise ValueError('proxy_ip_check_failed')
                    obj=json.loads(raw);value=obj.get('ip') if isinstance(obj,dict) else None
                    if not isinstance(value,str) or '%' in value:raise ValueError('proxy_ip_check_failed')
                    address=ipaddress.ip_address(value)
                    return {'ok':True,'ip':str(address),'elapsed_ms':round((time.monotonic()-started)*1000)}
    except (httpx.TimeoutException,TimeoutError):kind='proxy_check_timeout'
    except httpx.ProxyError:kind='proxy_connection_failed'
    except (ValueError,TypeError,ImportError):kind='proxy_ip_check_failed'
    except httpx.HTTPError:kind='proxy_connection_failed'
    return {'ok':False,'error_kind':kind,'elapsed_ms':round((time.monotonic()-started)*1000)}
