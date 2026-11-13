#!/usr/bin/env python3
"""默认只读检查；--deploy 发布；--rollback <release> 仅回退本应用程序。"""
import argparse
import base64
import datetime
import hashlib
import io
import json
from pathlib import Path
import secrets
import shlex
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
PASSWORD_FILE = Path('/Users/xiaoxiao/.codex/private/default-admin-password.txt')


def need(value, code):
    if not value:
        raise RuntimeError(code)


def archive():
    files = [ROOT / 'server.py'] + sorted(path for path in (ROOT / 'public').rglob('*') if path.is_file())
    data = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=data, mode='w:gz') as tar:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            need(not path.is_symlink() and not any(part.startswith('._') or part == '.DS_Store' for part in path.parts), 'unexpected_source_file')
            raw = path.read_bytes()
            digest.update(relative.encode() + b'\0' + raw + b'\0')
            info = tarfile.TarInfo(relative)
            info.size, info.mode, info.mtime = len(raw), 0o600, 0
            tar.addfile(info, io.BytesIO(raw))
    return data.getvalue(), digest.hexdigest()


def credentials():
    need(PASSWORD_FILE.is_file() and not PASSWORD_FILE.is_symlink(), 'private_password_missing')
    need(stat.S_IMODE(PASSWORD_FILE.stat().st_mode) == 0o600, 'private_password_permissions')
    password = PASSWORD_FILE.read_text().rstrip('\r\n')
    need(bool(password), 'private_password_empty')
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()
    return {'salt': salt.hex(), 'passwordHash': hashed, 'sessionSecret': secrets.token_hex(32)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--deploy', action='store_true')
    group.add_argument('--rollback', metavar='RELEASE')
    args = parser.parse_args()
    value = {'mode': 'inspect'}
    if args.deploy:
        packed, source_hash = archive()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        value = {'mode': 'deploy', 'archive': base64.b64encode(packed).decode(), 'archiveSha256': hashlib.sha256(packed).hexdigest(),
                 'release': stamp + '-' + source_hash[:12], 'config': credentials()}
    elif args.rollback:
        value = {'mode': 'rollback', 'release': args.rollback}
    source = (ROOT / 'scripts/deploy-remote.py').read_text()
    # Only public Python code travels in the command argument. Credentials and archive use stdin.
    command = shlex.join(['/opt/homebrew/bin/python3.12', '-c', source])
    run = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'mac-studio', command],
                         input=json.dumps(value).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    try:
        receipt = json.loads(run.stdout)
    except ValueError:
        receipt = {'ok': False, 'error': 'invalid_remote_response', 'secretsPrinted': False}
    if value['mode'] != 'inspect':
        output = ROOT / 'output'
        output.mkdir(exist_ok=True)
        name = 'deploy-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json'
        (output / name).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if run.returncode == 0 and receipt.get('ok') else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        safe = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(json.dumps({'ok': False, 'error': safe, 'secretsPrinted': False}))
        sys.exit(1)
