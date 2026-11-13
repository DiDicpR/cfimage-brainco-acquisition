import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from src.nas_transfer import SMBStore,download_assets,publish_assets,safe_path,snapshot_subject,upload_subject,restore_subject
from src.storage.database import SCHEMA


def fixture(root):
    native=root/'data/native';native.mkdir(parents=True)
    db=sqlite3.connect(native/'progress.sqlite');db.executescript(SCHEMA)
    for p in ['s001','s002']:
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',(p,p,1,'2026-11-13T01:00:00+00:00','2026-11-13T01:01:00+00:00','h',1,'p','brainco','brainco','completed'))
        folder=native/p/p;folder.mkdir(parents=True)
        (folder/'raw.bin').write_bytes((p*100).encode())
    db.commit();db.close();return native


class TransferTests(unittest.TestCase):
    def test_images_use_anonymous_s3_while_data_keeps_authenticated_backend(self):
        from src.nas_transfer import settings,store_for,ROOT
        cfg=settings(ROOT)
        self.assertEqual(cfg['asset_backend'],'s3')
        with patch('src.nas_transfer.S3Store') as s3,patch('src.nas_transfer.SMBStore') as smb:
            store_for(cfg,assets=True)
            s3.assert_called_once_with(cfg,assets=True,write=False)
            store_for(cfg,assets=False,write=True)
            smb.assert_called_once_with(cfg['data_root'])

    def test_anonymous_client_ignores_missing_aws_profile(self):
        import os
        from src.nas_transfer import settings,S3Store,ROOT
        from botocore import UNSIGNED
        with patch.dict(os.environ,{'AWS_PROFILE':'missing-profile-for-test'}),patch('botocore.session.Session.get_credentials',side_effect=AssertionError('Must not request credentials')):
            cfg=settings(ROOT);cfg['profile']='missing-profile-for-test'
            store=S3Store(cfg,assets=True)
            self.assertEqual(store.client.meta.config.signature_version,UNSIGNED)
            store.client.close()

    def test_path_escape_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in ['../outside','/absolute','a/../b','C:/file','x\\y','a//b']:
                with self.assertRaises(ValueError):safe_path(tmp,key)

    def test_asset_roundtrip_skip_and_bad_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project';(root/'assets/stimuli').mkdir(parents=True)
            image=root/'assets/stimuli/x.jpg';image.write_bytes(b'image')
            index=dict(schema_version=1,prefix='CFIMAGE-20261113',files=[dict(path='stimuli/x.jpg',size=5,sha256=hashlib.sha256(b'image').hexdigest())])
            (root/'assets/download-index.json').write_text(json.dumps(index))
            store=SMBStore(Path(tmp)/'nas')
            publish_assets(root,store,lambda _:None)
            image.unlink()
            self.assertEqual(download_assets(root,store,lambda _:None)['downloaded'],1)
            self.assertEqual(download_assets(root,store,lambda _:None)['downloaded'],0)
            image.unlink();(store.root/'stimuli/x.jpg').write_bytes(b'wrong')
            with self.assertRaises(IOError):download_assets(root,store,lambda _:None)
            self.assertFalse(image.exists())

    def test_subject_isolation_running_guard_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project';native=fixture(root);nas=SMBStore(Path(tmp)/'nas')
            result=upload_subject(native,'s001',root,nas,lambda _:None)
            restored=Path(tmp)/'restored';restore_subject(result['snapshot_key'],restored,root,nas,lambda _:None)
            self.assertEqual((restored/'native/s001/s001/raw.bin').read_bytes(),b's001'*100)
            self.assertFalse((restored/'native/s002').exists())
            db=sqlite3.connect(restored/'progress.sqlite')
            try:self.assertEqual(db.execute('SELECT participant_id FROM sessions').fetchall(),[('s001',)])
            finally:db.close()
            again=upload_subject(native,'s001',root,nas,lambda _:None)
            self.assertEqual(again['snapshot_key'],result['snapshot_key'])
            db=sqlite3.connect(native/'progress.sqlite');db.execute("UPDATE sessions SET status='running' WHERE participant_id='s001'");db.commit();db.close()
            with self.assertRaises(RuntimeError):upload_subject(native,'s001',root,nas,lambda _:None)

    def test_failed_upload_never_commits_snapshot(self):
        class BrokenStore(SMBStore):
            def write(self,key,source):raise OSError('Disconnected')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project';native=fixture(root);store=BrokenStore(Path(tmp)/'nas')
            with self.assertRaises(OSError):upload_subject(native,'s001',root,store,lambda _:None)
            self.assertEqual(list(store.root.rglob('snapshots/*.json')),[])
