"""Authenticated administration and OpenAI-compatible API for Failover Proxy."""
from __future__ import annotations
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from starlette.requests import ClientDisconnect

from . import __version__
from .config import validate_config
from .engine import Engine, GatewayError
from .store import Store
from .a6_settings import A6Settings, verify as a6_verify_credentials
from .a6_marketplace import tokens as a6_tokens, sellers as a6_sellers, MarketplaceError
from .pool_manager import PoolManager,PoolError
from .pool_api import routes as pool_routes
from .a6_client import A6Error
from .pool_gateway import guarded_config
from .admission import Admission, CapacityUnavailable, AdmissionCancelled
from .model_monitor import ModelMonitor

ROOT = Path(__file__).resolve().parent.parent
# Cookies are not port-scoped: every instance on one host needs its own name.
COOKIE = os.environ.get("FP_COOKIE_NAME") or "fp_admin_session_9097"


def error(kind, status=400, request_id=None, detail=None, code=None, retry_after=None):
    # detail is produced by diagnostics' allowlisted explanations, never a raw
    # upstream body. Keep response/header output controlled and bounded.
    message = kind + (": " + detail[:512] if isinstance(detail, str) and detail else "")
    headers = {"X-Request-ID": request_id} if request_id else {}
    if status == 429:
        headers["Retry-After"] = str(retry_after if isinstance(retry_after, int) and 1 <= retry_after <= 3600 else 1)
    elif isinstance(retry_after, int) and 1 <= retry_after <= 3600:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"error": {"message": message, "type": "gateway_error", "code": code or kind}, **({"request_id": request_id} if request_id else {})}, status_code=status, headers=headers)


async def body_json(request, maximum):
    data = bytearray()
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400,"invalid_content_length") from None
    if declared > maximum:
        raise HTTPException(413,"request_too_large")
    async for part in request.stream():
        data.extend(part)
        if len(data) > maximum:
            raise HTTPException(413,"request_too_large")
    try:
        value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non_finite_json")))
    except (ValueError,UnicodeDecodeError,RecursionError):
        raise HTTPException(400,"invalid_json") from None
    if not isinstance(value,dict):
        raise HTTPException(400,"json_object_required")
    return value


class Replay:
    """Own a request slot until the successful SSE buffer is sent or cancelled."""
    def __init__(self, events, release):
        self.events=events
        self.release=release
        self.closed=False
    async def close(self):
        if not self.closed:
            self.closed=True
            self.events.clear()
            self.release()
    async def run(self):
        try:
            for event in self.events:
                yield b"data: "+json.dumps(event,ensure_ascii=False,separators=(",",":")).encode()+b"\n\n"
            yield b"data: [DONE]\n\n"
        finally:
            await self.close()


class HeldJSONResponse(JSONResponse):
    def __init__(self, content, release, **kwargs):
        self.release=release
        super().__init__(content,**kwargs)
    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(30):
                await super().__call__(scope,receive,send)
        finally:
            self.body=b''
            self.release()


class HeldStreamResponse(StreamingResponse):
    def __init__(self, replay, **kwargs):
        self.replay=replay
        super().__init__(replay.run(),**kwargs)
    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(30):
                await super().__call__(scope,receive,send)
        finally:
            await self.replay.close()


class SecurityHeadersMiddleware:
    """Pure ASGI: preserve disconnect signals during upstream buffering."""
    def __init__(self, app):
        self.app=app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        headers={k.decode('latin1').lower():v.decode('latin1') for k,v in scope.get('headers',[])}
        if scope.get('path','').startswith('/admin/'):
            origin=headers.get('origin')
            expected=scope.get('scheme','http')+'://'+headers.get('host','')
            if (origin and origin != expected) or headers.get('sec-fetch-site')=='cross-site':
                return await error('cross_origin_forbidden',403)(scope,receive,send)
        async def secure_send(message):
            if message['type']=='http.response.start':
                extra={
                    'cache-control':'no-store','x-content-type-options':'nosniff',
                    'referrer-policy':'no-referrer','x-frame-options':'DENY',
                    'content-security-policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
                if scope.get('path','').startswith('/admin/pelican/') and scope.get('path','').endswith('/svg'):
                    from .pelican_api import SVG_CSP
                    extra['content-security-policy']=SVG_CSP+"; frame-ancestors 'none'"
                message['headers']=[(k,v) for k,v in message.get('headers',[]) if k.decode('latin1').lower() not in extra]+[(k.encode(),v.encode()) for k,v in extra.items()]
            await send(message)
        await self.app(scope,receive,secure_send)


def create_app(data_dir=None, transport=None):
    store=Store(Path(data_dir or os.environ.get("FP_DATA_DIR","/opt/failover-proxy/data")))
    username=os.environ.get("FP_ADMIN_USERNAME")
    password=os.environ.get("FP_ADMIN_PASSWORD")
    if username and password:
        store.initialize_admin(username,password)
    from .capability_store import CapabilityStore
    store.capabilities=CapabilityStore(store.data_dir.resolve())
    engine=Engine(store,transport=transport)
    monitor=ModelMonitor(store,store.data_dir,transport=transport)
    a6=A6Settings(store)
    pools=PoolManager(store,a6,monitor,store.data_dir,transport=transport)
    from .pelican import PelicanManager
    pelican=PelicanManager(store,store.data_dir,transport=transport)
    login_failures=defaultdict(deque)
    account_attempts=defaultdict(deque)
    inflight={"n":0}
    admission=Admission(inflight, wait_seconds=3.0, max_waiters=32)

    @asynccontextmanager
    async def lifespan(app):
        await monitor.start()
        await pools.start()
        try:
            yield
        finally:
            await pelican.close()
            await pools.close()
            await monitor.close()
            await engine.close()

    app=FastAPI(title="Failover Proxy",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store=store
    app.state.engine=engine
    app.state.monitor=monitor
    app.state.a6=a6
    app.state.pools=pools
    app.state.pelican=pelican
    app.state.capabilities=store.capabilities
    app.state.inflight=inflight
    app.state.admission=admission

    from .ip_allowlist import APIIPAllowlistMiddleware
    app.add_middleware(APIIPAllowlistMiddleware, store=store)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(str(exc.detail),exc.status_code)

    @app.exception_handler(Exception)
    async def unknown_error(request, exc):
        # Never expose exception strings carrying upstream URLs, bodies or keys.
        import logging
        logging.getLogger("failover").error("internal_error exception_type=%s",type(exc).__name__)
        return error("internal_error",500)

    async def session(request,write=False):
        data=await asyncio.to_thread(store.get_session,request.cookies.get(COOKIE,""))
        if not data:
            raise HTTPException(401,"admin_login_required")
        if write and not hmac.compare_digest(request.headers.get("x-csrf-token",""),data["csrf_token"]):
            raise HTTPException(403,"invalid_csrf_token")
        return data

    async def api_auth(request):
        auth=request.headers.get("authorization","")
        if not auth.startswith("Bearer "):
            raise HTTPException(401,"api_key_required")
        key=await asyncio.to_thread(store.authenticate_key,auth[7:])
        if not key:
            raise HTTPException(401,"invalid_api_key")
        return key

    @app.get("/health/liveliness")
    async def health():
        return {"status":"ok"}

    @app.get("/")
    async def index():
        return FileResponse(ROOT/"web"/"index.html")

    @app.get("/styles.css")
    @app.get("/app.js")
    async def asset(request:Request):
        return FileResponse(ROOT/"web"/request.url.path.lstrip("/"))

    @app.post("/admin/login")
    async def login(request:Request):
        ip=request.client.host if request.client else "unknown"
        now=time.monotonic()
        # Bounded in-memory limiter; never trust forwarded source headers.
        if len(login_failures)>4096:
            login_failures.clear()
        failures=login_failures[ip]
        while failures and failures[0]<now-300:
            failures.popleft()
        if len(failures)>=10:
            return error("login_rate_limited",429)
        # Reserve before reading the body or scheduling a password hash.
        failures.append(now)
        data=await body_json(request,4096)
        u,p=data.get("username"),data.get("password")
        if not isinstance(u,str) or not isinstance(p,str) or len(u)>100 or len(p)>1024:
            return error("invalid_credentials",401)
        # Reserve before the expensive hash; concurrent invalid logins cannot
        # all pass the limit while earlier hashes are still in progress.
        if not await asyncio.to_thread(store.authenticate_admin,u,p):
            return error("invalid_credentials",401)
        failures.clear()
        cookie,csrf=await asyncio.to_thread(store.create_session,u)
        response=JSONResponse({"username":u,"csrf_token":csrf})
        response.set_cookie(COOKIE,cookie,httponly=True,secure=request.url.scheme=="https",samesite="strict",max_age=43200,path="/")
        return response

    @app.get("/admin/session")
    async def current_session(request:Request):
        return await session(request)

    @app.post("/admin/logout")
    async def logout(request:Request):
        await session(request,True)
        await asyncio.to_thread(store.delete_session,request.cookies.get(COOKIE,""))
        response=JSONResponse({"ok":True})
        response.delete_cookie(COOKIE,path="/")
        return response

    @app.post('/admin/account')
    async def change_account(request:Request):
        current=await session(request,True)
        ip=request.client.host if request.client else 'unknown'
        now=time.monotonic()
        if len(account_attempts)>4096:account_attempts.clear()
        attempts=account_attempts[ip]
        while attempts and attempts[0]<now-300:attempts.popleft()
        if len(attempts)>=5:return error('account_change_rate_limited',429)
        attempts.append(now)
        data=await body_json(request,8192)
        try:
            await asyncio.to_thread(store.update_admin_credentials,current['username'],data.get('current_password'),data.get('new_username'),data.get('new_password',''),request.cookies.get(COOKIE,''))
        except ValueError as failure:
            kind=str(failure)
            return error(kind,401 if kind=='admin_login_required' else 403 if kind=='invalid_current_password' else 400)
        response=JSONResponse({'ok':True,'reauthenticate':True})
        response.delete_cookie(COOKIE,path='/')
        return response

    @app.get('/admin/capabilities')
    async def capability_evidence(request:Request):
        await session(request)
        return {'observations':await asyncio.to_thread(store.capabilities.observations),'summary':await asyncio.to_thread(store.capabilities.summary),'notice':'Наблюдения, не гарантированные потолки. Принятый max_tokens не доказывает достижимую длину ответа.'}

    @app.get('/admin/observability')
    async def activity_sources(request:Request):
        await session(request)
        from .observability import snapshot
        return await asyncio.to_thread(snapshot,store,monitor,pools)

    @app.get("/admin/config")
    async def get_config(request:Request):
        await session(request)
        return await asyncio.to_thread(store.get_config)

    @app.put("/admin/config")
    async def save_config(request:Request):
        await session(request,True)
        data=await body_json(request,1024*1024)
        try:
            clean=validate_config(data)
            await monitor.validate_routing_change(clean)
            result=await asyncio.to_thread(store.save_config,clean,clean["revision"])
        except (ValueError,TypeError,KeyError,AttributeError) as e:
            code=str(e) if isinstance(e,ValueError) else "invalid_config"
            return error(code,409 if code=="revision_conflict" else 400)
        # Changed config gets a fresh chance; only affects subsequent requests.
        engine.cooldowns.clear()
        engine.failures.clear()
        await monitor.refresh()
        monitor.wake.set()
        return result

    proxy_checks=set()

    @app.get('/admin/access')
    async def api_access_info(request:Request):
        await session(request)
        config=await asyncio.to_thread(store.get_config)
        return {'client_ip':request.client.host if request.client else None,
                'api_access':config.get('api_access',{'enabled':False,'allowed_ips':[]}),
                'forwarded_headers_trusted':False,'scope':'/v1/*'}

    @app.post('/admin/proxies/{proxy_id}/check')
    async def outbound_proxy_check(proxy_id:str,request:Request):
        await session(request,True)
        await body_json(request,1024)
        if proxy_id in proxy_checks or len(proxy_checks)>=3:
            return error('proxy_check_in_progress',409)
        from .outbound_proxy import resolve_proxy
        from .proxy_check import check_proxy
        try:
            config=await asyncio.to_thread(store.get_config,True)
            profile=resolve_proxy(config,proxy_id)
            if profile is None:return error('outbound_proxy_not_found',404)
        except ValueError:return error('outbound_proxy_not_found',404)
        proxy_checks.add(proxy_id)
        try:return await check_proxy(profile,transport=transport)
        finally:proxy_checks.discard(proxy_id)

    @app.get("/admin/stats")
    async def stats(request:Request,period:str="day"):
        await session(request)
        if period not in ("hour","day","week","month"):
            return error("invalid_period")
        result=await asyncio.to_thread(store.stats,period)
        result["generated_at"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        runtime=engine.runtime()
        for p in result.get("providers",[]):
            p.update(runtime.get(p["id"],{"cooldown_until":None}))
        from .provider_health import enrich
        monitor_info=await monitor.info()
        await asyncio.to_thread(enrich,store,monitor_info,result)
        return result

    @app.get('/admin/a6/tokens')
    async def a6_token_list(request:Request):
        await session(request)
        pat,user_id=a6.secret()
        if pat is None:return error('a6_not_configured',400)
        try:
            return await a6_tokens(pat,user_id,transport=transport,proxy=a6.proxy())
        except MarketplaceError as e:
            return error(e.kind,502)

    @app.get('/admin/a6/sellers')
    async def a6_seller_list(request:Request,model:str='',token_id:int|None=None,min_cache_hit_percent:float=40,min_success_percent:float|None=None,max_cache_usd:float|None=None,limit:int=50):
        await session(request)
        pat,user_id=a6.secret()
        if pat is None:return error('a6_not_configured',400)
        try:
            if token_id is None:return error('a6_token_required',400)
            client=pools.client()
            try:
                tokens=await client.tokens()
                if not any(t['id']==token_id and t['status']==1 for t in tokens):return error('a6_token_unavailable',400)
                rows=await client.search(token_id,model)
                from .pool_search import seller_result
                return seller_result(rows,model,token_id,min_cache_hit_percent,min_success_percent,max_cache_usd,max(1,min(limit,200)))
            finally:await client.close()
        except (MarketplaceError,A6Error,PoolError) as e:
            return error(e.kind,400 if e.kind in ('invalid_model','invalid_success_percent','invalid_cache_price','invalid_cache_hit_percent') else 502)

    @app.get('/admin/a6')
    async def a6_info(request:Request):
        await session(request)
        return await asyncio.to_thread(a6.status)

    @app.put('/admin/a6')
    async def a6_save(request:Request):
        await session(request,True)
        data=await body_json(request,8192)
        try:
            pat,user_id=data.get('pat'),data.get('user_id')
            # Blank PAT keeps the stored secret; changing user id alone is allowed.
            if isinstance(pat,str) and not pat.strip():
                current_pat,current_user=a6.secret()
                if current_pat is None:raise ValueError('a6_pat_required')
                pat=current_pat
            proxy_options={'proxy_id':data['proxy_id']} if 'proxy_id' in data else {}
            result=await asyncio.to_thread(a6.save,pat,user_id,**proxy_options)
        except (ValueError,TypeError) as e:
            return error(str(e) if isinstance(e,ValueError) else 'invalid_a6_settings',400)
        return result

    @app.post('/admin/a6/verify')
    async def a6_verify(request:Request):
        await session(request,True)
        data=await body_json(request,8192)
        try:
            pat,user_id=data.get('pat'),data.get('user_id')
            if isinstance(pat,str) and not pat.strip():
                pat,stored_user=a6.secret()
                if pat is None:raise ValueError('a6_pat_required')
                if user_id is None:user_id=stored_user
            pat,user_id=a6.validate(pat,user_id)
            from .outbound_proxy import resolve_proxy
            config=await asyncio.to_thread(store.get_config,True)
            profile=resolve_proxy(config,data.get('proxy_id',a6.proxy_id))
        except (ValueError,TypeError) as e:
            return error(str(e) if isinstance(e,ValueError) else 'invalid_a6_settings',400)
        return await a6_verify_credentials(pat,user_id,transport=transport,proxy=profile)

    @app.delete('/admin/a6')
    async def a6_clear(request:Request):
        await session(request,True)
        return await asyncio.to_thread(a6.clear)

    @app.get('/admin/health')
    async def admin_health(request:Request):
        await session(request)
        return {"status":"ok","version":__version__,"active_requests":inflight["n"],"queued_requests":inflight["queued"],"queue_wait_s":admission.wait_seconds,"queue_limit":admission.max_waiters,"buffered_mode":True}

    @app.post("/admin/providers/{provider_id}/test")
    async def test_provider(provider_id:str,request:Request):
        await session(request,True)
        config=await asyncio.to_thread(store.get_config,True)
        p=next((p for p in config["providers"] if p["id"]==provider_id),None)
        if not p:
            return error("provider_not_found",404)
        if not p.get("api_key"):
            return error("provider_key_required")
        return await engine.test_provider(p,config["settings"])

    @app.get('/admin/detector')
    async def detector_info(request:Request):
        await session(request)
        return await monitor.info()

    @app.put('/admin/detector/settings')
    async def detector_settings(request:Request):
        await session(request,True)
        data=await body_json(request,65536)
        try:
            return await monitor.save_settings(data)
        except (ValueError,KeyError,TypeError,AttributeError) as e:
            kind=str(e) if isinstance(e,ValueError) else 'invalid_detector_settings'
            return error(kind,409 if kind=='revision_conflict' else 400)

    @app.post('/admin/detector/check')
    async def detector_check(request:Request):
        await session(request,True)
        data=await body_json(request,4096)
        try:
            job=await monitor.enqueue(data.get('provider_id'),data.get('model'),data.get('reference_model'))
            return JSONResponse(job,status_code=202)
        except (ValueError,TypeError) as e:
            kind=str(e) if isinstance(e,ValueError) else 'invalid_check_request'
            return error(kind,409 if kind=='check_already_running' else 400)

    @app.post('/admin/detector/jobs/{job_id}/cancel')
    async def detector_cancel(job_id:str,request:Request):
        await session(request,True)
        return {'ok':await monitor.cancel(job_id)}

    @app.get('/admin/detector/log')
    async def detector_log(request:Request,limit:int=100):
        await session(request)
        return {'changes':await asyncio.to_thread(monitor.db.journal,max(1,min(limit,200)))}

    @app.get("/admin/keys")
    async def keys(request:Request):
        await session(request)
        return {"keys":await asyncio.to_thread(store.list_keys)}

    @app.post("/admin/keys")
    async def create_key(request:Request):
        await session(request,True)
        data=await body_json(request,8192)
        name,models=data.get("name"),data.get("models",[])
        if not isinstance(name,str) or not 1<=len(name.strip())<=100 or not isinstance(models,list) or len(models)>100 or any(not isinstance(x,str) for x in models):
            return error("invalid_key_settings")
        config=await asyncio.to_thread(store.get_config)
        if set(models)-{m["name"] for m in config["models"]}:
            return error("unknown_model")
        return await asyncio.to_thread(store.create_key,name.strip(),models)

    @app.post('/admin/keys/{key_id}/reveal')
    async def reveal_key(key_id:str,request:Request):
        await session(request,True)
        await body_json(request,1024)
        try:token=await asyncio.to_thread(store.reveal_key,key_id)
        except ValueError:return error('key_secret_unavailable',409)
        if token is None:return error('key_secret_unavailable',409)
        return JSONResponse({'token':token},headers={'Cache-Control':'no-store, private','Pragma':'no-cache'})

    @app.patch("/admin/keys/{key_id}")
    async def update_key(key_id:str,request:Request):
        await session(request,True)
        data=await body_json(request,1024)
        if not isinstance(data.get("enabled"),bool):
            return error("invalid_enabled")
        if not await asyncio.to_thread(store.update_key,key_id,data["enabled"]):
            return error("key_not_found",404)
        return {"ok":True}

    @app.delete("/admin/keys/{key_id}")
    async def delete_key(key_id:str,request:Request):
        await session(request,True)
        if not await asyncio.to_thread(store.delete_key,key_id):
            return error("key_not_found",404)
        return {"ok":True}

    @app.get("/v1/models")
    async def models(request:Request):
        key=await api_auth(request)
        config=await asyncio.to_thread(store.get_config)
        return {"object":"list","data":[{"id":m["name"],"object":"model","created":0,"owned_by":"failover-proxy","supports_vision":m["supports_vision"],"context_length":m.get("context_length"),"max_output_tokens":m.get("max_output_tokens")}
            for m in config["models"] if m["enabled"] and (not key["models"] or m["name"] in key["models"])]}

    @app.post("/v1/chat/completions")
    async def completion(request:Request):
        key=await api_auth(request)
        config=await asyncio.to_thread(store.get_config,True)
        request_id="req_"+uuid.uuid4().hex
        # Queue before consuming request bodies: waiting callers do not retain
        # large image/context buffers. No upstream call occurs while queued.
        limit=config["settings"]["max_concurrent_requests"]
        try:
            release=await admission.acquire(limit)
        except CapacityUnavailable:
            return error("gateway_capacity",429,request_id,detail="The gateway remained busy for the bounded queue wait. Retry after one second.")
        reserved=True
        task=None
        attribution={}
        try:
            async with asyncio.timeout(30):
                payload=await body_json(request,config["settings"]["max_request_bytes"])
            if not isinstance(payload.get("model"),str) or not isinstance(payload.get("messages"),list) or len(payload["messages"])>10000:
                return error("invalid_chat_request")
            if "stream" in payload and not isinstance(payload["stream"],bool):
                return error("invalid_stream")
            if key["models"] and payload["model"] not in key["models"]:
                return error("model_access_denied",403)
            # Do not accept caller overrides that could bypass configured routing.
            for name in ("api_key","api_base","base_url","provider","custom_llm_provider","fallbacks"):
                if name in payload:
                    return error("routing_override_forbidden")
            await asyncio.to_thread(store.increment_key_requests,key["id"])
            try:
                config=await guarded_config(pools,config,payload['model'])
            except (PoolError,A6Error) as failed:
                return error(getattr(failed,'kind','managed_pool_unavailable'),503,request_id)
            blocked=await monitor.blocked_providers(config)
            if blocked:
                # Quarantine is runtime policy, not an edit to the administrator's
                # enabled flags. A restored provider returns without losing config.
                config={**config,'providers':[{**p,'enabled':p['enabled'] and p['id'] not in blocked} for p in config['providers']]}
            task=asyncio.create_task(engine.generate(payload,config,request_id,key["id"],attribution=attribution))
            # Monitor disconnects during buffering so abandoned paid calls stop.
            while not task.done():
                done,_=await asyncio.wait({task},timeout=0.25)
                if done:
                    break
                if await request.is_disconnected():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return error("client_disconnected",499,request_id)
            try:
                result=await task
            except GatewayError as e:
                return error(e.kind,e.status,request_id,detail=e.detail)
            headers={"X-Request-ID":request_id,"X-Buffered-Response":"true","X-Upstream":attribution.get("upstream","unknown")}
            if payload.get("stream",False):
                replay=Replay(result.events,release)
                reserved=False
                return HeldStreamResponse(replay,media_type="text/event-stream",headers=headers)
            response=HeldJSONResponse(result,release,headers=headers)
            reserved=False
            return response
        except TimeoutError:
            return error("request_body_timeout",408,request_id)
        except ClientDisconnect:
            return error("client_disconnected",499,request_id)
        finally:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if reserved:
                release()

    app.include_router(pool_routes(pools,session,body_json,error))
    from .pelican_api import router as pelican_router
    app.include_router(pelican_router(pelican,session,body_json))
    return app


# Factory entry: uvicorn proxy.app:create_app --factory
