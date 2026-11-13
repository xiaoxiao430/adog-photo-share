#!/usr/bin/env python3
"""经stdin安全暂存私有成套映射；可在隔离副本检查迁移，不启动正式版本。"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shlex
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
REMOTE = r'''
import base64,datetime,hashlib,json,os,re,runpy,secrets,shutil,sqlite3,sys,tempfile
from pathlib import Path
value=json.load(sys.stdin);private=Path('/Users/liuminghao/.config/aidog-photos');data=Path('/Users/liuminghao/services/aidog-photos/data');target=private/'initial-packages.json';runtime=private/'config.json'
def need(ok,code):
 if not ok:raise RuntimeError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def rows(path,table):
 c=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
 try:return [dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
 finally:c.close()
def image_hashes(directory):
 return {p.name:sha(p.read_bytes()) for p in directory.iterdir() if p.is_file()}
def digest_rows(data):return sha(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
def fingerprint(directory):
 photos=rows(directory/'photos.sqlite3','photos');batches=rows(directory/'photos.sqlite3','batches')
 return {'photos':photos,'batches':batches,'media':image_hashes(directory/'images'),'thumbnails':image_hashes(directory/'thumbnails')}
def package_counts(path):
 c=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
 try:
  total=c.execute('SELECT count(*) FROM packages').fetchone()[0]
  available=c.execute("SELECT count(*) FROM packages k WHERE (SELECT count(*) FROM package_photos q JOIN photos p ON p.id=q.photo_id WHERE q.package_id=k.id AND p.status='available')=3").fetchone()[0]
  used=c.execute("SELECT count(*) FROM packages k WHERE EXISTS(SELECT 1 FROM package_photos q JOIN photos p ON p.id=q.photo_id WHERE q.package_id=k.id AND p.status='used')").fetchone()[0]
  return {'total':total,'available':available,'containsUsed':used,'photoMappings':c.execute('SELECT count(*) FROM package_photos').fetchone()[0]}
 finally:c.close()
try:
 raw=base64.b64decode(value['manifest'],validate=True);need(sha(raw)==value['manifestSha256'],'manifest_digest_mismatch');manifest=json.loads(raw);items=manifest.get('packages',[]);flat=[pid for x in items for pid in x.get('photoIds',[])];need(len(items)==15 and len(flat)==45 and len(set(flat))==45,'manifest_group_count')
 need(len({x['key'] for x in items})==15 and all(len(x['photoIds'])==3 for x in items),'manifest_group_keys')
 c=sqlite3.connect('file:'+str(data/'photos.sqlite3')+'?mode=ro',uri=True)
 try:
  actual={r[0] for r in c.execute("SELECT id FROM photos WHERE status!='deleted'")};need(actual==set(flat),'mapping_photo_set_does_not_match_inventory')
  imported=c.execute("SELECT value FROM settings WHERE key='packages_initial_import'").fetchone()
 finally:c.close()
 config_sha=sha(runtime.read_bytes());need(not target.is_symlink(),'manifest_target_symlink');created=False
 if target.exists():
  need(sha(target.read_bytes())==sha(raw),'existing_manifest_differs')
 else:
  need(not imported,'initial_import_already_recorded');temporary=target.with_name(target.name+'.tmp-'+secrets.token_hex(6));fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  try:
   with os.fdopen(fd,'wb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())
   os.replace(temporary,target);created=True
  finally:
   if temporary.exists():temporary.unlink()
 need(target.stat().st_mode&0o777==0o600,'manifest_permissions')
 result={'ok':True,'atUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'manifestCreated':created,'manifestSha256':sha(raw),'packageCount':len(items),'photoReferences':len(flat),'manifestMode':'0600','productionDatabaseMutated':False,'productionServiceRestarted':False}
 if value.get('checkMigration'):
  source=base64.b64decode(value['source'],validate=True);need(sha(source)==value['sourceSha256'],'source_digest_mismatch')
  with tempfile.TemporaryDirectory(prefix='aidog-package-migration-check-') as temp:
   temp=Path(temp);clone=temp/'data';clone.mkdir(mode=0o700);cfg=temp/'private';cfg.mkdir(mode=0o700)
   readonly=sqlite3.connect('file:'+str(data/'photos.sqlite3')+'?mode=ro',uri=True);copied=sqlite3.connect(clone/'photos.sqlite3')
   try:readonly.backup(copied)
   finally:copied.close();readonly.close()
   shutil.copytree(data/'images',clone/'images');shutil.copytree(data/'thumbnails',clone/'thumbnails');shutil.copy2(runtime,cfg/'config.json');shutil.copy2(target,cfg/'initial-packages.json')
   candidate=temp/'server.py';candidate.write_bytes(source);os.chmod(candidate,0o600)
   before=fingerprint(clone)
   os.environ.update({'AIDOG_PHOTOS_DATA':str(clone),'AIDOG_PHOTOS_CONFIG':str(cfg/'config.json'),'AIDOG_PHOTOS_INITIAL_PACKAGES':str(cfg/'initial-packages.json'),'AIDOG_PHOTOS_ORIGIN':'https://jmryqyx.cn','AIDOG_PHOTOS_BASE':'/aidog-photos'})
   namespace=runpy.run_path(str(candidate));need(namespace.get('PHOTO_SCHEMA_VERSION')==3,'candidate_schema_version');namespace['init']()
   after=fingerprint(clone);need(before['photos']==after['photos'],'migration_changed_photo_columns');need(before['media']==after['media'] and before['thumbnails']==after['thumbnails'],'migration_changed_media')
   old_batches={r['id']:r for r in before['batches']};new_batches={r['id']:r for r in after['batches']};need(all(all(new_batches[bid].get(key)==v for key,v in row.items()) for bid,row in old_batches.items()),'migration_changed_historical_batches')
   counts=package_counts(clone/'photos.sqlite3');need(counts['total']==15 and counts['photoMappings']==45,'package_import_count')
   first_packages=rows(clone/'photos.sqlite3','packages');first_mappings=rows(clone/'photos.sqlite3','package_photos');namespace['init']();need(first_packages==rows(clone/'photos.sqlite3','packages') and first_mappings==rows(clone/'photos.sqlite3','package_photos'),'initial_import_not_idempotent')
   c=sqlite3.connect('file:'+str(clone/'photos.sqlite3')+'?mode=ro',uri=True)
   try:marker=c.execute("SELECT value FROM settings WHERE key='packages_initial_import'").fetchone();columns=[r[1] for r in c.execute('PRAGMA table_info(batches)')]
   finally:c.close()
   statuses={status:sum(r['status']==status for r in before['photos']) for status in ['available','reserved','used']}
   result['isolatedMigration']={'ok':True,'sourceSha256':sha(source),'originalPhotoCount':len(before['photos']),'statusesPreserved':statuses,'photoAllColumnsPreserved':True,'oldBatchColumnsPreserved':True,'originalImageCount':len(before['media']),'originalThumbnailCount':len(before['thumbnails']),'allOriginalMediaSha256Preserved':True,'photoRowsSha256':digest_rows(after['photos']),'usedRecordsSha256':digest_rows([r for r in after['photos'] if r['status']=='used']),'packages':counts,'initialImportMarkerPresent':bool(marker),'secondInitNoDuplicates':True,'batchColumnCount':len(columns),'newBatchColumnsPresent':all(x in columns for x in ['package_id','copy_snapshot','title_snapshot']),'noProductionDataWrite':True,'temporaryCopyRemovedAtExit':True}
 need(sha(runtime.read_bytes())==config_sha,'production_runtime_config_changed');result['productionRuntimeConfigByteIdentical']=True
 print(json.dumps(result))
except Exception as error:
 safe=str(error) if isinstance(error,RuntimeError) else type(error).__name__;print(json.dumps({'ok':False,'error':safe,'secretsPrinted':False}));sys.exit(1)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--check-migration',action='store_true')
    args=parser.parse_args()
    path=args.manifest
    if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode)!=0o600:raise RuntimeError('private_manifest_permissions')
    raw=path.read_bytes();value={'manifest':base64.b64encode(raw).decode(),'manifestSha256':hashlib.sha256(raw).hexdigest(),'checkMigration':args.check_migration}
    if args.check_migration:
        source=(ROOT/'server.py').read_bytes();value.update({'source':base64.b64encode(source).decode(),'sourceSha256':hashlib.sha256(source).hexdigest()})
    command=shlex.join(['/opt/homebrew/bin/python3.12','-c',REMOTE])
    call=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','mac-studio',command],input=json.dumps(value).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    try:result=json.loads(call.stdout)
    except ValueError:result={'ok':False,'error':'invalid_staging_response','secretsPrinted':False}
    output=ROOT/'output';output.mkdir(exist_ok=True)
    import datetime
    name='package-migration-isolation-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json';(output/name).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));print('receipt='+name)
    return 0 if call.returncode==0 and result.get('ok') else 1


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:
        safe=str(error) if isinstance(error,RuntimeError) else type(error).__name__;print(json.dumps({'ok':False,'error':safe,'secretsPrinted':False}));sys.exit(1)
