"""Create a reviewable code-only export. Never git-add the entire acquisition folder."""
from pathlib import Path
import argparse,hashlib,json,re,shutil

ROOT=Path(__file__).resolve().parents[1]


def export(destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Use a new empty export directory')
    destination.mkdir(parents=True)
    files=[]
    for directory in ['src','tests','scripts']:
        for p in (ROOT/directory).rglob('*'):
            if p.suffix in ('.py','.ps1') and '__pycache__' not in p.parts and not (directory=='src' and 'tests' in p.relative_to(ROOT/'src').parts):files.append(p)
    files += [ROOT/n for n in ['main.py','pyproject.toml','uv.lock','.python-version','.gitignore','.gitattributes','metadata.toml','README.md','AGENTS.md','.github/workflows/ci.yml','.dvc/config','.dvc/.gitignore','config/config.toml','config/brainco.toml','config/transfer.toml','assets/manifest.csv','assets/schedule.csv','assets/download-index.json']]
    files += list(ROOT.glob('*.bat'))
    files += list((ROOT/'docs').glob('*.md'))
    files += [ROOT/'assets/README.md',ROOT/'data/README.md']
    vendor=ROOT/'vendor/brainco_standalone'
    names=['brainco_adapter.py','brainco_full_stream.py','brainco_sequence.py','brainco_wire.py','brainco_worker.py','continuous_eeg.py','device_lease.py','eeg.py','sample_clock.py']
    files += [vendor/n for n in names]
    files += [p for p in (vendor/'vendor/brainco_reference').rglob('*') if p.suffix in ('.py','.md')]
    files += [vendor/'tests/test_brainco_wire.py',vendor/'tests/test_brainco_reorder.py']
    for source in files:
        if not source.is_file():raise FileNotFoundError(source)
        relative=source.relative_to(ROOT);target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    # Machine-independent entry configuration; never distribute a selected subject.
    config=destination/'config/brainco.toml'
    config.write_text(re.sub(r'(?m)^participant = .*$', 'participant = ""',config.read_text(encoding='utf-8')),encoding='utf-8')
    # Reject credential formats, private-key bodies and unexpectedly large source files.
    report=[]
    for p in destination.rglob('*'):
        if not p.is_file():continue
        if p.stat().st_size>10*1024*1024:raise ValueError('Unexpected large file '+str(p))
        body=p.read_bytes()
        if re.search(rb'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,})|AKIA[A-Z0-9]{16}',body):raise ValueError('Secret pattern in '+str(p))
        report.append(dict(path=p.relative_to(destination).as_posix(),size=len(body),sha256=hashlib.sha256(body).hexdigest()))
    print(json.dumps(dict(files=len(report),bytes=sum(x['size'] for x in report)),indent=2))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination');args=parser.parse_args();export(args.destination)
