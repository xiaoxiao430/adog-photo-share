#!/usr/bin/env python3
"""默认只检查6张AI示例；--seed显式上传，按SHA幂等，不恢复已用照片。"""
import argparse
import datetime
import hashlib
import http.cookiejar
import importlib.util
import json
from pathlib import Path
import stat
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BASE = 'https://jmryqyx.cn/aidog-photos'
ORIGIN = 'https://jmryqyx.cn'
PASSWORD_FILE = Path('/Users/xiaoxiao/.codex/private/default-admin-password.txt')


class Failure(Exception):
    pass


def need(value, code):
    if not value:
        raise Failure(code)


def inspect():
    spec = importlib.util.spec_from_file_location('aidog_photos_validation', ROOT / 'server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = sorted((ROOT / 'demo-images').glob('AI示例_*.png'))
    need(len(files) == 6, 'exactly_six_demo_images_required')
    inspected = []
    seen = set()
    for path in files:
        need(path.is_file() and not path.is_symlink(), 'demo_file_must_be_regular')
        raw = path.read_bytes()
        need(0 < len(raw) <= 12 * 1024 * 1024, 'demo_image_size_limit')
        need(module.valid_image(raw, 'image/png'), 'demo_png_validation_failed')
        width, height = struct.unpack('>II', raw[16:24])
        digest = hashlib.sha256(raw).hexdigest()
        need(digest not in seen, 'duplicate_demo_image_bytes')
        seen.add(digest)
        inspected.append({'name': path.name, 'sha256': digest, 'bytes': len(raw), 'width': width, 'height': height})
    return inspected


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, path, method='GET', json_value=None, raw=None, filename=None):
        headers = {'Origin': ORIGIN}
        data = raw
        if json_value is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(json_value).encode()
        if raw is not None:
            headers['Content-Type'] = 'image/png'
            headers['X-File-Name'] = urllib.parse.quote(filename, safe='')
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=60) as response:
                need(response.status == 200, 'unexpected_http_status')
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise Failure('http_status_' + str(error.code)) from None
        except urllib.error.URLError:
            raise Failure('network_request_failed') from None

    def inventory(self):
        first = self.request('/api/admin/photos?page=1')
        photos = {photo['id']: photo for photo in first['photos']}
        pages = (first['total'] + first['pageSize'] - 1) // first['pageSize']
        for page in range(2, pages + 1):
            result = self.request('/api/admin/photos?page=' + str(page))
            photos.update({photo['id']: photo for photo in result['photos']})
        return first['stats'], photos


def seed(inspected):
    need(PASSWORD_FILE.is_file() and not PASSWORD_FILE.is_symlink(), 'private_password_missing')
    need(stat.S_IMODE(PASSWORD_FILE.stat().st_mode) == 0o600, 'private_password_permissions')
    password = PASSWORD_FILE.read_text().rstrip('\r\n')
    need(bool(password), 'private_password_empty')
    client = Client()
    logged = False
    report = {'ok': False, 'mode': 'seed', 'atUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'uploaded': 0, 'duplicates': 0, 'files': [], 'noClaimsOrInventoryReset': True, 'secretsPrinted': False}
    try:
        client.request('/api/admin/login', 'POST', json_value={'password': password})
        del password
        logged = True
        before_stats, before = client.inventory()
        report['beforeStats'] = before_stats
        report['originalTotal'] = before_stats['all']
        for image in inspected:
            path = ROOT / 'demo-images' / image['name']
            raw = path.read_bytes()
            need(hashlib.sha256(raw).hexdigest() == image['sha256'], 'demo_image_changed_after_inspect')
            result = client.request('/api/admin/photos', 'POST', raw=raw, filename=image['name'])
            need(isinstance(result.get('id'), str) and isinstance(result.get('duplicate'), bool), 'upload_response_shape')
            action = 'duplicate' if result['duplicate'] else 'uploaded'
            report['duplicates' if result['duplicate'] else 'uploaded'] += 1
            report['files'].append({**image, 'id': result['id'], 'result': action})
        after_stats, after = client.inventory()
        for pid, photo in before.items():
            if photo['status'] == 'used':
                need(pid in after and after[pid]['status'] == 'used' and after[pid]['usedAt'] == photo['usedAt'], 'previous_used_photo_changed')
        report['afterStats'] = after_stats
        report['newTotal'] = after_stats['all']
        report['previousUsedRecordsPreserved'] = True
        report['ok'] = True
    except Failure as error:
        report['error'] = str(error)
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        if logged:
            try:
                client.request('/api/admin/logout', 'POST', json_value={})
                report['logout'] = 200
            except Exception:
                report['logout'] = 'failed'
        output = ROOT / 'output'
        output.mkdir(exist_ok=True)
        filename = 'seed-demo-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json'
        (output / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--inspect', action='store_true', help='只读检查（默认）')
    group.add_argument('--seed', action='store_true', help='显式上传6张示例照片')
    args = parser.parse_args()
    inspected = inspect()
    if args.seed:
        result = seed(inspected)
    else:
        result = {'ok': True, 'mode': 'inspect', 'count': len(inspected), 'files': inspected, 'mutationsPerformed': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        safe = str(error) if isinstance(error, Failure) else type(error).__name__
        print(json.dumps({'ok': False, 'error': safe, 'secretsPrinted': False}, ensure_ascii=False))
        sys.exit(1)
