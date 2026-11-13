#!/usr/bin/env python3
"""按图库原图SHA构建私有缩略图；只有显式--apply才上传到Studio。"""
import argparse
import base64
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('aidog_thumbnail_admin', ROOT / 'scripts/seed-demo.py')
admin_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin_api)

REMOTE = r'''
import base64,hashlib,io,json,os,re,secrets,sqlite3,sys,tarfile
from pathlib import Path
value=json.load(sys.stdin)
root=Path(value['dataDir'])
def need(ok,code):
 if not ok:raise RuntimeError(code)
def digest(value):return hashlib.sha256(value).hexdigest()
def snapshot():
 c=sqlite3.connect('file:'+str(root/'photos.sqlite3')+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
 try:
  rows=[dict(r) for r in c.execute('SELECT * FROM photos ORDER BY id')]
  live=[r for r in rows if r['status']!='deleted']
  used=[r for r in rows if r['status']=='used']
 finally:c.close()
 media={}
 for r in live:
  p=root/'images'/r['id'];need(p.is_file() and not p.is_symlink(),'original_image_missing')
  h=digest(p.read_bytes());need(h==r['digest'],'original_digest_mismatch');media[r['id']]=h
 return {'count':len(live),'photoRowsSha256':digest(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()),'usedRecordsSha256':digest(json.dumps(used,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()),'mediaSha256':media,'photos':[{'id':r['id'],'digest':r['digest']} for r in live]}
try:
 before=snapshot()
 if value['mode']=='snapshot':result={'ok':True,**before}
 else:
  expected=value['expected'];need(before['photoRowsSha256']==expected['photoRowsSha256'] and before['mediaSha256']==expected['mediaSha256'] and before['usedRecordsSha256']==expected['usedRecordsSha256'],'inventory_changed_before_thumbnail_apply')
  raw=base64.b64decode(value['archive'],validate=True);need(digest(raw)==value['archiveSha256'],'thumbnail_archive_digest')
  destination=root/'thumbnails';need(not destination.is_symlink(),'thumbnail_directory_symlink');destination.mkdir(exist_ok=True,mode=0o700);os.chmod(destination,0o700)
  added=skipped=0;variants={}
  with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
   for member in tar.getmembers():
    need(member.isfile() and not member.issym() and not member.islnk() and re.fullmatch(r'[0-9a-f]{32}\.jpg',member.name),'invalid_thumbnail_archive_member')
    pid=member.name[:-4];need(pid in before['mediaSha256'],'thumbnail_id_not_in_inventory');need(member.size<=250000,'thumbnail_too_large')
    data=tar.extractfile(member).read();need(data[:3]==b'\xff\xd8\xff' and data[-2:]==b'\xff\xd9','thumbnail_jpeg_header')
    h=digest(data);target=destination/member.name;need(not target.is_symlink(),'thumbnail_target_symlink')
    if target.exists():need(digest(target.read_bytes())==h,'existing_thumbnail_content_conflict');skipped+=1
    else:
     temporary=destination/(member.name+'.tmp-'+secrets.token_hex(6));fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
     try:
      with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
      os.replace(temporary,target);added+=1
     finally:
      if temporary.exists():temporary.unlink()
    need(target.stat().st_mode&0o777==0o600,'thumbnail_permissions')
    variants[pid]={'sha256':h,'bytes':len(data)}
  need(len(variants)==before['count'],'thumbnail_inventory_count')
  after=snapshot();need(before==after,'original_inventory_changed_during_thumbnail_apply')
  aggregate=digest(json.dumps(variants,sort_keys=True,separators=(',',':')).encode())
  result={'ok':True,'originalPhotoCount':before['count'],'variantsAdded':added,'identicalVariantsSkipped':skipped,'variantCount':len(variants),'thumbnailTotalBytes':sum(r['bytes'] for r in variants.values()),'thumbnailAggregateSha256':aggregate,'originalPhotoRowsSha256':after['photoRowsSha256'],'originalUsedRecordsSha256':after['usedRecordsSha256'],'allOriginalMediaSha256Preserved':True,'photoRowsAndUseRecordsUnchanged':True,'thumbnailPermissions':'0600','originalsMutated':False}
 print(json.dumps(result))
except Exception as e:
 message=str(e) if isinstance(e,RuntimeError) else type(e).__name__;print(json.dumps({'ok':False,'error':message,'secretsPrinted':False}));sys.exit(1)
'''


def need(value, code):
    if not value:
        raise admin_api.Failure(code)


def remote(args, value):
    value['dataDir'] = args.remote_data_dir
    command = shlex.join(['/opt/homebrew/bin/python3.12', '-c', REMOTE])
    call = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', args.ssh_host, command],
                          input=json.dumps(value).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    try:
        result = json.loads(call.stdout)
    except ValueError:
        raise admin_api.Failure('invalid_remote_thumbnail_response')
    need(call.returncode == 0 and result.get('ok'), result.get('error', 'remote_thumbnail_operation_failed'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, action='append', required=True, help='明确指定本地原图目录，可重复')
    parser.add_argument('--build-dir', type=Path, required=True, help='缩略图构建目录，应位于私有.local目录')
    parser.add_argument('--ssh-host', default='mac-studio')
    parser.add_argument('--remote-data-dir', required=True, help='明确指定本应用私有数据目录')
    parser.add_argument('--apply', action='store_true', help='显式构建并原子传输；默认只读匹配检查')
    args = parser.parse_args()
    sources = {}
    for directory in args.source_dir:
        need(directory.is_dir(), 'source_directory_missing')
        for path in directory.glob('AI示例_*.png'):
            need(path.is_file() and not path.is_symlink(), 'source_file_invalid')
            raw = path.read_bytes()
            sources.setdefault(hashlib.sha256(raw).hexdigest(), path)
    client = admin_api.Client()
    password = admin_api.PASSWORD_FILE.read_text().rstrip('\r\n')
    logged = False
    result = {'ok': False, 'atUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'mode': 'apply' if args.apply else 'inspect', 'secretsPrinted': False}
    try:
        client.request('/api/admin/login', 'POST', json_value={'password': password})
        del password
        logged = True
        stats, photos = client.inventory()
        before = remote(args, {'mode': 'snapshot'})
        need(set(photos) == {p['id'] for p in before['photos']}, 'admin_and_disk_inventory_disagree')
        need(all(p['digest'] in sources for p in before['photos']), 'local_original_sha_match_missing')
        result.update({'originalPhotoCount':before['count'], 'matchedSourceCount':len(before['photos']), 'beforeStats':stats})
        if not args.apply:
            result['ok'] = True
            result['remoteMutationsPerformed'] = False
            return result
        build = args.build_dir.resolve()
        need(not build.is_symlink(), 'thumbnail_build_symlink')
        build.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(build, 0o700)
        entries = []
        reusable = {}
        if (build / 'manifest.json').is_file():
            try:
                old_manifest = json.loads((build / 'manifest.json').read_text())
                if old_manifest.get('maxDimension') == 480 and old_manifest.get('quality') == 72:
                    reusable = {entry['photoId']:entry for entry in old_manifest.get('files', [])}
            except (ValueError, KeyError):
                pass
        for index, photo in enumerate(sorted(before['photos'], key=lambda p:p['id'])):
            previous = reusable.get(photo['id'])
            if previous and previous.get('sourceSha256') == photo['digest']:
                existing = build / previous.get('file', '')
                if existing.is_file() and not existing.is_symlink() and hashlib.sha256(existing.read_bytes()).hexdigest() == previous.get('sha256'):
                    entries.append(previous)
                    continue
            with Image.open(sources[photo['digest']]) as original:
                image = ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail((480, 480), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format='JPEG', quality=72, optimize=True, progressive=True)
                data = buffer.getvalue()
                entry = {'photoId':photo['id'],'sourceSha256':photo['digest'],'file':photo['id']+'.jpg','sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'width':image.width,'height':image.height}
            need(len(data) <= 250000, 'generated_thumbnail_too_large')
            target = build / entry['file']
            need(not target.is_symlink(), 'thumbnail_build_file_symlink')
            with target.open('wb') as handle:handle.write(data)
            os.chmod(target, 0o600)
            entries.append(entry)
            if (index + 1) % 15 == 0:
                print(json.dumps({'stage':'generated','count':index+1}), flush=True)
        manifest = {'count':len(entries),'maxDimension':480,'format':'JPEG','quality':72,'optimize':True,'progressive':True,'files':entries}
        (build/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        os.chmod(build/'manifest.json',0o600)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            for entry in entries:
                data=(build/entry['file']).read_bytes();member=tarfile.TarInfo(entry['file']);member.size=len(data);member.mode=0o600;member.mtime=0;archive.addfile(member,io.BytesIO(data))
        packed=stream.getvalue()
        applied=remote(args,{'mode':'apply','expected':before,'archive':base64.b64encode(packed).decode(),'archiveSha256':hashlib.sha256(packed).hexdigest()})
        stats_after,_=client.inventory()
        result.update(applied)
        result.update({'afterStats':stats_after,'archiveSha256':hashlib.sha256(packed).hexdigest(),'maxDimension':480,'jpegQuality':72})
        return result
    finally:
        if logged:
            try:
                client.request('/api/admin/logout','POST',json_value={})
                result['logout']=200
            except Exception:
                result['logout']='failed'
        if args.apply:
            output=ROOT/'output';output.mkdir(exist_ok=True);name='thumbnail-build-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json';(output/name).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    try:
        result = main()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        sys.exit(0 if result.get('ok') else 1)
    except Exception as error:
        safe=str(error) if isinstance(error,admin_api.Failure) else type(error).__name__
        print(json.dumps({'ok':False,'error':safe,'secretsPrinted':False}));sys.exit(1)
