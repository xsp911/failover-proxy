"""Bounded fingerprint sampling with per-challenge retries and safe evidence."""
import asyncio
import math
import re
import unicodedata

from .engine import GatewayError

RETRY_DELAYS = (2, 5)


def parsed_number_count(text):
    # Match the vendored JS parseNumbers: longest valid 1..355 sequence without
    # intervening Unicode letters. ASCII digits match JS /\d+/ semantics.
    best=current=0;end=0
    for match in re.finditer(r'[0-9]+',text):
        if current and any(unicodedata.category(c).startswith('L') for c in text[end:match.start()]):
            best=max(best,current);current=0
        raw=match.group(0)
        significant=raw.lstrip('0') or '0'
        if len(significant)<=3 and 1<=int(significant)<=355:current+=1
        end=match.end()
    return max(best,current)


def complete_analysis(value):
    p=value.get('target_probability')
    return value.get('decision')=='ranked' and value.get('probability_status')=='reference_calibrated' and value.get('used_outputs')==3 and isinstance(p,(int,float)) and not isinstance(p,bool) and math.isfinite(p) and 0<=p<=1


async def collect_round(monitor,job,provider,cfg,engine,round_number,evidence):
    outputs=[]
    job.update(round=round_number,progress=0,phase='sampling',retry_attempt=1,max_attempts=3)
    for challenge in monitor.catalog['challenges']:
        expected=challenge['expected_count'];minimum=max(80,math.ceil(expected*.55))
        row={'round':round_number,'id':challenge['id'],'expected_count':expected,'minimum_numbers':minimum,
             'parsed_numbers':0,'accepted':False,'finish_reason':None,'attempts':0,'errors':[]}
        for attempt in range(3):
            await monitor.validate_probe(job,provider)
            if attempt==0:evidence.append(row)
            row['attempts']=attempt+1
            job.update(phase='sampling',retry_attempt=attempt+1)
            try:
                answer=await engine.generate({'model':job['model'],'messages':[{'role':'user','content':challenge['prompt']}],
                    'max_tokens':8192,'stream':True,'stream_options':{'include_usage':True}},cfg,
                    job['id']+f'_r{round_number}_{challenge["id"]}_a{attempt+1}','fingerprint-monitor',probe=True)
                text=answer.text
                reason=answer.finished.get(0)
                count=parsed_number_count(text)
                row.update(parsed_numbers=count,finish_reason=reason,accepted=count>=minimum and reason=='stop')
                if reason!='stop':
                    raise GatewayError('probe_incomplete_finish',502,reason=='length')
                if count<minimum:
                    raise GatewayError('insufficient_probe_numbers',502,True)
                outputs.append({'text':text,'expected_count':expected})
                job['progress']+=1
                break
            except GatewayError as error:
                row['errors'].append(error.kind)
                retryable=error.retryable and error.kind not in ('upstream_http_401','upstream_http_403','unsafe_upstream_address')
                if not retryable or attempt==2:raise
                # A failed attempt may have placed the single provider into a
                # normal traffic cooldown. Retry only this same synthetic task.
                engine.cooldowns.clear()
                job.update(phase='retry_wait',last_error_kind=error.kind)
                await asyncio.sleep(RETRY_DELAYS[attempt])
    job.update(phase='scoring',retry_attempt=1)
    async with monitor.scorer:
        result=await monitor.score(outputs,job['reference_model'])
    # Use original scorer diagnostics for exact counts; Python count exists for
    # partial/failed rounds where full scoring never ran.
    diagnostics=result.get('diagnostics',[])
    for row,d in zip((x for x in evidence if x['round']==round_number),diagnostics):
        row.update(parsed_numbers=d['parsed_numbers'],minimum_numbers=d['minimum_numbers'],accepted=d['accepted'])
    return result
