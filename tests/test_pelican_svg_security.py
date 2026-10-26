import pytest
from proxy.pelican_svg import inspect_svg


def svg(body,attrs=''):return '<svg xmlns="http://www.w3.org/2000/svg" '+attrs+'>'+body+'</svg>'


@pytest.mark.parametrize('payload',[
 '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg>&xxe;</svg>',
 '<?xml version="1.0"?><svg/>',
 svg('<script>alert(1)</script>'),
 svg('<foreignObject><p>HTML</p></foreignObject>'),
 svg('<image href="https://evil.invalid/x"/>'),
 svg('<use href="data:image/svg+xml,evil"/>'),
 svg('<rect width="10" onload="alert(1)"/>'),
 svg('<animate attributeName="href" values="#x;https://evil.invalid"/>'),
 svg('<animate attributeName="style" values="fill:red"/>'),
 svg('<style>@import "https://evil.invalid/x";</style>'),
 svg('<style>rect{fill:url(https://evil.invalid/x)}</style>'),
 svg('<style>rect{fill:u\\72l(https://evil.invalid/x)}</style>'),
 svg('<g xmlns="http://evil.invalid"><rect/></g>'),
 svg('<animate begin="click" attributeName="x" values="1;2"/>'),
 svg('<rect/>', 'viewBox="0 0 1e100 30"'),
 '<svg>'+('<g>'*51)+('</g>'*51)+'</svg>',
 '<svg>'+('<rect/>'*5001)+'</svg>',
])
def test_unsafe_output_has_no_renderable_svg(payload):
    result=inspect_svg(payload,'CODE1234');assert result['svg'] is None and not result['checks']['safe_svg'] and result['error_kind'].startswith('pelican_')


def test_safe_svg_local_reference_animation_comment_and_nonce():
    value=svg('<!-- wheel --><defs><linearGradient id="paint"><stop offset="0" stop-color="orange"/></linearGradient></defs><circle fill="url(#paint)" r="20"><animate attributeName="r" values="20;25;20" dur="2s" repeatCount="indefinite"/></circle><text x="800" y="600">CODE1234</text>', 'viewBox="0 0 960 640"')
    result=inspect_svg(value,'CODE1234');assert all(result['checks'].values()) and result['svg'] and result['error_kind'] is None


def test_safe_static_missing_nonce_is_not_semantic_success():
    result=inspect_svg(svg('<circle r="2"/>'),'CODE1234');assert result['svg'] and result['checks']=={'safe_svg':True,'animation':False,'nonce':False}


def test_output_limit_and_multiple_roots_rejected():
    assert inspect_svg('<svg>'+(' '*262144)+'</svg>','x')['svg'] is None
    assert inspect_svg('<svg/><svg/>','x')['svg'] is None
