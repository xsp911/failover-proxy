#!/usr/bin/env python3
"""Validate ready-to-serve static UI. No build, Node or npm required."""
from html.parser import HTMLParser
from pathlib import Path
import hashlib
import json
import re

ROOT=Path(__file__).resolve().parents[1]


class Markup(HTMLParser):
    def __init__(self):super().__init__();self.ids=[];self.resources=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if a.get('id'):self.ids.append(a['id'])
        if tag in ('script','link') and (a.get('src') or a.get('href')):self.resources.append(a.get('src',a.get('href')))


def main():
    files=[ROOT/'web'/name for name in ('index.html','styles.css','app.js')]
    parser=Markup();parser.feed(files[0].read_text(encoding='utf8'))
    if len(parser.ids)!=len(set(parser.ids)):raise ValueError('duplicate_ui_ids')
    if any(url.startswith(('http:','https:','//')) for url in parser.resources):raise ValueError('remote_ui_dependency')
    css=files[1].read_text(encoding='utf8')
    if '@import' in css or re.search(r'url\([\"\']?https?://',css):raise ValueError('remote_css_dependency')
    print(json.dumps({'ok':True,'frontend_build_required':False,'frontend_node_required':False,'files':[{'name':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]},indent=2))

if __name__=='__main__':main()
