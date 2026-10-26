"""Regression coverage for observed Windows CI locale/checkout failures."""
import ast
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_portable_utf8_boundaries_are_explicit():
    names=['tests/test_frontend_static.py','tests/test_detector_scoring.py','proxy/model_monitor.py']
    for name in names:
        tree=ast.parse((ROOT/name).read_text(encoding='utf8'))
        reads=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='read_text']
        assert reads
        assert all(any(k.arg=='encoding' and isinstance(k.value,ast.Constant) and k.value.value in ('utf8','utf-8') for k in n.keywords) for n in reads),name
    scorer=(ROOT/'tests/test_detector_scoring.py').read_text(encoding='utf8')
    assert 'text=True, encoding="utf-8"' in scorer


def test_pinned_artifacts_disable_checkout_text_conversion():
    names=['detector/upstream/shared-detector.ts','detector/upstream/detector-codec.ts','detector/upstream/positional.ts','detector/LICENSE','detector/shared_detector.bin']
    result=subprocess.run(['git','check-attr','text','--',*names],cwd=ROOT,capture_output=True,text=True,encoding='utf8',check=True)
    assert len(result.stdout.splitlines())==len(names)
    assert all(line.endswith(': text: unset') for line in result.stdout.splitlines())


def test_large_svg_payloads_have_short_explicit_pytest_ids():
    tree=ast.parse((ROOT/'tests/test_pelican_svg_security.py').read_text(encoding='utf8'))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='test_unsafe_output_has_no_renderable_svg')
    mark=function.decorator_list[0]
    identifiers=next(k.value for k in mark.keywords if k.arg=='ids')
    values=ast.literal_eval(identifiers)
    assert len(values)==17 and len(values)==len(set(values))
    assert all(isinstance(value,str) and 0<len(value)<80 for value in values)
