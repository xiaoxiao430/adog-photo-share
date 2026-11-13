(() => {
  'use strict';
  const BASE = '/aidog-photos';
  const $ = id => document.getElementById(id);
  const grid = $('photo-grid'), save = $('save-button'), next = $('next-button');
  const message = $('guest-message'), dialog = $('save-dialog');
  const PENDING_KEY = 'aidog-photos-pending-v1';
  let batch = null, files = [], objectUrls = [], loading = false, sharing = false, expired = false;
  let prepareController = null, loadGeneration = 0, heartbeat = null, renewInFlight = null;
  let pendingReceipt = null;
  try { pendingReceipt = JSON.parse(localStorage.getItem(PENDING_KEY) || 'null'); } catch {}

  function dateNumber(value) {
    if (!value) return 0;
    if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
    return Date.parse(value) || 0;
  }
  function tell(text, type = '') {
    message.textContent = text;
    message.className = 'message' + (type ? ' ' + type : '');
    message.hidden = !text;
  }
  async function api(path, body, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 18000);
    try {
      const response = await fetch(BASE + '/api' + path, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}), ...options,
      });
      let data;
      try { data = await response.json(); } catch { throw new Error('连接暂时中断，请重试。'); }
      if (!response.ok) { const error = new Error(data.error || '操作暂未完成，请重试。'); error.status = response.status; throw error; }
      return data;
    } finally { clearTimeout(timer); }
  }
  function expiredNow() { return !!batch && batch.status !== 'used' && dateNumber(batch.expiresAt) <= Date.now() + 2500; }
  function revokeFiles() {
    prepareController?.abort();
    prepareController = null;
    objectUrls.forEach(url => URL.revokeObjectURL(url));
    objectUrls = []; files = [];
  }
  function updateButtons() {
    const isExpired = expired || expiredNow();
    if (isExpired && batch?.status !== 'used') expired = true;
    const ready = files.length === 3;
    save.disabled = loading || sharing || (!batch && !expired);
    if (expired) {
      save.disabled = loading || sharing;
      save.querySelector('span').textContent = '重新领取照片';
    } else if (pendingReceipt) {
      save.disabled = loading || sharing;
      save.querySelector('span').textContent = '重试同步保存记录';
    } else if (!batch) {
      save.querySelector('span').textContent = '暂时没有可领取的照片';
    } else if (sharing) {
      save.querySelector('span').textContent = '正在保存…';
    } else if (loading) {
      save.querySelector('span').textContent = '准备照片…';
    } else {
      save.querySelector('span').textContent = batch.status === 'used' ? '再次下载' : '下载照片';
      if (!ready) $('save-hint').textContent = '可长按图片保存，或点按钮逐张下载。';
    }
    next.disabled = loading || sharing || !!pendingReceipt;
    next.textContent = batch?.status === 'used' ? '下一组' : '换一组';
    const state = $('batch-state');
    state.className = 'pill' + (batch?.status === 'used' ? ' used' : '');
    state.textContent = batch?.status === 'used' ? '已领取' : expired ? '请重新领取' : batch ? '本组 3 张' : loading ? '准备中' : '待补充';
  }
  function expireView() {
    if (!batch || batch.status === 'used') return;
    expired = true; revokeFiles();
    if (dialog.open) dialog.close();
    tell('这组照片的领取时间已到，请重新领取后再保存。');
    updateButtons();
  }
  function platformCards(platforms) {
    const container = $('platforms'); container.replaceChildren();
    const order = ['meituan', 'dianping', 'douyin'];
    const list = Array.isArray(platforms) ? platforms : Object.entries(platforms || {}).map(([id, value]) => ({id, ...(typeof value === 'string' ? {url: value} : value)}));
    const names = {meituan: '美团', dianping: '大众点评', douyin: '抖音'};
    order.forEach(id => {
      const platform = list.find(item => item.id === id) || {id, name: names[id], url: ''};
      let url = '';
      try { const parsed = new URL(platform.url); if (parsed.protocol === 'https:') url = parsed.href; } catch {}
      const card = document.createElement(url ? 'a' : 'div');
      card.className = 'platform-card' + (url ? '' : ' disabled');
      if (url) { card.href = url; card.rel = 'noopener noreferrer'; }
      const icon = document.createElement('img'); icon.src = BASE + '/brand/' + id + '.png'; icon.alt = ''; icon.width = 36; icon.height = 36;
      const title = document.createElement('span'); title.textContent = names[id];
      const hint = document.createElement('span'); hint.className = 'platform-arrow'; hint.textContent = url ? '打开平台 ↗' : '链接待设置';
      card.append(icon, title, hint); container.append(card);
    });
  }
  function preview(photo) {
    $('preview-image').src = photo.url;
    $('preview-image').alt = photo.name || '门店照片';
    $('preview-dialog').showModal();
  }
  function renderPhotos() {
    grid.replaceChildren();
    grid.setAttribute('aria-busy', 'false');
    $('ai-sample-note').hidden = !batch?.photos?.some(photo => (photo.name || '').startsWith('AI示例'));
    $('empty-state').hidden = !!batch;
    grid.hidden = !batch;
    if (!batch) return;
    batch.photos.forEach((photo, index) => {
      const tile = document.createElement('figure'); tile.className = 'photo-tile';
      const button = document.createElement('button'); button.type = 'button'; button.setAttribute('aria-label', '查看第 ' + (index + 1) + ' 张照片');
      const image = document.createElement('img'); image.src = photo.url; image.alt = photo.name || ('门店照片 ' + (index + 1)); image.decoding = 'async';
      button.append(image); button.addEventListener('click', () => preview(photo));
      const number = document.createElement('span'); number.className = 'photo-number'; number.textContent = String(index + 1).padStart(2, '0') + ((photo.name || '').startsWith('AI示例') ? ' · AI示意' : '');
      tile.append(button, number); grid.append(tile);
    });
  }
  async function prefetchPhotos() {
    if (!batch) return;
    const current = batch.id, generation = ++loadGeneration;
    revokeFiles(); loading = true; updateButtons();
    const controller = new AbortController(); prepareController = controller;
    const timer = setTimeout(() => controller.abort(), 24000);
    try {
      const prepared = await Promise.all(batch.photos.map(async (photo, index) => {
        const response = await fetch(photo.url, {credentials: 'same-origin', signal: controller.signal, cache: 'no-store'});
        if (!response.ok) throw new Error('照片暂未准备好。');
        const type = (response.headers.get('Content-Type') || '').split(';')[0].trim();
        const ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[type];
        if (!ext) throw new Error('图片格式暂不支持整组保存。');
        const blob = await response.blob();
        if (!blob.size || blob.size > 12 * 1024 * 1024) throw new Error('图片较大，可以长按保存。');
        return new File([blob], '爱多格-' + (index + 1) + '.' + ext, {type});
      }));
      if (generation !== loadGeneration || current !== batch?.id) return;
      if (expiredNow()) { expireView(); return; }
      files = prepared;
      objectUrls = prepared.map(file => URL.createObjectURL(file));
      $('save-hint').textContent = canShare() ? '在系统面板选择“存储图像”或保存图片。' : '若未弹出保存面板，可长按照片逐张保存。';
    } catch (error) {
      if (generation !== loadGeneration || current !== batch?.id) return;
      files = []; objectUrls = [];
      $('save-hint').textContent = '可长按图片保存，或点按钮逐张下载。';
    } finally {
      clearTimeout(timer);
      if (generation === loadGeneration) { loading = false; updateButtons(); }
    }
  }
  function canShare() {
    if (files.length !== 3 || !navigator.share || !navigator.canShare) return false;
    try { return navigator.canShare({files}); } catch { return false; }
  }
  async function renew() {
    if (!batch || batch.status === 'used' || expired || renewInFlight) return renewInFlight;
    const id = batch.id;
    renewInFlight = api('/batch/renew', {batchId: id}).then(data => {
      if (batch?.id === id && data.expiresAt) batch.expiresAt = data.expiresAt;
      updateButtons();
    }).catch(error => {
      if (batch?.id !== id) return;
      if (error.status === 409 || error.status === 410 || error.status === 404 || expiredNow()) expireView();
      else tell('网络暂时不稳定，请保持此页，稍后重试保存。');
    }).finally(() => { renewInFlight = null; });
    return renewInFlight;
  }
  async function commit(method) {
    if (!batch) throw new Error('请重新领取照片。');
    const record = {batchId: batch.id, method};
    pendingReceipt = record;
    try { localStorage.setItem(PENDING_KEY, JSON.stringify(record)); } catch {}
    updateButtons();
    try {
      const result = await api('/batch/commit', record);
      if (batch?.id === record.batchId) Object.assign(batch, {status: 'used', usedAt: result.usedAt, method});
      pendingReceipt = null;
      try { localStorage.removeItem(PENDING_KEY); } catch {}
      updateButtons();
    } catch (error) {
      tell('保存记录暂未同步，请保留此页并重试。照片不会自动换成下一组。', 'error');
      updateButtons();
      throw error;
    }
  }
  function manualPanel() {
    if (!batch || expired || expiredNow()) { expireView(); return; }
    const panel = $('save-dialog-photos'); panel.replaceChildren();
    batch.photos.forEach((photo, index) => {
      const item = document.createElement('div');
      const image = document.createElement('img'); image.src = photo.url; image.alt = photo.name || ('门店照片 ' + (index + 1));
      const link = document.createElement('a'); link.className = 'download-one'; link.href = objectUrls[index] || photo.url; link.download = files[index]?.name || photo.name || ('爱多格-' + (index + 1) + '.jpg'); link.textContent = '下载第 ' + (index + 1) + ' 张';
      item.append(image, link); panel.append(item);
    });
    dialog.showModal();
  }
  async function refresh(replace = false) {
    if (loading || sharing) return;
    loading = true; updateButtons(); tell('');
    try {
      if (pendingReceipt) {
        try {
          await api('/batch/commit', pendingReceipt);
          pendingReceipt = null; try { localStorage.removeItem(PENDING_KEY); } catch {}
        } catch (error) {
          tell('上次保存记录还未同步，请保持网络连接后重试。', 'error');
          throw error;
        }
      }
      const response = await api(replace && batch ? '/batch/release' : '/bootstrap', replace && batch ? {batchId: batch.id} : undefined);
      revokeFiles(); loadGeneration++;
      batch = response.batch && response.batch.photos?.length === 3 ? response.batch : null;
      expired = false;
      $('store-name').textContent = response.storeName || '爱多格宠物医院';
      platformCards(response.platforms);
      renderPhotos();
      loading = false;
      if (batch) await prefetchPhotos();
      else $('save-hint').textContent = '请稍后刷新，或联系门店补充照片。';
    } catch (error) {
      if (!message.textContent) tell(error.name === 'AbortError' ? '连接超时，请重试。' : error.message, 'error');
      if (!batch && !pendingReceipt) expired = true;
    } finally { loading = false; updateButtons(); }
  }
  save.addEventListener('click', () => {
    if (pendingReceipt) { refresh(); return; }
    if (expired || expiredNow()) { expired = true; refresh(); return; }
    if (!batch || loading || sharing) return;
    if (!canShare()) { manualPanel(); return; }
    const current = batch.id;
    sharing = true; updateButtons();
    // Start renewal without awaiting it; native sharing must use this tap's activation.
    renew();
    let operation;
    try { operation = navigator.share({files: [...files]}); }
    catch (error) { operation = Promise.reject(error); }
    Promise.resolve(operation).then(async () => {
      if (batch?.id !== current) return;
      await commit('system_share');
      tell('已交给系统保存，请确认相册。', 'success');
    }).catch(error => {
      if (error.name === 'AbortError') tell('已取消，这组照片仍为你保留。');
      else if (!pendingReceipt) { tell('系统暂时无法整组保存，可以逐张保存。'); manualPanel(); }
    }).finally(() => { sharing = false; updateButtons(); });
  });
  next.addEventListener('click', () => refresh(true));
  $('manual-confirm').addEventListener('click', async () => {
    if (!batch || sharing || expired || expiredNow()) { expireView(); return; }
    const button = $('manual-confirm'); button.disabled = true;
    try {
      await commit('manual_confirm'); dialog.close();
      tell('已记录这组照片的使用时间，可以前往平台上传。', 'success');
    } catch {} finally { button.disabled = false; }
  });
  $('close-save-dialog').addEventListener('click', () => dialog.close());
  $('close-preview').addEventListener('click', () => $('preview-dialog').close());
  [dialog, $('preview-dialog')].forEach(element => element.addEventListener('click', event => { if (event.target === element) { const r = element.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) element.close(); } }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { if (expiredNow()) expireView(); else renew(); } });
  window.addEventListener('online', () => { if (pendingReceipt && !sharing) refresh(); else renew(); });
  heartbeat = setInterval(() => { if (expiredNow() && !sharing) expireView(); else if (!document.hidden || sharing) renew(); }, 45000);
  window.addEventListener('pagehide', () => { if (heartbeat) clearInterval(heartbeat); }, {once: true});
  window.addEventListener('pageshow', event => { if (event.persisted) { heartbeat = setInterval(() => { if (expiredNow() && !sharing) expireView(); else if (!document.hidden || sharing) renew(); }, 45000); if (expiredNow()) expireView(); else renew(); } });
  refresh();
})();
