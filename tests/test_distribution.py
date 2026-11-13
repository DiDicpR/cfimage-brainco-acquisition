import csv,hashlib,json,unittest
from pathlib import Path
from src.config import load_config

ROOT=Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_pinned_manifest_schedule_and_download_index_without_images(self):
        config=load_config(ROOT/'config/brainco.toml')
        for name in ('manifest','schedule'):
            self.assertEqual(hashlib.sha256(config.assets[name].read_bytes()).hexdigest(),config.assets[name+'_sha256'])
        rows=list(csv.DictReader(config.assets['manifest'].open(encoding='utf-8',newline='')))
        index=json.loads((ROOT/'assets/download-index.json').read_text(encoding='utf-8'))
        self.assertEqual(len(index['files']),2580)
        self.assertEqual({r['file_path']:r['sha256'] for r in rows},{r['path']:r['sha256'] for r in index['files']})

    def test_blank_subject_can_open_gui_but_is_not_a_valid_collection_identity(self):
        import tempfile
        text=(ROOT/'config/brainco.toml').read_text(encoding='utf-8')
        import re
        text=re.sub(r'(?m)^participant = .*$', 'participant = ""',text)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'brainco.toml';path.write_text(text,encoding='utf-8')
            self.assertEqual(load_config(path).protocol['participant'],'')
