// Regression checks against the existing disposable local QA service only.
// The original browser test should have left its synthetic photos used.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';

const require = createRequire(process.env.AIDOG_PHOTOS_QA_PACKAGE_JSON || new URL('../package.json', import.meta.url));
const {chromium} = require('playwright');
const root = path.resolve(import.meta.dirname, '..');
const origin = 'http://127.0.0.1:18989', base = origin + '/aidog-photos';
assert.equal(new URL(base).hostname, '127.0.0.1');
const downloads = await fs.mkdtemp(path.join(os.tmpdir(), 'aidog-browser-downloads-'));
const browser = await chromium.launch({headless: true, executablePath: process.env.PHOTO_SAVE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const checks = [], pageErrors = [], unexpectedNetwork = [], contexts = [], pages = [];
const check = (name, value) => { assert(value, name); checks.push(name); };
const sameSet = (a, b) => a.length === b.length && a.every(value => b.includes(value));
const imageUrls = page => page.locator('.photo-tile img').evaluateAll(imgs => imgs.map(img => img.getAttribute('src')));
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
let adminContext;

async function localContext() {
  const context = await browser.newContext({viewport: {width: 390, height: 844}, acceptDownloads: true});
  contexts.push(context);
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== origin) {
      unexpectedNetwork.push(url.origin); return route.abort();
    }
    return route.continue();
  });
  return context;
}

async function guest() {
  const context = await localContext();
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'share', {value: undefined, configurable: true});
    Object.defineProperty(navigator, 'canShare', {value: undefined, configurable: true});
  });
  const page = await context.newPage(); pages.push(page);
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(base + '/');
  await page.waitForFunction(() => document.querySelectorAll('.photo-tile img').length === 3 && !document.getElementById('save-button').disabled);
  return {context, page};
}

async function stats() {
  const response = await adminContext.request.get(base + '/api/admin/photos');
  assert.equal(response.status(), 200); return response.json();
}

async function waitStats(expected) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const value = await stats();
    if (Object.entries(expected).every(([key, count]) => value.stats[key] === count)) return value;
    await new Promise(resolve => setTimeout(resolve, 60));
  }
  assert.fail('QA statistics did not reach ' + JSON.stringify(expected));
}

try {
  adminContext = await localContext();
  const password = (await fs.readFile(path.join(root, '.local/qa/qa-password'), 'utf8')).trimEnd();
  const login = await adminContext.request.post(base + '/api/admin/login', {headers: {Origin: origin}, data: {password}});
  assert.equal(login.status(), 200);
  const before = await stats();
  check('QA begins with no available or reserved photos', before.stats.available === 0 && before.stats.reserved === 0);
  const originals = new Map(before.photos.filter(p => p.status === 'used').map(p => [p.id, p.usedAt]));
  const names = (await fs.readdir(path.join(root, 'demo-images'))).filter(name => /^AI示例_.*\.png$/.test(name)).sort();
  assert.equal(names.length, 6);
  for (const name of names) {
    const bytes = await fs.readFile(path.join(root, 'demo-images', name));
    const response = await adminContext.request.post(base + '/api/admin/photos', {
      headers: {Origin: origin, 'Content-Type': 'image/png', 'X-File-Name': encodeURIComponent(name)}, data: bytes,
    });
    assert.equal(response.status(), 200); const result = await response.json();
    assert.equal(result.duplicate, false, 'Run this test once against a fresh local QA demo library.');
  }
  const seeded = await waitStats({available: 6, reserved: 0, used: before.stats.used});
  check('Six existing demo PNGs added to local QA only', seeded.stats.all === before.stats.all + 6);

  const a = await guest(), b = await guest(), c = await guest();
  const previewA = await imageUrls(a.page), previewB = await imageUrls(b.page), previewC = await imageUrls(c.page);
  check('Three independent contexts all display three photos', previewA.length === 3 && previewB.length === 3 && previewC.length === 3);
  check('Unclaimed previews can overlap', sameSet(previewA, previewB));
  const viewing = await stats();
  check('Three viewers leave all six photos available', viewing.stats.available === 6 && viewing.stats.reserved === 0);

  await a.page.locator('#save-button').click();
  await a.page.locator('#save-dialog').waitFor({state: 'visible'});
  const claimedA = await imageUrls(a.page);
  check('A opens manual saving with three locked photos', (await waitStats({available: 3, reserved: 3})).stats.reserved === 3);
  await b.page.locator('#save-button').click();
  await b.page.locator('#save-dialog').waitFor({state: 'visible'});
  const claimedB = await imageUrls(b.page);
  check('B automatically replaces a stale overlapping preview', !claimedA.some(url => claimedB.includes(url)));
  const panelB = await b.page.locator('#save-dialog-photos img').evaluateAll(imgs => imgs.map(img => img.getAttribute('src')));
  check('B saving panel uses its new locked snapshot', sameSet(panelB, claimedB));
  check('Only the two actual saving flows reserve six photos', (await waitStats({available: 0, reserved: 6})).stats.reserved === 6);

  for (let index = 0; index < 3; index++) {
    const response = await b.context.request.get(origin + panelB[index]);
    assert.equal(response.status(), 200); const expected = await response.body();
    const downloading = b.page.waitForEvent('download');
    await b.page.locator('.download-one').nth(index).click();
    const download = await downloading;
    assert.equal(await download.failure(), null);
    const file = path.join(downloads, `photo-${index + 1}.png`);
    await download.saveAs(file);
    const saved = await fs.readFile(file);
    check(`Downloaded PNG ${index + 1} matches server bytes`, saved.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) && digest(saved) === digest(expected));
    const decoded = await b.page.evaluate(async url => {
      const blob = await (await fetch(url)).blob();
      const bitmap = await createImageBitmap(blob); const result = {width: bitmap.width, height: bitmap.height}; bitmap.close(); return result;
    }, panelB[index]);
    check(`Downloaded PNG ${index + 1} decodes correctly`, decoded.width > 0 && decoded.height > 0);
  }
  check('Three actual browser download files were created', (await fs.readdir(downloads)).length === 3);

  await c.page.reload(); await c.page.locator('#empty-state').waitFor({state: 'visible'});
  check('C correctly reports no free group while both saving panels own photos', await c.page.locator('#empty-state').isVisible());
  check('The empty page is in the foreground for automatic retry', await c.page.evaluate(() => !document.hidden));
  await a.page.locator('#close-save-dialog').click();
  await a.page.locator('#save-dialog').waitFor({state: 'hidden'});
  const released = await waitStats({available: 3, reserved: 3, used: before.stats.used});
  check('Closing A saving panel releases three photos without usage', released.stats.available === 3);
  await c.page.waitForFunction(() => document.getElementById('empty-state').hidden && document.querySelectorAll('.photo-tile img').length === 3, undefined, {timeout: 10000});
  check('C automatically recovers a full group after photos are released', await c.page.locator('.photo-tile img').count() === 3 && !await c.page.locator('#empty-state').isVisible());
  check('Automatic recovery and A preview do not reserve the free group', (await stats()).stats.available === 3 && (await stats()).stats.reserved === 3);

  await b.page.locator('#manual-confirm').click();
  const finished = await waitStats({available: 3, reserved: 0, used: before.stats.used + 3});
  check('B confirmation records exactly its three used photos', finished.stats.used === before.stats.used + 3);
  check('Original used synthetic records keep their timestamps', [...originals].every(([id, stamp]) => finished.photos.some(p => p.id === id && p.status === 'used' && p.usedAt === stamp)));
  check('No JavaScript errors or external network requests occurred', pageErrors.length === 0 && unexpectedNetwork.length === 0);
  await fs.writeFile(path.join(root, 'output/preview-browser-check.json'), JSON.stringify({
    ok: true, checks: checks.length, checkNames: checks, pageErrors, unexpectedNetwork,
    localQAOrigin: origin, actualBrowserDownloads: 3, phoneAlbumSaveVerified: false,
    productionTouched: false, finalStats: finished.stats,
  }, null, 2) + '\n');
  console.log(JSON.stringify({ok: true, checks: checks.length, actualBrowserDownloads: 3, phoneAlbumSaveVerified: false, productionTouched: false, finalStats: finished.stats}));
} finally {
  for (const page of pages) {
    try { if (await page.locator('#save-dialog').isVisible()) await page.locator('#close-save-dialog').click(); } catch {}
  }
  try { await adminContext?.request.post(base + '/api/admin/logout', {headers: {Origin: origin}, data: {}}); } catch {}
  await browser.close(); await fs.rm(downloads, {recursive: true, force: true});
}
