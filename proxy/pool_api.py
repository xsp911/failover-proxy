"""Authenticated managed-pool administration; no unconfirmed remote writes."""
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from .pool_manager import PoolError
from .a6_client import A6Error


def routes(manager,session,body_json,error):
    r=APIRouter()

    def fail(exc):
        kind=getattr(exc,'kind',str(exc) if isinstance(exc,ValueError) else 'pool_operation_failed')
        return error(kind,getattr(exc,'status',400 if isinstance(exc,ValueError) else 502))

    @r.get('/admin/a6/pools')
    async def listing(request:Request):
        await session(request)
        try:return await manager.info()
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools')
    async def create(request:Request):
        await session(request,True)
        try:return await manager.policy(await body_json(request,32768))
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.put('/admin/a6/pools/{pool_id}')
    async def update(pool_id:str,request:Request):
        await session(request,True)
        try:return await manager.policy(await body_json(request,32768),pool_id)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.delete('/admin/a6/pools/{pool_id}')
    async def delete(pool_id:str,request:Request):
        await session(request,True)
        try:
            data=await body_json(request,1024)
            return {'ok':await manager.delete(pool_id,data.get('revision'),data.get('confirm_remote') is True)}
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/discover')
    async def discover(pool_id:str,request:Request):
        await session(request,True)
        try:return JSONResponse(await manager.enqueue(pool_id,'discover'),status_code=202)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/run')
    async def run_now(pool_id:str,request:Request):
        await session(request,True)
        try:
            data=await body_json(request,1024)
            return JSONResponse(await manager.run_now(pool_id,data.get('confirm_cost') is True),status_code=202)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/restart')
    async def restart(pool_id:str,request:Request):
        await session(request,True)
        try:
            data=await body_json(request,1024)
            return JSONResponse(await manager.restart(pool_id,data.get('confirm_cost') is True),status_code=202)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/verify')
    async def verify(pool_id:str,request:Request):
        await session(request,True)
        try:
            data=await body_json(request,1024)
            return JSONResponse(await manager.enqueue(pool_id,'verify',data.get('confirm_cost') is True),status_code=202)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/jobs/{job_id}/cancel')
    async def cancel(job_id:str,request:Request):
        await session(request,True)
        return {'ok':await manager.cancel(job_id)}

    @r.post('/admin/a6/pools/{pool_id}/preview')
    async def preview(pool_id:str,request:Request):
        await session(request,True)
        try:return await manager.preview(pool_id)
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/publish')
    async def publish(pool_id:str,request:Request):
        await session(request,True)
        try:
            data=await body_json(request,4096)
            return await manager.publish(pool_id,data.get('preview_id'),data.get('confirm'))
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.get('/admin/a6/pools/{pool_id}/events')
    async def membership_events(pool_id:str,request:Request,limit:int=50):
        await session(request)
        try:
            import asyncio
            return {'events':await asyncio.to_thread(manager.membership_history.events,pool_id,max(1,min(limit,200)))}
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    @r.post('/admin/a6/pools/{pool_id}/unban')
    async def unban(pool_id:str,request:Request):
        await session(request,True)
        try:
            import asyncio
            await manager.get(pool_id)
            data=await body_json(request,1024)
            channel=data.get('channel_id')
            if type(channel) is not int or channel<=0:raise ValueError('invalid_channel_id')
            await asyncio.to_thread(manager.auto.unban,pool_id,channel)
            return {'ok':True}
        except (PoolError,A6Error,ValueError) as e:return fail(e)

    return r
