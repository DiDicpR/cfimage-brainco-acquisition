"""Verified, resumable file transfers. No credentials in source or reports."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 4 * 1024 * 1024


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(BLOCK), b''): h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def comparable_path(path):
    """Windows may resolve an existing child with an extended prefix only."""
    value=str(path)
    if os.name=='nt':
        if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
        elif value.startswith('\\\\?\\'):value=value[4:]
    return Path(value)


def safe_path(root, key):
    p = PurePosixPath(key)
    if not key or p.is_absolute() or any(x in ('', '.', '..') for x in key.split('/')) or '\\' in key or ':' in key:
        raise ValueError('Unsafe relative transfer path')
    root = Path(root).resolve()
    result = root.joinpath(*p.parts).resolve()
    # Compare equivalent Windows spellings, retaining realpath/symlink checks.
    # Re-resolve the root after the child: another download may have created it.
    boundary=comparable_path(root.resolve())
    if not comparable_path(result).is_relative_to(boundary):
        raise ValueError(f'Transfer path escapes its root: key={key!r}, root={str(boundary)!r}, resolved={str(result)!r}. Check directory junctions/symlinks.')
    return result


def settings(root=ROOT):
    with (root/'config/transfer.toml').open('rb') as f: c = tomllib.load(f)
    local = root/'config/transfer.local.toml'
    if local.exists():
        with local.open('rb') as f: overrides = tomllib.load(f)
        if set(overrides)-set(c): raise ValueError('Unknown transfer configuration field')
        c.update(overrides)
    if c['backend'] not in ('smb', 's3'): raise ValueError('backend must be smb or s3')
    if c.get('asset_backend',c['backend']) not in ('smb','s3'): raise ValueError('asset_backend must be smb or s3')
    if c['prefix'] != 'CFIMAGE-20261113': raise ValueError('Unexpected NAS prefix')
    return c


class SMBStore:
    def __init__(self, root):
        value=str(Path(root).absolute())
        if os.name=='nt' and not value.startswith('\\\\?\\'):
            value='\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value
        self.root=Path(value)
    def exists(self, key): return safe_path(self.root, key).is_file()
    def read(self, key, target):
        with safe_path(self.root,key).open('rb') as src, Path(target).open('wb') as dst:
            shutil.copyfileobj(src, dst, BLOCK)
    def digest(self, key): return sha256(safe_path(self.root,key))
    def write(self, key, source):
        target = safe_path(self.root,key)
        target.parent.mkdir(parents=True,exist_ok=True)
        partial = target.with_name('.upload-'+uuid.uuid4().hex+'.partial')
        try:
            with Path(source).open('rb') as src, partial.open('xb') as dst:
                shutil.copyfileobj(src,dst,BLOCK);dst.flush();os.fsync(dst.fileno())
            if sha256(partial) != sha256(source): raise IOError('NAS write readback mismatch')
            os.replace(partial,target)
        finally:
            if partial.exists(): partial.unlink()


class S3Store:
    def __init__(self, c, *, assets, write=False):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        self.bucket = c['asset_bucket'] if assets else c['data_bucket']
        self.prefix = c['prefix']+'/'
        if assets and not write:
            # Public images must not depend on an operator's AWS account/profile.
            import botocore.session
            from botocore.configprovider import ConstantProvider
            core=botocore.session.get_session()
            core.get_component('config_store').set_config_provider('profile',ConstantProvider(None))
            core.set_config_variable('config_file',os.devnull)
            core.set_config_variable('credentials_file',os.devnull)
            session=boto3.Session(botocore_session=core)
        else:
            session = boto3.Session(profile_name=c['profile'] or None)
        if write and session.get_credentials() is None:
            raise RuntimeError('S3 upload requires a local AWS profile or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY; SMB credentials are not S3 keys.')
        self.client = session.client('s3',endpoint_url=c['endpoint'],region_name='us-east-1',
            config=Config(signature_version='s3v4' if write or not assets else UNSIGNED,
                          proxies={},connect_timeout=10,read_timeout=60,retries={'max_attempts':3},s3={'addressing_style':'path'}))
    def exists(self,key):
        from botocore.exceptions import ClientError
        try: self.client.head_object(Bucket=self.bucket,Key=self.prefix+key);return True
        except ClientError as e:
            if e.response['Error']['Code'] in ('404','NoSuchKey','NotFound'): return False
            raise
    def read(self,key,target): self.client.download_file(self.bucket,self.prefix+key,str(target))
    def write(self,key,source): self.client.upload_file(str(source),self.bucket,self.prefix+key)
    def digest(self,key):
        response=self.client.get_object(Bucket=self.bucket,Key=self.prefix+key)
        h=hashlib.sha256()
        with closing(response['Body']) as stream:
            for block in iter(lambda:stream.read(BLOCK),b''):h.update(block)
        return h.hexdigest()


def store_for(c, *, assets, write=False, root=ROOT):
    backend=c.get('asset_backend',c['backend']) if assets else c['backend']
    return SMBStore(c['asset_root'] if assets else c['data_root']) if backend=='smb' else S3Store(c,assets=assets,write=write)


def asset_index(root=ROOT):
    index=json.loads((root/'assets/download-index.json').read_text(encoding='utf-8'))
    if index.get('schema_version')!=1 or index.get('prefix')!='CFIMAGE-20261113': raise ValueError('Invalid asset index')
    seen=set()
    for item in index['files']:
        safe_path(root/'assets',item['path'])
        if item['path'].casefold() in seen or not re.fullmatch('[0-9a-f]{64}',item['sha256']):raise ValueError('Invalid/duplicate asset entry')
        seen.add(item['path'].casefold())
    return index


def download_assets(root=ROOT, store=None, progress=print):
    # Stabilize the canonical root before index checks and parallel mkdir calls.
    (root/'assets').mkdir(parents=True,exist_ok=True)
    index=asset_index(root); store=store or store_for(settings(root),assets=True,root=root)
    def prepare(item):
        target=safe_path(root/'assets',item['path'])
        if target.is_file() and target.stat().st_size==item['size'] and sha256(target)==item['sha256']:
            return 0
        target.parent.mkdir(parents=True,exist_ok=True)
        partial=target.with_name(target.name+'.'+uuid.uuid4().hex+'.partial')
        try:
            store.read(item['path'],partial)
            if partial.stat().st_size!=item['size'] or sha256(partial)!=item['sha256']:raise IOError('Asset checksum mismatch: '+item['path'])
            os.replace(partial,target)
        finally:
            if partial.exists():partial.unlink()
        return 1
    downloaded=0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for n,changed in enumerate(pool.map(prepare,index['files']),1):
            downloaded+=changed
            if n%25==0 or n==len(index['files']):progress(f'Verified {n}/{len(index["files"])}, downloaded: {downloaded}')
    progress(f'Assets verified: {len(index["files"])}, downloaded: {downloaded}')
    return {'verified':len(index['files']),'downloaded':downloaded}


def publish_assets(root=ROOT, store=None, progress=print):
    index=asset_index(root); store=store or store_for(settings(root),assets=True,write=True,root=root)
    for n,item in enumerate(index['files'],1):
        source=safe_path(root/'assets',item['path'])
        if sha256(source)!=item['sha256']:raise ValueError('Local asset changed: '+item['path'])
        if not store.exists(item['path']) or store.digest(item['path'])!=item['sha256']:
            store.write(item['path'],source)
        if store.digest(item['path'])!=item['sha256']:raise IOError('Remote asset mismatch')
        if n%50==0 or n==len(index['files']):progress(f'Publish {n}/{len(index["files"])}')
    # Only publish the index after all objects are readable and verified.
    store.write('download-index.json',root/'assets/download-index.json')
    return {'published':len(index['files'])}


def subject_database(source, destination, participant):
    """New database contains only this subject, never other subjects' deleted pages."""
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.execute('BEGIN')
        active=src.execute("SELECT COUNT(*) FROM sessions WHERE participant_id=? AND status='running'",(participant,)).fetchone()[0]
        if active:raise RuntimeError('This subject has a running/unfinalized session; stop and finalize it before upload.')
        if not src.execute('SELECT COUNT(*) FROM sessions WHERE participant_id=?',(participant,)).fetchone()[0]:raise ValueError('No recorded sessions for this subject')
        schema=src.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for table,sql in schema:
            dst.execute(sql)
            cols=[r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            if 'participant_id' in cols: where='participant_id=?'
            elif 'session_id' in cols: where='session_id IN (SELECT session_id FROM sessions WHERE participant_id=?)'
            elif 'event_id' in cols:where='event_id IN (SELECT event_id FROM events WHERE session_id IN (SELECT session_id FROM sessions WHERE participant_id=?))'
            elif 'trial_id' in cols:where='trial_id IN (SELECT trial_id FROM trials WHERE session_id IN (SELECT session_id FROM sessions WHERE participant_id=?))'
            elif 'recording_id' in cols:where='recording_id IN (SELECT recording_id FROM eeg_recordings WHERE session_id IN (SELECT session_id FROM sessions WHERE participant_id=?))'
            else:raise ValueError('No subject filter for table '+table)
            rows=src.execute(f'SELECT * FROM "{table}" WHERE {where}',(participant,))
            dst.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in cols)})',rows)
        dst.commit()
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise IOError('Subject database integrity error')


def snapshot_subject(dataset_root, participant, destination):
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',participant):raise ValueError('Invalid subject ID')
    dataset_root=Path(dataset_root).resolve(); destination=Path(destination)
    subject=safe_path(dataset_root,participant)
    if not subject.is_dir():raise ValueError('No local data for this subject')
    subject_database(dataset_root/'progress.sqlite',destination/'progress.sqlite',participant)
    sources=[(subject,'native/'+participant)]
    handoff=dataset_root.parent/('sub-'+participant.removeprefix('sub-'))
    if handoff.is_dir():sources.append((handoff,'handoff/'+handoff.name))
    for folder,prefix in sources:
        for file in sorted(folder.rglob('*')):
            if file.is_symlink() or not file.resolve().is_relative_to(folder.resolve()):raise ValueError('Linked file in subject directory')
            if not file.is_file() or file.name.endswith(('-wal','-shm','.partial')):continue
            target=safe_path(destination,prefix+'/'+file.relative_to(folder).as_posix());target.parent.mkdir(parents=True,exist_ok=True)
            before=file.stat()
            with file.open('rb') as stream: is_db=stream.read(16)==b'SQLite format 3\x00'
            if is_db:
                with closing(sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(target)) as copy:
                    original.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise IOError('Invalid recording database')
            else:shutil.copyfile(file,target)
            after=file.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('Data changed during upload snapshot; stop acquisition first')


def upload_subject(dataset_root,participant,root=ROOT,store=None,progress=print):
    # Same process-independent lease used by acquisition; never race a recording.
    from .acquisition_devices.brainco_native import kit_module
    local=root/'.local';local.mkdir(exist_ok=True)
    machine_file=local/'machine-id'
    if not machine_file.exists():
        try:
            with machine_file.open('x') as f:f.write(uuid.uuid4().hex)
        except FileExistsError:pass
    machine=machine_file.read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}',machine):raise ValueError('Invalid local machine ID')
    store=store or store_for(settings(root),assets=False,write=True,root=root)
    with kit_module('device_lease').DeviceLease(), tempfile.TemporaryDirectory(prefix='upload-',dir=local) as tmp:
        staging=Path(tmp); progress('Creating a consistent snapshot of this subject...')
        snapshot_subject(dataset_root,participant,staging)
        entries=[]
        prefix='sub-'+participant.removeprefix('sub-')+'/'+machine
        for file in sorted(staging.rglob('*')):
            if not file.is_file():continue
            relative=file.relative_to(staging).as_posix();digest=sha256(file)
            key=prefix+'/objects/'+digest
            if not store.exists(key) or store.digest(key)!=digest:store.write(key,file)
            if store.digest(key)!=digest:raise IOError('NAS readback hash mismatch: '+relative)
            entries.append(dict(path=relative,key=key,sha256=digest,size=file.stat().st_size))
            progress(f'Uploaded and verified {len(entries)} files')
        manifest=dict(schema_version=1,participant_id=participant,machine_id=machine,files=entries,
                      quality='backup_only_not_eegqc_approved',hardware_sync_verified=False)
        content=canonical(manifest);snapshot=hashlib.sha256(content).hexdigest()
        manifest_file=staging/'upload-manifest.json';manifest_file.write_bytes(content)
        key=prefix+'/snapshots/'+snapshot+'.json'
        store.write(key,manifest_file)
        if store.digest(key)!=snapshot:raise IOError('Manifest readback failed')
        result=dict(participant=participant,files=len(entries),bytes=sum(x['size'] for x in entries),snapshot_key=key,verified=True)
        reports=root/'reports/transfers';reports.mkdir(parents=True,exist_ok=True)
        (reports/(snapshot+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
        return result


def restore_subject(snapshot_key,destination,root=ROOT,store=None,progress=print):
    store=store or store_for(settings(root),assets=False,root=root)
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise ValueError('Restore destination must be empty')
    destination.mkdir(parents=True,exist_ok=True)
    expected=PurePosixPath(snapshot_key).stem
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Invalid snapshot key')
    with tempfile.TemporaryDirectory() as tmp:
        manifest=Path(tmp)/'manifest.json';store.read(snapshot_key,manifest)
        if sha256(manifest)!=expected:raise IOError('Snapshot manifest hash mismatch')
        value=json.loads(manifest.read_text(encoding='utf-8'))
        for item in value['files']:
            target=safe_path(destination,item['path']);target.parent.mkdir(parents=True,exist_ok=True)
            partial=target.with_name(target.name+'.partial');store.read(item['key'],partial)
            if partial.stat().st_size!=item['size'] or sha256(partial)!=item['sha256']:raise IOError('Restore content checksum mismatch')
            partial.replace(target);progress('Restored '+item['path'])
        shutil.copyfile(manifest,destination/'upload-manifest.json')
    return {'restored':len(value['files'])}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('action',choices=['download','publish','upload','restore']);p.add_argument('--participant');p.add_argument('--config',default='config/brainco.toml');p.add_argument('--snapshot');p.add_argument('--output')
    args=p.parse_args(argv)
    try:
        if args.action=='download':result=download_assets()
        elif args.action=='publish':result=publish_assets()
        elif args.action=='restore':
            if not args.snapshot or not args.output:raise ValueError('--snapshot and --output are required')
            result=restore_subject(args.snapshot,args.output)
        else:
            from .config import load_config
            if not args.participant:raise ValueError('--participant is required')
            result=upload_subject(load_config(ROOT/args.config).output_dir,args.participant)
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as e:print(f'Transfer failed: {type(e).__name__}: {e}');return 1

if __name__=='__main__':raise SystemExit(main())
