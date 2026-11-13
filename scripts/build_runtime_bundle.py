"""Build a hash-pinned Windows runtime mirror; publish separately with authorized credentials."""
import argparse,concurrent.futures,hashlib,json,re,shutil,tarfile,tomllib,urllib.parse,urllib.request,zipfile
from pathlib import Path
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename,canonicalize_name

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def download(url,path,expected=None):
    if path.exists() and (not expected or digest(path)==expected):return
    temporary=path.with_suffix(path.suffix+'.part')
    with urllib.request.urlopen(url,timeout=90) as src,temporary.open('wb') as dst:shutil.copyfileobj(src,dst,4*1024*1024)
    if expected and digest(temporary)!=expected:raise ValueError('Upstream file hash mismatch: '+path.name)
    temporary.replace(path)


def build(folder,uv,python_url):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    bundle=folder/'bundle';bundle.mkdir(exist_ok=True)
    wheels=bundle/'wheels';wheels.mkdir(exist_ok=True)
    requirements=(ROOT/'config/requirements-runtime.txt').read_text(encoding='utf-8')
    names=set()
    for line in requirements.splitlines():
        if not line or line[0].isspace() or line.startswith('#'):continue
        req=Requirement(line.removesuffix('\\').strip())
        if not req.marker or req.marker.evaluate():names.add(canonicalize_name(req.name))
    lock=tomllib.loads((ROOT/'uv.lock').read_text(encoding='utf-8'))
    tags=set(sys_tags());selected=[]
    for package in lock['package']:
        if canonicalize_name(package['name']) not in names:continue
        for wheel in package.get('wheels',[]):
            filename=urllib.parse.unquote(urllib.parse.urlparse(wheel['url']).path.rsplit('/',1)[-1])
            if parse_wheel_filename(filename)[3]&tags:
                selected.append((wheel['url'],wheels/filename,wheel['hash'].removeprefix('sha256:')));break
        else:raise ValueError('No compatible Windows wheel for '+package['name'])
    if len(selected)!=len(names):raise ValueError('Incomplete lock package selection')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for n,_ in enumerate(pool.map(lambda args:download(*args),selected),1):print(f'Wheel {n}/{len(selected)}',flush=True)
    python_archive=folder/'python.tar.gz'
    download(python_url,python_archive)
    if not (bundle/'python/python.exe').exists():
        with tarfile.open(python_archive) as tf:tf.extractall(bundle,filter='data')
    if not (bundle/'python/python.exe').is_file():raise ValueError('Python distribution layout mismatch')
    shutil.copyfile(uv,bundle/'uv.exe')
    manifest=dict(schema_version=1,python_source=python_url,python_archive_sha256=digest(python_archive),
                  files=[dict(path=p.relative_to(bundle).as_posix(),sha256=digest(p),size=p.stat().st_size) for p in sorted(bundle.rglob('*')) if p.is_file()])
    (bundle/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    archive=folder/'runtime.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3) as z:
        for file in sorted(bundle.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(bundle).as_posix(),compress_type=zipfile.ZIP_STORED if file.suffix=='.whl' else zipfile.ZIP_DEFLATED)
    h=digest(archive);key='CFIMAGE-20261113/_runtime/win-x64-'+h[:16]+'.zip'
    config=dict(schema_version=1,platform='windows-x86_64',sha256=h,size=archive.stat().st_size,
                urls=['http://172.16.4.2:9000/oi-task-assets/'+key,'http://truenas-s658v2.omni-intel.cn:9000/oi-task-assets/'+key],
                uv_lock_sha256=digest(ROOT/'uv.lock'),requirements_sha256=digest(ROOT/'config/requirements-runtime.txt'),
                wheel_count=len(selected),wheel_bytes=sum(p.stat().st_size for _,p,_ in selected))
    (ROOT/'config/runtime-bootstrap.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
    print(json.dumps(config,indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--folder',required=True);p.add_argument('--uv',required=True);p.add_argument('--python-url',required=True)
    args=p.parse_args();build(args.folder,args.uv,args.python_url)
