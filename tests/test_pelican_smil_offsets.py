import pytest
from proxy.pelican_svg import inspect_svg


@pytest.mark.parametrize('offset',['-0.8s','-0.8','-.8s','+0.25s','-800ms','0s','indefinite'])
def test_signed_numeric_smil_clock_offsets_safe(offset):
    svg='<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"><animate attributeName="r" values="5;10;5" begin="'+offset+'" dur="2s"/></circle><text>CODE</text></svg>'
    result=inspect_svg(svg,'CODE');assert result['svg'] and all(result['checks'].values()),result


@pytest.mark.parametrize('offset',['click','foo.click','javascript:alert(1)','0s;foo.click','-Infinitys','--0.8s'])
def test_signed_numeric_fix_does_not_admit_event_or_code_timing(offset):
    svg='<svg xmlns="http://www.w3.org/2000/svg"><circle><animate attributeName="r" begin="'+offset+'"/></circle></svg>'
    assert inspect_svg(svg,'CODE')['svg'] is None
