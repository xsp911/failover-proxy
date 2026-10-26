import pytest
from proxy.pelican import PelicanManager
from proxy.engine import GatewayError


def test_length_exposes_reason_and_usage_without_generated_text():
    value={'choices':[{'finish_reason':'length','message':{'content':'private partial SVG'}}],'usage':{'completion_tokens':16000,'completion_tokens_details':{'reasoning_tokens':15900}}}
    with pytest.raises(GatewayError) as exc:PelicanManager._json_output(value,'chat')
    assert exc.value.kind=='pelican_output_limit'
    d=exc.value.pelican_diagnostics
    assert d['finish_reason']=='length' and d['usage']['reasoning_tokens']==15900
    assert 'private' not in str(d)


def test_responses_incomplete_retains_usage():
    value={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'usage':{'output_tokens':16000}}
    with pytest.raises(GatewayError) as exc:PelicanManager._json_output(value,'responses')
    assert exc.value.kind=='pelican_output_limit' and exc.value.pelican_diagnostics['usage']['output_tokens']==16000
