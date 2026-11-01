import io, json, os, tempfile, unittest, zipfile, tarfile
from pathlib import Path

class AutoVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); os.environ['AUTOVAULT_HOME']=self.tmp.name
        import sys; sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'autovault'))
        import app
        self.app=app; app.ROOT=Path(self.tmp.name); app.LIB=app.ROOT/'workflows'; app.DB=app.ROOT/'autovault.sqlite3'; app.init()
    def tearDown(self):
        self.tmp.cleanup(); os.environ.pop('AUTOVAULT_HOME',None)
    def test_id_path_rejects_traversal(self):
        with self.assertRaises(ValueError): self.app.path_for('../escape')
    def test_create_and_git_snapshot(self):
        wid='sample-automation'; p=self.app.path_for(wid); p.mkdir()
        (p/'main.py').write_text('print(1)')
        with self.app.db() as c: c.execute("INSERT INTO workflows(id,name,kind,created,updated) VALUES(?,?,?,?,?)",(wid,'Sample','python',self.app.now(),self.app.now()))
        rev=self.app.snapshot(wid,'Initial')
        self.assertEqual(len(rev),12); self.assertIn('main.py',self.app.git(p,'ls-files').stdout)
    def test_file_path_is_confined(self):
        p=self.app.path_for('test'); p.mkdir(); (p/'ok.txt').write_text('ok')
        self.assertEqual((p/'ok.txt').read_text(),'ok')
        self.assertNotIn(p.resolve(),(p/'..'/'outside').resolve().parents)
    def test_zip_import_keeps_project_tree(self):
        p=self.app.path_for('project'); p.mkdir()
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('src/main.py','print("ok")')
            z.writestr('README.md','# sample')
        self.assertEqual(self.app.safe_extract_zip(buf.getvalue(),p),2)
        self.assertEqual((p/'src'/'main.py').read_text(),'print("ok")')
    def test_zip_import_rejects_path_traversal(self):
        p=self.app.path_for('unsafe'); p.mkdir()
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z: z.writestr('../escape.txt','unsafe')
        with self.assertRaises(ValueError): self.app.safe_extract_zip(buf.getvalue(),p)
        self.assertFalse((Path(self.tmp.name)/'escape.txt').exists())
    def test_runs_table_is_initialized(self):
        with self.app.db() as c:
            cols={r['name'] for r in c.execute('PRAGMA table_info(runs)')}
        self.assertTrue({'workflow_id','stdout','stderr','started','duration_ms'}.issubset(cols))
    def test_empty_library_backup_contains_workflow_root(self):
        class Response:
            def __init__(self): self.wfile=io.BytesIO()
            def send_response(self,*_): pass
            def send_header(self,*_): pass
            def end_headers(self): pass
        response=Response()
        self.app.Handler.backup_all(response)
        result=json.loads(response.wfile.getvalue())
        with tarfile.open(result['file'],'r:gz') as archive:
            self.assertTrue(any(member.name.rstrip('/')=='workflows' and member.isdir() for member in archive.getmembers()))

if __name__=='__main__': unittest.main()
