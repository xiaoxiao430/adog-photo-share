"""HTTP integration checks against disposable local data; never contact production."""
import concurrent.futures
from dataclasses import dataclass
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote
import zlib


PROJECT = Path(__file__).resolve().parents[1]
BASE = '/aidog-photos'


def synthetic_png():
    """A valid, unique 2x2 RGB PNG with ordinary CRC-checked chunks."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = b'\0' + secrets.token_bytes(6) + b'\0' + secrets.token_bytes(6)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


@dataclass
class Result:
    status: int
    data: object
    body: bytes
    headers: list


class Client:
    def __init__(self, port, origin, cookies=None):
        self.port, self.origin = port, origin
        self.cookies = dict(cookies or {})

    def fork(self):
        return Client(self.port, self.origin, self.cookies)

    def request(self, method, path, value=None, raw=None, headers=None):
        supplied = dict(headers or {})
        supplied.setdefault('X-Aidog-Photo-Flow', '3')
        body = raw
        if value is not None:
            body = json.dumps(value).encode()
            supplied.setdefault('Content-Type', 'application/json')
        if method != 'GET':
            supplied.setdefault('Origin', self.origin)
        if self.cookies:
            supplied.setdefault('Cookie', '; '.join(f'{key}={val}' for key, val in self.cookies.items()))
        supplied['Connection'] = 'close'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=6)
        try:
            conn.request(method, BASE + path, body=body, headers=supplied)
            response = conn.getresponse()
            payload, response_headers = response.read(), response.getheaders()
            for key, val in response_headers:
                if key.lower() == 'set-cookie':
                    jar = SimpleCookie(val)
                    for cookie_key, item in jar.items():
                        if item['max-age'] == '0':
                            self.cookies.pop(cookie_key, None)
                        else:
                            self.cookies[cookie_key] = item.value
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeError):
                data = None
            return Result(response.status, data, payload, response_headers)
        finally:
            conn.close()


class PhotoServerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aidog-package-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.config = self.home / 'config.json'
        self.password = secrets.token_urlsafe(22)
        salt = secrets.token_bytes(16)
        self.config.write_text(json.dumps({'salt': salt.hex(), 'passwordHash': hashlib.scrypt(
            self.password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()}))
        self.config.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.origin = f'http://localhost:{self.port}'
        self.env = {**os.environ, 'AIDOG_PHOTOS_BASE': BASE, 'AIDOG_PHOTOS_ORIGIN': self.origin,
                    'AIDOG_PHOTOS_DATA': str(self.data), 'AIDOG_PHOTOS_CONFIG': str(self.config),
                    'AIDOG_PHOTOS_INITIAL_PACKAGES': str(self.home / 'initial-packages.json'),
                    'AIDOG_PHOTOS_PORT': str(self.port), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.log = (self.home / 'server.log').open('w+')
        self.addCleanup(self.log.close)
        self.process = None
        self.addCleanup(self.stop_server)
        self.admin = self.client()
        self.start_server()
        self.assertEqual(self.admin.request('POST', '/api/admin/login', {'password': self.password}).status, 200)

    def start_server(self):
        self.process = subprocess.Popen([sys.executable, str(PROJECT / 'server.py')], cwd=PROJECT,
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail('Disposable server exited: ' + self.log.read())
            try:
                if self.admin.request('GET', '/api/health').status == 200:
                    return
            except (OSError, http.client.HTTPException):
                time.sleep(0.025)
        self.fail('Disposable server did not start')

    def stop_server(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)

    def restart(self):
        self.stop_server()
        self.start_server()

    def client(self):
        return Client(self.port, self.origin)

    def sql(self, query, args=()):
        with sqlite3.connect(self.data / 'photos.sqlite3') as c:
            c.row_factory = sqlite3.Row
            return [dict(row) for row in c.execute(query, args)]

    def form(self, files, fields):
        boundary = 'aidog-test-' + secrets.token_hex(14)
        chunks = []
        for key, value in fields:
            value = value if isinstance(value, bytes) else str(value).encode()
            chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value, b'\r\n']
        for index, file in enumerate(files):
            name, content, mime = file
            chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(), content, b'\r\n']
        chunks.append(f'--{boundary}--\r\n'.encode())
        return b''.join(chunks), 'multipart/form-data; boundary=' + boundary

    def files(self, count=3):
        return [(f'合成照片-{index}.png', synthetic_png(), 'image/png') for index in range(count)]

    def submit(self, files=None, copy='这是本地合成图片的固定参考文案，仅用于功能验证。', title='合成图文套装',
               upload_id=None, actor=None, extra_fields=()):
        fields = list(extra_fields)
        if copy is not None:
            fields.append(('copy', copy))
        if title is not None:
            fields.append(('title', title))
        fields.append(('uploadId', upload_id or secrets.token_hex(12)))
        payload, mime = self.form(self.files() if files is None else files, fields)
        return (actor or self.admin).request('POST', '/api/admin/packages', raw=payload,
                                            headers={'Content-Type': mime})

    def create(self, index=0, files=None):
        files = self.files() if files is None else files
        title, copy = f'固定第{index}套', f'第{index}套固定参考文案：三张合成图片按本套顺序展示。'
        result = self.submit(files, copy=copy, title=title)
        self.assertEqual(result.status, 200, result.data)
        self.assertFalse(result.data['duplicate'])
        return result.data['package'], files

    def packages(self, **query):
        suffix = '?' + '&'.join(f'{key}={value}' for key, value in query.items()) if query else ''
        result = self.admin.request('GET', '/api/admin/packages' + suffix)
        self.assertEqual(result.status, 200, result.data)
        return result.data

    def photos(self):
        result = self.admin.request('GET', '/api/admin/photos')
        self.assertEqual(result.status, 200, result.data)
        return result.data

    def bootstrap(self, actor):
        result = actor.request('GET', '/api/bootstrap')
        self.assertEqual(result.status, 200, result.data)
        return result.data['batch']

    def prepare(self, actor, selected):
        result = actor.request('POST', '/api/batch/prepare', {'batchId': selected['id']})
        self.assertEqual(result.status, 200, result.data)
        return result.data['batch']

    def commit(self, actor, selected, method='system_share'):
        return actor.request('POST', '/api/batch/commit', {'batchId': selected['id'], 'method': method})

    def assert_snapshot(self, selected, package):
        self.assertEqual(selected['packageId'], package['id'])
        self.assertEqual(selected['title'], package['title'])
        self.assertEqual(selected['copy'], package['copy'])
        self.assertEqual([p['id'] for p in selected['photos']], [p['id'] for p in package['photos']])

    def assert_empty(self):
        self.assertEqual(self.packages()['stats']['all'], 0)
        self.assertEqual(self.photos()['stats']['all'], 0)
        self.assertEqual(list((self.data / 'images').iterdir()), [])
        self.assertEqual(list((self.data / 'thumbnails').iterdir()), [])

    def assert_jpeg_decodes(self, payload):
        self.assertTrue(payload.startswith(b'\xff\xd8\xff') and payload.endswith(b'\xff\xd9'))
        if not Path('/usr/bin/sips').is_file():
            self.skipTest('Native JPEG decode check needs macOS sips')
        source = self.home / (secrets.token_hex(8) + '.jpg')
        target = source.with_suffix('.png')
        source.write_bytes(payload)
        result = subprocess.run(['/usr/bin/sips', '-s', 'format', 'png', str(source), '--out', str(target)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0)
        decoded = target.read_bytes()
        self.assertTrue(decoded.startswith(b'\x89PNG\r\n\x1a\n'))
        width, height = struct.unpack('!II', decoded[16:24])
        self.assertGreater(min(width, height), 0)
        self.assertLessEqual(max(width, height), 480)

    def test_missing_copy_or_photos_never_creates_partial_inventory(self):
        for files, copy in [(self.files(), None), ([], '只有文案')]:
            with self.subTest(files=len(files), missing_copy=copy is None):
                self.assertEqual(self.submit(files, copy=copy).status, 400)
                self.assert_empty()

    def test_unicode_whitespace_and_overlong_copy_rejected_atomically(self):
        for copy in ['', '\u3000\u2003\u00a0\n\t', '字' * 1001]:
            with self.subTest(length=len(copy)):
                self.assertEqual(self.submit(copy=copy).status, 400)
                self.assert_empty()

    def test_copy_accepts_utf8_at_boundary_and_normalizes_newlines(self):
        response = self.submit(copy='字' * 1000)
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['package']['copy'], '字' * 1000)
        response = self.submit(copy='  中文正文\r\n第二行  ', title='')
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['package']['copy'], '中文正文\n第二行')
        self.assertEqual(response.data['package']['title'], '中文正文')

    def test_two_or_four_photos_rejected_without_writing_files(self):
        for count in [2, 4]:
            with self.subTest(count=count):
                self.assertEqual(self.submit(self.files(count)).status, 400)
                self.assert_empty()

    def test_three_identical_photos_rejected(self):
        file = self.files(1)[0]
        self.assertEqual(self.submit([file, file, file]).status, 400)
        self.assert_empty()

    def test_damaged_third_image_rolls_back_entire_package(self):
        files = self.files()
        files[2] = ('broken.png', b'\x89PNG\r\n\x1a\n', 'image/png')
        self.assertEqual(self.submit(files).status, 400)
        self.assert_empty()

    def test_invalid_utf8_duplicate_field_and_upload_id_rejected(self):
        invalid = [dict(copy=b'\xff\xfe'), dict(extra_fields=[('copy', 'duplicate')]), dict(upload_id='bad')]
        for kwargs in invalid:
            with self.subTest(kwargs=list(kwargs)):
                self.assertEqual(self.submit(**kwargs).status, 400)
                self.assert_empty()

    def test_valid_multipart_binds_order_copy_and_original_bytes(self):
        package, files = self.create(7)
        self.assertEqual(package['copy'], '第7套固定参考文案：三张合成图片按本套顺序展示。')
        self.assertEqual([p['name'] for p in package['photos']], [f[0] for f in files])
        for photo, (_, original, _) in zip(package['photos'], files):
            result = self.admin.request('GET', photo['url'].removeprefix(BASE))
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, original)
        self.assertEqual(self.packages()['stats'], {'available': 1, 'reserved': 0, 'used': 0, 'all': 1})
        self.assertEqual(self.photos()['stats']['all'], 3)

    def test_upload_id_retry_is_idempotent(self):
        files, upload_id = self.files(), secrets.token_hex(12)
        first = self.submit(files, upload_id=upload_id)
        second = self.submit(files, upload_id=upload_id)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.data['id'], second.data['id'])
        self.assertTrue(second.data['duplicate'])
        self.assertEqual(self.packages()['stats']['all'], 1)
        self.assertEqual(len(list((self.data / 'images').iterdir())), 3)

    def test_same_upload_id_with_changed_content_or_order_conflicts(self):
        files, upload_id = self.files(), secrets.token_hex(12)
        self.assertEqual(self.submit(files, upload_id=upload_id).status, 200)
        for changed, copy in [(list(reversed(files)), '这是本地合成图片的固定参考文案，仅用于功能验证。'),
                              (files, '已修改文案'), (self.files(), '已修改全部图片')]:
            with self.subTest(copy=copy):
                self.assertEqual(self.submit(changed, copy=copy, upload_id=upload_id).status, 409)
        self.assertEqual(self.packages()['stats']['all'], 1)
        self.assertEqual(self.photos()['stats']['all'], 3)

    def test_same_three_photos_new_id_returns_existing_used_package(self):
        package, files = self.create()
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest))
        receipt = self.commit(guest, selected)
        self.assertEqual(receipt.status, 200)
        duplicate = self.submit(list(reversed(files)), copy='不能覆盖原文案')
        self.assertEqual(duplicate.status, 200, duplicate.data)
        self.assertTrue(duplicate.data['duplicate'])
        self.assertEqual(duplicate.data['id'], package['id'])
        self.assertEqual(duplicate.data['package']['status'], 'used')
        self.assertEqual(duplicate.data['package']['copy'], package['copy'])
        self.assertEqual(duplicate.data['package']['usedAt'], receipt.data['usedAt'])
        self.assertEqual(self.photos()['stats'], {'available': 0, 'reserved': 0, 'used': 3, 'all': 3})

    def test_partial_duplicate_across_packages_conflicts_atomically(self):
        _, files = self.create()
        self.assertEqual(self.submit([files[0], *self.files(2)]).status, 409)
        self.assertEqual(self.packages()['stats']['all'], 1)
        self.assertEqual(self.photos()['stats']['all'], 3)
        self.assertEqual(len(list((self.data / 'images').iterdir())), 3)

    def test_legacy_single_photo_upload_forbidden(self):
        result = self.admin.request('POST', '/api/admin/photos', raw=synthetic_png(), headers={'Content-Type': 'image/png'})
        self.assertEqual(result.status, 400)
        self.assert_empty()

    def test_preview_copy_and_image_reads_leave_package_available(self):
        package, _ = self.create()
        guests = [self.client() for _ in range(10)]
        for actor in guests:
            selected = self.bootstrap(actor)
            self.assert_snapshot(selected, package)
            self.assertEqual(selected['status'], 'preview')
            for photo in selected['photos']:
                self.assertEqual(actor.request('GET', photo['previewUrl'].removeprefix(BASE)).status, 200)
            self.assertEqual(self.commit(actor, selected).status, 409)
        self.assertEqual(self.packages()['stats']['available'], 1)
        self.assertEqual(self.photos()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})

    def test_eight_concurrent_prepares_preserve_complete_nonoverlapping_packages(self):
        expected = {p['id']: p for p, _ in [self.create(i) for i in range(8)]}
        guests = [self.client() for _ in range(8)]
        previews = [self.bootstrap(g) for g in guests]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            selected = list(pool.map(lambda pair: self.prepare(*pair), zip(guests, previews)))
        self.assertEqual(len({b['packageId'] for b in selected}), 8)
        ids = [p['id'] for b in selected for p in b['photos']]
        self.assertEqual(len(ids), len(set(ids)))
        for b in selected:
            self.assert_snapshot(b, expected[b['packageId']])
        self.assertEqual(self.packages()['stats']['reserved'], 8)
        self.assertEqual(self.photos()['stats']['reserved'], 24)

    def test_collision_switches_whole_package_and_matching_copy(self):
        expected = {p['id']: p for p, _ in [self.create(1), self.create(2)]}
        a, b = self.client(), self.client()
        pa, pb = self.bootstrap(a), self.bootstrap(b)
        self.assertEqual(pa['packageId'], pb['packageId'])
        sa, sb = self.prepare(a, pa), self.prepare(b, pb)
        self.assertNotEqual(sa['packageId'], sb['packageId'])
        self.assertNotEqual(sa['copy'], sb['copy'])
        self.assert_snapshot(sa, expected[sa['packageId']])
        self.assert_snapshot(sb, expected[sb['packageId']])

    def test_no_second_package_cannot_split_or_reuse_reserved_group(self):
        self.create()
        a, b = self.client(), self.client()
        pa, pb = self.bootstrap(a), self.bootstrap(b)
        self.prepare(a, pa)
        refused = b.request('POST', '/api/batch/prepare', {'batchId': pb['id']})
        self.assertEqual(refused.status, 409)
        self.assertEqual(self.commit(b, pb).status, 409)
        self.assertEqual(self.photos()['stats']['reserved'], 3)

    def test_prepared_and_used_snapshots_stay_stable_and_put_is_forbidden(self):
        package, _ = self.create()
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest))
        for state in ['reserved', 'used']:
            if state == 'used':
                self.assertEqual(self.commit(guest, selected).status, 200)
            changed = self.admin.request('PUT', '/api/admin/packages/' + package['id'], {'title': '改标题', 'copy': '改正文'})
            self.assertEqual(changed.status, 409)
            again = self.bootstrap(guest)
            self.assertEqual(again['status'], state)
            self.assert_snapshot(again, package)
            self.assertEqual(again['id'], selected['id'])

    def test_preview_copy_edit_is_refreshed_when_preparing_snapshot(self):
        package, _ = self.create()
        guest = self.client()
        preview = self.bootstrap(guest)
        changed = self.admin.request('PUT', '/api/admin/packages/' + package['id'], {'title': '新的固定标题', 'copy': '新的固定文案，与本套三张照片保持绑定。'})
        self.assertEqual(changed.status, 200, changed.data)
        selected = self.prepare(guest, preview)
        self.assert_snapshot(selected, changed.data['package'])
        self.assertNotEqual(selected['copy'], preview['copy'])

    def test_fifteen_package_rotation_keeps_each_copy_with_its_own_three_photos(self):
        expected = {p['id']: p for p, _ in [self.create(i) for i in range(15)]}
        guest, seen = self.client(), set()
        selected = self.bootstrap(guest)
        for index in range(15):
            self.assertNotIn(selected['packageId'], seen)
            seen.add(selected['packageId'])
            self.assert_snapshot(selected, expected[selected['packageId']])
            if index < 14:
                result = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
                self.assertEqual(result.status, 200, result.data)
                selected = result.data['batch']
        again = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
        self.assertEqual(again.status, 200)
        self.assert_snapshot(again.data['batch'], expected[again.data['batch']['packageId']])
        self.assertEqual(self.packages()['stats'], {'available': 15, 'reserved': 0, 'used': 0, 'all': 15})

    def test_cancel_releases_whole_package_without_using_it(self):
        package, _ = self.create()
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest))
        cancelled = guest.request('POST', '/api/batch/cancel', {'batchId': selected['id']})
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(cancelled.data['batch']['status'], 'preview')
        self.assert_snapshot(cancelled.data['batch'], package)
        self.assertEqual(self.photos()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})

    def test_expired_reservation_recovers_the_same_complete_package(self):
        package, _ = self.create()
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest))
        self.sql('UPDATE batches SET expires=0 WHERE id=?', (selected['id'],))
        self.assertEqual(self.commit(guest, selected).status, 409)
        replacement = self.bootstrap(self.client())
        self.assertEqual(replacement['status'], 'preview')
        self.assert_snapshot(replacement, package)
        self.assertEqual(self.photos()['stats']['available'], 3)

    def test_commit_is_atomic_for_three_photos_and_retry_is_idempotent(self):
        package, _ = self.create()
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest))
        first = self.commit(guest, selected)
        second = self.commit(guest, selected, 'manual_confirm')
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first.data, second.data)
        used = self.bootstrap(guest)
        self.assert_snapshot(used, package)
        self.assertEqual({p['usedAt'] for p in used['photos']}, {first.data['usedAt']})
        self.assertEqual({p['method'] for p in used['photos']}, {'system_share'})
        self.assertIsNone(self.bootstrap(self.client()))

    def test_guest_photo_and_thumbnail_permissions_match_package_ownership(self):
        self.create()
        owner, stranger = self.client(), self.client()
        selected = self.bootstrap(owner)
        self.bootstrap(stranger)
        selected = self.prepare(owner, selected)
        for p in selected['photos']:
            for url in [p['url'], p['previewUrl']]:
                suffix = url.removeprefix(BASE)
                self.assertEqual(owner.request('GET', suffix).status, 200)
                self.assertEqual(stranger.request('GET', suffix).status, 404)
                self.assertEqual(self.client().request('GET', suffix).status, 401)
            self.assertEqual(self.client().request('GET', '/api/admin/image/' + p['id'] + '?variant=preview').status, 401)
        self.assertEqual(self.commit(stranger, selected).status, 409)
        self.assertEqual(stranger.request('POST', '/api/batch/cancel', {'batchId': selected['id']}).status, 409)

    def test_jpeg_thumbnail_does_not_change_original_and_missing_variant_falls_back(self):
        package, files = self.create()
        guest = self.client()
        selected = self.bootstrap(guest)
        for photo, (_, original, _) in zip(selected['photos'], files):
            preview = guest.request('GET', photo['previewUrl'].removeprefix(BASE))
            self.assertEqual(preview.status, 200)
            self.assertEqual(dict(preview.headers)['Content-Type'], 'image/jpeg')
            admin_preview = self.admin.request('GET', '/api/admin/image/' + photo['id'] + '?variant=preview')
            self.assertEqual(admin_preview.status, 200)
            self.assertEqual(dict(admin_preview.headers)['Content-Type'], 'image/jpeg')
            self.assertEqual(admin_preview.body, preview.body)
            self.assert_jpeg_decodes(preview.body)
            self.assertEqual(guest.request('GET', photo['url'].removeprefix(BASE)).body, original)
            self.assertEqual((self.data / 'images' / photo['id']).read_bytes(), original)
        photo = selected['photos'][0]
        thumbnail = self.data / 'thumbnails' / (photo['id'] + '.jpg')
        thumbnail.unlink()
        fallback = guest.request('GET', photo['previewUrl'].removeprefix(BASE))
        self.assertEqual(dict(fallback.headers)['Content-Type'], 'image/png')
        self.assertEqual(fallback.body, files[0][1])
        self.assertFalse(thumbnail.exists())
        self.assertEqual(self.packages()['stats']['available'], 1)
        self.assertEqual(self.photos()['stats']['used'], 0)

    def test_anonymous_admin_and_cross_site_writes_are_blocked(self):
        anonymous = self.client()
        self.assertEqual(anonymous.request('GET', '/api/admin/packages').status, 401)
        self.assertEqual(self.submit(actor=anonymous).status, 401)
        for origin in ['', 'null', 'https://unrelated.invalid']:
            result = self.admin.request('PUT', '/api/admin/packages/anything', {'copy': '不应写入'}, headers={'Origin': origin})
            self.assertEqual(result.status, 403)
        self.assertEqual(self.admin.request('GET', '/api/admin/packages', headers={'Sec-Fetch-Site': 'cross-site'}).status, 403)
        self.assert_empty()

    def test_stale_flow_versions_must_reload_and_cannot_claim(self):
        self.create()
        guest = self.client()
        selected = self.bootstrap(guest)
        for flow in ['', '1', '2']:
            for method, url, value in [('GET', '/api/bootstrap', None), ('POST', '/api/batch/prepare', {'batchId': selected['id']})]:
                result = guest.request(method, url, value, headers={'X-Aidog-Photo-Flow': flow})
                self.assertEqual(result.status, 409)
                self.assertIs(result.data['reloadRequired'], True)
        self.assertEqual(self.packages()['stats']['available'], 1)
        self.assertEqual(self.photos()['stats']['reserved'], 0)

    def test_available_package_edit_and_delete_manage_entire_group(self):
        package, _ = self.create()
        self.assertEqual(self.admin.request('DELETE', '/api/admin/photos/' + package['photos'][0]['id']).status, 409)
        changed = self.admin.request('PUT', '/api/admin/packages/' + package['id'], {'title': '可用套装的新标题', 'copy': '这段新文案仍对应原来的三张照片。'})
        self.assertEqual(changed.status, 200)
        self.assertEqual([p['id'] for p in changed.data['package']['photos']], [p['id'] for p in package['photos']])
        removed = self.admin.request('DELETE', '/api/admin/packages/' + package['id'])
        self.assertEqual(removed.status, 200)
        self.assertEqual(self.packages()['stats']['all'], 0)
        self.assertEqual(self.photos()['stats']['all'], 0)
        self.assertEqual(list((self.data / 'images').iterdir()), [])

    def test_package_pagination_and_filters_count_packages_not_photos(self):
        for i in range(13):
            self.create(i)
        first, second = self.packages(), self.packages(page=2)
        self.assertEqual(first['pageSize'], 12)
        self.assertEqual(first['total'], 13)
        self.assertEqual(len(first['packages']), 12)
        self.assertEqual(len(second['packages']), 1)
        self.assertFalse({p['id'] for p in first['packages']} & {p['id'] for p in second['packages']})
        self.assertEqual(self.packages(status='used')['total'], 0)

    def test_wrong_password_throttle_logout_and_cookie_kind_are_enforced(self):
        outsider = self.client()
        statuses = [outsider.request('POST', '/api/admin/login', {'password': 'wrong'}).status for _ in range(20)]
        self.assertEqual(statuses, [401] * 19 + [429])
        other = self.client()
        self.assertEqual(other.request('POST', '/api/admin/login', {'password': self.password}, headers={'X-Real-IP': '192.0.2.10'}).status, 200)
        guest = self.client()
        self.bootstrap(guest)
        guest.cookies['aidog_photos_admin'] = guest.cookies['aidog_photos_guest']
        self.assertEqual(guest.request('GET', '/api/admin/packages').status, 401)
        old = dict(self.admin.cookies)
        self.assertEqual(self.admin.request('POST', '/api/admin/logout', {}).status, 200)
        self.assertEqual(Client(self.port, self.origin, old).request('GET', '/api/admin/packages').status, 401)

    def test_platform_links_reject_untrusted_domains_atomically(self):
        original = self.packages()
        links = {p['id']: p['url'] for p in original['platforms']}
        for bad in ['https://meituan.com.evil.invalid/', 'http://meituan.com/', 'https://user@meituan.com/', 'javascript:alert(1)']:
            result = self.admin.request('PUT', '/api/admin/settings', {'storeName': '不得保存', 'platforms': {**links, 'meituan': bad}})
            self.assertEqual(result.status, 400)
        self.assertEqual(self.packages()['storeName'], original['storeName'])

    def test_oversized_multipart_rejected_before_body_read(self):
        result = self.admin.request('POST', '/api/admin/packages', raw=b'', headers={
            'Content-Type': 'multipart/form-data; boundary=never', 'Content-Length': str(36 * 1024 * 1024 + 65537)})
        self.assertEqual(result.status, 413)
        self.assert_empty()

    def test_concurrent_same_upload_id_creates_one_package_only(self):
        files, upload_id = self.files(), secrets.token_hex(12)
        actors = [self.admin.fork() for _ in range(4)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda actor: self.submit(files, upload_id=upload_id, actor=actor), actors))
        self.assertEqual([r.status for r in results], [200] * 4)
        self.assertEqual(len({r.data['id'] for r in results}), 1)
        self.assertEqual(sum(not r.data['duplicate'] for r in results), 1)
        self.assertEqual(self.packages()['stats']['all'], 1)

    def legacy_photos(self, count, used=0):
        ids = [secrets.token_hex(16) for _ in range(count)]
        stamp = int(time.time() * 1000) - 600000
        with sqlite3.connect(self.data / 'photos.sqlite3') as c:
            for index, pid in enumerate(ids):
                raw = synthetic_png()
                (self.data / 'images' / pid).write_bytes(raw)
                is_used = index < used
                c.execute('INSERT INTO photos(id,name,mime,digest,size,status,created,used_at,method,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?)', (pid, f'旧合成图-{index}.png', 'image/png', hashlib.sha256(raw).hexdigest(), len(raw), 'used' if is_used else 'available', stamp + index, stamp if is_used else None, 'manual_confirm' if is_used else None, 'legacy-batch' if is_used else None))
        return ids, stamp

    def test_old_orphan_photos_without_copy_are_not_exposed_as_packages(self):
        self.legacy_photos(3)
        self.assertEqual(self.photos()['stats']['available'], 3)
        self.assertEqual(self.packages()['stats']['all'], 0)
        self.assertIsNone(self.bootstrap(self.client()))

    def test_initial_manifest_migration_preserves_45_originals_and_old_batch_once(self):
        self.stop_server()
        ids, used_at = self.legacy_photos(45, used=3)
        stamp = int(time.time() * 1000)
        with sqlite3.connect(self.data / 'photos.sqlite3') as c:
            c.execute("INSERT INTO batches(id,session_id,status,expires,created,used_at,method,prepared_at) VALUES('legacy-batch','legacy-session','used',?,?,?,?,?)", (stamp + 100000, used_at - 1000, used_at, 'manual_confirm', used_at - 500))
            for position, pid in enumerate(ids[:3]):
                c.execute('INSERT INTO batch_photos VALUES(?,?,?)', ('legacy-batch', pid, position))
            c.execute('DROP INDEX batch_package_session')
            for column in ['package_id', 'copy_snapshot', 'title_snapshot']:
                c.execute('ALTER TABLE batches DROP COLUMN ' + column)
            c.execute('DROP TABLE package_photos')
            c.execute('DROP TABLE packages')
        before_photos = self.sql('SELECT * FROM photos ORDER BY id')
        before_batch = self.sql("SELECT * FROM batches WHERE id='legacy-batch'")[0]
        media = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.data / 'images').iterdir()}
        manifest = {'packages': [{'key': f'initial-{i + 1:02d}', 'title': f'旧固定第{i + 1}套', 'body': f'第{i + 1}套固定参考文案。', 'photoIds': ids[i * 3:i * 3 + 3]} for i in range(15)]}
        (self.home / 'initial-packages.json').write_text(json.dumps(manifest, ensure_ascii=False))
        self.start_server()
        self.assertEqual(self.packages()['stats'], {'available': 14, 'reserved': 0, 'used': 1, 'all': 15})
        self.assertEqual(self.photos()['stats'], {'available': 42, 'reserved': 0, 'used': 3, 'all': 45})
        for _ in range(2):
            self.assertEqual(self.sql('SELECT * FROM photos ORDER BY id'), before_photos)
            current_batch = self.sql("SELECT * FROM batches WHERE id='legacy-batch'")[0]
            self.assertEqual({key: current_batch[key] for key in before_batch}, before_batch)
            self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.data / 'images').iterdir()}, media)
            self.assertEqual(len(self.sql('SELECT * FROM packages')), 15)
            self.assertEqual(len(self.sql('SELECT * FROM package_photos')), 45)
            self.restart()
        first = self.packages(status='used')['packages'][0]
        self.assertEqual(first['id'], 'initial-01')
        self.assertEqual(first['usedAt'], used_at)
        self.assertEqual([p['id'] for p in first['photos']], ids[:3])


if __name__ == '__main__':
    unittest.main(verbosity=2)
