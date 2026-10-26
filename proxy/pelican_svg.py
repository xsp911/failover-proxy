"""Independently implemented restrictive SVG validator; no generated scripts run."""
import math
import re
import xml.etree.ElementTree as ET

NS='http://www.w3.org/2000/svg'
TAGS=set('svg g defs symbol use title desc path rect circle ellipse line polyline polygon text tspan textPath linearGradient radialGradient stop clipPath mask pattern filter feGaussianBlur feOffset feBlend feColorMatrix feComposite feFlood feMerge feMergeNode animate animateTransform animateMotion mpath style'.split())
ATTRS=set('id class x y x1 y1 x2 y2 cx cy r rx ry width height viewBox preserveAspectRatio d points transform opacity fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset stroke-miterlimit font-family font-size font-weight font-style text-anchor dominant-baseline alignment-baseline dx dy rotate textLength lengthAdjust offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod fx fy fr clip-path clipPathUnits mask maskUnits maskContentUnits patternUnits patternContentUnits patternTransform patternContentUnits filter filterUnits primitiveUnits in in2 result stdDeviation type values value mode operator k1 k2 k3 k4 flood-color flood-opacity color-interpolation-filters href startOffset method spacing style attributeName attributeType from to by dur begin end repeatCount repeatDur calcMode keyTimes keySplines keyPoints additive accumulate path keyPoints xmlns'.split())
ANIMATED=set('x y x1 y1 x2 y2 cx cy r rx ry width height d points transform opacity fill fill-opacity stroke stroke-width stroke-opacity stroke-dashoffset stroke-dasharray rotate offset stop-color stop-opacity'.split())
CSS_BAD=re.compile(r'\\|/\*|\*/|@import|@font-face|javascript\s*:|expression\s*\(|(?:https?|data|file|ftp)\s*:|[<>]',re.I)
URL=re.compile(r'url\s*\((.*?)\)',re.I|re.S)
ID=re.compile(r'#[A-Za-z_][A-Za-z0-9_.:-]*\Z')


def _css(value):
    if CSS_BAD.search(value) or any(ord(c)<32 and c not in '\n\r\t' for c in value):raise ValueError('unsafe_style')
    for match in URL.finditer(value):
        if not ID.fullmatch(match[1].strip().strip('\"\'')):raise ValueError('external_resource')
    # Disallow obfuscated/malformed url tokens that escaped the complete matcher.
    if re.search(r'url\s*\(',URL.sub('',value),re.I):raise ValueError('unsafe_style')


def inspect_svg(output,nonce):
    checks={'safe_svg':False,'animation':False,'nonce':False}
    def fail(code):return {'svg':None,'checks':checks,'error_kind':'pelican_'+code}
    if not isinstance(output,str) or len(output.encode('utf8',errors='replace'))>262144:return fail('svg_size_limit')
    if re.search(r'<!DOCTYPE|<!ENTITY|<\?',output,re.I):return fail('unsafe_svg')
    # Ordinary XML comments carry no rendered content. Remove only complete
    # bounded comments, never permit declarations/entities or processing code.
    text=re.sub(r'<!--[\s\S]*?-->','',output).strip()
    if '<!' in text:return fail('unsafe_svg')
    if text.startswith('```'):
        match=re.fullmatch(r'```(?:svg|xml)?\s*([\s\S]*?)\s*```',text,re.I)
        if not match:return fail('invalid_svg')
        text=match[1]
    try:
        root=ET.fromstring(text)
        if root.tag not in ('svg','{'+NS+'}svg'):return fail('invalid_svg')
        stack=[(root,1)];count=0
        while stack:
            node,depth=stack.pop();count+=1
            if count>5000 or depth>50:raise ValueError('svg_complexity_limit')
            tag=node.tag
            if not isinstance(tag,str):raise ValueError('unsafe_svg')
            if tag.startswith('{'):
                namespace,tag=tag[1:].split('}',1)
                if namespace!=NS:raise ValueError('unsafe_svg')
            if tag not in TAGS:raise ValueError('unsafe_svg')
            for attribute,value in node.attrib.items():
                if len(value)>10000:raise ValueError('svg_complexity_limit')
                name=attribute
                if attribute.startswith('{'):
                    namespace,name=attribute[1:].split('}',1)
                    if namespace!='http://www.w3.org/1999/xlink' or name!='href':raise ValueError('unsafe_svg')
                if name not in ATTRS or name.lower().startswith('on'):raise ValueError('unsafe_svg')
                if name=='href' and not ID.fullmatch(value):raise ValueError('external_resource')
                if name=='attributeName' and value not in ANIMATED:raise ValueError('unsafe_svg')
                if name in ('begin','end') and not re.fullmatch(r'(?:[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:ms|s)?|indefinite)',value):raise ValueError('unsafe_svg')
                if name in ('style','fill','stroke','clip-path','mask','filter','cursor','values','from','to','by'):_css(value)
            if tag=='style':
                content=''.join(node.itertext());_css(content)
                if re.search(r'@keyframes\b',content) and re.search(r'animation(?:-name)?\s*:',content):checks['animation']=True
            if tag in ('animate','animateTransform','animateMotion'):checks['animation']=True
            if tag=='text' and nonce and nonce in ''.join(node.itertext()):checks['nonce']=True
            stack.extend((child,depth+1) for child in node)
        raw=root.get('viewBox')
        if raw:
            box=[float(x) for x in re.split(r'[\s,]+',raw.strip())]
            if len(box)!=4 or not all(math.isfinite(x) and abs(x)<=100000 for x in box) or not 0<box[2]<=10000 or not 0<box[3]<=10000:raise ValueError('invalid_svg_dimensions')
        else:root.set('viewBox','0 0 960 640')
        root.set('width','960');root.set('height','640')
        if root.tag=='svg':root.set('xmlns',NS)
        ET.register_namespace('',NS)
        svg=ET.tostring(root,encoding='unicode')
        checks['safe_svg']=True
        error='pelican_missing_animation' if not checks['animation'] else 'pelican_missing_nonce' if not checks['nonce'] else None
        return {'svg':svg,'checks':checks,'error_kind':error}
    except ET.ParseError:return fail('invalid_svg')
    except (ValueError,OverflowError) as e:
        code=str(e)
        return fail(code if code in ('unsafe_svg','external_resource','unsafe_style','svg_complexity_limit','invalid_svg_dimensions') else 'invalid_svg')
