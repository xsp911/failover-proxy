'use strict';
// Trusted, offline renderer. SVG is validated by the Python caller.
const MAX_INPUT = 300 * 1024;
const MAX_OUTPUT = 3 * 1024 * 1024;
const FRAME_TIMES = [0, 0.5, 1, 1.5];
const SCENE_URL = 'http://render.invalid/scene.svg';
function playwrightModule() {
  for (const name of [process.env.FP_PLAYWRIGHT_MODULE, require('node:path').resolve(__dirname, '../../runtime/node_modules/playwright'), 'playwright', '/tmp/failover-browser-check/node_modules/playwright']) {
    if (!name || (name === process.env.FP_PLAYWRIGHT_MODULE && !require('node:path').isAbsolute(name))) continue;
    try { return require(name); } catch (_) { /* operator-installed fallback */ }
  }
  throw new Error('pelican_renderer_unavailable');
}
async function input() {
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT) throw new Error('pelican_renderer_input_limit');
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.svg !== 'string' || Buffer.byteLength(value.svg) > MAX_INPUT) {
    throw new Error('pelican_renderer_invalid_input');
  }
  return value.svg;
}
async function main() {
  const { chromium } = playwrightModule();
  if (process.argv.includes('--check')) { process.stdout.write('available\n'); return; }
  const svg = await input();
  let browser;
  try {
    try { browser = await chromium.launch({headless: true, args: ['--no-sandbox']}); }
    catch (_) { throw new Error('pelican_renderer_unavailable'); }
    const context = await browser.newContext({
      viewport: {width: 480, height: 320}, deviceScaleFactor: 1,
      javaScriptEnabled: true, serviceWorkers: 'block', acceptDownloads: false, offline: true,
    });
    let sceneDelivered = false;
    await context.route('**/*', async route => {
      const request = route.request();
      if (!sceneDelivered && request.url() === SCENE_URL && request.method() === 'GET' && request.isNavigationRequest()) {
        sceneDelivered = true;
        await route.fulfill({status: 200, contentType: 'image/svg+xml', headers: {
          'Content-Security-Policy': "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
          'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        }, body: svg});
      } else await route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    await page.goto(SCENE_URL, {waitUntil: 'load', timeout: 5000});
    const shapeCount = await page.evaluate(() => {
      const root = document.documentElement;
      root.setAttribute('width', '480'); root.setAttribute('height', '320');
      if (typeof root.pauseAnimations === 'function') root.pauseAnimations();
      return Array.from(document.querySelectorAll('path,rect,circle,ellipse,line,polyline,polygon,text,use'))
        .filter(node => !node.closest('defs,clipPath,mask,pattern,symbol')).length;
    });
    const frames = [];
    for (const time of FRAME_TIMES) {
      await page.evaluate(async time => {
        const root = document.documentElement;
        if (typeof root.setCurrentTime === 'function') root.setCurrentTime(time);
        for (const animation of document.getAnimations()) { animation.pause(); animation.currentTime = time * 1000; }
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      }, time);
      frames.push((await page.screenshot({type: 'png', animations: 'allow', timeout: 5000})).toString('base64'));
    }
    // This separate empty page handles only PNG bytes, never the SVG.
    const comparePage = await context.newPage();
    const shares = await comparePage.evaluate(async encoded => {
      const pixels = [];
      for (const base64 of encoded) {
        const bytes = Uint8Array.from(atob(base64), value => value.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
        if (bitmap.width !== 480 || bitmap.height !== 320) throw new Error('invalid_frame');
        const canvas = new OffscreenCanvas(480, 320);
        const ctx = canvas.getContext('2d', {willReadFrequently: true});
        ctx.drawImage(bitmap, 0, 0); bitmap.close();
        pixels.push(ctx.getImageData(0, 0, 480, 320).data);
      }
      return pixels.slice(1).map((current, index) => {
        const previous = pixels[index]; let changed = 0;
        for (let pixel = 0; pixel < current.length; pixel += 4) {
          if ([0, 1, 2, 3].some(channel => Math.abs(current[pixel + channel] - previous[pixel + channel]) > 30)) changed++;
        }
        return changed / (480 * 320);
      });
    }, frames);
    const result = JSON.stringify({shape_count: shapeCount, frame_times: FRAME_TIMES,
      changed_pixel_shares: shares, motion_detected: shares.some(value => value > 0),
      semantic_quality_evaluated: false, frames});
    if (Buffer.byteLength(result) > MAX_OUTPUT) throw new Error('pelican_renderer_output_limit');
    process.stdout.write(result);
  } finally { if (browser) await browser.close(); }
}
main().catch(error => {
  const code = /^pelican_renderer_[a-z_]+$/.test(error.message || '') ? error.message : 'pelican_renderer_failed';
  process.stderr.write(code + '\n'); process.exitCode = 1;
});
