(() => {
  'use strict';
  const BASE = '/aidog-photos';
  const $ = id => document.getElementById(id);
  const grid = $('admin-photo-grid'), main = $('admin-main'), loginSection = $('login-section');
  const message = $('admin-message');
  const loginMessage = document.createElement('div'); loginMessage.className = 'message error'; loginMessage.hidden = true; loginMessage.setAttribute('role', 'status');
  $('login-form').append(loginMessage);
  const names = {meituan: '美团', dianping: '大众点评', douyin: '抖音'};
  const states = {available: '可领取', reserved: '领取中', used: '已使用'};
  const methods = {system_share: '系统交付', manual_confirm: '本人确认保存', browser_download: '浏览器下载'};
  let page = 1, filter = 'all', loading = false, uploading = false;
  let settings = {storeName: '', platforms: []}, loadId = 0;
  let selectedFiles = [], draftUrls = [], uploadId = null, editPackageId = null;
  function tell(text, type = '') {
    const target = loginSection.hidden ? message : loginMessage;
    target.textContent = text; target.className = 'message' + (type ? ' ' + type : ''); target.hidden = !text;
  }
  async function api(path, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), options.timeout || 45000);
    try {
      const request = {...options}; delete request.timeout;
      const response = await fetch(BASE + '/api' + path, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal, ...request});
      let data;
      try { data = await response.json(); } catch { throw new Error('连接暂时中断，请重试。'); }
      if (!response.ok) { const error = new Error(data.error || '操作暂未完成，请重试。'); error.status = response.status; throw error; }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，请重试。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  const json = (method, data) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  function showLogin() {
    main.hidden = true; loginSection.hidden = false; $('logout').hidden = true; $('password').focus();
  }
  function showMain() { main.hidden = false; loginSection.hidden = true; $('logout').hidden = false; loginMessage.hidden = true; }
  function timeText(value) {
    if (!value) return '';
    const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
    if (!Number.isFinite(date.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(date);
  }
  function textElement(tag, text, className) {
    const element = document.createElement(tag); if (className) element.className = className; element.textContent = text; return element;
  }
  function setStats(stats) { ['all', 'available', 'reserved', 'used'].forEach(key => { $('stat-' + key).textContent = String(stats?.[key] ?? 0); }); }
  function renderPackage(pack) {
    const card = document.createElement('article'); card.className = 'package-card';
    const photos = document.createElement('div'); photos.className = 'package-card-photos';
    (pack.photos || []).forEach((photo, index) => {
      const image = document.createElement('img'); image.src = photo.previewUrl || photo.url;
      image.alt = photo.name || ('套装照片 ' + (index + 1)); image.loading = 'lazy'; image.decoding = 'async'; photos.append(image);
    });
    const body = document.createElement('div'); body.className = 'package-card-body';
    const heading = document.createElement('div'); heading.className = 'package-card-heading';
    const title = textElement('h3', pack.title || '图文套装'); title.title = pack.title || '';
    heading.append(title, textElement('span', states[pack.status] || pack.status, 'status-badge ' + (['used', 'reserved'].includes(pack.status) ? pack.status : '')));
    const copy = textElement('p', pack.copy || '', 'package-card-copy'); body.append(heading, copy);
    if ((pack.copy || '').length > 100 || (pack.copy || '').split('\n').length > 4) {
      const expand = textElement('button', '展开文案', 'text-button'); expand.type = 'button'; expand.setAttribute('aria-expanded', 'false');
      expand.addEventListener('click', () => {
        const expanded = expand.getAttribute('aria-expanded') !== 'true';
        copy.classList.toggle('expanded', expanded); expand.setAttribute('aria-expanded', String(expanded)); expand.textContent = expanded ? '收起文案' : '展开文案';
      }); body.append(expand);
    }
    const footer = document.createElement('div'); footer.className = 'package-card-footer';
    const records = document.createElement('div');
    records.append(textElement('p', pack.usedAt ? '使用：' + timeText(pack.usedAt) : '上传：' + timeText(pack.createdAt), 'photo-card-time'));
    if (pack.status === 'used') records.append(textElement('p', methods[pack.method] || '已领取', 'photo-method'));
    const actions = document.createElement('div'); actions.className = 'package-card-buttons';
    if (pack.status === 'available') {
      const edit = textElement('button', '编辑文案', 'text-button'); edit.type = 'button';
      edit.addEventListener('click', () => openPackageEditor(pack));
      const remove = textElement('button', '删除整套', 'photo-delete'); remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (!window.confirm('删除这套未使用的图文？3 张照片和文案将一起删除。')) return;
        remove.disabled = true;
        try {
          await api('/admin/packages/' + encodeURIComponent(pack.id), {method: 'DELETE'});
          tell('整套图文已删除。', 'success'); await loadPackages();
        } catch (error) { tell(error.message, 'error'); remove.disabled = false; }
      }); actions.append(edit, remove);
    } else actions.append(textElement('span', pack.status === 'used' ? '已使用，只读' : '领取中，只读', 'package-edit-copy-readonly'));
    footer.append(records, actions); body.append(footer); card.append(photos, body); return card;
  }
  async function loadPackages() {
    const id = ++loadId; loading = true; grid.setAttribute('aria-busy', 'true'); $('refresh-photos').disabled = true;
    try {
      const response = await api('/admin/packages?status=' + encodeURIComponent(filter) + '&page=' + page);
      if (id !== loadId) return;
      const packages = response.packages || [];
      settings = {storeName: response.storeName || '', platforms: response.platforms || []};
      setStats(response.stats); grid.replaceChildren(...packages.map(renderPackage));
      $('admin-empty').hidden = packages.length > 0;
      $('admin-empty').querySelector('h3').textContent = filter === 'all' ? '还没有套装' : '暂无' + (states[filter] || '') + '套装';
      $('admin-empty').querySelector('p').textContent = filter === 'all' ? '选择 3 张照片，填写文案后保存第一套。' : '可以切换其他状态查看。';
      const size = response.pageSize || 60;
      $('page-info').textContent = '共 ' + response.total + ' 套 · 第 ' + page + ' 页';
      $('previous-page').disabled = page <= 1; $('next-page').disabled = page * size >= response.total;
      $('library-heading').textContent = filter === 'all' ? '全部套装' : (states[filter] + '套装');
    } catch (error) { if (error.status === 401) showLogin(); tell(error.message, 'error'); }
    finally { if (id === loadId) { loading = false; grid.setAttribute('aria-busy', 'false'); $('refresh-photos').disabled = false; } }
  }
  function createUploadId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  }
  function countChars(value) { return Array.from(value).length; }
  function updateDraftValidity() {
    const copy = $('package-copy').value.trim(), title = $('package-title').value.trim();
    const length = countChars(copy), valid = selectedFiles.length === 3 && length >= 1 && length <= 1000 && countChars(title) <= 80;
    $('copy-count').textContent = countChars($('package-copy').value) + ' / 1000';
    $('package-submit').disabled = uploading || !valid;
    $('draft-validity').textContent = valid ? '已配好 3 张照片和文案，可以保存整套。' : '选满 3 张照片并填写文案后保存。';
  }
  function renderDraftPhotos() {
    const container = $('draft-photos'); container.replaceChildren();
    for (let index = 0; index < 3; index++) {
      if (selectedFiles[index]) {
        const tile = document.createElement('div'); tile.className = 'draft-photo';
        const image = document.createElement('img'); image.src = draftUrls[index]; image.alt = selectedFiles[index].name;
        tile.append(image); container.append(tile);
      } else container.append(textElement('div', String(index + 1), 'draft-placeholder'));
    }
  }
  function resetDraft() {
    draftUrls.forEach(url => URL.revokeObjectURL(url)); draftUrls = []; selectedFiles = []; uploadId = null;
    $('photo-files').value = ''; $('copy-file').value = ''; $('package-title').value = ''; $('package-copy').value = '';
    $('draft-photo-hint').textContent = '请选择可分享的 JPG / PNG / WebP，每张不超过 12MB。';
    renderDraftPhotos(); updateDraftValidity();
  }
  function lockDraft(locked) {
    ['photo-files', 'copy-file', 'package-title', 'package-copy'].forEach(id => { $(id).disabled = locked; });
    $('package-submit').textContent = locked ? '正在保存整套…' : '保存整套';
  }
  function openPackageEditor(pack) {
    editPackageId = pack.id;
    $('edit-package-title').value = pack.title || ''; $('edit-package-copy').value = pack.copy || '';
    $('edit-package-message').hidden = true; $('package-edit-save').disabled = false; $('package-edit-dialog').showModal();
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('login-button'); button.disabled = true; button.textContent = '正在登录…'; tell('');
    try {
      await api('/admin/login', json('POST', {password: $('password').value}));
      $('password').value = ''; showMain(); await loadPackages();
    } catch (error) { tell(error.message, 'error'); }
    finally { button.disabled = false; button.textContent = '登录'; }
  });
  $('logout').addEventListener('click', async () => {
    $('logout').disabled = true;
    try { await api('/admin/logout', json('POST', {})); grid.replaceChildren(); showLogin(); tell('已退出管理后台。'); }
    catch (error) { tell(error.message, 'error'); }
    finally { $('logout').disabled = false; }
  });
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    filter = button.dataset.filter; page = 1;
    document.querySelectorAll('[data-filter]').forEach(tab => { const active = tab === button; tab.classList.toggle('active', active); tab.setAttribute('aria-pressed', String(active)); });
    loadPackages();
  }));
  $('refresh-photos').addEventListener('click', loadPackages);
  $('previous-page').addEventListener('click', () => { if (loading || page <= 1) return; page--; loadPackages(); });
  $('next-page').addEventListener('click', () => { if (loading) return; page++; loadPackages(); });
  $('photo-files').addEventListener('change', () => {
    if (uploading) return;
    const picked = Array.from($('photo-files').files || []);
    if (!picked.length) return;
    const extensions = {jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp'};
    const invalid = picked.find(file => !['image/jpeg', 'image/png', 'image/webp'].includes(file.type || extensions[file.name.split('.').pop().toLowerCase()]) || !file.size || file.size > 12 * 1024 * 1024);
    draftUrls.forEach(url => URL.revokeObjectURL(url)); draftUrls = []; selectedFiles = []; uploadId = null;
    if (picked.length > 3) tell('每套必须正好 3 张照片，请重新选择。', 'error');
    else if (invalid) tell(invalid.name + '：请使用 12MB 以内的 JPG、PNG 或 WebP。', 'error');
    else { selectedFiles = picked; draftUrls = picked.map(file => URL.createObjectURL(file)); tell(''); }
    $('draft-photo-hint').textContent = '已选 ' + selectedFiles.length + ' / 3 张，保存时按预览顺序组成一套。';
    renderDraftPhotos(); updateDraftValidity();
  });
  ['package-title', 'package-copy'].forEach(id => $(id).addEventListener('input', () => { uploadId = null; updateDraftValidity(); }));
  $('copy-file').addEventListener('change', async () => {
    const file = $('copy-file').files?.[0]; if (!file || uploading) return;
    try {
      if (file.size > 16 * 1024) throw new Error('文案文件过大，请使用 1000 字以内的 TXT。');
      const text = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer()).trim();
      if (!text || countChars(text) > 1000) throw new Error('文案需为 1 至 1000 字。');
      $('package-copy').value = text; uploadId = null; updateDraftValidity(); tell('TXT 文案已导入，请核对后保存整套。');
    } catch (error) { tell(error instanceof TypeError ? '请使用 UTF-8 编码的 TXT 文件。' : error.message, 'error'); }
    finally { $('copy-file').value = ''; }
  });
  $('package-form').addEventListener('submit', async event => {
    event.preventDefault(); updateDraftValidity();
    if (uploading || $('package-submit').disabled) return;
    const title = $('package-title').value.trim(), copy = $('package-copy').value.trim();
    if (selectedFiles.length !== 3 || !copy || countChars(copy) > 1000 || countChars(title) > 80) return;
    uploadId ||= createUploadId();
    const payload = new FormData();
    selectedFiles.forEach(file => {
      const mime = file.type || {jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp'}[file.name.split('.').pop().toLowerCase()];
      const upload = file.type ? file : new File([file], file.name, {type: mime, lastModified: file.lastModified});
      payload.append('files', upload, file.name);
    });
    payload.append('title', title); payload.append('copy', copy); payload.append('uploadId', uploadId);
    uploading = true; lockDraft(true); updateDraftValidity(); tell('');
    $('upload-progress').hidden = false; $('upload-bar').removeAttribute('value');
    $('upload-status').textContent = '正在将 3 张照片和文案一起保存，请稍候…';
    try {
      const response = await api('/admin/packages', {method: 'POST', timeout: 120000, body: payload});
      if (!response.id) throw new Error('保存结果暂未确认，请保留当前内容重试。');
      resetDraft(); $('upload-bar').value = 1;
      $('upload-status').textContent = response.duplicate ? '这套图文已经存在，没有新增重复库存。' : '整套已保存：3 张照片和一段文案。';
      tell(response.duplicate ? '这 3 张照片已组成套装，未重复添加。' : '图文套装已保存，顾客可整套领取。', 'success');
      page = 1; await loadPackages();
    } catch (error) {
      if (error.status === 401) showLogin();
      $('upload-status').textContent = '尚未确认保存完成，照片和文案已保留，可直接重试。';
      tell(error.message + ' 当前 3 张照片和文案已保留，重试不会重复入库。', 'error');
    } finally { uploading = false; lockDraft(false); updateDraftValidity(); }
  });
  $('package-edit-close').addEventListener('click', () => $('package-edit-dialog').close());
  ['edit-package-title', 'edit-package-copy'].forEach(id => $(id).addEventListener('input', () => {
    const copy = $('edit-package-copy').value.trim();
    $('package-edit-save').disabled = !copy || countChars(copy) > 1000 || countChars($('edit-package-title').value.trim()) > 80;
  }));
  $('package-edit-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!editPackageId) return;
    const copy = $('edit-package-copy').value.trim(), title = $('edit-package-title').value.trim();
    if (!copy || countChars(copy) > 1000 || countChars(title) > 80) return;
    const button = $('package-edit-save'), localMessage = $('edit-package-message'); button.disabled = true; button.textContent = '正在保存…'; localMessage.hidden = true;
    try {
      await api('/admin/packages/' + encodeURIComponent(editPackageId), json('PUT', {title, copy}));
      $('package-edit-dialog').close(); tell('配套文案已更新，照片保持不变。', 'success'); await loadPackages();
    } catch (error) { localMessage.textContent = error.message; localMessage.className = 'message error'; localMessage.hidden = false; }
    finally { button.disabled = false; button.textContent = '保存修改'; }
  });
  function platformList() {
    if (Array.isArray(settings.platforms)) return settings.platforms;
    return Object.entries(settings.platforms).map(([id, value]) => ({id, ...(typeof value === 'string' ? {url: value} : value)}));
  }
  $('settings-open').addEventListener('click', () => {
    $('settings-store-name').value = settings.storeName;
    const fields = $('settings-platform-fields'); fields.replaceChildren();
    const platforms = platformList();
    Object.entries(names).forEach(([id, name]) => {
      const label = document.createElement('label'); label.htmlFor = 'platform-' + id; label.className = 'field-heading';
      const icon = document.createElement('img'); icon.src = BASE + '/brand/' + id + '-small.png'; icon.alt = '';
      label.append(icon, document.createTextNode(name));
      const input = document.createElement('input'); input.id = 'platform-' + id; input.name = id; input.type = 'url'; input.inputMode = 'url'; input.placeholder = 'https://…'; input.value = platforms.find(item => item.id === id)?.url || '';
      fields.append(label, input);
    });
    $('settings-message').hidden = true; $('settings-dialog').showModal();
  });
  $('settings-close').addEventListener('click', () => $('settings-dialog').close());
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('settings-save'), localMessage = $('settings-message'); button.disabled = true; button.textContent = '正在保存…'; localMessage.hidden = true;
    try {
      const platforms = Object.fromEntries(Object.keys(names).map(id => [id, $('platform-' + id).value.trim()]));
      const storeName = $('settings-store-name').value.trim();
      await api('/admin/settings', json('PUT', {storeName, platforms}));
      settings = {storeName, platforms}; $('settings-dialog').close(); tell('平台链接已保存，顾客页面即刻生效。', 'success');
    } catch (error) { localMessage.textContent = error.message; localMessage.className = 'message error'; localMessage.hidden = false; }
    finally { button.disabled = false; button.textContent = '保存设置'; }
  });
  api('/admin/session').then(() => { showMain(); return loadPackages(); }).catch(error => { showLogin(); if (error.status !== 401) tell(error.message, 'error'); });
})();
