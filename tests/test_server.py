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
        supplied.setdefault('X-Aidog-Photo-Flow', '2')
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
        self.temp = tempfile.TemporaryDirectory(prefix='aidog-photo-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        config = self.home / 'config.json'
        self.password = secrets.token_urlsafe(22)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.scrypt(self.password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()
        config.write_text(json.dumps({'salt': salt.hex(), 'passwordHash': password_hash}))
        config.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.origin = f'http://localhost:{self.port}'
        env = {**os.environ, 'AIDOG_PHOTOS_BASE': BASE, 'AIDOG_PHOTOS_ORIGIN': self.origin,
               'AIDOG_PHOTOS_DATA': str(self.data), 'AIDOG_PHOTOS_CONFIG': str(config),
               'AIDOG_PHOTOS_PORT': str(self.port), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env = env
        self.log = (self.home / 'server.log').open('w+')
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([sys.executable, str(PROJECT / 'server.py')], cwd=PROJECT,
                                        env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_server)
        self.admin = self.client()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail('Local server exited: ' + self.log.read())
            try:
                if self.admin.request('GET', '/api/health').status == 200:
                    break
            except (OSError, http.client.HTTPException):
                time.sleep(0.025)
        else:
            self.fail('Local server did not start')
        self.assertEqual(self.admin.request('POST', '/api/admin/login', {'password': self.password}).status, 200)

    def stop_server(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)

    def client(self):
        return Client(self.port, self.origin)

    def upload(self, count, client=None):
        actor = client or self.admin
        results = []
        for index in range(count):
            content = synthetic_png()
            response = actor.request('POST', '/api/admin/photos', raw=content,
                                     headers={'Content-Type': 'image/png', 'X-File-Name': quote(f'合成照片-{index}.png')})
            self.assertEqual(response.status, 200, response.data)
            self.assertFalse(response.data['duplicate'])
            results.append((response.data['id'], content))
        return results

    def dashboard(self):
        response = self.admin.request('GET', '/api/admin/photos')
        self.assertEqual(response.status, 200, response.data)
        return response.data

    def bootstrap(self, client):
        response = client.request('GET', '/api/bootstrap')
        self.assertEqual(response.status, 200, response.data)
        return response.data

    def commit(self, client, batch_id, method='system_share'):
        return client.request('POST', '/api/batch/commit', {'batchId': batch_id, 'method': method})

    def prepare(self, client, batch_id):
        response = client.request('POST', '/api/batch/prepare', {'batchId': batch_id})
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['batch']['status'], 'reserved')
        self.assertEqual(len(response.data['batch']['photos']), 3)
        return response.data['batch']

    def restart_server(self):
        self.stop_server()
        self.process = subprocess.Popen([sys.executable, str(PROJECT / 'server.py')], cwd=PROJECT,
                                        env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail('Local server exited on restart: ' + self.log.read())
            try:
                if self.admin.request('GET', '/api/health').status == 200:
                    return
            except (OSError, http.client.HTTPException):
                time.sleep(0.025)
        self.fail('Local server did not restart')

    def test_bulk_upload_preserves_unique_bytes_and_never_preuses(self):
        originals = dict(self.upload(9))
        dashboard = self.dashboard()
        self.assertEqual(dashboard['stats'], {'available': 9, 'reserved': 0, 'used': 0, 'all': 9})
        self.assertEqual({p['id'] for p in dashboard['photos']}, set(originals))
        for item in dashboard['photos']:
            self.assertIsNone(item['usedAt'])
            response = self.admin.request('GET', '/api/admin/image/' + item['id'])
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, originals[item['id']])

    def test_preview_and_repeated_bootstrap_do_not_mark_used(self):
        self.upload(6)
        guest = self.client()
        first = self.bootstrap(guest)['batch']
        self.assertEqual(first['status'], 'preview')
        self.assertEqual(len(first['photos']), 3)
        for item in first['photos']:
            response = guest.request('GET', '/api/image/' + item['id'])
            self.assertEqual(response.status, 200)
            self.assertTrue(response.body.startswith(b'\x89PNG'))
        again = self.bootstrap(guest)['batch']
        self.assertEqual(again['id'], first['id'])
        self.assertIsNone(again['usedAt'])
        self.assertTrue(all(p['usedAt'] is None for p in again['photos']))
        self.assertEqual(self.dashboard()['stats'], {'available': 6, 'reserved': 0, 'used': 0, 'all': 6})

    def test_concurrent_sessions_receive_disjoint_complete_groups(self):
        self.upload(24)
        guests = [self.client() for _ in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.bootstrap, guests))
        self.assertTrue(all(r['batch']['status'] == 'preview' for r in results))
        self.assertTrue(all(len(r['batch']['photos']) == 3 for r in results))
        self.assertEqual(self.dashboard()['stats'], {'available': 24, 'reserved': 0, 'used': 0, 'all': 24})
        pairs = list(zip(guests, [r['batch']['id'] for r in results]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            prepared = list(pool.map(lambda pair: self.prepare(*pair), pairs))
        photo_ids = []
        self.assertEqual(len({b['id'] for b in prepared}), 8)
        for selected in prepared:
            photo_ids.extend(p['id'] for p in selected['photos'])
        self.assertEqual(len(photo_ids), len(set(photo_ids)))
        self.assertEqual(self.dashboard()['stats']['reserved'], 24)
        self.assertIsNone(self.bootstrap(self.client())['batch'])

    def test_inventory_below_three_is_not_partially_reserved(self):
        self.upload(2)
        guest = self.client()
        self.assertIsNone(self.bootstrap(guest)['batch'])
        self.assertEqual(self.dashboard()['stats']['available'], 2)
        self.upload(1)
        self.assertEqual(len(self.bootstrap(guest)['batch']['photos']), 3)

    def test_many_preview_visitors_do_not_exhaust_a_six_photo_library(self):
        self.upload(6)
        guests = [self.client() for _ in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(self.bootstrap, guests))
        self.assertTrue(all(r['batch'] and r['batch']['status'] == 'preview' for r in results))
        self.assertTrue(all(len(r['batch']['photos']) == 3 for r in results))
        self.assertEqual(self.dashboard()['stats'], {'available': 6, 'reserved': 0, 'used': 0, 'all': 6})

    def test_stale_client_flow_must_reload_before_preview_or_renew(self):
        self.upload(3)
        guest = self.client()
        preview = self.bootstrap(guest)['batch']
        for stale in ['', '1']:
            for method, path, body in [('GET', '/api/bootstrap', None),
                                       ('POST', '/api/batch/renew', {'batchId': preview['id']})]:
                with self.subTest(flow=stale, path=path):
                    response = guest.request(method, path, body, headers={'X-Aidog-Photo-Flow': stale})
                    self.assertEqual(response.status, 409, response.data)
                    self.assertIs(response.data['reloadRequired'], True)
                    self.assertIn('刷新', response.data['error'])
                    self.assertEqual(dict(response.headers).get('Connection'), 'close')
        self.assertEqual(self.dashboard()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})
        self.assertEqual(self.bootstrap(guest)['batch']['status'], 'preview')

    def test_preview_cannot_commit_and_renew_does_not_lock_inventory(self):
        self.upload(3)
        guest = self.client()
        preview = self.bootstrap(guest)['batch']
        self.assertEqual(self.commit(guest, preview['id']).status, 409)
        renewed = guest.request('POST', '/api/batch/renew', {'batchId': preview['id']})
        self.assertEqual(renewed.status, 200, renewed.data)
        # A non-exclusive preview may keep its own page record alive, while no photo is locked.
        self.assertGreaterEqual(renewed.data['expiresAt'], preview['expiresAt'])
        self.assertEqual(self.dashboard()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})
        prepared = self.prepare(guest, preview['id'])
        short_deadline = int(time.time() * 1000) + 5000
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            connection.execute('UPDATE batches SET expires=? WHERE id=?', (short_deadline, prepared['id']))
        renewed = guest.request('POST', '/api/batch/renew', {'batchId': prepared['id']})
        self.assertEqual(renewed.status, 200, renewed.data)
        self.assertGreater(renewed.data['expiresAt'], short_deadline)
        self.assertEqual(self.dashboard()['stats']['reserved'], 3)

    def test_prepare_reselects_collision_and_returns_full_authorized_snapshot(self):
        self.upload(6)
        first, second = self.client(), self.client()
        preview_a, preview_b = self.bootstrap(first)['batch'], self.bootstrap(second)['batch']
        original_b_ids = {p['id'] for p in preview_b['photos']}
        self.assertEqual({p['id'] for p in preview_a['photos']}, original_b_ids)
        chosen_a = self.prepare(first, preview_a['id'])
        for pid in original_b_ids:
            self.assertEqual(second.request('GET', '/api/image/' + pid).status, 404)
        chosen_b = self.prepare(second, preview_b['id'])
        self.assertFalse({p['id'] for p in chosen_a['photos']} & {p['id'] for p in chosen_b['photos']})
        for photo in chosen_b['photos']:
            self.assertEqual(second.request('GET', '/api/image/' + photo['id']).status, 200)
            self.assertEqual(first.request('GET', '/api/image/' + photo['id']).status, 404)
        self.assertEqual(self.dashboard()['stats'], {'available': 0, 'reserved': 6, 'used': 0, 'all': 6})

    def test_prepare_conflict_without_another_group_cannot_duplicate_delivery(self):
        self.upload(3)
        first, second = self.client(), self.client()
        a, b = self.bootstrap(first)['batch'], self.bootstrap(second)['batch']
        a = self.prepare(first, a['id'])
        refused = second.request('POST', '/api/batch/prepare', {'batchId': b['id']})
        self.assertEqual(refused.status, 409, refused.data)
        self.assertIn('换一组', refused.data['error'])
        self.assertEqual(self.commit(second, b['id']).status, 409)
        self.assertEqual(self.commit(first, a['id']).status, 200)
        for p in a['photos']:
            self.assertEqual(second.request('GET', '/api/image/' + p['id']).status, 404)
        self.assertEqual(self.dashboard()['stats'], {'available': 0, 'reserved': 0, 'used': 3, 'all': 3})

    def test_releasing_prepared_group_returns_all_photos_to_preview_pool(self):
        self.upload(6)
        guest = self.client()
        selected = self.prepare(guest, self.bootstrap(guest)['batch']['id'])
        response = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['batch']['status'], 'preview')
        self.assertFalse({p['id'] for p in response.data['batch']['photos']} & {p['id'] for p in selected['photos']})
        self.assertEqual(self.commit(guest, selected['id']).status, 409)
        self.assertEqual(self.dashboard()['stats'], {'available': 6, 'reserved': 0, 'used': 0, 'all': 6})

    def test_single_group_can_be_previewed_again_after_switch_without_being_used(self):
        self.upload(3)
        guest = self.client()
        selected = self.bootstrap(guest)['batch']
        response = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['batch']['status'], 'preview')
        self.assertNotEqual(response.data['batch']['id'], selected['id'])
        self.assertEqual({p['id'] for p in response.data['batch']['photos']}, {p['id'] for p in selected['photos']})
        self.assertEqual(self.dashboard()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})

    def test_rotation_visits_all_twelve_photos_before_repeating_without_consumption(self):
        self.upload(12)
        guest = self.client()
        selected = self.bootstrap(guest)['batch']
        seen = set()
        for index in range(4):
            self.assertEqual(selected['status'], 'preview')
            current = {photo['id'] for photo in selected['photos']}
            self.assertEqual(len(current), 3)
            self.assertFalse(current & seen, 'The first four groups must show twelve distinct photos')
            seen.update(current)
            if index < 3:
                response = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
                self.assertEqual(response.status, 200, response.data)
                selected = response.data['batch']
        self.assertEqual(len(seen), 12)
        fifth = guest.request('POST', '/api/batch/release', {'batchId': selected['id']})
        self.assertEqual(fifth.status, 200, fifth.data)
        self.assertEqual(fifth.data['batch']['status'], 'preview')
        self.assertEqual(len(fifth.data['batch']['photos']), 3)
        self.assertTrue({photo['id'] for photo in fifth.data['batch']['photos']} <= seen)
        dashboard = self.dashboard()
        self.assertEqual(dashboard['stats'], {'available': 12, 'reserved': 0, 'used': 0, 'all': 12})
        self.assertTrue(all(photo['usedAt'] is None for photo in dashboard['photos']))

    def test_startup_migrates_legacy_preview_reservations_without_restoring_used(self):
        self.upload(6)
        used_guest, legacy_guest = self.client(), self.client()
        used = self.prepare(used_guest, self.bootstrap(used_guest)['batch']['id'])
        receipt = self.commit(used_guest, used['id'])
        self.assertEqual(receipt.status, 200, receipt.data)
        legacy = self.bootstrap(legacy_guest)['batch']
        # Recreate the previous release's column layout and automatic preview reservation.
        # All writes are confined to this test's temporary database.
        self.stop_server()
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            connection.execute("UPDATE batches SET status='reserved' WHERE id=?", (legacy['id'],))
            for photo in legacy['photos']:
                connection.execute("UPDATE photos SET status='reserved',batch_id=? WHERE id=?", (legacy['id'], photo['id']))
            connection.execute('ALTER TABLE batches DROP COLUMN prepared_at')
        self.restart_server()
        dashboard = self.dashboard()
        self.assertEqual(dashboard['stats'], {'available': 3, 'reserved': 0, 'used': 3, 'all': 6})
        self.assertEqual({p['usedAt'] for p in dashboard['photos'] if p['status'] == 'used'}, {receipt.data['usedAt']})
        self.assertEqual(self.bootstrap(legacy_guest)['batch']['status'], 'preview')
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            self.assertEqual(connection.execute('SELECT status FROM batches WHERE id=?', (legacy['id'],)).fetchone()[0], 'preview')
            self.assertIn('prepared_at', {r[1] for r in connection.execute('PRAGMA table_info(batches)')})

    def test_commit_is_idempotent_and_timestamps_match_all_three(self):
        self.upload(6)
        guest = self.client()
        preview = self.bootstrap(guest)['batch']
        reserved = self.prepare(guest, preview['id'])
        first = self.commit(guest, reserved['id'])
        self.assertEqual(first.status, 200, first.data)
        self.assertEqual(first.data['status'], 'used')
        self.assertGreater(first.data['usedAt'], 0)
        second = self.commit(guest, reserved['id'], 'manual_confirm')
        self.assertEqual(second.status, 200, second.data)
        self.assertEqual(second.data, first.data)
        batch = self.bootstrap(guest)['batch']
        self.assertEqual(batch['id'], reserved['id'])
        self.assertEqual(batch['usedAt'], first.data['usedAt'])
        self.assertEqual({p['usedAt'] for p in batch['photos']}, {first.data['usedAt']})
        self.assertEqual({p['status'] for p in batch['photos']}, {'used'})
        self.assertEqual({p['method'] for p in batch['photos']}, {'system_share'})
        self.assertEqual(self.dashboard()['stats']['used'], 3)

    def test_other_session_cannot_read_commit_or_release_another_group(self):
        self.upload(6)
        owner, stranger = self.client(), self.client()
        owned = self.bootstrap(owner)['batch']
        other = self.bootstrap(stranger)['batch']
        self.assertNotEqual(owned['id'], other['id'])
        owned = self.prepare(owner, owned['id'])
        for photo in owned['photos']:
            self.assertEqual(stranger.request('GET', '/api/image/' + photo['id']).status, 404)
            self.assertEqual(self.client().request('GET', '/api/image/' + photo['id']).status, 401)
        self.assertEqual(self.commit(stranger, owned['id']).status, 409)
        self.assertEqual(stranger.request('POST', '/api/batch/prepare', {'batchId': owned['id']}).status, 409)
        self.assertEqual(stranger.request('POST', '/api/batch/release', {'batchId': owned['id']}).status, 409)
        self.assertEqual(self.dashboard()['stats']['used'], 0)
        self.assertEqual(self.commit(owner, owned['id']).status, 200)
        self.assertEqual(self.commit(stranger, owned['id']).status, 409)

    def test_cancel_release_does_not_consume_or_write_used_time(self):
        self.upload(6)
        guest = self.client()
        first = self.bootstrap(guest)['batch']
        response = guest.request('POST', '/api/batch/release', {'batchId': first['id']})
        self.assertEqual(response.status, 200, response.data)
        self.assertEqual(response.data['batch']['status'], 'preview')
        self.assertNotEqual(response.data['batch']['id'], first['id'])
        self.assertFalse({p['id'] for p in first['photos']} &
                         {p['id'] for p in response.data['batch']['photos']})
        dashboard = self.dashboard()
        self.assertEqual(dashboard['stats'], {'available': 6, 'reserved': 0, 'used': 0, 'all': 6})
        self.assertTrue(all(p['usedAt'] is None for p in dashboard['photos']))
        self.assertEqual(self.commit(guest, first['id']).status, 409)

    def test_used_photos_are_never_allocated_to_new_sessions(self):
        self.upload(6)
        first, second = self.client(), self.client()
        old = self.bootstrap(first)['batch']
        old = self.prepare(first, old['id'])
        self.assertEqual(self.commit(first, old['id']).status, 200)
        new = self.bootstrap(second)['batch']
        new = self.prepare(second, new['id'])
        self.assertFalse({p['id'] for p in old['photos']} & {p['id'] for p in new['photos']})
        self.assertEqual(self.commit(second, new['id']).status, 200)
        self.assertIsNone(self.bootstrap(self.client())['batch'])
        self.assertEqual(self.dashboard()['stats']['used'], 6)

    def test_concurrent_duplicate_uploads_create_only_one_inventory_item(self):
        content = synthetic_png()
        actors = [self.admin.fork() for _ in range(6)]
        def upload_same(actor):
            return actor.request('POST', '/api/admin/photos', raw=content, headers={'Content-Type': 'image/png'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(upload_same, actors))
        self.assertEqual([r.status for r in results], [200] * 6)
        self.assertEqual(len({r.data['id'] for r in results}), 1)
        self.assertEqual(sum(not r.data['duplicate'] for r in results), 1)
        self.assertEqual(self.dashboard()['stats']['all'], 1)

    def test_duplicate_digest_does_not_reenter_used_inventory(self):
        uploaded = self.upload(3)
        guest = self.client()
        owned = self.bootstrap(guest)['batch']
        owned = self.prepare(guest, owned['id'])
        self.assertEqual(self.commit(guest, owned['id']).status, 200)
        for pid, content in uploaded:
            duplicate = self.admin.request('POST', '/api/admin/photos', raw=content,
                                           headers={'Content-Type': 'image/png', 'X-File-Name': 'renamed.png'})
            self.assertEqual(duplicate.status, 200)
            self.assertEqual(duplicate.data, {'id': pid, 'duplicate': True})
        self.assertEqual(self.dashboard()['stats'], {'available': 0, 'reserved': 0, 'used': 3, 'all': 3})
        self.assertIsNone(self.bootstrap(self.client())['batch'])

    def test_wrong_password_is_rejected_and_attempts_are_throttled(self):
        outsider = self.client()
        # The successful setup login already used one of this IP's 20 attempts.
        statuses = [outsider.request('POST', '/api/admin/login', {'password': 'wrong-' + secrets.token_hex(4)}).status
                    for _ in range(20)]
        self.assertEqual(statuses[:-1], [401] * 19)
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(outsider.request('POST', '/api/admin/login', {'password': self.password}).status, 429)
        self.assertNotIn('aidog_photos_admin', outsider.cookies)
        # The loopback service receives per-visitor IPs from its trusted local nginx proxy.
        another_ip = self.client()
        self.assertEqual(another_ip.request('POST', '/api/admin/login', {'password': self.password},
                                            headers={'X-Real-IP': '192.0.2.20'}).status, 200)
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            connection.execute('UPDATE limits SET reset=0')
        self.assertEqual(outsider.request('POST', '/api/admin/login', {'password': self.password}).status, 200)

    def test_anonymous_admin_and_cross_site_writes_are_blocked(self):
        anonymous = self.client()
        self.assertEqual(anonymous.request('GET', '/api/admin/photos').status, 401)
        self.assertEqual(anonymous.request('GET', '/api/admin/session').status, 401)
        for headers in ({'Origin': 'https://unrelated.invalid'}, {'Origin': 'null'},
                        {'Origin': ''}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.admin.request('POST', '/api/admin/photos', raw=synthetic_png(),
                                               headers={'Content-Type': 'image/png', **headers}).status, 403)
        self.assertEqual(self.admin.request('GET', '/api/admin/photos', headers={'Sec-Fetch-Site': 'cross-site'}).status, 403)
        self.assertEqual(self.dashboard()['stats']['all'], 0)

    def test_untrusted_platform_settings_are_atomic_and_rejected(self):
        previous = self.dashboard()
        good = {p['id']: p['url'] for p in previous['platforms']}
        for malicious in ['https://meituan.com.evil.invalid/a', 'http://www.meituan.com/a',
                          'https://user@meituan.com/a', 'https://www.meituan.com:444/a',
                          'https://www.meituan.com\\@evil.invalid/a', 'https://meituan.com./a',
                          'https://www.douyin.com/a', 'javascript:alert(1)',
                          'https://www.meituan.com/a\nb']:
            with self.subTest(url=malicious):
                response = self.admin.request('PUT', '/api/admin/settings',
                                              {'storeName': 'must not persist', 'platforms': {**good, 'meituan': malicious}})
                self.assertEqual(response.status, 400, response.data)
        current = self.dashboard()
        self.assertEqual(current['storeName'], previous['storeName'])
        self.assertEqual(current['platforms'], previous['platforms'])

    def test_valid_settings_accept_platform_official_subdomains(self):
        links = {'meituan': 'https://dpurl.cn/test', 'dianping': 'https://m.dianping.com/shop/test',
                 'douyin': 'https://v.douyin.com/test/'}
        response = self.admin.request('PUT', '/api/admin/settings', {'storeName': '合成测试门店', 'platforms': links})
        self.assertEqual(response.status, 200, response.data)
        current = self.dashboard()
        self.assertEqual(current['storeName'], '合成测试门店')
        self.assertEqual({p['id']: p['url'] for p in current['platforms']}, links)

    def test_invalid_image_mime_and_file_bytes_are_rejected(self):
        for mime, content in [('image/png', b'not a picture'), ('text/html', synthetic_png()),
                              ('image/jpeg', synthetic_png()), ('image/png', b'')]:
            with self.subTest(mime=mime, content_size=len(content)):
                response = self.admin.request('POST', '/api/admin/photos', raw=content, headers={'Content-Type': mime})
                self.assertEqual(response.status, 400, response.data)
        self.assertEqual(self.dashboard()['stats']['all'], 0)

    def test_truncated_magic_only_png_is_not_accepted_as_a_photo(self):
        response = self.admin.request('POST', '/api/admin/photos', raw=b'\x89PNG\r\n\x1a\n',
                                      headers={'Content-Type': 'image/png'})
        self.assertEqual(response.status, 400, response.data)
        self.assertEqual(self.dashboard()['stats']['all'], 0)

    def test_corrupt_png_crc_and_fake_jpeg_webp_are_rejected(self):
        png = bytearray(synthetic_png())
        png[29] ^= 1  # Damage IHDR CRC, retaining all normal chunk lengths.
        for mime, content in [('image/png', bytes(png)), ('image/png', synthetic_png()[:-5]),
                              ('image/jpeg', b'\xff\xd8\xff\xd9'),
                              ('image/jpeg', b'\xff\xd8\xff\xe0\x00\x02\xff\xd9'),
                              ('image/webp', b'RIFF\x04\x00\x00\x00WEBP'),
                              ('image/webp', b'RIFF\xff\xff\xff\x7fWEBPVP8 fake')]:
            with self.subTest(mime=mime, content_size=len(content)):
                response = self.admin.request('POST', '/api/admin/photos', raw=content,
                                              headers={'Content-Type': mime})
                self.assertEqual(response.status, 400, response.data)
        self.assertEqual(self.dashboard()['stats']['all'], 0)

    def test_cookie_is_http_only_scoped_and_not_a_plaintext_database_token(self):
        actor = self.client()
        login = actor.request('POST', '/api/admin/login', {'password': self.password})
        self.assertEqual(login.status, 200)
        cookie = next(val for key, val in login.headers if key.lower() == 'set-cookie')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('Path=/aidog-photos/', cookie)
        token = actor.cookies['aidog_photos_admin']
        self.assertNotIn(token, login.body.decode())
        self.assertEqual(len(token), 64)
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            sessions = [r[0] for r in connection.execute("SELECT id FROM sessions WHERE kind='admin'")]
        self.assertNotIn(token, sessions)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), sessions)

    def test_expired_admin_session_and_guest_cookie_cannot_authorize_admin(self):
        guest = self.client()
        self.bootstrap(guest)
        self.assertEqual(guest.request('GET', '/api/admin/session').status, 401)
        guest_token_as_admin = self.client()
        guest_token_as_admin.cookies['aidog_photos_admin'] = guest.cookies['aidog_photos_guest']
        self.assertEqual(guest_token_as_admin.request('GET', '/api/admin/session').status, 401)
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            connection.execute("UPDATE sessions SET expires=0 WHERE kind='admin'")
        self.assertEqual(self.admin.request('GET', '/api/admin/photos').status, 401)

    def test_path_traversal_cannot_read_source_or_external_files(self):
        guest = self.client()
        canonical = guest.request('GET', '/admin/')
        self.assertEqual(canonical.status, 308)
        self.assertEqual(dict(canonical.headers).get('Location'), BASE + '/admin')
        for path in ['/../server.py', '/%2e%2e/server.py', '/..%2fserver.py', '/%2fetc/passwd']:
            with self.subTest(path=path):
                response = guest.request('GET', path)
                self.assertEqual(response.status, 404, response.data)

    def test_admin_pagination_has_consistent_totals_without_duplicates(self):
        self.upload(64)
        first = self.dashboard()
        second = self.admin.request('GET', '/api/admin/photos?page=2').data
        self.assertEqual(first['pageSize'], 60)
        self.assertEqual(first['total'], 64)
        self.assertEqual(second['total'], 64)
        self.assertEqual(len(first['photos']), 60)
        self.assertEqual(len(second['photos']), 4)
        self.assertFalse({p['id'] for p in first['photos']} & {p['id'] for p in second['photos']})

    def test_oversized_content_length_rejected_without_reading_body(self):
        response = self.admin.request('POST', '/api/admin/photos', raw=b'',
                                      headers={'Content-Type': 'image/png', 'Content-Length': str(12 * 1024 * 1024 + 1)})
        self.assertEqual(response.status, 413, response.data)
        self.assertEqual(self.dashboard()['stats']['all'], 0)

    def test_expired_batch_commit_rejected_and_photos_recovered(self):
        self.upload(3)
        guest = self.client()
        expired = self.bootstrap(guest)['batch']
        expired = self.prepare(guest, expired['id'])
        # Change only the disposable test database's clock boundary; no sleep or production writes.
        with sqlite3.connect(self.data / 'photos.sqlite3') as connection:
            connection.execute('UPDATE batches SET expires=? WHERE id=?', (int(time.time() * 1000) - 1000, expired['id']))
        self.assertEqual(self.commit(guest, expired['id']).status, 409)
        replacement = self.bootstrap(self.client())['batch']
        self.assertIsNotNone(replacement)
        self.assertNotEqual(replacement['id'], expired['id'])
        self.assertEqual({p['id'] for p in replacement['photos']}, {p['id'] for p in expired['photos']})
        self.assertEqual(guest.request('GET', '/api/image/' + expired['photos'][0]['id']).status, 404)
        self.assertEqual(replacement['status'], 'preview')
        self.assertEqual(self.dashboard()['stats'], {'available': 3, 'reserved': 0, 'used': 0, 'all': 3})

    def test_reserved_and_used_photos_cannot_be_deleted(self):
        self.upload(3)
        guest = self.client()
        selected = self.bootstrap(guest)['batch']
        selected = self.prepare(guest, selected['id'])
        pid = selected['photos'][0]['id']
        self.assertEqual(self.admin.request('DELETE', '/api/admin/photos/' + pid).status, 409)
        self.assertEqual(self.commit(guest, selected['id']).status, 200)
        self.assertEqual(self.admin.request('DELETE', '/api/admin/photos/' + pid).status, 409)
        self.assertEqual(self.dashboard()['stats']['used'], 3)

    def test_logout_revokes_cookie_session(self):
        cookie_before = dict(self.admin.cookies)
        self.assertEqual(self.admin.request('POST', '/api/admin/logout', {}).status, 200)
        self.assertNotIn('aidog_photos_admin', self.admin.cookies)
        stolen_old = Client(self.port, self.origin, cookie_before)
        self.assertEqual(stolen_old.request('GET', '/api/admin/session').status, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
