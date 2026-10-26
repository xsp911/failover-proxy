"""Bounded, offline diagnostics for an already-sanitized SVG. No model calls."""
import asyncio
import base64
import binascii
import json
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import zlib

from .pelican_svg import inspect_svg

SCRIPT = Path(__file__).with_suffix('.cjs').resolve()
TIMEOUT_SECONDS = 25
ACQUIRE_TIMEOUT_SECONDS = 1
MAX_STDOUT = 4 * 1024 * 1024
MAX_RESULT = 3 * 1024 * 1024
MAX_STDERR = 16 * 1024
FRAME_TIMES = [0, .5, 1, 1.5]
_SEMAPHORE = asyncio.Semaphore(1)


def _environment():
    # Operator configuration only; never copy provider credentials/proxy variables.
    return {key: os.environ[key] for key in ('PATH', 'HOME', 'FP_PLAYWRIGHT_MODULE') if key in os.environ}


def _node():
    return shutil.which('node', path=os.environ.get('PATH', os.defpath))


async def _read_bounded(stream, maximum):
    chunks = []; size = 0
    while True:
        chunk = await stream.read(min(65536, maximum + 1 - size))
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk); size += len(chunk)
        if size > maximum:
            raise ValueError('pelican_renderer_output_limit')


async def _invoke(payload=None, check=False):
    # Optional renderer is unavailable on Windows until process-tree cleanup is
    # backed by a tested Job Object implementation. Core Python/UI still work.
    if os.name=='nt':raise ValueError('pelican_renderer_unavailable')
    node = _node()
    if not node:
        raise ValueError('pelican_renderer_unavailable')
    process = None
    tasks = []
    try:
        process = await asyncio.create_subprocess_exec(
            node, str(SCRIPT), *(['--check'] if check else []),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=_environment(), start_new_session=True,
        )
        async def feed():
            try:
                if payload:
                    process.stdin.write(payload)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
        tasks = [asyncio.create_task(feed()),
                 asyncio.create_task(_read_bounded(process.stdout, MAX_STDOUT)),
                 asyncio.create_task(_read_bounded(process.stderr, MAX_STDERR)),
                 asyncio.create_task(process.wait())]
        try:
            _, stdout, stderr, code = await asyncio.wait_for(asyncio.gather(*tasks), TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ValueError('pelican_renderer_timeout') from None
        if code:
            if stderr.strip() == b'pelican_renderer_unavailable':
                raise ValueError('pelican_renderer_unavailable')
            raise ValueError('pelican_renderer_failed')
        return stdout
    except OSError:
        raise ValueError('pelican_renderer_unavailable') from None
    finally:
        if process is not None:
            # Kill Chromium descendants even if Node exited before cancellation.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            await process.wait()


async def renderer_available():
    """Cheap module preflight; actual capture additionally checks browser launch."""
    try:
        return (await _invoke(check=True)).strip() == b'available'
    except ValueError:
        return False


def _valid_png(data):
    # Validate a bounded, complete PNG container, CRCs, fixed IHDR dimensions.
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    position = 8; first = True; idat = False
    while position + 12 <= len(data):
        size = struct.unpack('>I', data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        end = position + 12 + size
        if end > len(data) or size > MAX_RESULT:
            return False
        body = data[position + 8:position + 8 + size]
        crc = struct.unpack('>I', data[end - 4:end])[0]
        if zlib.crc32(kind + body) & 0xffffffff != crc:
            return False
        if first:
            if kind != b'IHDR' or size != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            if (width, height) != (480, 320) or depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                return False
            first = False
        elif kind == b'IHDR':
            return False
        if kind == b'IDAT':
            idat = True
        if kind == b'IEND':
            return size == 0 and idat and end == len(data)
        position = end
    return False


def _validated_result(raw):
    try:
        if len(raw) > MAX_RESULT:
            raise ValueError()
        result = json.loads(raw)
        fields = {'shape_count', 'frame_times', 'changed_pixel_shares', 'motion_detected', 'semantic_quality_evaluated', 'frames'}
        if not isinstance(result, dict) or set(result) != fields:
            raise ValueError()
        if type(result['shape_count']) is not int or not 0 <= result['shape_count'] <= 5000:
            raise ValueError()
        times = result['frame_times']
        if not isinstance(times, list) or len(times) != 4 or any(type(value) not in (int, float) for value in times) or times != FRAME_TIMES:
            raise ValueError()
        shares = result['changed_pixel_shares']
        if not isinstance(shares, list) or len(shares) != 3 or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in shares):
            raise ValueError()
        if type(result['motion_detected']) is not bool or result['motion_detected'] != any(value > 0 for value in shares) or result['semantic_quality_evaluated'] is not False:
            raise ValueError()
        frames = result['frames']
        if not isinstance(frames, list) or len(frames) != 4:
            raise ValueError()
        for frame in frames:
            if not isinstance(frame, str) or len(frame) > MAX_RESULT or not frame:
                raise ValueError()
            png = base64.b64decode(frame, validate=True)
            if base64.b64encode(png).decode('ascii') != frame or not _valid_png(png):
                raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, OverflowError, binascii.Error, struct.error):
        raise ValueError('pelican_renderer_invalid_output') from None


async def capture(svg):
    """Capture four PNGs; caller persists them. Missing nonce is intentionally OK."""
    if not isinstance(svg, str):
        raise ValueError('pelican_renderer_unsafe_svg')
    inspected = inspect_svg(svg, '')
    if not inspected.get('checks', {}).get('safe_svg') or not inspected.get('svg'):
        raise ValueError('pelican_renderer_unsafe_svg')
    payload = json.dumps({'svg': inspected['svg']}, ensure_ascii=False).encode('utf8')
    if len(payload) > 300 * 1024:
        raise ValueError('pelican_renderer_input_limit')
    try:
        await asyncio.wait_for(_SEMAPHORE.acquire(), ACQUIRE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise ValueError('pelican_renderer_busy') from None
    try:
        return _validated_result(await _invoke(payload))
    finally:
        _SEMAPHORE.release()
