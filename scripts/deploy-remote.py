#!/usr/bin/env python3
"""只管理独立 aidog-photos 服务；输入仅经 SSH stdin，不打印配置。"""
import base64
import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path('/Users/liuminghao/services/aidog-photos')
PRIVATE = Path('/Users/liuminghao/.config/aidog-photos')
CFG = PRIVATE / 'config.json'
NGINX = Path('/opt/homebrew/etc/nginx/servers/jmryqyx.conf')
NGINX_BIN = '/opt/homebrew/bin/nginx'
PYTHON = '/opt/homebrew/bin/python3.12'
PLIST = Path('/Users/liuminghao/Library/LaunchAgents/com.xtobs.aidog-photos.plist')
DOMAIN = 'gui/' + str(os.getuid())
LABEL = DOMAIN + '/com.xtobs.aidog-photos'
BASE = '/aidog-photos'
ORIGIN = 'https://jmryqyx.cn'
PORT = 18988


def need(value, code):
    if not value:
        raise RuntimeError(code)


def command(args, check=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check:
        need(result.returncode == 0, 'command_failed_' + Path(args[0]).name)
    return result


def private_write(path, data, mode=0o600):
    need(not path.is_symlink(), 'write_target_symlink')
    temp = path.with_name(path.name + '.tmp-' + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def json_write(path, data):
    private_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(.25)
        return connection.connect_ex(('127.0.0.1', port)) == 0


def request(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read(2048)
    except urllib.error.HTTPError as error:
        return error.code, b''


def wait_public(url, expected=200, seconds=15):
    # nginx reload returns before its replacement workers accept requests.
    until = time.monotonic() + seconds
    last_status = None
    while time.monotonic() < until:
        try:
            last_status, _ = request(url)
            if last_status == expected:
                return last_status
        except OSError:
            pass
        time.sleep(.35)
    raise RuntimeError('public_status_' + str(last_status) + '_expected_' + str(expected))


def wait_health(port, seconds=20):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            status, body = request('http://127.0.0.1:' + str(port) + BASE + '/api/health')
            if status == 200:
                need(json.loads(body).get('ok') is True, 'health_shape')
                return
        except (OSError, ValueError):
            pass
        time.sleep(.25)
    raise RuntimeError('service_health_timeout')


def stop():
    command(['launchctl', 'disable', LABEL], check=False)
    command(['launchctl', 'bootout', LABEL], check=False)
    until = time.monotonic() + 15
    while port_open(PORT) and time.monotonic() < until:
        time.sleep(.2)
    need(not port_open(PORT), 'service_stop_timeout')


def start():
    command(['launchctl', 'enable', LABEL])
    command(['launchctl', 'bootstrap', DOMAIN, str(PLIST)])
    wait_health(PORT)


def env(data_dir, port):
    return dict(os.environ, AIDOG_PHOTOS_DATA=str(data_dir), AIDOG_PHOTOS_CONFIG=str(CFG),
                AIDOG_PHOTOS_PORT=str(port), AIDOG_PHOTOS_BASE=BASE,
                AIDOG_PHOTOS_ORIGIN=ORIGIN, PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')


def inspect():
    raw = NGINX.read_text()
    return {'ok': True, 'mode': 'inspect', 'python': sys.version.split()[0],
            'scryptAvailable': hasattr(hashlib, 'scrypt'), 'port': PORT,
            'portOccupied': port_open(PORT), 'currentRelease': os.readlink(ROOT / 'current') if (ROOT / 'current').is_symlink() else None,
            'privateConfigPresent': CFG.is_file(), 'photosNamespacePresent': 'aidog-photos/config/' in raw,
            'existingShareHttpIncludeCount': raw.count('/aidog-share/config/http.locations.conf;'),
            'existingShareHttpsIncludeCount': raw.count('/aidog-share/config/https.locations.conf;'),
            'nginxSyntaxPasses': command([NGINX_BIN, '-t'], check=False).returncode == 0,
            'mutationsPerformed': False}


def install_config(value):
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PRIVATE, 0o700)
    if CFG.exists():
        need(CFG.is_file() and not CFG.is_symlink() and CFG.stat().st_mode & 0o777 == 0o600, 'existing_config_permissions')
        existing = json.loads(CFG.read_text())
        need(re.fullmatch('[0-9a-f]{64}', existing.get('passwordHash', '')) and re.fullmatch('[0-9a-f]{32}', existing.get('salt', '')), 'existing_config_shape')
        return False
    need(re.fullmatch('[0-9a-f]{64}', value.get('passwordHash', '')) and re.fullmatch('[0-9a-f]{32}', value.get('salt', '')), 'new_config_shape')
    need(re.fullmatch('[0-9a-f]{64}', value.get('sessionSecret', '')), 'new_secret_shape')
    json_write(CFG, value)
    return True


def extract_release(value):
    name = value['release']
    need(re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', name) is not None, 'release_name')
    raw = base64.b64decode(value['archive'], validate=True)
    need(hashlib.sha256(raw).hexdigest() == value['archiveSha256'], 'archive_sha256')
    release = ROOT / 'releases' / name
    need(not release.exists(), 'release_already_present')
    release.mkdir(mode=0o700)
    files = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            need(member.isfile() and not member.issym() and not member.islnk(), 'archive_special_file')
            need(not path.is_absolute() and '..' not in path.parts and not any(p.startswith('._') for p in path.parts), 'archive_unsafe_name')
            need(member.name == 'server.py' or member.name.startswith('public/'), 'archive_unexpected_path')
            need(member.size <= 20 * 1024 * 1024, 'archive_file_size')
            dest = release / path
            need(not dest.exists(), 'archive_duplicate')
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = tar.extractfile(member)
            need(handle is not None, 'archive_read')
            dest.write_bytes(handle.read())
            os.chmod(dest, 0o600)
            files.append(member.name)
    for required in ['server.py', 'public/index.html', 'public/admin.html', 'public/app.css', 'public/guest.js', 'public/admin.js', 'public/fonts/yahei.woff2']:
        need(required in files, 'archive_missing_required')
    command([PYTHON, '-c', 'import ast,pathlib; ast.parse(pathlib.Path(' + repr(str(release / 'server.py')) + ').read_text())'])
    return name, release, len(files)


def diagnose(release):
    port = 18989
    need(not port_open(port), 'diagnostic_port_busy')
    with tempfile.TemporaryDirectory(prefix='aidog-photos-diagnostic-') as temp:
        child = subprocess.Popen([PYTHON, str(release / 'server.py')], env=env(Path(temp) / 'data', port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_health(port)
            for route in ['/', '/admin', '/app.css', '/guest.js', '/admin.js', '/fonts/yahei.woff2']:
                status, _ = request('http://127.0.0.1:' + str(port) + BASE + route)
                need(status == 200, 'diagnostic_route_' + route.replace('/', '_'))
            return True
        finally:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def plist_bytes():
    logs = Path('/Users/liuminghao/Library/Logs/aidog-photos')
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = env(ROOT / 'data', PORT)
    keys = ['AIDOG_PHOTOS_DATA', 'AIDOG_PHOTOS_CONFIG', 'AIDOG_PHOTOS_PORT', 'AIDOG_PHOTOS_BASE', 'AIDOG_PHOTOS_ORIGIN', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE']
    return plistlib.dumps({'Label': 'com.xtobs.aidog-photos', 'ProgramArguments': [PYTHON, str(ROOT / 'current/server.py')],
        'WorkingDirectory': str(ROOT), 'EnvironmentVariables': {key: values[key] for key in keys},
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
        'StandardOutPath': str(logs / 'stdout.log'), 'StandardErrorPath': str(logs / 'stderr.log'), 'Umask': 0o077})


def nginx_files():
    private_write(ROOT / 'config/http.locations.conf', b'''# Isolated aidog-photos customer tool.
location = /aidog-photos { return 301 https://jmryqyx.cn/aidog-photos/$is_args$args; }
location ^~ /aidog-photos/ { return 308 https://jmryqyx.cn$request_uri; }
''')
    private_write(ROOT / 'config/https.locations.conf', b'''# Only this service's namespace; preserve all other app routes.
location = /aidog-photos { return 301 /aidog-photos/$is_args$args; }
location ^~ /aidog-photos/ {
    proxy_pass http://127.0.0.1:18988;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Connection "";
    proxy_connect_timeout 5s;
    proxy_read_timeout 45s;
    proxy_send_timeout 45s;
    client_max_body_size 13m;
    proxy_buffering off;
    access_log off;
    add_header Referrer-Policy no-referrer always;
    add_header X-Content-Type-Options nosniff always;
}
''')


def add_nginx_includes(raw):
    text = raw.decode()
    for kind in ['http', 'https']:
        own = '    include ' + str(ROOT / ('config/' + kind + '.locations.conf')) + ';\n'
        if own in text:
            need(text.count(own) == 1, 'duplicate_own_include')
            continue
        anchor = '    # aidog-share: isolated customer sharing app\n'
        anchor += '    include /Users/liuminghao/services/aidog-share/config/' + kind + '.locations.conf;\n'
        need(text.count(anchor) == 1, 'nginx_anchor_not_unique')
        text = text.replace(anchor, '    # aidog-photos: independent photo pickup tool\n' + own + '\n' + anchor, 1)
    return text.encode()


def remove_own_new_includes(raw, original):
    # Preserve concurrent edits to unrelated services on failure.
    text = raw.decode()
    for kind in ['http', 'https']:
        block = '    # aidog-photos: independent photo pickup tool\n    include ' + str(ROOT / ('config/' + kind + '.locations.conf')) + ';\n\n'
        if block.encode() not in original:
            text = text.replace(block, '', 1)
    return text.encode()


def switch_current(relative):
    current = ROOT / 'current'
    temporary = ROOT / ('current.tmp-' + secrets.token_hex(6))
    os.symlink(relative, temporary)
    os.replace(temporary, current)


def main(value):
    if value.get('mode') == 'inspect':
        return inspect()
    need(value.get('mode') in ['deploy', 'rollback'], 'invalid_mode')
    need(hasattr(hashlib, 'scrypt'), 'scrypt_missing')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in [ROOT, ROOT / 'releases', ROOT / 'config', ROOT / 'backups']:
        path.mkdir(exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    lock = open(ROOT / '.deploy.lock', 'a')
    os.chmod(ROOT / '.deploy.lock', 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    old_link = os.readlink(ROOT / 'current') if (ROOT / 'current').is_symlink() else None
    old_plist = PLIST.read_bytes() if PLIST.exists() else None
    old_nginx = NGINX.read_bytes()
    config_created = install_config(value.get('config', {}))
    if value['mode'] == 'rollback':
        name = value.get('release', '')
        need(re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', name), 'rollback_release_name')
        release = ROOT / 'releases' / name
        need(release.is_dir() and not release.is_symlink(), 'rollback_release_missing')
        file_count = None
    else:
        name, release, file_count = extract_release(value)
    diagnosed = diagnose(release)
    had_service = command(['launchctl', 'print', LABEL], check=False).returncode == 0
    need(had_service or not port_open(PORT), 'port_owned_by_other_service')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'backups' / (stamp + '-' + secrets.token_hex(4))
    backup.mkdir(mode=0o700)
    private_write(backup / 'nginx-before.conf', old_nginx)
    if old_plist:
        private_write(backup / 'launchd-before.plist', old_plist)
    stopped = False
    nginx_changed = False
    try:
        if had_service:
            stop()
            stopped = True
        if (ROOT / 'data').exists():
            with tarfile.open(backup / 'data-before.tar.gz', 'w:gz') as tar:
                tar.add(ROOT / 'data', arcname='data', recursive=True)
            os.chmod(backup / 'data-before.tar.gz', 0o600)
        nginx_files()
        # Other projects may publish while our isolated diagnostic is running.
        # Rebase our two include additions onto the latest unrelated config.
        nginx_apply_baseline = NGINX.read_bytes()
        candidate = add_nginx_includes(nginx_apply_baseline)
        if candidate != nginx_apply_baseline:
            need(NGINX.read_bytes() == nginx_apply_baseline, 'nginx_concurrent_edit_before_write')
            private_write(NGINX, candidate, 0o644)
            nginx_changed = True
        command([NGINX_BIN, '-t'])
        private_write(PLIST, plist_bytes())
        switch_current('releases/' + name)
        start()
        command([NGINX_BIN, '-s', 'reload'])
        statuses = {}
        for route in ['/', '/admin', '/api/health']:
            status = wait_public(ORIGIN + BASE + route)
            statuses[route] = status
        status = wait_public(ORIGIN + BASE + '/api/admin/session', 401)
        receipt = {'ok': True, 'mode': value['mode'], 'atUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'release': name, 'previousRelease': old_link, 'publicUrl': ORIGIN + BASE + '/', 'adminUrl': ORIGIN + BASE + '/admin',
            'listen': '127.0.0.1:' + str(PORT), 'launchdLabel': LABEL, 'python': sys.version.split()[0],
            'privateConfigCreated': config_created, 'privateConfigPreserved': not config_created,
            'archiveSha256': value.get('archiveSha256'), 'sourceFiles': file_count, 'isolatedDiagnosticPassed': diagnosed,
            'nginxSyntaxPassed': True, 'nginxUnrelatedBytesPreserved': remove_own_new_includes(NGINX.read_bytes(), nginx_apply_baseline) == nginx_apply_baseline,
            'concurrentUnrelatedNginxUpdateObserved': nginx_apply_baseline != old_nginx,
            'newNamespaceOnly': True, 'oldAppDataAndConfigMutated': False, 'backup': str(backup), 'checks': statuses,
            'anonymousAdmin': status, 'phoneAlbumSaveVerified': False}
        json_write(ROOT / 'deploy-state.json', receipt)
        return receipt
    except Exception:
        # Roll back only our own service and our newly inserted include lines.
        if command(['launchctl', 'print', LABEL], check=False).returncode == 0:
            stop()
        if old_link:
            switch_current(old_link)
        elif (ROOT / 'current').is_symlink():
            (ROOT / 'current').unlink()
        if old_plist:
            private_write(PLIST, old_plist)
        elif PLIST.exists():
            PLIST.unlink()
        if nginx_changed:
            current_nginx = NGINX.read_bytes()
            restored = remove_own_new_includes(current_nginx, old_nginx)
            private_write(NGINX, restored, 0o644)
            if command([NGINX_BIN, '-t'], check=False).returncode == 0:
                command([NGINX_BIN, '-s', 'reload'], check=False)
        if had_service and old_link and old_plist:
            start()
        raise


try:
    result = main(json.load(sys.stdin))
    print(json.dumps(result, ensure_ascii=False), flush=True)
except Exception as error:
    # Messages only contain fixed codes. Never serialize arbitrary exception text or stdin.
    safe = str(error) if isinstance(error, RuntimeError) and re.fullmatch(r'[A-Za-z0-9_./-]{1,120}', str(error)) else type(error).__name__
    print(json.dumps({'ok': False, 'error': safe, 'secretsPrinted': False}), flush=True)
    sys.exit(1)
