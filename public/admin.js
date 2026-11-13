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
  function renderPhoto(photo) {
    const card = document.createElement('article'); card.className = 'admin-photo-card';
    const image = document.createElement('img'); image.src = photo.url; image.alt = photo.name || '门店照片'; image.loading = 'lazy'; image.decoding = 'async';
    const body = document.createElement('div'); body.className = 'photo-card-body';
    const title = textElement('p', photo.name || '未命名照片', 'photo-card-title'); title.title = photo.name || '';
    const meta = document.createElement('div'); meta.className = 'photo-card-meta';
    meta.append(textElement('span', states[photo.status] || photo.status, 'status-badge ' + (['used', 'reserved'].includes(photo.status) ? photo.status : '')));
    if (photo.status === 'available') {
      const remove = textElement('button', '删除', 'photo-delete'); remove.type = 'button';
      remove.setAttribute('aria-label', '删除照片：' + (photo.name || '未命名照片'));
      remove.addEventListener('click', async () => {
        if (!window.confirm('删除这张未使用的照片？')) return;
        remove.disabled = true;
        try {
          await api('/admin/photos/' + encodeURIComponent(photo.id), {method: 'DELETE'});
          tell('照片已删除。', 'success'); await loadPhotos();
        } catch (error) { tell(error.message, 'error'); remove.disabled = false; }
      });
      meta.append(remove);
    }
    const time = textElement('p', (photo.usedAt ? '使用：' + timeText(photo.usedAt) : '上传：' + timeText(photo.createdAt)), 'photo-card-time');
    body.append(title, meta, time);
    if (photo.status === 'used') body.append(textElement('p', methods[photo.method] || '已领取', 'photo-method'));
    card.append(image, body); return card;
  }
  async function loadPhotos() {
    const id = ++loadId; loading = true; grid.setAttribute('aria-busy', 'true'); $('refresh-photos').disabled = true;
    try {
      const response = await api('/admin/photos?status=' + encodeURIComponent(filter) + '&page=' + page);
      if (id !== loadId) return;
      settings = {storeName: response.storeName || '', platforms: response.platforms || []};
      setStats(response.stats); grid.replaceChildren(...response.photos.map(renderPhoto));
      $('admin-empty').hidden = response.photos.length > 0;
      $('admin-empty').querySelector('h3').textContent = filter === 'all' ? '还没有照片' : '暂无' + (states[filter] || '') + '照片';
      $('admin-empty').querySelector('p').textContent = filter === 'all' ? '点击“选择照片”，可以一次上传多张。' : '可以切换其他状态查看。';
      const size = response.pageSize || 60;
      $('page-info').textContent = '共 ' + response.total + ' 张 · 第 ' + page + ' 页';
      $('previous-page').disabled = page <= 1;
      $('next-page').disabled = page * size >= response.total;
      $('library-heading').textContent = filter === 'all' ? '全部照片' : (states[filter] + '照片');
    } catch (error) {
      if (error.status === 401) showLogin();
      tell(error.message, 'error');
    } finally {
      if (id === loadId) { loading = false; grid.setAttribute('aria-busy', 'false'); $('refresh-photos').disabled = false; }
    }
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('login-button'); button.disabled = true; button.textContent = '正在登录…'; tell('');
    try {
      await api('/admin/login', json('POST', {password: $('password').value}));
      $('password').value = ''; showMain(); await loadPhotos();
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
    loadPhotos();
  }));
  $('refresh-photos').addEventListener('click', loadPhotos);
  $('previous-page').addEventListener('click', () => { if (loading || page <= 1) return; page--; loadPhotos(); });
  $('next-page').addEventListener('click', () => { if (loading) return; page++; loadPhotos(); });
  $('photo-files').addEventListener('change', async () => {
    if (uploading) return;
    const selected = Array.from($('photo-files').files || []); if (!selected.length) return;
    uploading = true; $('photo-files').disabled = true; $('upload-progress').hidden = false;
    const label = document.querySelector('.upload-label'); label.setAttribute('aria-disabled', 'true'); label.textContent = '上传中…';
    let success = 0, duplicates = 0, failures = [];
    $('upload-bar').max = selected.length; $('upload-bar').value = 0;
    tell('');
    for (let index = 0; index < selected.length; index++) {
      const file = selected[index];
      $('upload-status').textContent = '正在上传 ' + (index + 1) + ' / ' + selected.length;
      const extensions = {jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp'};
      const type = file.type || extensions[file.name.split('.').pop().toLowerCase()];
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(type)) failures.push(file.name + '：请使用 JPG、PNG 或 WebP');
      else if (!file.size || file.size > 12 * 1024 * 1024) failures.push(file.name + '：每张需在 12MB 以内');
      else {
        try {
          const result = await api('/admin/photos', {method: 'POST', timeout: 90000, headers: {'Content-Type': type, 'X-File-Name': encodeURIComponent(file.name)}, body: file});
          if (result.duplicate) duplicates++; else success++;
        } catch (error) {
          failures.push(file.name + '：' + error.message);
          if (error.status === 401) { showLogin(); break; }
        }
      }
      $('upload-bar').value = index + 1;
    }
    $('upload-status').textContent = '新增 ' + success + ' 张' + (duplicates ? '，重复跳过 ' + duplicates + ' 张' : '') + (failures.length ? '，失败 ' + failures.length + ' 张' : '，上传完成');
    const summary = '新增 ' + success + ' 张照片。' + (duplicates ? '重复照片已跳过 ' + duplicates + ' 张。' : '');
    tell(failures.length ? summary + failures.slice(0, 4).join('；') + (failures.length > 4 ? '；其余失败照片请重选后再上传。' : '。失败照片可重选后再上传，已成功的照片会自动跳过。') : summary + '每次领取 3 张，使用后自动退出可领取列表。', failures.length ? 'error' : 'success');
    uploading = false; $('photo-files').disabled = false; $('photo-files').value = ''; label.removeAttribute('aria-disabled'); label.textContent = '选择照片';
    if (loginSection.hidden) { page = 1; await loadPhotos(); }
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
      const icon = document.createElement('img'); icon.src = BASE + '/brand/' + id + '.png'; icon.alt = '';
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
  api('/admin/session').then(() => { showMain(); return loadPhotos(); }).catch(error => { showLogin(); if (error.status !== 401) tell(error.message, 'error'); });
})();
