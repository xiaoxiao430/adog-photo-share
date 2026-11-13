#!/usr/bin/env python3
"""独立照片领取服务。仅标准库；数据和登录配置均位于源码目录之外。"""
import gzip
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import time
import zlib
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PHOTO_SCHEMA_VERSION = 3
BASE = os.environ.get('AIDOG_PHOTOS_BASE', '/aidog-photos').rstrip('/')
ORIGIN = os.environ.get('AIDOG_PHOTOS_ORIGIN', 'http://127.0.0.1:18988').rstrip('/')
DATA = Path(os.environ.get('AIDOG_PHOTOS_DATA', str(Path.home() / '.local/share/aidog-photos')))
CONFIG = Path(os.environ.get('AIDOG_PHOTOS_CONFIG', str(Path.home() / '.config/aidog-photos/config.json')))
LEASE = 15 * 60 * 1000
DELIVERY_LEASE = 5 * 60 * 1000
DAY = 86400000
MAX_IMAGE = 12 * 1024 * 1024
PLATFORMS = [('meituan', '美团'), ('dianping', '大众点评'), ('douyin', '抖音')]
DEFAULT_LINKS = {
    'meituan': 'https://dpurl.cn/ajAp9rLz',
    'dianping': 'https://m.dianping.com/shopshare/k7zLt1kUlCSVzcjh?msource=Appshare2021&utm_source=shop_share&shoptype=95&shopcategoryid=25148&isoversea=0&shareid=NPD8uo5WBn_1793890842',
    'douyin': 'https://www.douyin.com/',
}


def now():
    return int(time.time() * 1000)


class Error(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


@contextmanager
def database(write=False):
    conn = sqlite3.connect(str(DATA / 'photos.sqlite3'), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    (DATA / 'images').mkdir(exist_ok=True, mode=0o700)
    (DATA / 'thumbnails').mkdir(exist_ok=True, mode=0o700)
    with database(True) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,expires INTEGER NOT NULL,current_batch TEXT);
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,status TEXT NOT NULL,expires INTEGER NOT NULL,created INTEGER NOT NULL,used_at INTEGER,method TEXT);
        CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY,name TEXT NOT NULL,mime TEXT NOT NULL,digest TEXT NOT NULL UNIQUE,size INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'available',created INTEGER NOT NULL,used_at INTEGER,method TEXT,batch_id TEXT);
        CREATE TABLE IF NOT EXISTS batch_photos(batch_id TEXT NOT NULL,photo_id TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(batch_id,photo_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,reset INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS packages(id TEXT PRIMARY KEY,title TEXT NOT NULL,copy TEXT NOT NULL,created INTEGER NOT NULL,upload_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS package_photos(package_id TEXT NOT NULL,photo_id TEXT NOT NULL UNIQUE,position INTEGER NOT NULL,PRIMARY KEY(package_id,position));
        CREATE INDEX IF NOT EXISTS photo_status ON photos(status,created,id);
        CREATE INDEX IF NOT EXISTS batch_session ON batches(session_id,created);
        ''')
        # Old releases reserved the entire inventory during page viewing. Only
        # batches with an explicit prepare time may now hold exclusive photos.
        if not c.in_transaction:
            c.execute('BEGIN IMMEDIATE')
        if 'prepared_at' not in {r['name'] for r in c.execute('PRAGMA table_info(batches)')}:
            c.execute('ALTER TABLE batches ADD COLUMN prepared_at INTEGER')
            c.execute("UPDATE photos SET status='available',batch_id=NULL WHERE status='reserved' AND batch_id IN (SELECT id FROM batches WHERE status='reserved' AND prepared_at IS NULL)")
            c.execute("UPDATE batches SET status='preview' WHERE status='reserved' AND prepared_at IS NULL")
        for column in ('package_id', 'copy_snapshot', 'title_snapshot'):
            if column not in {r['name'] for r in c.execute('PRAGMA table_info(batches)')}:
                c.execute('ALTER TABLE batches ADD COLUMN ' + column + ' TEXT')
        c.execute('CREATE INDEX IF NOT EXISTS batch_package_session ON batches(package_id,session_id,created)')
        migration = Path(os.environ.get('AIDOG_PHOTOS_INITIAL_PACKAGES', str(CONFIG.parent / 'initial-packages.json')))
        if migration.is_file() and not c.execute("SELECT 1 FROM settings WHERE key='packages_initial_import'").fetchone():
            entries = json.loads(migration.read_text()).get('packages', [])
            if c.execute('SELECT COUNT(*) FROM packages').fetchone()[0]:
                raise RuntimeError('package_migration_already_started')
            flat = [pid for item in entries for pid in item.get('photoIds', [])]
            if not entries or len(flat) != len(set(flat)) or len(flat) != 3 * len(entries):
                raise RuntimeError('package_migration_invalid_photos')
            for item in entries:
                text = item.get('copy', item.get('body', '')).strip()
                title = item.get('title', '').strip()
                if not text or len(text) > 1000 or not title or len(title) > 80:
                    raise RuntimeError('package_migration_invalid_copy')
                for pid in item['photoIds']:
                    if not c.execute("SELECT 1 FROM photos WHERE id=? AND status!='deleted'", (pid,)).fetchone():
                        raise RuntimeError('package_migration_photo_missing')
                package_id = item.get('key') or secrets.token_hex(16)
                c.execute('INSERT INTO packages VALUES(?,?,?,?,NULL)', (package_id, title, text, now()))
                for position, pid in enumerate(item['photoIds']):
                    c.execute('INSERT INTO package_photos VALUES(?,?,?)', (package_id, pid, position))
            c.execute("INSERT INTO settings VALUES('packages_initial_import',?)", (str(now()),))
        c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('storeName', '爱多格宠物医院·景观路总院'))
        for key, value in DEFAULT_LINKS.items():
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('url:' + key, value))
    os.chmod(DATA / 'photos.sqlite3', 0o600)


def expire(c):
    stamp = now()
    c.execute("UPDATE photos SET status='available',batch_id=NULL WHERE status='reserved' AND batch_id IN (SELECT id FROM batches WHERE status='reserved' AND expires<=?)", (stamp,))
    c.execute("UPDATE batches SET status='expired' WHERE status IN ('reserved','preview') AND expires<=?", (stamp,))
    c.execute('DELETE FROM limits WHERE reset<?', (stamp - DAY,))
    c.execute('DELETE FROM sessions WHERE expires<?', (stamp,))


def settings(c):
    values = dict(c.execute('SELECT key,value FROM settings').fetchall())
    return {'storeName': values.get('storeName', '爱多格宠物医院'), 'platforms': [
        {'id': key, 'name': label, 'url': values.get('url:' + key, '')} for key, label in PLATFORMS]}


def photo(p, admin=False):
    url = BASE + ('/api/admin/image/' if admin else '/api/image/') + p['id']
    return {'id': p['id'], 'name': p['name'], 'url': url, 'previewUrl': url + '?variant=preview',
            'status': p['status'], 'createdAt': p['created'], 'usedAt': p['used_at'], 'method': p['method'], 'batchId': p['batch_id']}


def batch(c, b):
    if not b:
        return None
    rows = c.execute('SELECT p.* FROM photos p JOIN batch_photos bp ON bp.photo_id=p.id WHERE bp.batch_id=? ORDER BY bp.position', (b['id'],)).fetchall()
    return {'id': b['id'], 'packageId': b['package_id'], 'title': b['title_snapshot'] or '', 'copy': b['copy_snapshot'] or '', 'status': b['status'], 'expiresAt': b['expires'], 'usedAt': b['used_at'],
            'method': b['method'], 'photos': [photo(p) for p in rows], 'receipts': []}


def package_rows(c, package_id):
    return c.execute('SELECT p.* FROM photos p JOIN package_photos pp ON p.id=pp.photo_id WHERE pp.package_id=? ORDER BY pp.position', (package_id,)).fetchall()


def package_data(c, row):
    rows = package_rows(c, row['id'])
    states = [p['status'] for p in rows]
    state = 'used' if 'used' in states else 'reserved' if 'reserved' in states else 'available' if len(rows) == 3 and all(s == 'available' for s in states) else 'archived'
    dates = [p['used_at'] for p in rows if p['used_at']]
    methods = [p['method'] for p in rows if p['method']]
    return {'id': row['id'], 'title': row['title'], 'copy': row['copy'], 'photos': [photo(p, True) for p in rows], 'status': state,
            'createdAt': row['created'], 'usedAt': max(dates) if dates else None, 'method': methods[0] if methods else None}


def available_packages(c, sid=None, previous=None):
    query = "SELECT k.* FROM packages k LEFT JOIN (SELECT package_id,MAX(created) seen_at FROM batches WHERE session_id=? GROUP BY package_id) seen ON seen.package_id=k.id WHERE (SELECT COUNT(*) FROM package_photos WHERE package_id=k.id)=3 AND NOT EXISTS(SELECT 1 FROM package_photos pp JOIN photos p ON p.id=pp.photo_id WHERE pp.package_id=k.id AND p.status!='available')"
    order = ' ORDER BY COALESCE(seen.seen_at,0),k.created,k.id'
    rows = c.execute(query + (' AND k.id!=?' if previous else '') + order, [sid or ''] + ([previous] if previous else [])).fetchall()
    if not rows and previous:
        rows = c.execute(query + order, (sid or '',)).fetchall()
    return rows


def set_batch_package(c, bid, row):
    c.execute('DELETE FROM batch_photos WHERE batch_id=?', (bid,))
    for i, p in enumerate(package_rows(c, row['id'])):
        c.execute('INSERT INTO batch_photos VALUES(?,?,?)', (bid, p['id'], i))
    c.execute('UPDATE batches SET package_id=?,copy_snapshot=?,title_snapshot=? WHERE id=?', (row['id'], row['copy'], row['title'], bid))


def reserve(c, sid, previous=None):
    expire(c)
    s = c.execute('SELECT current_batch FROM sessions WHERE id=?', (sid,)).fetchone()
    b = c.execute("SELECT * FROM batches WHERE id=? AND session_id=? AND package_id IS NOT NULL AND status IN ('preview','reserved','used')", (s['current_batch'], sid)).fetchone() if s else None
    if b and b['status'] == 'preview':
        count = c.execute("SELECT COUNT(*) FROM photos p JOIN batch_photos bp ON p.id=bp.photo_id WHERE bp.batch_id=? AND p.status='available'", (b['id'],)).fetchone()[0]
        current_package = c.execute('SELECT * FROM packages WHERE id=?', (b['package_id'],)).fetchone()
        if count != 3 or not current_package:
            c.execute("UPDATE batches SET status='released' WHERE id=?", (b['id'],))
            b = None
        else:
            set_batch_package(c, b['id'], current_package)
            b = c.execute('SELECT * FROM batches WHERE id=?', (b['id'],)).fetchone()
    if not b:
        rows = available_packages(c, sid, previous)
        if rows:
            bid, stamp = secrets.token_hex(16), now()
            c.execute("INSERT INTO batches(id,session_id,status,expires,created) VALUES(?,?,'preview',?,?)", (bid, sid, stamp + LEASE, stamp))
            set_batch_package(c, bid, rows[0])
            c.execute('UPDATE sessions SET current_batch=? WHERE id=?', (bid, sid))
            b = c.execute('SELECT * FROM batches WHERE id=?', (bid,)).fetchone()
    return {**settings(c), 'batch': batch(c, b), 'availableCount': 3 * len(available_packages(c))}


def prepare(c, b):
    if b['status'] in ('reserved', 'used'):
        return b
    current_package = c.execute('SELECT * FROM packages WHERE id=?', (b['package_id'],)).fetchone()
    count = c.execute("SELECT COUNT(*) FROM photos WHERE status='available' AND id IN (SELECT photo_id FROM batch_photos WHERE batch_id=?)", (b['id'],)).fetchone()[0]
    if count != 3 or not current_package:
        rows = available_packages(c, b['session_id'], b['package_id'])
        if not rows:
            raise Error(409, '这组照片已被领取，请换一组重试')
        current_package = rows[0]
    set_batch_package(c, b['id'], current_package)
    stamp = now()
    changed = c.execute("UPDATE photos SET status='reserved',batch_id=? WHERE status='available' AND id IN (SELECT photo_id FROM batch_photos WHERE batch_id=?)", (b['id'], b['id'])).rowcount
    if changed != 3:
        raise Error(409, '这组照片已被领取，请换一组重试')
    c.execute("UPDATE batches SET status='reserved',prepared_at=?,expires=? WHERE id=?", (stamp, stamp + DELIVERY_LEASE, b['id']))
    return c.execute('SELECT * FROM batches WHERE id=?', (b['id'],)).fetchone()


def valid_link(key, value):
    if not isinstance(value, str) or len(value) > 3000:
        raise Error(400, '请填写对应平台的完整门店链接')
    value = value.strip()
    if not value:
        return ''
    domains = {'meituan': ('meituan.com', 'meituan.net', 'mturl.cn', 'dpurl.cn'), 'dianping': ('dianping.com', 'dpurl.cn'), 'douyin': ('douyin.com', 'iesdouyin.com')}
    try:
        u = urlsplit(value)
        allowed = u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443) and any(u.hostname == d or u.hostname.endswith('.' + d) for d in domains[key])
    except (ValueError, AttributeError):
        allowed = False
    if not allowed or re.search(r'[\x00-\x20]', value):
        raise Error(400, '请使用对应平台官方App分享的HTTPS门店链接')
    return value


def valid_image(data, mime):
    """Reject truncated headers/chunks before retaining an uploaded file."""
    try:
        if mime == 'image/png' and data[:8] == b'\x89PNG\r\n\x1a\n':
            offset, pixels, payload = 8, False, False
            while offset + 12 <= len(data):
                size = int.from_bytes(data[offset:offset + 4], 'big')
                kind = data[offset + 4:offset + 8]
                end = offset + 12 + size
                if end > len(data) or size > MAX_IMAGE:
                    return False
                body = data[offset + 8:offset + 8 + size]
                crc = int.from_bytes(data[end - 4:end], 'big')
                if zlib.crc32(kind + body) & 0xffffffff != crc:
                    return False
                if offset == 8:
                    if kind != b'IHDR' or size != 13:
                        return False
                    width, height = struct.unpack('>II', body[:8])
                    pixels = 0 < width <= 20000 and 0 < height <= 20000 and width * height <= 100000000
                if kind == b'IDAT' and size:
                    payload = True
                if kind == b'IEND':
                    return pixels and payload and size == 0 and end == len(data)
                offset = end
            return False
        if mime == 'image/jpeg' and data[:3] == b'\xff\xd8\xff' and data[-2:] == b'\xff\xd9':
            offset, dimensions = 2, False
            while offset + 4 <= len(data):
                if data[offset] != 255:
                    return False
                while data[offset] == 255:
                    offset += 1
                marker = data[offset]
                offset += 1
                size = int.from_bytes(data[offset:offset + 2], 'big')
                if size < 2 or offset + size > len(data):
                    return False
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                    if size < 8:
                        return False
                    height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                    dimensions = 0 < width <= 20000 and 0 < height <= 20000 and width * height <= 100000000
                if marker == 0xda:
                    return dimensions and offset + size < len(data) - 2
                offset += size
            return False
        if mime == 'image/webp' and len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if int.from_bytes(data[4:8], 'little') + 8 != len(data):
                return False
            offset, image = 12, False
            while offset + 8 <= len(data):
                kind = data[offset:offset + 4]
                size = int.from_bytes(data[offset + 4:offset + 8], 'little')
                if offset + 8 + size > len(data):
                    return False
                if kind in (b'VP8 ', b'VP8L') and size >= 5:
                    image = True
                offset += 8 + size + size % 2
            return image and offset == len(data)
    except (IndexError, ValueError, struct.error):
        return False
    return False


def create_thumbnail(pid):
    """Build once on upload; preview GET never decodes or recompresses images."""
    source = DATA / 'images' / pid
    target = DATA / 'thumbnails' / (pid + '.jpg')
    if target.is_file():
        return True
    temporary = target.with_name('.' + pid + '-' + secrets.token_hex(4) + '.jpg')
    try:
        if Path('/usr/bin/sips').is_file():
            subprocess.run(['/usr/bin/sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', '68', '-Z', '480', str(source), '--out', str(temporary)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
        else:
            # Optional portability fallback. The deployed Mac uses built-in sips.
            from PIL import Image, ImageOps
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail((480, 480))
                image.save(temporary, 'JPEG', quality=68, optimize=True)
        content = temporary.read_bytes()
        if not valid_image(content, 'image/jpeg') or len(content) > 512 * 1024:
            return False
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return True
    except (OSError, ImportError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        temporary.unlink(missing_ok=True)


def package_text(value):
    text = value.get('copy', '')
    title = value.get('title', '')
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 1000:
        raise Error(400, '每套必须填写1至1000字的对应文案')
    if not isinstance(title, str) or len(title.strip()) > 80:
        raise Error(400, '套装标题最多80字')
    text = text.replace('\r\n', '\n').strip()
    return title.strip() or text.split('\n', 1)[0][:18], text


def accepts_gzip(header):
    qualities = {}
    for entry in (header or '').split(','):
        parts = entry.split(';')
        coding = parts[0].strip().lower()
        if coding not in ('gzip', '*'):
            continue
        weights = [part.partition('=')[2].strip() for part in parts[1:]
                   if part.partition('=')[0].strip().lower() == 'q']
        quality = 1.0
        if weights:
            quality = (float(weights[0]) if len(weights) == 1 and
                       re.fullmatch(r'(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', weights[0]) else 0.0)
        # An explicit refusal wins over a wildcard or a repeated coding entry.
        qualities[coding] = min(qualities.get(coding, quality), quality)
    return qualities.get('gzip', qualities.get('*', 0.0)) > 0


def matches_etag(header, etag):
    return any(value.strip() == '*' or value.strip().removeprefix('W/') == etag
               for value in (header or '').split(','))


class Handler(BaseHTTPRequestHandler):
    server_version = 'AidogPhotos/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status, payload, mime='application/json; charset=utf-8', headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if not isinstance(payload, bytes) else payload
        headers = dict(headers or {})
        cache = headers.pop('Cache-Control', 'no-store')
        if urlsplit(self.path).path.startswith(BASE + '/api/'):
            cache = 'no-store'
        self.send_response(status)
        if status != 304:
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in headers.items():
            self.send_header(k, v)
        for cookie in getattr(self, 'cookies_out', []):
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if status != 304:
            self.wfile.write(body)

    def static_file(self, file):
        body = file.read_bytes()
        compressible = file.suffix.lower() in ('.html', '.js', '.css')
        headers = {'Cache-Control': 'no-cache'}
        if re.fullmatch(r'.+-[a-f0-9]{8,64}\.woff2', file.name):
            headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif file.suffix.lower() in ('.woff2', '.png'):
            headers['Cache-Control'] = 'public, max-age=86400'
        if compressible:
            headers['Vary'] = 'Accept-Encoding'
            if accepts_gzip(self.headers.get('Accept-Encoding')):
                body = gzip.compress(body, compresslevel=6, mtime=0)
                headers['Content-Encoding'] = 'gzip'
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers['ETag'] = etag
        if matches_etag(self.headers.get('If-None-Match'), etag):
            return self.respond(304, b'', headers=headers)
        return self.respond(200, body, mimetypes.guess_type(str(file))[0] or 'application/octet-stream', headers)

    def body(self, maximum=16384, raw=False):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise Error(400, '请求格式不正确')
        if size <= 0 or size > maximum:
            raise Error(413 if size > maximum else 400, '每张照片最多12MB' if raw else '提交内容格式不正确')
        content = self.rfile.read(size)
        if len(content) != size:
            raise Error(400, '传输中断，请重试')
        if raw:
            return content
        try:
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise Error(400, '提交内容格式不正确')

    def package_form(self):
        content_type = self.headers.get('Content-Type', '')
        if not content_type.lower().startswith('multipart/form-data;'):
            raise Error(400, '请一次选择3张照片并填写对应文案')
        raw = self.body(36 * 1024 * 1024 + 65536, True)
        try:
            message = BytesParser(policy=policy.default).parsebytes(('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('ascii') + raw)
            if not message.is_multipart() or message.defects:
                raise ValueError()
            fields, files = {}, []
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                content = part.get_payload(decode=True)
                if content is None:
                    raise ValueError()
                if name == 'files' and part.get_filename() is not None:
                    filename = re.sub(r'[\x00-\x1f\x7f]', '', part.get_filename().replace('\\', '/').rsplit('/', 1)[-1])[:180] or '照片'
                    files.append({'name': filename, 'mime': part.get_content_type(), 'data': content, 'digest': hashlib.sha256(content).hexdigest()})
                elif name in ('copy', 'title', 'uploadId') and name not in fields:
                    fields[name] = content.decode('utf-8', errors='strict')
                else:
                    raise ValueError()
        except (ValueError, UnicodeError, AttributeError):
            raise Error(400, '上传格式不正确，请重新选择3张图片和文案')
        title, text = package_text(fields)
        if len(files) != 3:
            raise Error(400, '每套必须正好上传3张照片')
        upload_id = fields.get('uploadId', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{12,80}', upload_id):
            raise Error(400, '本次上传标识无效，请刷新后台重试')
        for file in files:
            if not 0 < len(file['data']) <= MAX_IMAGE:
                raise Error(413, '每张照片最多12MB')
            if not valid_image(file['data'], file['mime']):
                raise Error(400, '照片内容不完整，请选择JPG、PNG或WebP照片')
        if len({file['digest'] for file in files}) != 3:
            raise Error(400, '一套中的3张照片不能重复')
        return title, text, upload_id, files

    def cookie(self, kind, value, seconds):
        result = f'aidog_photos_{kind}={value}; Path={BASE}/; Max-Age={seconds}; HttpOnly; SameSite=Lax'
        if ORIGIN.startswith('https:'):
            result += '; Secure'
        self.cookies_out.append(result)

    def identity(self, c, kind='guest', create=False):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
        except Exception:
            pass
        item = jar.get('aidog_photos_' + kind)
        value = item.value if item else ''
        if re.fullmatch(r'[a-f0-9]{64}', value):
            sid = hashlib.sha256(value.encode()).hexdigest()
            if c.execute('SELECT id FROM sessions WHERE id=? AND kind=? AND expires>?', (sid, kind, now())).fetchone():
                return sid
        if not create:
            raise Error(401, '请先登录管理后台' if kind == 'admin' else '页面已失效，请刷新重试')
        self.limit(c, 'guest-new', 120, 3600000)
        value = secrets.token_hex(32)
        sid = hashlib.sha256(value.encode()).hexdigest()
        c.execute('INSERT INTO sessions VALUES(?,?,?,NULL)', (sid, kind, now() + 30 * DAY))
        self.cookie(kind, value, 30 * 86400)
        return sid

    def limit(self, c, action, maximum, interval):
        # This server listens only on loopback. The nginx location replaces the
        # incoming header, so visitors cannot supply their own rate-limit key.
        address = self.headers.get('X-Real-IP') or self.client_address[0]
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = self.client_address[0]
        key = hashlib.sha256((action + ':' + address).encode()).hexdigest()
        row = c.execute('SELECT * FROM limits WHERE key=?', (key,)).fetchone()
        if not row or row['reset'] <= now():
            c.execute('INSERT OR REPLACE INTO limits VALUES(?,?,?)', (key, 1, now() + interval))
        elif row['count'] >= maximum:
            raise Error(429, '操作较频繁，请稍后再试')
        else:
            c.execute('UPDATE limits SET count=count+1 WHERE key=?', (key,))

    def run(self):
        self.cookies_out = []
        self.connection.settimeout(45)
        try:
            url = urlsplit(self.path)
            if url.path == BASE:
                return self.respond(308, b'', headers={'Location': BASE + '/'})
            if not url.path.startswith(BASE + '/'):
                raise Error(404, '页面不存在')
            path = unquote(url.path[len(BASE):])
            if path == '/admin/' and self.command == 'GET':
                return self.respond(308, b'', headers={'Location': BASE + '/admin'})
            if self.command != 'GET':
                if self.headers.get('Origin') != ORIGIN or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise Error(403, '请从本站页面操作')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and path.startswith('/api/'):
                raise Error(403, '请从本站页面操作')
            if path == '/api/bootstrap' or path.startswith('/api/batch/'):
                if self.headers.get('X-Aidog-Photo-Flow') != '3':
                    self.close_connection = True
                    return self.respond(409, {'error': '页面已更新，请刷新网页再下载照片', 'reloadRequired': True}, headers={'Connection': 'close'})
            if path == '/api/health' and self.command == 'GET':
                return self.respond(200, {'ok': True, 'service': 'aidog-photos'})
            if path == '/api/admin/login' and self.command == 'POST':
                value = self.body()
                # Persist attempts even when the password is wrong.
                with database(True) as c:
                    self.limit(c, 'admin-login', 20, 15 * 60000)
                try:
                    config = json.loads(CONFIG.read_text())
                    password = value.get('password', '')
                    if not isinstance(password, str) or len(password) > 1024:
                        raise Error(401, '密码不正确')
                    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(config['salt']), n=16384, r=8, p=1, dklen=32).hex()
                    valid = hmac.compare_digest(actual, config['passwordHash'])
                except (OSError, KeyError, ValueError):
                    raise Error(503, '后台登录尚未配置')
                if not valid:
                    raise Error(401, '密码不正确')
                with database(True) as c:
                    token = secrets.token_hex(32)
                    sid = hashlib.sha256(token.encode()).hexdigest()
                    c.execute('INSERT INTO sessions VALUES(?,?,?,NULL)', (sid, 'admin', now() + 12 * 3600000))
                    self.cookie('admin', token, 12 * 3600)
                return self.respond(200, {'ok': True})
            if path.startswith('/api/admin/'):
                with database(True) as c:
                    self.identity(c, 'admin')
                return self.admin(path, url)
            if path in ('/api/bootstrap', '/api/batch/reserve') and self.command in ('GET', 'POST'):
                if self.command == 'POST':
                    self.body()
                with database(True) as c:
                    sid = self.identity(c, create=True)
                    result = reserve(c, sid)
                return self.respond(200, result)
            if path.startswith('/api/batch/') and self.command == 'POST':
                value = self.body()
                with database(True) as c:
                    sid = self.identity(c)
                    expire(c)
                    b = c.execute('SELECT * FROM batches WHERE id=? AND session_id=?', (str(value.get('batchId', '')), sid)).fetchone()
                    if not b or b['status'] not in ('preview', 'reserved', 'used'):
                        raise Error(409, '这组照片已过期，请刷新领取新照片')
                    if path == '/api/batch/commit':
                        if b['status'] == 'preview':
                            raise Error(409, '请先点击下载照片，再确认保存')
                        method = value.get('method')
                        if method not in ('system_share', 'manual_confirm', 'browser_download'):
                            raise Error(400, '保存方式不正确')
                        if b['status'] != 'used':
                            count = c.execute("SELECT COUNT(*) FROM photos WHERE batch_id=? AND status='reserved'", (b['id'],)).fetchone()[0]
                            if count != 3:
                                raise Error(409, '照片状态已变化，请刷新重试')
                            stamp = now()
                            c.execute("UPDATE photos SET status='used',used_at=?,method=? WHERE batch_id=? AND status='reserved'", (stamp, method, b['id']))
                            c.execute("UPDATE batches SET status='used',used_at=?,method=? WHERE id=?", (stamp, method, b['id']))
                        b = c.execute('SELECT * FROM batches WHERE id=?', (b['id'],)).fetchone()
                        result = {'status': 'used', 'usedAt': b['used_at'], 'method': b['method']}
                    elif path == '/api/batch/prepare':
                        b = prepare(c, b)
                        result = {'expiresAt': b['expires'], 'batch': batch(c, b)}
                    elif path == '/api/batch/renew':
                        deadline = now() + (DELIVERY_LEASE if b['status'] == 'reserved' else LEASE)
                        c.execute("UPDATE batches SET expires=? WHERE id=? AND status IN ('reserved','preview')", (deadline, b['id']))
                        result = {'expiresAt': deadline if b['status'] != 'used' else b['expires']}
                    elif path == '/api/batch/cancel':
                        if b['status'] == 'reserved':
                            c.execute("UPDATE photos SET status='available',batch_id=NULL WHERE batch_id=? AND status='reserved'", (b['id'],))
                            c.execute("UPDATE batches SET status='preview',prepared_at=NULL,expires=? WHERE id=?", (now() + LEASE, b['id']))
                        result = reserve(c, sid)
                    elif path == '/api/batch/release':
                        previous = b['package_id']
                        if b['status'] in ('preview', 'reserved'):
                            c.execute("UPDATE photos SET status='available',batch_id=NULL WHERE batch_id=? AND status='reserved'", (b['id'],))
                            c.execute("UPDATE batches SET status='released' WHERE id=?", (b['id'],))
                        c.execute('UPDATE sessions SET current_batch=NULL WHERE id=?', (sid,))
                        result = reserve(c, sid, previous)
                    else:
                        raise Error(404, '接口不存在')
                return self.respond(200, result)
            if path.startswith('/api/image/') and self.command == 'GET':
                pid = path.rsplit('/', 1)[-1]
                with database(True) as c:
                    sid = self.identity(c)
                    expire(c)
                    p = c.execute("SELECT p.* FROM photos p JOIN batch_photos bp ON bp.photo_id=p.id JOIN batches b ON b.id=bp.batch_id WHERE p.id=? AND b.session_id=? AND ((b.status='preview' AND p.status='available') OR (b.status IN ('reserved','used') AND p.batch_id=b.id))", (pid, sid)).fetchone()
                    if not p:
                        raise Error(404, '照片不存在或页面已过期')
                return self.image(p, parse_qs(url.query).get('variant', [''])[0] == 'preview')
            if path.startswith('/api/'):
                raise Error(404, '接口不存在')
            if self.command != 'GET':
                raise Error(405, '不支持此操作')
            name = 'index.html' if path == '/' else 'admin.html' if path in ('/admin', '/admin/') else path.lstrip('/')
            file = (ROOT / 'public' / name).resolve()
            if not file.is_relative_to(ROOT / 'public') or not file.is_file():
                raise Error(404, '页面不存在')
            return self.static_file(file)
        except Error as e:
            self.close_connection = True
            self.respond(e.status, {'error': e.message}, headers={'Connection': 'close'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.respond(500, {'error': '暂时无法完成，请稍后重试'}, headers={'Connection': 'close'})

    def image(self, p, preview=False):
        file = DATA / 'images' / p['id']
        mime = p['mime']
        thumbnail = DATA / 'thumbnails' / (p['id'] + '.jpg')
        if preview and thumbnail.is_file():
            file, mime = thumbnail, 'image/jpeg'
        if not file.is_file():
            raise Error(404, '照片文件暂不可用')
        self.respond(200, file.read_bytes(), mime)

    def admin(self, path, url):
        if path == '/api/admin/session' and self.command == 'GET':
            return self.respond(200, {'ok': True})
        if path == '/api/admin/logout' and self.command == 'POST':
            self.body()
            with database(True) as c:
                sid = self.identity(c, 'admin')
                c.execute('DELETE FROM sessions WHERE id=?', (sid,))
            self.cookie('admin', '', 0)
            return self.respond(200, {'ok': True})
        if path.startswith('/api/admin/image/') and self.command == 'GET':
            with database() as c:
                p = c.execute("SELECT * FROM photos WHERE id=? AND status!='deleted'", (path.rsplit('/', 1)[-1],)).fetchone()
                if not p:
                    raise Error(404, '照片不存在')
            return self.image(p, parse_qs(url.query).get('variant', [''])[0] == 'preview')
        if path == '/api/admin/packages' and self.command == 'GET':
            query = parse_qs(url.query)
            state = query.get('status', ['all'])[0]
            if state not in ('all', 'available', 'reserved', 'used'):
                raise Error(400, '筛选状态不正确')
            try:
                page = max(1, min(100000, int(query.get('page', ['1'])[0])))
            except ValueError:
                raise Error(400, '页码不正确')
            with database(True) as c:
                expire(c)
                items = [package_data(c, row) for row in c.execute('SELECT * FROM packages ORDER BY created DESC,id DESC')]
                stats = {s: sum(p['status'] == s for p in items) for s in ('available', 'reserved', 'used')}
                stats['all'] = len(items)
                filtered = [p for p in items if state == 'all' or p['status'] == state]
                result = {**settings(c), 'packages': filtered[(page - 1) * 12:page * 12], 'total': len(filtered), 'page': page, 'pageSize': 12, 'stats': stats}
            return self.respond(200, result)
        if path == '/api/admin/packages' and self.command == 'POST':
            title, text, upload_id, files = self.package_form()
            written, new_ids = [], []
            try:
                with database(True) as c:
                    previous = c.execute('SELECT * FROM packages WHERE upload_id=?', (upload_id,)).fetchone()
                    if previous:
                        old_digests = [p['digest'] for p in package_rows(c, previous['id'])]
                        if old_digests != [f['digest'] for f in files] or previous['copy'] != text or previous['title'] != title:
                            raise Error(409, '本次上传内容已修改，请重新提交')
                        result = {'id': previous['id'], 'duplicate': True, 'package': package_data(c, previous)}
                    else:
                        existing = [c.execute('SELECT * FROM photos WHERE digest=?', (f['digest'],)).fetchone() for f in files]
                        linked = [c.execute('SELECT package_id FROM package_photos WHERE photo_id=?', (p['id'],)).fetchone() if p else None for p in existing]
                        if all(linked) and len({p['package_id'] for p in linked}) == 1:
                            row = c.execute('SELECT * FROM packages WHERE id=?', (linked[0]['package_id'],)).fetchone()
                            result = {'id': row['id'], 'duplicate': True, 'package': package_data(c, row)}
                        else:
                            if any(linked) or any(p and p['status'] != 'available' for p in existing):
                                raise Error(409, '部分照片已属于其他套装或已使用，请换图后整套上传')
                            ids = []
                            for file, current in zip(files, existing):
                                if current:
                                    ids.append(current['id'])
                                    continue
                                pid = secrets.token_hex(16)
                                target = DATA / 'images' / pid
                                with target.open('xb') as out:
                                    os.chmod(target, 0o600)
                                    out.write(file['data'])
                                written.append(target)
                                c.execute("INSERT INTO photos(id,name,mime,digest,size,status,created) VALUES(?,?,?,?,?,'available',?)", (pid, file['name'], file['mime'], file['digest'], len(file['data']), now()))
                                ids.append(pid)
                                new_ids.append(pid)
                            package_id = secrets.token_hex(16)
                            c.execute('INSERT INTO packages VALUES(?,?,?,?,?)', (package_id, title, text, now(), upload_id))
                            for position, pid in enumerate(ids):
                                c.execute('INSERT INTO package_photos VALUES(?,?,?)', (package_id, pid, position))
                            row = c.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
                            result = {'id': package_id, 'duplicate': False, 'package': package_data(c, row)}
            except Exception:
                for file in written:
                    file.unlink(missing_ok=True)
                raise
            for pid in new_ids:
                create_thumbnail(pid)
            return self.respond(200, result)
        if path.startswith('/api/admin/packages/') and self.command in ('PUT', 'DELETE'):
            package_id = path.rsplit('/', 1)[-1]
            value = self.body() if self.command == 'PUT' else None
            removed = []
            with database(True) as c:
                expire(c)
                row = c.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
                if not row:
                    raise Error(404, '这套图文不存在')
                item = package_data(c, row)
                if item['status'] != 'available':
                    raise Error(409, '只有未领取的套装可以修改或删除，已有记录会保留')
                if self.command == 'PUT':
                    title, text = package_text(value)
                    c.execute('UPDATE packages SET title=?,copy=? WHERE id=?', (title, text, package_id))
                    item = package_data(c, c.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone())
                else:
                    removed = [p['id'] for p in item['photos']]
                    for pid in removed:
                        c.execute("UPDATE photos SET status='deleted' WHERE id=? AND status='available'", (pid,))
                    c.execute('DELETE FROM package_photos WHERE package_id=?', (package_id,))
                    c.execute('DELETE FROM packages WHERE id=?', (package_id,))
            for pid in removed:
                (DATA / 'images' / pid).unlink(missing_ok=True)
                (DATA / 'thumbnails' / (pid + '.jpg')).unlink(missing_ok=True)
            return self.respond(200, {'ok': True, 'package': item})
        if path == '/api/admin/photos' and self.command == 'GET':
            query = parse_qs(url.query)
            status = query.get('status', ['all'])[0]
            if status not in ('all', 'available', 'reserved', 'used'):
                raise Error(400, '筛选状态不正确')
            try:
                page = max(1, min(100000, int(query.get('page', ['1'])[0])))
            except ValueError:
                raise Error(400, '页码不正确')
            with database(True) as c:
                expire(c)
                stats = {r['status']: r['count'] for r in c.execute("SELECT status,COUNT(*) count FROM photos WHERE status!='deleted' GROUP BY status")}
                stats = {key: stats.get(key, 0) for key in ('available', 'reserved', 'used')}
                stats['all'] = sum(stats.values())
                where, args = ("status!='deleted'", []) if status == 'all' else ('status=?', [status])
                total = c.execute('SELECT COUNT(*) FROM photos WHERE ' + where, args).fetchone()[0]
                rows = c.execute('SELECT * FROM photos WHERE ' + where + ' ORDER BY created DESC,id LIMIT 60 OFFSET ?', args + [(page - 1) * 60]).fetchall()
                result = {**settings(c), 'photos': [photo(p, True) for p in rows], 'total': total, 'page': page, 'pageSize': 60, 'stats': stats}
            return self.respond(200, result)
        if path == '/api/admin/photos' and self.command == 'POST':
            raise Error(400, '请使用整套上传：3张照片和对应文案缺一不可')
        if path.startswith('/api/admin/photos/') and self.command == 'DELETE':
            with database(True) as c:
                expire(c)
                pid = path.rsplit('/', 1)[-1]
                p = c.execute('SELECT * FROM photos WHERE id=?', (pid,)).fetchone()
                if not p:
                    raise Error(404, '照片不存在')
                if c.execute('SELECT 1 FROM package_photos WHERE photo_id=?', (pid,)).fetchone():
                    raise Error(409, '这张照片已配入套装，请按套管理')
                if p['status'] != 'available':
                    raise Error(409, '只能删除尚未领取的可用照片，已使用记录会保留')
                c.execute("UPDATE photos SET status='deleted' WHERE id=?", (pid,))
            (DATA / 'images' / pid).unlink(missing_ok=True)
            (DATA / 'thumbnails' / (pid + '.jpg')).unlink(missing_ok=True)
            return self.respond(200, {'ok': True})
        if path == '/api/admin/settings' and self.command == 'PUT':
            value = self.body()
            name = value.get('storeName', '')
            links = value.get('platforms', {})
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(links, dict):
                raise Error(400, '请填写门店名称和平台链接')
            cleaned = {key: valid_link(key, links.get(key, '')) for key, _ in PLATFORMS}
            with database(True) as c:
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('storeName', name.strip()))
                for key, link in cleaned.items():
                    c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('url:' + key, link))
            return self.respond(200, {'ok': True})
        raise Error(404, '接口不存在')

    do_GET = run
    do_POST = run
    do_PUT = run
    do_DELETE = run


class PhotoServer(ThreadingHTTPServer):
    # Each phone opens several image requests at once. The default backlog of
    # five can reset simultaneous connections before a handler is allocated.
    request_queue_size = 128
    daemon_threads = True


if __name__ == '__main__':
    os.umask(0o077)
    init()
    port = int(os.environ.get('AIDOG_PHOTOS_PORT', '18988'))
    server = PhotoServer(('127.0.0.1', port), Handler)
    print(f'Aidog photos ready: http://127.0.0.1:{port}{BASE}/', flush=True)
    server.serve_forever()
