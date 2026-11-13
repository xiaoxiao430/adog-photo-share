"""Static HTTP compression and cache checks against an isolated local server."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from test_server import Client, PROJECT


class StaticHttpIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='aidog-static-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.home = Path(cls.temp.name)
        cls.password = secrets.token_urlsafe(20)
        salt = secrets.token_bytes(16)
        config = cls.home / 'config.json'
        config.write_text(json.dumps({'salt': salt.hex(), 'passwordHash': hashlib.scrypt(
            cls.password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()}))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            cls.port = probe.getsockname()[1]
        cls.origin = f'http://localhost:{cls.port}'
        env = {**os.environ, 'AIDOG_PHOTOS_BASE': '/aidog-photos', 'AIDOG_PHOTOS_ORIGIN': cls.origin,
               'AIDOG_PHOTOS_DATA': str(cls.home / 'data'), 'AIDOG_PHOTOS_CONFIG': str(config),
               'AIDOG_PHOTOS_INITIAL_PACKAGES': str(cls.home / 'initial-packages.json'),
               'AIDOG_PHOTOS_PORT': str(cls.port), 'PYTHONDONTWRITEBYTECODE': '1'}
        cls.log = (cls.home / 'server.log').open('w+')
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([sys.executable, str(PROJECT / 'server.py')], cwd=PROJECT,
                                       env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop_server)
        cls.client = Client(cls.port, cls.origin)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError('Disposable static server exited')
            try:
                if cls.client.request('GET', '/api/health').status == 200:
                    return
            except OSError:
                time.sleep(0.025)
        raise RuntimeError('Disposable static server did not start')

    @classmethod
    def stop_server(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=4)

    def request(self, path, **headers):
        return self.client.request('GET', path, headers=headers)

    def test_html_javascript_and_css_negotiate_real_gzip(self):
        for path, file in [('/', 'index.html'), ('/guest.js', 'guest.js'), ('/app.css', 'app.css')]:
            with self.subTest(path=path):
                response = self.request(path, **{'Accept-Encoding': 'gzip'})
                headers = dict(response.headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(headers['Content-Encoding'], 'gzip')
                self.assertEqual(headers['Vary'], 'Accept-Encoding')
                self.assertEqual(headers['Cache-Control'], 'no-cache')
                self.assertEqual(int(headers['Content-Length']), len(response.body))
                self.assertEqual(gzip.decompress(response.body), (PROJECT / 'public' / file).read_bytes())

    def test_explicit_zero_or_invalid_gzip_quality_never_compresses(self):
        rejected = ['gzip;q=0', 'gzip;q=0.000, *;q=1', 'GZIP;Q=0', 'gzip;q=bogus',
                    'gzip;q=1.1', 'gzip;q=0.0000', 'gzip;q=1;q=0',
                    'gzip;q=0, gzip;q=1', 'br', 'xgzip']
        for encoding in rejected:
            with self.subTest(encoding=encoding):
                response = self.request('/guest.js', **{'Accept-Encoding': encoding})
                self.assertEqual(response.status, 200)
                self.assertNotIn('Content-Encoding', dict(response.headers))
                self.assertEqual(dict(response.headers)['Vary'], 'Accept-Encoding')
                self.assertEqual(response.body, (PROJECT / 'public/guest.js').read_bytes())

    def test_nonzero_gzip_and_wildcard_quality_allow_compression(self):
        for encoding in ['gzip', 'gzip;q=0.1', 'br;q=1, *;q=0.6', 'GZIP;Q=1.000', '*', 'gzip;q=1, *;q=0']:
            with self.subTest(encoding=encoding):
                response = self.request('/app.css', **{'Accept-Encoding': encoding})
                self.assertEqual(dict(response.headers)['Content-Encoding'], 'gzip')
                self.assertEqual(gzip.decompress(response.body), (PROJECT / 'public/app.css').read_bytes())

    def test_etag_conditional_get_has_no_304_body_or_incorrect_length(self):
        first = self.request('/guest.js', **{'Accept-Encoding': 'identity'})
        etag = dict(first.headers)['ETag']
        self.assertEqual(etag, '"' + hashlib.sha256(first.body).hexdigest() + '"')
        cached = self.request('/guest.js', **{'Accept-Encoding': 'identity', 'If-None-Match': etag})
        self.assertEqual(cached.status, 304)
        self.assertEqual(cached.body, b'')
        headers = dict(cached.headers)
        self.assertEqual(headers['ETag'], etag)
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        self.assertNotIn('Content-Length', headers)
        self.assertNotIn('Content-Type', headers)

    def test_gzip_and_identity_etags_never_validate_the_wrong_representation(self):
        plain = self.request('/guest.js', **{'Accept-Encoding': 'identity'})
        zipped = self.request('/guest.js', **{'Accept-Encoding': 'gzip'})
        a, b = dict(plain.headers)['ETag'], dict(zipped.headers)['ETag']
        self.assertNotEqual(a, b)
        cached = self.request('/guest.js', **{'Accept-Encoding': 'gzip', 'If-None-Match': b})
        self.assertEqual(cached.status, 304)
        self.assertEqual(cached.body, b'')
        self.assertEqual(self.request('/guest.js', **{'Accept-Encoding': 'identity', 'If-None-Match': b}).status, 200)
        self.assertEqual(self.request('/guest.js', **{'Accept-Encoding': 'gzip', 'If-None-Match': a}).status, 200)

    def test_get_weak_etags_lists_and_wildcard_match_but_unknown_tag_does_not(self):
        first = self.request('/app.css', **{'Accept-Encoding': 'identity'})
        etag = dict(first.headers)['ETag']
        for tag in ['W/' + etag, '"other", W/' + etag, '*']:
            with self.subTest(tag=tag):
                self.assertEqual(self.request('/app.css', **{'Accept-Encoding': 'identity', 'If-None-Match': tag}).status, 304)
        self.assertEqual(self.request('/app.css', **{'If-None-Match': '"other"'}).status, 200)

    def test_query_does_not_change_static_cache_classification_or_asset_bytes(self):
        font = next((PROJECT / 'public/fonts').glob('yahei-ui-*.woff2'))
        response = self.request('/fonts/' + font.name + '?check=/api/anything.js', **{'Accept-Encoding': 'gzip'})
        headers = dict(response.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')
        self.assertNotIn('Content-Encoding', headers)
        self.assertEqual(response.body, font.read_bytes())
        logo = self.request('/brand/adog-logo.png?v=2', **{'Accept-Encoding': 'gzip'})
        self.assertEqual(dict(logo.headers)['Cache-Control'], 'public, max-age=86400')
        self.assertNotIn('Content-Encoding', dict(logo.headers))
        self.assertEqual(logo.body, (PROJECT / 'public/brand/adog-logo.png').read_bytes())
        script = self.request('/guest.js?v=3')
        self.assertEqual(dict(script.headers)['Cache-Control'], 'no-cache')

    def test_unhashed_font_revalidates_daily_and_is_never_gzipped(self):
        response = self.request('/fonts/yahei-ui.woff2?version=2', **{'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status, 200)
        self.assertEqual(dict(response.headers)['Cache-Control'], 'public, max-age=86400')
        self.assertNotIn('Content-Encoding', dict(response.headers))

    def test_missing_assets_do_not_get_static_cache_or_validation_headers(self):
        for path in ['/does-not-exist.png', '/does-not-exist.woff2?v=3', '/does-not-exist.css']:
            with self.subTest(path=path):
                response = self.request(path, **{'Accept-Encoding': 'gzip', 'If-None-Match': '*'})
                self.assertEqual(response.status, 404)
                headers = dict(response.headers)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('ETag', headers)
                self.assertNotIn('Content-Encoding', headers)

    def test_api_success_and_errors_stay_uncached_and_uncompressed(self):
        for path, status in [('/api/health', 200), ('/api/admin/packages', 401), ('/api/not-found.png', 404)]:
            with self.subTest(path=path):
                response = self.request(path, **{'Accept-Encoding': 'gzip', 'If-None-Match': '*'})
                self.assertEqual(response.status, status)
                headers = dict(response.headers)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('ETag', headers)
                self.assertNotIn('Content-Encoding', headers)
                self.assertIsInstance(response.data, dict)
        admin = Client(self.port, self.origin)
        self.assertEqual(admin.request('POST', '/api/admin/login', {'password': self.password}).status, 200)
        private = admin.request('GET', '/api/admin/packages', headers={'Accept-Encoding': 'gzip', 'If-None-Match': '*'})
        self.assertEqual(private.status, 200)
        self.assertEqual(dict(private.headers)['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', dict(private.headers))
        self.assertNotIn('Content-Encoding', dict(private.headers))


if __name__ == '__main__':
    unittest.main(verbosity=2)
