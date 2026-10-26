import pytest
from proxy.context_preflight import observe_context
from proxy.capability_observer import observe
from proxy.capability_store import CapabilityStore


def test_off_by_default_and_observe_never_claims_exact_tokens():
    payload={'messages':[{'role':'user','content':'тест'*2000}],'max_tokens':1000}
    assert observe_context(payload,{'context_length':1000}) is None
    value=observe_context(payload,{'context_length':1000,'context_preflight':'observe'})
    assert value['over_limit'] and value['reliability']=='heuristic' and value['action']=='observe_only'


def test_image_data_not_mistaken_for_text_tools_counted():
    payload={'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+'x'*100000}},{'type':'text','text':'hello'}]}],'tools':[{'function':{'name':'tool','parameters':{'description':'y'*4000}}}]}
    value=observe_context(payload,{'context_preflight':'observe'})
    assert value['contains_media'] and 1000<value['estimated_input_tokens']<1500


def test_deep_tools_and_nonstring_type_cannot_break_observe():
    value='deeptext'
    for _ in range(1200):value={'nested':value}
    result=observe_context({'tools':[value,{'type':[]}]},{'context_preflight':'observe'})
    assert result['estimated_input_tokens']==2


def test_observer_context_filters_raw_fields(tmp_path):
    store=CapabilityStore(tmp_path);context=observe_context({'messages':[{'content':'hi'}]},{'context_preflight':'observe'})
    observe(store,provider={'id':'p'},model='m',protocol='chat',request_id='req',source='gateway',payload={'max_tokens':10},outcome='success',http_status=200,context=context)
    rows=store.observations();assert len(rows)==1 and rows[0]['context']['reliability']=='heuristic'
    assert rows[0]['parameter_accepted'] is True
