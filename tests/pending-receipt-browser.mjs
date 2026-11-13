// Fully isolated frontend regression: never contacts a deployed service or writes business data.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AIDOG_PHOTOS_QA_PACKAGE_JSON || new URL('../package.json', import.meta.url));
const {chromium} = require('playwright');
const root = path.resolve(import.meta.dirname, '..'), pendingKey = 'aidog-photos-pending-v1';
const png = await fs.readFile(root + '/public/brand/adog-logo.png');
const calls = [], pageErrors = [], checks = [];
let commitMode = 'expired';
const oldRecord = {batchId: 'older-unsynced-batch', method: 'manual_confirm'};
const snapshot = {
  storeName: '爱多格宠物医院', platforms: [], availableCount: 3,
  batch: {id: 'current-preview-batch', packageId: 'current-package', status: 'preview', copy: '当前这套的配套文案。', expiresAt: Date.now() + 600000,
    photos: [0, 1, 2].map(index => ({id: String(index), name: '照片' + index + '.png', url: '/aidog-photos/api/image/' + index, previewUrl: '/aidog-photos/api/image/' + index + '?variant=preview'}))},
};
const service = http.createServer(async (req, res) => {
  try {
    const chunks = []; for await (const value of req) chunks.push(value);
    const body = Buffer.concat(chunks).toString('utf8');
    calls.push({url: req.url, body});
    const url = new URL(req.url, 'http://local'), route = url.pathname.replace('/aidog-photos', '');
    let data, mime = 'application/json', code = 200;
    if (route === '/api/bootstrap') data = JSON.stringify(snapshot);
    else if (route === '/api/batch/commit') {
      if (commitMode === 'expired') { code = 409; data = JSON.stringify({error: '这套照片已过期，请重新领取'}); }
      else if (commitMode === 'unconfirmed') data = JSON.stringify({ok: true});
      else data = JSON.stringify({status: 'used', usedAt: Date.now(), method: oldRecord.method});
    } else if (route.startsWith('/api/image/')) { data = png; mime = 'image/png'; }
    else {
      data = await fs.readFile(root + '/public' + (route === '/' ? '/index.html' : route));
      mime = route.endsWith('.js') ? 'text/javascript' : route.endsWith('.css') ? 'text/css' : route.endsWith('.woff2') ? 'font/woff2' : route.endsWith('.png') ? 'image/png' : 'text/html';
    }
    res.writeHead(code, {'Content-Type': mime, 'Cache-Control': 'no-store'}); res.end(data);
  } catch { res.writeHead(404, {'Content-Type': 'application/json'}); res.end('{}'); }
});
await new Promise(resolve => service.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + service.address().port;
const browser = await chromium.launch({headless: true, executablePath: process.env.PHOTO_SAVE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const check = (name, value) => { assert(value, name); checks.push(name); };
try {
  const page = await browser.newPage({viewport: {width: 390, height: 844}});
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(([key, record]) => {
    localStorage.setItem(key, JSON.stringify(record));
    Object.defineProperty(navigator, 'share', {value: undefined});
    Object.defineProperty(navigator, 'canShare', {value: undefined});
  }, [pendingKey, oldRecord]);
  await page.goto(origin + '/aidog-photos/');
  await page.locator('#guest-copy-section:not([hidden])').waitFor();
  check('pending receipt does not block bootstrap', calls.some(call => call.url === '/aidog-photos/api/bootstrap'));
  check('all three thumbnails and copy are visible', await page.locator('.photo-tile img').count() === 3 && await page.locator('#guest-copy').textContent() === snapshot.batch.copy);
  check('record is not silently deleted on loading', JSON.parse(await page.evaluate(key => localStorage.getItem(key), pendingKey)).batchId === oldRecord.batchId);
  check('automatic loading does not consume the old or a new set', !calls.some(call => /\/batch\/(commit|prepare|release)/.test(call.url)));
  check('switch remains disabled while receipt is unsynced', await page.locator('#next-button').isDisabled());
  check('main action is a separate receipt retry', (await page.locator('#save-button').textContent()).includes('重试同步保存记录'));
  await page.locator('.photo-tile button').first().click();
  check('pending preview cannot fetch new originals or open a save dialog', !calls.some(call => /^\/aidog-photos\/api\/image\/\d+$/.test(call.url)) && !await page.locator('#save-dialog').evaluate(element => element.open));
  await page.locator('#save-button').click();
  await page.waitForFunction(() => document.querySelector('#guest-message').textContent.includes('需要门店核对'));
  check('expired receipt keeps its original id and method', JSON.stringify(JSON.parse(await page.evaluate(key => localStorage.getItem(key), pendingKey))) === JSON.stringify(oldRecord));
  check('failed retry does not replace current fixed photos or copy', await page.locator('.photo-tile img').first().getAttribute('src') === snapshot.batch.photos[0].previewUrl && await page.locator('#guest-copy').textContent() === snapshot.batch.copy);
  commitMode = 'unconfirmed';
  await page.locator('#save-button').click();
  await page.waitForFunction(() => document.querySelector('#guest-message').textContent.includes('暂未同步'));
  check('unconfirmed HTTP200 cannot clear the receipt', await page.evaluate(key => localStorage.getItem(key) !== null, pendingKey));
  commitMode = 'used';
  await page.locator('#save-button').click();
  await page.waitForFunction(key => localStorage.getItem(key) === null, pendingKey);
  check('only confirmed used status clears the old receipt', await page.evaluate(key => localStorage.getItem(key) === null, pendingKey));
  check('all retry requests refer to the exact old batch', calls.filter(call => call.url.includes('/batch/commit')).every(call => JSON.parse(call.body).batchId === oldRecord.batchId));
  check('retry success never automatically downloads or switches sets', !calls.some(call => /\/batch\/(prepare|release)/.test(call.url)));
  check('normal download and switch restore only after sync', await page.locator('#next-button').isEnabled() && (await page.locator('#save-button').textContent()).includes('下载照片'));
  check('no browser page errors', pageErrors.length === 0);
  console.log(JSON.stringify({ok: true, checks, pageErrors, productionTouched: false}));
} finally { await browser.close(); await new Promise(resolve => service.close(resolve)); }
