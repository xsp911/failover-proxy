"""Explicit single-scene SVG generation inspired by Manxue AI; no scheduler."""
import asyncio
import json
import secrets
import time
import uuid
from datetime import datetime,timezone

import httpx

from .engine import Completion,GatewayError,destination,parse_sse,usage_values
from .pelican_store import PelicanStore

TIMEOUT_SECONDS=300
MAX_OUTPUT_TOKENS=16000
HIGH_EFFORT_OUTPUT_TOKENS=64000
# SSE includes repeated envelopes and reasoning, not just the resulting SVG.
# Keep transport bounded independently from the validator's256KiB image limit.
MAX_TRANSPORT_BYTES=16*1024*1024
EFFORTS=('none','minimal','low','medium','high','xhigh')
SCENE={'id':'seaside-boardwalk','title':'Пеликан на велосипеде — морская набережная'}
PROMPT='''Create one self-contained SVG 2D looping animation of exactly ONE cute pelican riding exactly ONE bicycle on a sunny seaside wooden boardwalk. This is one continuous fixed scene, no panels, scene changes or additional birds.
The pelican has a long broad orange beak and a visible throat pouch, webbed feet touching the pedals, and slightly spread wings for balance. Show a coherent bicycle with two wheels, connected frame, handlebars and crank pedals. Wheels rotate continuously and the feet follow the pedals while staying connected. Bright background and lively colors.
Use a 960 by 640 viewBox. Display the verification code {nonce} as visible SVG text at the lower-right corner.
Keep the SVG compact (ideally below12000 characters). Complete the drawing within the available output budget. Return only ONE complete SVG. Inline CSS or SMIL animation is allowed. Do not use HTML, JavaScript, event handlers, external images, external fonts, network resources, or tools.'''


def stamp():return datetime.now(timezone.utc).isoformat()


class PelicanManager:
    def __init__(self,store,data_dir,transport=None):
        self.store=store;self.db=PelicanStore(data_dir);self.transport=transport
        self.tasks={};self.runs={};self.lock=asyncio.Lock()
    async def close(self):
        tasks=list(self.tasks.values())
        for id in list(self.tasks):await self.cancel(id)
    async def info(self):
        config=await asyncio.to_thread(self.store.get_config,True)
        providers=[]
        for p in config['providers']:
            if not p['enabled'] or not p.get('api_key'):continue
            models=sorted({r['upstream_model'] for m in config['models'] if m['enabled'] for r in m['routes'] if r['enabled'] and r['provider_id']==p['id']})
            if models:providers.append({'id':p['id'],'name':p['name'],'models':models})
        return {'providers':providers,'runs':await asyncio.to_thread(self.db.list),'scene':{**SCENE,'prompt':PROMPT.replace('{nonce}','[код запуска]')},'timeout_seconds':TIMEOUT_SECONDS,'max_output_tokens':MAX_OUTPUT_TOKENS,'max_parallel':1,'effort_levels':list(EFFORTS)}
    async def enqueue(self,data):
        if data.get('confirm_cost') is not True:raise ValueError('pelican_cost_confirmation_required')
        protocol=data.get('protocol','chat')
        if protocol not in ('chat','responses'):raise ValueError('pelican_invalid_protocol')
        effort=data.get('effort')
        if effort in ('', 'default'):effort=None
        if effort is not None and (not isinstance(effort,str) or effort not in EFFORTS):raise ValueError('pelican_invalid_effort')
        async with self.lock:
            if any(not t.done() for t in self.tasks.values()):raise ValueError('pelican_busy')
            self.tasks={key:task for key,task in self.tasks.items() if not task.done()}
            config=await asyncio.to_thread(self.store.get_config,True)
            p=next((p for p in config['providers'] if p['id']==data.get('provider_id') and p['enabled'] and p.get('api_key')),None)
            if not p:raise ValueError('pelican_provider_unavailable')
            models={r['upstream_model'] for m in config['models'] if m['enabled'] for r in m['routes'] if r['enabled'] and r['provider_id']==p['id']}
            if data.get('model') not in models:raise ValueError('pelican_model_unavailable')
            run={'id':'pelican_'+uuid.uuid4().hex,'provider_id':p['id'],'provider_name':p['name'],'model':data['model'],'protocol':protocol,'status':'queued','phase':'queued','created_at':stamp(),'finished_at':None,'elapsed_ms':0,'error_kind':None,'checks':{},'svg_available':False,'usage':{},'timeout_seconds':300,'scene':SCENE['id'],'generation_count':1,'effort':effort,'max_output_tokens':HIGH_EFFORT_OUTPUT_TOKENS if effort in ('high','xhigh') else MAX_OUTPUT_TOKENS}
            await asyncio.to_thread(self.db.save,run);self.runs[run['id']]=run
            self.tasks[run['id']]=asyncio.create_task(self._run(run,p,config['settings']))
            return dict(run)
    async def cancel(self,id):
        task=self.tasks.get(id)
        if not task or task.done():return False
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        run=self.runs.pop(id,None)
        if run and run.get('status')=='queued':
            run.update(status='cancelled',error_kind='pelican_cancelled',phase='finished',finished_at=stamp())
            await asyncio.to_thread(self.db.save,run)
        self.tasks.pop(id,None);return True
    async def _run(self,run,provider,settings):
        started=time.monotonic();svg=None
        try:
            run.update(status='running',phase='generating');await asyncio.to_thread(self.db.save,run)
            nonce=secrets.token_hex(4).upper();prompt=PROMPT.format(nonce=nonce)
            async with asyncio.timeout(TIMEOUT_SECONDS):text,usage=await self._generate(provider,settings,run['model'],run['protocol'],prompt,run.get('effort'))
            run['usage']=usage;run['phase']='validating'
            await asyncio.to_thread(self.db.save,run)
            from .pelican_svg import inspect_svg
            # A provider must never accidentally echo its bearer credential into
            # the persisted SVG. Fail closed, without storing/logging that text.
            if provider['api_key'] in text:raise GatewayError('pelican_sensitive_output')
            validation=await asyncio.to_thread(inspect_svg,text,nonce)
            run['checks']=validation['checks'];svg=validation['svg'];run['svg_available']=bool(svg)
            run.update(status='done' if svg else 'error',error_kind=validation['error_kind'],phase='finished')
        except asyncio.CancelledError:
            run.update(status='cancelled',error_kind='pelican_cancelled',phase='finished');raise
        except TimeoutError:run.update(status='error',error_kind='pelican_timeout',phase='finished')
        except GatewayError as error:
            run.update(status='error',error_kind=error.kind,phase='finished')
            diagnostics=getattr(error,'pelican_diagnostics',{})
            if diagnostics:run.update(diagnostics=diagnostics,usage=diagnostics.get('usage',run.get('usage',{})))
        except httpx.TimeoutException:run.update(status='error',error_kind='pelican_timeout',phase='finished')
        except httpx.HTTPError:run.update(status='error',error_kind='pelican_connection_failed',phase='finished')
        except Exception:run.update(status='error',error_kind='pelican_generation_failed',phase='finished')
        finally:
            run.update(finished_at=stamp(),elapsed_ms=round((time.monotonic()-started)*1000))
            await asyncio.shield(asyncio.to_thread(self.db.save,run,svg))
            observer=getattr(self.store,'capabilities',None)
            if observer is not None:
                from .capability_observer import observe
                kind=run.get('error_kind') or ''
                http_status=int(kind[-3:]) if kind.startswith('upstream_http_') and kind[-3:].isdigit() else 200 if run.get('usage') or run.get('checks') else None
                diagnostics=run.get('diagnostics',{})
                await asyncio.to_thread(observe,observer,provider=provider,model=run['model'],protocol=run['protocol'],request_id=run['id'],source='pelican',payload={'max_output_tokens':run.get('max_output_tokens')},outcome='success' if run['status']=='done' else 'cancelled' if run['status']=='cancelled' else 'error',http_status=http_status,usage=run.get('usage'),finish_reason=diagnostics.get('finish_reason'),error_kind=run.get('error_kind'))
            self.tasks.pop(run['id'],None);self.runs.pop(run['id'],None)
    async def _generate(self,provider,settings,model,protocol,prompt,effort=None):
        suffix='/responses' if protocol=='responses' else '/chat/completions'
        url,headers,extensions=await destination(provider['base_url']+suffix,settings['trust_private_upstreams'])
        headers.update({'Authorization':'Bearer '+provider['api_key'],'Content-Type':'application/json','Accept-Encoding':'identity'})
        payload={'model':model,'stream':True}
        output_limit=HIGH_EFFORT_OUTPUT_TOKENS if effort in ('high','xhigh') else MAX_OUTPUT_TOKENS
        if protocol=='responses':payload.update(input=[{'role':'user','content':[{'type':'input_text','text':prompt}]}],max_output_tokens=output_limit)
        else:payload.update(messages=[{'role':'user','content':prompt}],max_tokens=output_limit,stream_options={'include_usage':True})
        if effort is not None:
            if protocol=='responses':payload['reasoning']={'effort':effort}
            else:payload['reasoning_effort']=effort
        from .provider_policy import apply_provider_policy
        payload=apply_provider_policy(payload,provider)
        timeout=httpx.Timeout(300,connect=min(20,settings['connect_timeout_s']),read=300)
        async with httpx.AsyncClient(transport=self.transport,trust_env=False,follow_redirects=False) as client:
            async with client.stream('POST',url,headers=headers,extensions=extensions,json=payload,timeout=timeout) as response:
                if response.status_code!=200:raise GatewayError('upstream_http_'+str(response.status_code),502)
                if response.headers.get('content-encoding','identity').lower() not in ('identity',''):raise GatewayError('compressed_upstream_response')
                is_sse='text/event-stream' in response.headers.get('content-type','')
                if is_sse and protocol=='chat':
                    completion=Completion(model,MAX_TRANSPORT_BYTES,32768);await parse_sse(response,completion);completion.validate()
                    text=''.join(e.get('choices',[{}])[0].get('delta',{}).get('content','') for e in completion.events if e.get('choices') and e['choices'][0].get('index',0)==0)
                    if completion.finished.get(0)!='stop':
                        self._completion_failure(completion.finished.get(0),completion.usage,text,completion.bytes)
                    return text,self._usage(completion.usage)
                raw=bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw)>MAX_TRANSPORT_BYTES:raise GatewayError('response_size_limit')
                if is_sse:return self._responses_sse(bytes(raw))
                try:value=json.loads(raw)
                except (ValueError,UnicodeError):raise GatewayError('invalid_upstream_response') from None
                return self._json_output(value,protocol)
    @staticmethod
    def _usage(raw):
        usage=usage_values(raw)
        if isinstance(raw,dict):
            for name in ('completion_tokens_details','output_tokens_details'):
                details=raw.get(name)
                if isinstance(details,dict) and type(details.get('reasoning_tokens')) is int:usage['reasoning_tokens']=max(0,details['reasoning_tokens'])
        return usage

    @classmethod
    def _completion_failure(cls,reason,usage,text='',transport_bytes=None):
        safe=reason if reason in ('length','content_filter','stop','tool_calls','function_call','max_output_tokens','incomplete','failed') else 'unknown'
        error=GatewayError('pelican_output_limit' if safe in ('length','max_output_tokens') else 'pelican_content_filter' if safe=='content_filter' else 'pelican_incomplete_generation')
        error.pelican_diagnostics={'finish_reason':safe,'usage':cls._usage(usage),'output_characters':len(text) if isinstance(text,str) else 0}
        if transport_bytes is not None:error.pelican_diagnostics['transport_bytes']=transport_bytes
        raise error

    @classmethod
    def _json_output(cls,value,protocol):
        if not isinstance(value,dict) or value.get('error'):raise GatewayError('invalid_upstream_response')
        if protocol=='chat':
            choices=value.get('choices',[])
            if not choices:raise GatewayError('pelican_incomplete_generation')
            text=choices[0].get('message',{}).get('content')
            if choices[0].get('finish_reason')!='stop':cls._completion_failure(choices[0].get('finish_reason'),value.get('usage'),text)
        else:
            if value.get('status')!='completed':cls._completion_failure((value.get('incomplete_details') or {}).get('reason',value.get('status')),value.get('usage'))
            text=''.join(c.get('text','') for item in value.get('output',[]) if item.get('type')=='message' for c in item.get('content',[]) if c.get('type')=='output_text')
        if not isinstance(text,str) or not text.strip():raise GatewayError('invalid_upstream_response')
        return text,cls._usage(value.get('usage'))
    @classmethod
    def _responses_sse(cls,raw):
        try:
            normalized=raw.decode('utf8').replace('\r\n','\n').replace('\r','\n');completed=None
            for event in normalized.split('\n\n'):
                data='\n'.join(line[5:].removeprefix(' ') for line in event.split('\n') if line.startswith('data:'))
                if not data or data=='[DONE]':continue
                value=json.loads(data)
                if value.get('type')=='response.incomplete':return cls._json_output(value.get('response',{}),'responses')
                if value.get('type') in ('error','response.failed'):raise GatewayError('pelican_incomplete_generation')
                if value.get('type')=='response.completed':completed=value.get('response')
            if completed is None:raise GatewayError('pelican_incomplete_generation')
            return cls._json_output(completed,'responses')
        except (ValueError,UnicodeError,AttributeError,TypeError):raise GatewayError('invalid_upstream_response') from None
