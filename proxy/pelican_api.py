"""Admin-only SVG test API. No public gallery, no arbitrary code rendering."""
import asyncio
import re

from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse,Response


SVG_CSP="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; sandbox"


def router(manager,session,body_json):
    r=APIRouter()
    def valid(id):return bool(re.fullmatch(r'pelican_[0-9a-f]{32}',id))
    @r.get('/admin/pelican')
    async def info(request:Request):
        await session(request)
        return await manager.info()
    @r.post('/admin/pelican')
    async def start(request:Request):
        await session(request,True)
        try:
            data=await body_json(request,4096)
            if not isinstance(data,dict):raise ValueError('invalid_pelican_request')
            return JSONResponse(await manager.enqueue(data),status_code=202)
        except ValueError as e:
            return JSONResponse({'error':{'code':str(e)}},status_code=409 if str(e)=='pelican_busy' else 400)
    @r.post('/admin/pelican/{id}/cancel')
    async def cancel(id:str,request:Request):
        await session(request,True)
        if not valid(id):return JSONResponse({'error':{'code':'pelican_not_found'}},status_code=404)
        return {'ok':await manager.cancel(id)}
    @r.post('/admin/pelican/{id}/motion')
    async def motion(id:str,request:Request):
        await session(request,True)
        saved=await asyncio.to_thread(manager.db.get,id) if valid(id) else None
        if not saved or not saved[1]:return JSONResponse({'error':{'code':'pelican_svg_unavailable'}},status_code=404)
        cached=await asyncio.to_thread(manager.db.motion,id)
        if cached:return cached
        try:
            from .pelican_motion import capture
            result=await capture(saved[1])
            return await asyncio.to_thread(manager.db.save_motion,id,result)
        except ValueError as e:
            return JSONResponse({'error':{'code':str(e)}},status_code=409 if str(e)=='pelican_renderer_busy' else 503)
    @r.get('/admin/pelican/{id}/motion')
    async def motion_result(id:str,request:Request):
        await session(request)
        value=await asyncio.to_thread(manager.db.motion,id) if valid(id) else None
        if value is None:return JSONResponse({'error':{'code':'pelican_motion_unavailable'}},status_code=404)
        return value
    @r.get('/admin/pelican/{id}/frames/{index}')
    async def frame(id:str,index:int,request:Request):
        await session(request)
        value=await asyncio.to_thread(manager.db.motion,id,True) if valid(id) and 0<=index<4 else None
        if value is None:return JSONResponse({'error':{'code':'pelican_motion_unavailable'}},status_code=404)
        import base64
        return Response(base64.b64decode(value['frames'][index],validate=True),media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
    @r.get('/admin/pelican/{id}/svg')
    async def svg(id:str,request:Request):
        await session(request)
        saved=await asyncio.to_thread(manager.db.get,id) if valid(id) else None
        if not saved or not saved[1]:return JSONResponse({'error':{'code':'pelican_svg_unavailable'}},status_code=404)
        return Response(saved[1],media_type='image/svg+xml',headers={'Content-Security-Policy':SVG_CSP,'Content-Disposition':'inline; filename="pelican.svg"','X-Content-Type-Options':'nosniff','Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    return r
