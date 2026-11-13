// Real HTTP/browser checks against the isolated package QA instance only.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AIDOG_PHOTOS_QA_PACKAGE_JSON || new URL('../package.json',import.meta.url));
const {chromium}=require('playwright');
const root=path.resolve(import.meta.dirname,'..'),base='http://127.0.0.1:18989/aidog-photos';
const browser=await chromium.launch({headless:true,executablePath:process.env.PHOTO_SAVE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
let checks=0;const errors=[];const check=value=>{assert(value);checks++;};
try {
  const context=await browser.newContext({viewport:{width:1360,height:1000}}),admin=await context.newPage();
  admin.on('pageerror',e=>errors.push(e.message));
  await admin.goto(base+'/admin');await admin.locator('#password').fill(await fs.readFile(root+'/.local/package-qa/qa-password','utf8'));
  await admin.locator('#login-button').click();await admin.waitForFunction(()=>document.getElementById('stat-all').textContent==='15');
  check(await admin.locator('#package-submit').isDisabled());
  await admin.locator('#photo-files').setInputFiles([1,2].map(i=>root+'/.local/qa/photos/qa-'+i+'.png'));
  await admin.locator('#package-copy').fill('缺少一张照片的测试');check(await admin.locator('#package-submit').isDisabled());
  await admin.locator('#photo-files').setInputFiles([1,2,3].map(i=>root+'/.local/qa/photos/qa-'+i+'.png'));
  await admin.locator('#package-copy').fill('');check(await admin.locator('#package-submit').isDisabled());
  const text='这是一套浏览器测试图文。\n'+('复制按钮应取得完整内容，图片与文案一起保存。'.repeat(8));
  await admin.locator('#package-copy').fill(text);await admin.locator('#package-title').fill('浏览器测试套');check(await admin.locator('#package-submit').isEnabled());
  await admin.locator('#package-submit').click();await admin.waitForFunction(()=>document.getElementById('stat-all').textContent==='16');
  let listing=await (await context.request.get(base+'/api/admin/packages')).json();
  const created=listing.packages.find(x=>x.title==='浏览器测试套');check(created?.copy===text&&created.photos.length===3);
  check(await admin.locator('#package-copy').inputValue()==='');
  const guestContext=await browser.newContext({viewport:{width:390,height:844},permissions:['clipboard-read','clipboard-write']});
  await guestContext.addInitScript(()=>Object.defineProperty(navigator,'canShare',{value:()=>false}));
  const guest=await guestContext.newPage();guest.on('pageerror',e=>errors.push(e.message));const requests=[];guest.on('request',r=>requests.push(r.url()));
  await guest.goto(base+'/');await guest.waitForFunction(()=>document.querySelectorAll('.photo-tile img').length===3&&!document.getElementById('copy-button').disabled);
  await guest.waitForLoadState('networkidle');await guest.evaluate(()=>document.fonts.ready);
  check(requests.filter(x=>x.includes('/api/image/')&&!x.includes('variant=preview')).length===0);
  check(requests.filter(x=>x.includes('/fonts/')&&x.endsWith('yahei.woff2')).length===0);
  const snapshots=[];
  for(let i=0;i<3;i++){
    const copy=await guest.locator('#guest-copy').textContent();const ids=await guest.locator('.photo-tile img').evaluateAll(imgs=>imgs.map(x=>x.src));
    check(copy.trim().length>0&&ids.length===3);snapshots.push({copy,ids});
    await guest.locator('#copy-button').click();check(await guest.evaluate(()=>navigator.clipboard.readText())===copy);
    if(i<2){await guest.locator('#next-button').click();await guest.waitForFunction(previous=>document.getElementById('guest-copy').textContent!==previous,copy);}
  }
  check(new Set(snapshots.map(x=>x.copy)).size===3);
  check(new Set(snapshots.flatMap(x=>x.ids)).size===9);
  const beforeCopy=await guest.locator('#guest-copy').textContent();
  listing=await (await context.request.get(base+'/api/admin/packages')).json();check(listing.stats.used===1&&listing.stats.reserved===0);
  await guest.screenshot({path:root+'/output/图文套装-真实手机布局.png',fullPage:true});
  await guest.locator('#save-button').click();await guest.locator('#save-dialog').waitFor({state:'visible'});
  await guest.waitForFunction(()=>[...document.querySelectorAll('.download-one')].every(a=>a.href.startsWith('blob:')));
  check(await guest.locator('.download-one').count()===3);
  check(await guest.locator('#guest-copy').textContent()===beforeCopy);
  await guest.locator('#manual-confirm').click();await guest.waitForFunction(()=>document.getElementById('batch-state').textContent==='已领取');
  listing=await (await context.request.get(base+'/api/admin/packages')).json();check(listing.stats.used===2);
  await admin.locator('[data-filter="used"]').click();await admin.waitForFunction(()=>document.querySelectorAll('.package-card').length===2);
  check(await admin.getByRole('button',{name:'编辑文案',exact:true}).count()===0);
  await admin.screenshot({path:root+'/output/图文套装-真实后台.png',fullPage:true});
  check(errors.length===0);
  await fs.writeFile(root+'/output/packages-browser-real.json',JSON.stringify({ok:true,checks,pageErrors:errors,clipboardFullText:true,threePhotosAndCopyBound:true,noProductionRequests:true},null,2)+'\n');
  console.log(JSON.stringify({ok:true,checks,pageErrors:errors.length,productionTouched:false}));
} finally {await browser.close();}
