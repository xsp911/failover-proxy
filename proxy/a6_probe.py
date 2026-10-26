"""Targeted synthetic fingerprint probes; never changes production pins.

The ordinary test token is bound to a strict one-channel subscription carrying
server price caps; suffixed targeted keys are forbidden because they bypass
those caps. Accept only request-id attribution matching channel AND token.
"""
from __future__ import annotations
import asyncio
import json
import time

import httpx

from .engine import GatewayError, parse_sse
from .a6_client import A6Error
from .probe_stream import ProbeCompletion
from .probe_rounds import parsed_number_count, complete_analysis


class ProbeError(Exception):
    def __init__(self,kind):super().__init__(kind);self.kind=kind


async def responses_text(response,model,max_bytes=16777216):
    """Bounded Responses SSE lifecycle parser, no raw response history."""
    pending=[];size=0;text='';completed=False;terminal=None
    async for line in response.aiter_lines():
        size+=len(line.encode('utf8'))+1
        if size>max_bytes:raise ProbeError('probe_response_size_limit')
        if line.startswith('data:'):
            pending.append(line[5:].removeprefix(' '));continue
        if line or not pending:continue
        value='\n'.join(pending);pending.clear()
        if value=='[DONE]':continue
        try:event=json.loads(value)
        except ValueError:raise ProbeError('probe_invalid_responses_json') from None
        if not isinstance(event,dict):raise ProbeError('probe_invalid_responses_event')
        kind=event.get('type')
        if kind in ('error','response.failed','response.incomplete'):raise ProbeError('probe_upstream_response_failed')
        if kind=='response.output_text.delta':
            delta=event.get('delta','')
            if not isinstance(delta,str):raise ProbeError('probe_invalid_responses_event')
            text+=delta
            if len(text)>65536:raise ProbeError('probe_content_limit')
        if kind=='response.completed':
            obj=event.get('response') or {}
            if obj.get('status')!='completed':raise ProbeError('probe_incomplete_finish')
            completed=True;terminal=obj
    if not completed:raise ProbeError('probe_incomplete_stream')
    # Some Responses providers emit only the final completed object.
    if not text:
        for item in (terminal or {}).get('output',[]):
            if item.get('type')=='message':
                for block in item.get('content',[]):
                    if block.get('type')=='output_text':text+=block.get('text','')
        if len(text)>65536:raise ProbeError('probe_content_limit')
    return text,'stop'


class TargetProbe:
    def __init__(self,client,monitor,transport=None):
        self.client=client
        self.monitor=monitor
        self.transport=transport

    async def challenge(self,pool,channel,secret,challenge):
        # Target is selected by the dedicated test token's strict one-channel
        # subscription. Channel-suffixed secrets bypass caps (verified pilot),
        # therefore ordinary secret only; logs must still prove attribution.
        auth='Bearer '+(secret if secret.startswith('sk-') else 'sk-'+secret)
        url='https://api.a6api.com/v1/'+('responses' if pool['protocol']=='responses' else 'chat/completions')
        body={'model':pool['model'],'stream':True}
        message=[{'role':'user','content':challenge['prompt']}]
        if pool['protocol']=='responses':body.update(input=message,max_output_tokens=8192,store=False)
        else:body.update(messages=message,max_tokens=8192,stream_options={'include_usage':True})
        headers={'Authorization':auth,'Content-Type':'application/json','Accept-Encoding':'identity'}
        request_id=None
        activity=getattr(self,'activity',None)
        if activity is not None:await activity('connecting')
        try:
            async with asyncio.timeout(180):
                async with httpx.AsyncClient(transport=self.transport,trust_env=False,follow_redirects=False,
                    timeout=httpx.Timeout(120,connect=10),limits=httpx.Limits(max_keepalive_connections=0)) as http:
                    async with http.stream('POST',url,json=body,headers=headers) as response:
                        request_id=response.headers.get('x-oneapi-request-id') or response.headers.get('x-request-id')
                        if activity is not None:await activity('streaming',http_status=response.status_code,request_id=request_id)
                        bind=getattr(self,'billing_bind',None)
                        if bind is not None and request_id:await bind(request_id)
                        if response.status_code!=200:
                            # No raw body/secret in errors; targeting permissions must fail closed.
                            raise ProbeError('targeted_probe_http_'+str(response.status_code))
                        if response.headers.get('content-encoding','identity').lower() not in ('','identity'):
                            raise ProbeError('probe_compressed_response')
                        if 'text/event-stream' not in response.headers.get('content-type','').lower():
                            raise ProbeError('probe_invalid_content_type')
                        if pool['protocol']=='responses':text,finish=await responses_text(response,pool['model'])
                        else:
                            result=ProbeCompletion(pool['model'],16777216,65536)
                            try:await parse_sse(response,result)
                            except GatewayError as e:raise ProbeError(e.kind) from None
                            text,finish=result.text,result.finished.get(0)
        finally:
            billing=getattr(self,'billing_settler',None)
            if billing is not None:
                # Errors (HTTP/SSE/parser) may still be billed. Always retain
                # their exact relay ID; absence of a log is not a free request.
                import anyio
                with anyio.move_on_after(20,shield=True):
                    await billing(request_id,pool['test_token_id'])
        if finish!='stop':raise ProbeError('probe_incomplete_finish')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=160 or any(ord(x)<33 or ord(x)>126 for x in request_id):
            raise ProbeError('probe_request_id_unavailable')
        if activity is not None:await activity('attribution')
        attribution=None
        for delay in (0,1,2,3):
            if delay:await asyncio.sleep(delay)
            try:
                cached=self.client.cached_logs(request_id,pool['test_token_id']) if hasattr(self.client,'cached_logs') else None
                logs=cached if cached is not None and delay==0 else await self.client.logs(request_id,pool['test_token_id'])
            except A6Error:continue
            matches=[x for x in logs if x.get('request_id')==request_id and x.get('token_id')==pool['test_token_id']]
            if matches:
                attribution=matches[0];break
        if not attribution:raise ProbeError('probe_attribution_unavailable')
        if attribution.get('actual_channel_id')!=channel or attribution.get('fallback_used') is True:
            raise ProbeError('probe_channel_mismatch')
        return {'text':text,'expected_count':challenge['expected_count']}, {'id':challenge['id'],'parsed_numbers':parsed_number_count(text),
            'expected_count':challenge['expected_count'],'finish_reason':finish,'actual_channel_id':channel,'request_id':request_id}

    async def run(self,pool,candidate,secret,reserve_request):
        rows=[];rounds=[];last=None;start=time.monotonic()
        try:
            async with asyncio.timeout(1800):
                for number in (1,2):
                    outputs=[]
                    for challenge in self.monitor.catalog['challenges']:
                        for attempt in range(3):
                            reservation=await reserve_request(candidate,challenge)
                            if reservation is not None and hasattr(self,'billing_settler'):self.billing_reservation=reservation
                            try:
                                answer,evidence=await self.challenge(pool,candidate['channel_id'],secret,challenge)
                                if evidence['parsed_numbers']<max(80,__import__('math').ceil(challenge['expected_count']*.55)):
                                    raise ProbeError('probe_insufficient_numbers')
                                evidence.update(round=number,attempts=attempt+1);rows.append(evidence)
                                outputs.append(answer)
                                break
                            except (ProbeError,httpx.HTTPError) as failed:
                                kind=getattr(failed,'kind','probe_network_error')
                                transient=kind in ('probe_timeout_or_network','probe_insufficient_numbers','probe_incomplete_stream','probe_incomplete_finish','upstream_timeout','upstream_connection','probe_network_error') or kind.startswith('targeted_probe_http_5') or kind=='targeted_probe_http_429'
                                if not transient or attempt==2:raise
                                await asyncio.sleep((2,5)[attempt])
                    if getattr(self,'activity',None) is not None:await self.activity('scoring',round=number)
                    async with self.monitor.scorer:
                        last=await self.monitor.score(outputs,pool['reference_model'])
                    if not complete_analysis(last):raise ProbeError('probe_uncalibrated_or_insufficient')
                    score=last['target_probability'];rounds.append({'round':number,'probability':score})
                    if score*100>=pool['threshold_percent']:
                        return {'verification_status':'verified','probability':score,'actual_channel_id':candidate['channel_id'],
                            'request_ids':[r['request_id'] for r in rows],'duration_ms':round((time.monotonic()-start)*1000),
                            'rounds':rounds,'predicted_model':last.get('prediction'),'predicted_probability':last.get('probability')}
                return {'verification_status':'suspect','probability':last['target_probability'],'actual_channel_id':candidate['channel_id'],
                    'request_ids':[r['request_id'] for r in rows],'rounds':rounds,'predicted_model':last.get('prediction'),'predicted_probability':last.get('probability')}
        except (ProbeError,GatewayError) as e:
            return {'verification_status':'unknown','probability':None,'actual_channel_id':None,'reason':getattr(e,'kind','probe_failed')}
        except (TimeoutError,httpx.HTTPError):
            return {'verification_status':'unknown','probability':None,'actual_channel_id':None,'reason':'probe_timeout_or_network'}
