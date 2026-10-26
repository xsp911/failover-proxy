"""UI ships as ready-to-serve local files, not a frontend toolchain."""
from html.parser import HTMLParser
from pathlib import Path
import re
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


class Markup(HTMLParser):
    def __init__(self):super().__init__();self.ids=[];self.resources=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'id' in attrs:self.ids.append(attrs['id'])
        if tag in ('script','link'):
            value=attrs.get('src',attrs.get('href',''))
            if value:self.resources.append(value)


def test_static_interface_has_unique_contract_ids_and_local_resources():
    html=(ROOT/'web/index.html').read_text();parsed=Markup();parsed.feed(html)
    assert len(parsed.ids)==len(set(parsed.ids))
    assert all(not u.startswith(('https:','http:','//')) for u in parsed.resources)
    assert {'tab-stats','tab-market','tab-detector','tab-detector-log','tab-pelican','tab-settings','journal-search','request-search','journal-detail'}<=set(parsed.ids)
    assert '<html lang="ru">' in html
    assert '<script type="module"' not in html


def test_stylesheet_ready_to_serve_and_motion_accessibility():
    css=(ROOT/'web/styles.css').read_text()
    assert '@import' not in css
    assert 'prefers-reduced-motion' in css and ':focus-visible' in css
    assert '.workspace-sidebar' in css and '.chart-tooltip' in css
    assert not re.search(r'url\([\"\']?https?://',css)


def test_python_factory_starts_and_serves_ui_without_node(tmp_path):
    code='''import asyncio,sys,shutil
from pathlib import Path
import httpx
from proxy.app import create_app
async def main():
 assert shutil.which('node') is None
 app=create_app(Path(sys.argv[1]))
 async with app.router.lifespan_context(app):
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
   for path in ('/','/app.js','/styles.css'):
    response=await c.get(path);assert response.status_code==200
  assert not app.state.monitor.available
asyncio.run(main())'''
    environment={**os.environ,'PATH':'','PYTHONDONTWRITEBYTECODE':'1'}
    for key in ('FP_ADMIN_USERNAME','FP_ADMIN_PASSWORD','FP_DATA_DIR'):environment.pop(key,None)
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path/'data')],cwd=ROOT,env=environment,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stderr


def test_scripts_do_not_store_credentials_in_browser():
    source=(ROOT/'web/app.js').read_text()
    assert 'localStorage' not in source and 'sessionStorage' not in source
    assert 'window.__' not in source
