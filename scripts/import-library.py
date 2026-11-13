#!/usr/bin/env python3
"""检查显式目录的AI示例PNG；仅--upload才写入图库，SHA重复不恢复使用状态。"""
import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parent.parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load('aidog_seed_common', ROOT / 'scripts/seed-demo.py')
validation = load('aidog_image_validation', ROOT / 'server.py')


def inspect(directory):
    shared.need(directory.is_dir() and not directory.is_symlink(), 'source_directory_required')
    files = sorted(directory.glob('AI示例_*.png'))
    shared.need(bool(files), 'ai_example_png_files_required')
    rows, seen = [], set()
    for file in files:
        shared.need(file.is_file() and not file.is_symlink(), 'source_must_be_regular')
        raw = file.read_bytes()
        shared.need(0 < len(raw) <= 12 * 1024 * 1024, 'image_size_limit')
        shared.need(validation.valid_image(raw, 'image/png'), 'invalid_png')
        digest = hashlib.sha256(raw).hexdigest()
        shared.need(digest not in seen, 'duplicate_source_bytes')
        seen.add(digest)
        rows.append({'name': file.name, 'sha256': digest, 'bytes': len(raw)})
    return rows


def upload(directory, rows):
    shared.need(shared.PASSWORD_FILE.is_file() and not shared.PASSWORD_FILE.is_symlink(), 'private_password_missing')
    shared.need(stat.S_IMODE(shared.PASSWORD_FILE.stat().st_mode) == 0o600, 'private_password_permissions')
    password = shared.PASSWORD_FILE.read_text().rstrip('\r\n')
    client = shared.Client()
    logged = False
    result = {'ok': False, 'mode': 'upload', 'atUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'uploaded': 0, 'duplicates': 0, 'files': [], 'noClaimsOrInventoryReset': True, 'secretsPrinted': False}
    try:
        client.request('/api/admin/login', 'POST', json_value={'password': password})
        del password
        logged = True
        before_stats, before = client.inventory()
        result['beforeStats'] = before_stats
        result['originalTotal'] = before_stats['all']
        for row in rows:
            raw = (directory / row['name']).read_bytes()
            shared.need(hashlib.sha256(raw).hexdigest() == row['sha256'], 'source_changed_after_inspection')
            value = client.request('/api/admin/photos', 'POST', raw=raw, filename=row['name'])
            shared.need(isinstance(value.get('duplicate'), bool) and isinstance(value.get('id'), str), 'upload_shape')
            action = 'duplicate' if value['duplicate'] else 'uploaded'
            result['duplicates' if value['duplicate'] else 'uploaded'] += 1
            result['files'].append({**row, 'id': value['id'], 'result': action})
        after_stats, after = client.inventory()
        for pid, previous in before.items():
            if previous['status'] == 'used':
                shared.need(pid in after and after[pid]['status'] == 'used' and after[pid]['usedAt'] == previous['usedAt'], 'previous_used_record_changed')
        result['afterStats'] = after_stats
        result['newTotal'] = after_stats['all']
        result['previousUsedRecordsPreserved'] = True
        result['ok'] = True
    except shared.Failure as error:
        result['error'] = str(error)
    except Exception as error:
        result['error'] = type(error).__name__
    finally:
        if logged:
            try:
                client.request('/api/admin/logout', 'POST', json_value={})
                result['logout'] = 200
            except Exception:
                result['logout'] = 'failed'
        output = ROOT / 'output'
        output.mkdir(exist_ok=True)
        name = 'import-library-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json'
        (output / name).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path, help='明确指定的AI示例PNG目录')
    parser.add_argument('--upload', action='store_true', help='旧散图上传已停用，请在后台整套填写文案')
    args = parser.parse_args()
    if args.upload:
        raise shared.Failure('package_copy_required_use_admin_package_form')
    directory = args.directory.resolve()
    rows = inspect(directory)
    result = upload(directory, rows) if args.upload else {'ok': True, 'mode': 'inspect', 'count': len(rows), 'files': rows, 'mutationsPerformed': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        safe = str(error) if isinstance(error, shared.Failure) else type(error).__name__
        print(json.dumps({'ok': False, 'error': safe, 'secretsPrinted': False}, ensure_ascii=False))
        sys.exit(1)
