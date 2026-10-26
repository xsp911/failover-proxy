"""Observe-only context estimate. Never reject requests from an approximation."""
import json


def observe_context(payload,model):
    if model.get('context_preflight','off')!='observe':return None
    chunks=[];media=False
    stack=[payload.get('messages',payload.get('input',[])),payload.get('tools',[]),payload.get('response_format',{})]
    while stack:
        value=stack.pop()
        if isinstance(value,str):chunks.append(value)
        elif isinstance(value,list):stack.extend(value)
        elif isinstance(value,dict):
            kind=value.get('type','')
            if isinstance(kind,str) and kind in ('image_url','input_image','input_audio','audio','video_url','file','input_file'):
                media=True;continue
            stack.extend(item for key,item in value.items() if key not in ('url','data','base64'))
    # UTF8/4 is deliberately labeled heuristic, not a tokenizer or a bound.
    byte_count=sum(len(s.encode('utf8')) for s in chunks)
    estimated=(byte_count+3)//4
    output=payload.get('max_completion_tokens',payload.get('max_output_tokens',payload.get('max_tokens')))
    if type(output) is not int or output<0:output=0
    limit=model.get('context_length');limit=limit if type(limit) is int and limit>0 else None
    return {'estimated_input_tokens':estimated,'estimated_total_tokens':estimated+output,'context_length':limit,'method':'utf8_bytes_div4_excluding_media','reliability':'heuristic','over_limit':bool(limit and estimated+output>limit),'contains_media':media,'requested_output_tokens':output,'action':'observe_only'}
