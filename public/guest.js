(() => {
  'use strict';
  const BASE = '/aidog-photos';
  const $ = id => document.getElementById(id);
  const grid = $('photo-grid'), save = $('save-button'), next = $('next-button');
  const message = $('guest-message'), dialog = $('save-dialog');
  const PENDING_KEY = 'aidog-photos-pending-v1';
  let batch = null, files = [], objectUrls = [], filesKey = '';
  let loading = false, preparing = false, sharing = false, committing = false, cancelling = false;
  let expired = false, loadError = false, saveArmed = false, pendingCancel = null;
  let photoController = null, prefetchPromise = null, loadGeneration = 0, renewInFlight = null;
  let heartbeat = null, emptyPoll = null, suppressDialogCancel = false;
  let pendingReceipt = null;
  try {
    const value = JSON.parse(localStorage.getItem(PENDING_KEY) || 'null');
    if (value && typeof value.batchId === 'string' && ['system_share', 'manual_confirm', 'browser_download'].includes(value.method)) pendingReceipt = value;
  } catch {}

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
      const {headers: extraHeaders, ...requestOptions} = options;
      const headers = new Headers(extraHeaders);
      if (body !== undefined) headers.set('Content-Type', 'application/json');
      headers.set('X-Aidog-Photo-Flow', '2');
      const response = await fetch(BASE + '/api' + path, {
        credentials: 'same-origin', cache: 'no-store',
        ...(body === undefined ? {} : {method: 'POST', body: JSON.stringify(body)}),
        ...requestOptions, headers, signal: controller.signal,
      });
      let data;
      try { data = await response.json(); } catch { throw new Error('连接暂时中断，请重试。'); }
      if (data.reloadRequired === true) window.location.reload();
      if (!response.ok) { const error = new Error(data.error || '操作暂未完成，请重试。'); error.status = response.status; throw error; }
      return data;
    } finally { clearTimeout(timer); }
  }
  function busy() { return loading || preparing || sharing || committing || cancelling; }
  function expiredNow() { return !!batch && batch.status !== 'used' && dateNumber(batch.expiresAt) <= Date.now() + 2500; }
  function photoKey(value = batch) { return value ? value.id + ':' + value.photos.map(photo => photo.id).join(',') : ''; }
  function canDeliver() { return !!batch && ['reserved', 'used'].includes(batch.status) && !expired && !expiredNow(); }
  function revokeFiles() {
    loadGeneration++;
    photoController?.abort(); photoController = null; prefetchPromise = null;
    objectUrls.forEach(url => URL.revokeObjectURL(url));
    objectUrls = []; files = []; filesKey = '';
  }
  function updateHint() {
    if (!batch) $('save-hint').textContent = '请稍后重试，或联系门店补充照片。';
    else if (batch.status === 'preview') $('save-hint').textContent = '点击下载照片后，选择保存到手机。';
    else if (saveArmed && canShare()) $('save-hint').textContent = '照片已准备好，再点一次保存。';
    else if (canShare()) $('save-hint').textContent = '在系统面板选择“存储图像”或保存图片。';
    else $('save-hint').textContent = '可在保存面板长按照片，或逐张下载。';
  }
  function updateButtons() {
    if (expiredNow() && batch?.status !== 'used') expired = true;
    save.disabled = busy() || (!batch && !expired && !loadError && !pendingReceipt && !pendingCancel);
    const label = save.querySelector('span');
    if (cancelling) label.textContent = '正在取消…';
    else if (committing) label.textContent = '同步保存记录…';
    else if (sharing) label.textContent = '正在保存…';
    else if (preparing || loading) label.textContent = '准备照片…';
    else if (pendingReceipt) label.textContent = '重试同步保存记录';
    else if (pendingCancel) label.textContent = '重试取消保存';
    else if (expired || loadError) label.textContent = '重新领取照片';
    else if (!batch) label.textContent = '暂时没有可领取的照片';
    else if (batch.status === 'used') label.textContent = '再次下载';
    else label.textContent = saveArmed && batch.status === 'reserved' ? '保存这3张' : '下载照片';
    next.disabled = busy() || !!pendingReceipt || !!pendingCancel;
    next.textContent = batch?.status === 'used' ? '下一组' : '换一组';
    const state = $('batch-state');
    state.className = 'pill' + (batch?.status === 'used' ? ' used' : '');
    state.textContent = batch?.status === 'used' ? '已领取' : expired ? '请重新领取' : batch ? '本组 3 张' : loading ? '准备中' : '待补充';
    updateHint();
  }
  function closeManualQuietly() {
    if (dialog.open) { suppressDialogCancel = true; dialog.close(); }
  }
  function expireView() {
    if (!batch || batch.status === 'used') return;
    expired = true; saveArmed = false; revokeFiles(); closeManualQuietly();
    tell('这组照片的领取时间已到，请重新领取后再保存。'); updateButtons();
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
      const card = document.createElement(url ? 'a' : 'div'); card.className = 'platform-card' + (url ? '' : ' disabled');
      if (url) { card.href = url; card.rel = 'noopener noreferrer'; }
      const icon = document.createElement('img'); icon.src = BASE + '/brand/' + id + '.png'; icon.alt = ''; icon.width = 36; icon.height = 36;
      const title = document.createElement('span'); title.textContent = names[id];
      const hint = document.createElement('span'); hint.className = 'platform-arrow'; hint.textContent = url ? '打开平台 ↗' : '链接待设置';
      card.append(icon, title, hint); container.append(card);
    });
  }
  function preview(photo) {
    $('preview-image').src = photo.url; $('preview-image').alt = photo.name || '照片预览';
    $('preview-dialog').querySelector('p').textContent = canDeliver() ? '可长按保存图片' : '照片预览，下载请返回页面点“下载照片”。';
    $('preview-dialog').showModal();
  }
  function renderPhotos() {
    grid.replaceChildren(); grid.setAttribute('aria-busy', 'false');
    $('ai-sample-note').hidden = !batch?.photos?.some(photo => (photo.name || '').startsWith('AI示例'));
    $('empty-state').hidden = !!batch; grid.hidden = !batch;
    if (!batch) return;
    batch.photos.forEach((photo, index) => {
      const tile = document.createElement('figure'); tile.className = 'photo-tile';
      const button = document.createElement('button'); button.type = 'button'; button.setAttribute('aria-label', '查看第 ' + (index + 1) + ' 张照片');
      const image = document.createElement('img'); image.src = photo.url; image.alt = photo.name || ('照片 ' + (index + 1)); image.decoding = 'async';
      button.append(image); button.addEventListener('click', () => preview(photo));
      const number = document.createElement('span'); number.className = 'photo-number'; number.textContent = String(index + 1).padStart(2, '0') + ((photo.name || '').startsWith('AI示例') ? ' · AI示意' : '');
      tile.append(button, number); grid.append(tile);
    });
  }
  function applyBatch(value) {
    const valid = value && value.photos?.length === 3 ? value : null;
    const changed = photoKey(valid) !== photoKey();
    if (changed) revokeFiles();
    batch = valid; expired = false; loadError = false;
    if (batch?.status !== 'reserved') saveArmed = false;
    renderPhotos(); updateButtons();
    return changed;
  }
  function applyBootstrap(value) {
    $('store-name').textContent = value.storeName || '爱多格宠物医院';
    platformCards(value.platforms); return applyBatch(value.batch);
  }
  function canShare() {
    if (files.length !== 3 || filesKey !== photoKey() || !navigator.share || !navigator.canShare) return false;
    try { return navigator.canShare({files}); } catch { return false; }
  }
  function prefetchPhotos() {
    if (!batch) return Promise.resolve(false);
    const key = photoKey();
    if (files.length === 3 && filesKey === key) return Promise.resolve(true);
    if (prefetchPromise && filesKey === key) return prefetchPromise;
    revokeFiles(); filesKey = key;
    const generation = loadGeneration, photos = [...batch.photos];
    const controller = new AbortController(); photoController = controller;
    const timer = setTimeout(() => controller.abort(), 24000);
    const task = (async () => {
      try {
        const prepared = await Promise.all(photos.map(async (photo, index) => {
          const response = await fetch(photo.url, {credentials: 'same-origin', signal: controller.signal, cache: 'no-store'});
          if (!response.ok) throw new Error('照片暂未准备好。');
          const type = (response.headers.get('Content-Type') || '').split(';')[0].trim();
          const ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}[type];
          if (!ext) throw new Error('图片格式暂不支持整组保存。');
          const blob = await response.blob();
          if (!blob.size || blob.size > 12 * 1024 * 1024) throw new Error('图片较大，可以长按保存。');
          return new File([blob], '爱多格-' + (index + 1) + '.' + ext, {type});
        }));
        if (generation !== loadGeneration || key !== photoKey()) return false;
        if (expiredNow()) { expireView(); return false; }
        files = prepared; objectUrls = prepared.map(file => URL.createObjectURL(file));
        return true;
      } catch {
        // Preview files can become unavailable after another visitor claims them.
        // Preparing a download will obtain a valid locked snapshot before delivery.
        if (generation === loadGeneration && key === photoKey()) { files = []; objectUrls = []; }
        return false;
      } finally {
        clearTimeout(timer);
        if (generation === loadGeneration) { prefetchPromise = null; photoController = null; updateButtons(); }
      }
    })();
    prefetchPromise = task; return task;
  }
  async function renew() {
    if (!batch || batch.status === 'used' || expired || pendingCancel || cancelling || renewInFlight) return renewInFlight;
    if (document.hidden && !(batch.status === 'reserved' && sharing)) return;
    const id = batch.id, stage = batch.status;
    renewInFlight = api('/batch/renew', {batchId: id}).then(data => {
      if (batch?.id === id && batch.status === stage && data.expiresAt) batch.expiresAt = data.expiresAt;
      updateButtons();
    }).catch(error => {
      if (batch?.id !== id) return;
      if ([404, 409, 410].includes(error.status) || expiredNow()) {
        if (batch.status === 'preview' && !document.hidden && !busy()) refresh(false, true);
        else expireView();
      } else tell('网络暂时不稳定，请保持此页，稍后重试保存。');
    }).finally(() => { renewInFlight = null; });
    return renewInFlight;
  }
  async function commit(method) {
    if (!canDeliver()) throw new Error('请先下载照片，再确认保存。');
    const record = {batchId: batch.id, method}; pendingReceipt = record; committing = true;
    try { localStorage.setItem(PENDING_KEY, JSON.stringify(record)); } catch {}
    updateButtons();
    try {
      const result = await api('/batch/commit', record);
      if (batch?.id === record.batchId) Object.assign(batch, {status: 'used', usedAt: result.usedAt, method});
      pendingReceipt = null; saveArmed = false;
      try { localStorage.removeItem(PENDING_KEY); } catch {}
    } catch (error) {
      tell('保存记录暂未同步，请保留此页并重试。照片不会自动换成下一组。', 'error'); throw error;
    } finally { committing = false; updateButtons(); }
  }
  async function cancelPrepared() {
    if (cancelling || pendingReceipt || committing || (!pendingCancel && batch?.status !== 'reserved')) return;
    const id = pendingCancel || batch.id; pendingCancel = id; cancelling = true; saveArmed = false; updateButtons();
    try {
      const response = await api('/batch/cancel', {batchId: id});
      pendingCancel = null;
      if (batch?.id === id) { applyBootstrap(response); prefetchPhotos(); }
      tell('已取消，可以重新下载这组照片。');
    } catch (error) {
      if ([404, 409, 410].includes(error.status)) {
        pendingCancel = null; expired = true;
        tell('这组照片已过期，请重新领取。');
      } else tell('已取消，取消记录待同步。请连接网络后重试。', 'error');
    } finally { cancelling = false; updateButtons(); }
  }
  function manualPanel() {
    if (!canDeliver()) { expireView(); return; }
    const panel = $('save-dialog-photos'); panel.replaceChildren();
    batch.photos.forEach((photo, index) => {
      const item = document.createElement('div');
      const image = document.createElement('img'); image.src = photo.url; image.alt = photo.name || ('照片 ' + (index + 1));
      const link = document.createElement('a'); link.className = 'download-one'; link.href = objectUrls[index] || photo.url; link.download = files[index]?.name || photo.name || ('爱多格-' + (index + 1) + '.jpg'); link.textContent = '下载第 ' + (index + 1) + ' 张';
      item.append(image, link); panel.append(item);
    });
    if (!dialog.open) dialog.showModal();
  }
  function startSystemShare() {
    if (!canDeliver() || !canShare() || sharing) return;
    const id = batch.id; sharing = true; saveArmed = false; updateButtons();
    // A reserved snapshot already owns all three photos. Do not await network here.
    renew();
    let operation;
    try { operation = navigator.share({files: [...files]}); }
    catch (error) { operation = Promise.reject(error); }
    Promise.resolve(operation).then(async () => {
      if (batch?.id !== id) return;
      await commit('system_share'); tell('已交给系统保存，请确认相册。', 'success');
    }).catch(async error => {
      if (error.name === 'AbortError') await cancelPrepared();
      else if (!pendingReceipt) { tell('系统暂时无法整组保存，可以逐张保存。'); manualPanel(); }
    }).finally(() => { sharing = false; updateButtons(); });
  }
  async function prepareDownload() {
    if (!batch || busy()) return;
    const id = batch.id, before = photoKey(); preparing = true; updateButtons(); tell('');
    try {
      if (batch.status === 'preview') {
        const response = await api('/batch/prepare', {batchId: id});
        if (batch?.id !== id) return;
        if (!response.batch || !['reserved', 'used'].includes(response.batch.status)) throw new Error('照片暂未准备好，请重试。');
        applyBatch(response.batch);
        if (photoKey() !== before) tell('原来的照片已被领取，已更新为新的 3 张。');
      }
      if (!canDeliver()) { expireView(); return; }
      if (navigator.share && navigator.canShare) await prefetchPhotos();
      else prefetchPhotos();
      if (!canDeliver()) { expireView(); return; }
      if (canShare()) {
        if (navigator.userActivation?.isActive === true) {
          preparing = false; startSystemShare();
        } else {
          saveArmed = true; tell('照片已准备好，再点一次保存。');
        }
      } else manualPanel();
    } catch (error) {
      if ([404, 409, 410].includes(error.status)) {
        preparing = false;
        await refresh(false, true);
        tell('这组照片已被领取或已过期，页面已更新，请重新下载。');
      } else tell(error.name === 'AbortError' ? '连接超时，请重试下载。' : error.message, 'error');
    } finally { preparing = false; updateButtons(); }
  }
  async function refresh(replace = false, silent = false) {
    if (busy() || dialog.open) return;
    loading = true; updateButtons(); if (!silent) tell('');
    try {
      if (pendingReceipt) {
        try {
          await api('/batch/commit', pendingReceipt);
          pendingReceipt = null; try { localStorage.removeItem(PENDING_KEY); } catch {}
        } catch (error) { tell('上次保存记录还未同步，请保持网络连接后重试。', 'error'); throw error; }
      }
      let response;
      try { response = await api(replace && batch ? '/batch/release' : '/bootstrap', replace && batch ? {batchId: batch.id} : undefined); }
      catch (error) {
        if (replace && [404, 409, 410].includes(error.status)) response = await api('/bootstrap');
        else throw error;
      }
      pendingCancel = null; applyBootstrap(response); saveArmed = false;
      if (batch) prefetchPhotos();
    } catch (error) {
      if (!silent || !message.textContent) tell(error.name === 'AbortError' ? '连接超时，请重试。' : error.message, 'error');
      if (!batch && !pendingReceipt) loadError = true;
    } finally { loading = false; updateButtons(); }
  }
  save.addEventListener('click', () => {
    if (busy()) return;
    if (pendingReceipt) { refresh(); return; }
    if (pendingCancel) { cancelPrepared(); return; }
    if (expired || expiredNow() || loadError) { refresh(); return; }
    if (!batch) return;
    if (canDeliver() && canShare()) { startSystemShare(); return; }
    if (canDeliver() && !(navigator.share && navigator.canShare)) { manualPanel(); return; }
    prepareDownload();
  });
  next.addEventListener('click', () => refresh(true));
  $('manual-confirm').addEventListener('click', async () => {
    if (!canDeliver() || sharing || committing) { if (expiredNow()) expireView(); return; }
    const button = $('manual-confirm'); button.disabled = true;
    try {
      await commit('manual_confirm'); dialog.close();
      tell('已记录这组照片的使用时间，可以前往平台上传。', 'success');
    } catch {} finally { button.disabled = false; }
  });
  dialog.addEventListener('close', () => {
    if (suppressDialogCancel) { suppressDialogCancel = false; return; }
    if (batch?.status === 'reserved' && !pendingReceipt && !committing) cancelPrepared();
  });
  $('close-save-dialog').addEventListener('click', () => dialog.close());
  $('close-preview').addEventListener('click', () => $('preview-dialog').close());
  [dialog, $('preview-dialog')].forEach(element => element.addEventListener('click', event => { if (event.target === element) { const r = element.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) element.close(); } }));
  function foreground() {
    if (document.hidden || busy()) return;
    if (pendingReceipt) { refresh(false, true); return; }
    if (pendingCancel) { cancelPrepared(); return; }
    if (!batch || (batch.status === 'preview' && (expired || expiredNow()))) refresh(false, true);
    else if (expiredNow()) expireView();
    else renew();
  }
  function startTimers() {
    clearInterval(heartbeat); clearInterval(emptyPoll);
    heartbeat = setInterval(() => {
      if (document.hidden && !(batch?.status === 'reserved' && sharing)) return;
      if (pendingCancel && !busy()) { cancelPrepared(); return; }
      if (expiredNow() && !sharing) {
        if (batch.status === 'preview' && !busy()) refresh(false, true);
        else expireView();
      } else renew();
    }, 45000);
    emptyPoll = setInterval(() => { if (!document.hidden && !batch && !busy() && !pendingReceipt && !pendingCancel) refresh(false, true); }, 8000);
  }
  document.addEventListener('visibilitychange', foreground);
  window.addEventListener('online', foreground);
  window.addEventListener('pagehide', () => { clearInterval(heartbeat); clearInterval(emptyPoll); });
  window.addEventListener('pageshow', event => { if (event.persisted) { startTimers(); foreground(); } });
  startTimers(); refresh();
})();
