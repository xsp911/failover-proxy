import asyncio
import base64
import json
import os
import signal
import struct
import zlib

import pytest

from proxy import pelican_motion as motion

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 320"><circle cx="30" cy="50" r="20"><animate attributeName="cx" from="30" to="400" dur="2s" repeatCount="indefinite"/></circle></svg>'


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 480, 320, 8, 6, 0, 0, 0))
    data += chunk(b'IDAT', zlib.compress((b'\0' + bytes(480 * 4)) * 320)) + chunk(b'IEND', b'')
    return base64.b64encode(data).decode('ascii')


def result():
    return {'shape_count': 1, 'frame_times': [0, .5, 1, 1.5], 'changed_pixel_shares': [0, 0, 0],
            'motion_detected': False, 'semantic_quality_evaluated': False, 'frames': [png()] * 4}


@pytest.mark.asyncio
async def test_capture_revalidates_before_subprocess(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail('unsafe input must not start a process')
    monkeypatch.setattr(motion, '_invoke', unexpected)
    for svg in ('<svg><script>alert(1)</script></svg>', '<svg><use href="file:///etc/passwd"/></svg>', '<svg onload="alert(1)"/>', 'x' * 300000, None):
        with pytest.raises(ValueError, match='pelican_renderer_unsafe_svg'):
            await motion.capture(svg)


@pytest.mark.asyncio
async def test_capture_nonce_not_required_and_returns_only_validated_frames(monkeypatch):
    seen = []
    async def invoke(payload):
        seen.append(json.loads(payload))
        return json.dumps(result()).encode()
    monkeypatch.setattr(motion, '_invoke', invoke)
    assert await motion.capture(SVG) == result()
    assert seen[0]['svg'].startswith('<svg')
    assert 'script' not in seen[0]['svg']


def test_result_rejects_malicious_or_malformed_formats():
    modifications = [
        {'frames': ['data:image/png;base64,' + png()] * 4}, {'frames': [base64.b64encode(b'<svg/>').decode()] * 4},
        {'frame_times': [0, .5, 1, 2]}, {'frames': [png()] * 5}, {'shape_count': True},
        {'changed_pixel_shares': [float('nan'), 0, 0]}, {'motion_detected': True}, {'semantic_quality_evaluated': True},
        {'unexpected': 'x'}, {'frames': [png()[:-4] + 'AAAA'] * 4},
    ]
    for modification in modifications:
        value = result(); value.update(modification)
        with pytest.raises(ValueError, match='pelican_renderer_invalid_output'):
            motion._validated_result(json.dumps(value).encode())


@pytest.mark.asyncio
async def test_semaphore_busy_does_not_launch(monkeypatch):
    semaphore = asyncio.Semaphore(0)
    monkeypatch.setattr(motion, '_SEMAPHORE', semaphore)
    monkeypatch.setattr(motion, 'ACQUIRE_TIMEOUT_SECONDS', .001)
    with pytest.raises(ValueError, match='pelican_renderer_busy'):
        await motion.capture(SVG)


class Pipe:
    def __init__(self):
        self.written = b''
    def write(self, data):
        self.written += data
    async def drain(self):
        pass
    def close(self):
        pass


@pytest.mark.asyncio
async def test_subprocess_minimal_environment_absolute_script_kills_group(monkeypatch):
    process = type('Process', (), {})()
    process.pid = 987654321
    process.stdin = Pipe()
    process.stdout = asyncio.StreamReader(); process.stdout.feed_data(json.dumps(result()).encode()); process.stdout.feed_eof()
    process.stderr = asyncio.StreamReader(); process.stderr.feed_eof()
    async def wait():
        return 0
    process.wait = wait
    seen = {}; killed = []
    async def start(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return process
    monkeypatch.setattr(motion.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(motion, '_node', lambda: '/usr/bin/node')
    monkeypatch.setattr(motion.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setenv('OPENAI_API_KEY', 'never-forward')
    monkeypatch.setenv('HTTPS_PROXY', 'never-forward')
    monkeypatch.setenv('FP_PLAYWRIGHT_MODULE', '/operator/playwright')
    await motion.capture(SVG)
    assert seen['args'] == ('/usr/bin/node', str(motion.SCRIPT))
    assert motion.SCRIPT.is_absolute() and seen['kwargs']['start_new_session'] is True
    assert set(seen['kwargs']['env']) <= {'PATH', 'HOME', 'FP_PLAYWRIGHT_MODULE'}
    assert seen['kwargs']['env']['FP_PLAYWRIGHT_MODULE'] == '/operator/playwright'
    assert killed == [(process.pid, signal.SIGKILL)]
    assert json.loads(process.stdin.written)['svg']


@pytest.mark.asyncio
async def test_bounded_reader_and_timeout_cleanup(monkeypatch):
    stream = asyncio.StreamReader(); stream.feed_data(b'12345'); stream.feed_eof()
    with pytest.raises(ValueError, match='output_limit'):
        await motion._read_bounded(stream, 4)
    process = type('Process', (), {})()
    process.pid = 987654321; process.stdin = Pipe()
    process.stdout = asyncio.StreamReader(); process.stderr = asyncio.StreamReader()
    killed = []
    async def wait():
        return 0
    process.wait = wait
    async def start(*args, **kwargs):
        return process
    monkeypatch.setattr(motion.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(motion, '_node', lambda: '/usr/bin/node')
    monkeypatch.setattr(motion.os, 'killpg', lambda *args: killed.append(args))
    monkeypatch.setattr(motion, 'TIMEOUT_SECONDS', .001)
    with pytest.raises(ValueError, match='pelican_renderer_timeout'):
        await motion._invoke(b'{}')
    assert killed == [(process.pid, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_missing_node_reported_unavailable(monkeypatch):
    monkeypatch.setattr(motion, '_node', lambda: None)
    assert await motion.renderer_available() is False
    with pytest.raises(ValueError, match='pelican_renderer_unavailable'):
        await motion.capture(SVG)


@pytest.mark.asyncio
async def test_actual_offline_renderer_when_available():
    if not await motion.renderer_available():
        pytest.skip('Node/Playwright unavailable')
    try:
        value = await motion.capture(SVG)
    except ValueError as error:
        if str(error) == 'pelican_renderer_unavailable':
            pytest.skip('Playwright browser unavailable')
        raise
    assert value['shape_count'] == 1
    assert value['motion_detected'] is True
    assert value['semantic_quality_evaluated'] is False
    assert len(value['frames']) == 4 and all(value['changed_pixel_shares'])
    static = await motion.capture('<svg xmlns="http://www.w3.org/2000/svg"><circle cx="30" cy="30" r="20"/></svg>')
    assert static['motion_detected'] is False and static['changed_pixel_shares'] == [0, 0, 0]
