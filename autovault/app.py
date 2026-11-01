#!/usr/bin/env python3
"""AutoVault: local-first automation library and version manager."""
from __future__ import annotations
import argparse, base64, datetime as dt, difflib, hashlib, html, io, json, mimetypes, os, re, shutil, sqlite3, subprocess, sys, tarfile, tempfile, threading, urllib.parse, uuid, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("AUTOVAULT_HOME", Path.home() / ".autovault")).expanduser().resolve()
LIB = ROOT / "workflows"
DB = ROOT / "autovault.sqlite3"
STATIC = Path(__file__).parent / "static"
TYPES = {"n8n": [".json"], "python": [".py"], "javascript": [".js", ".mjs", ".cjs", ".ts"], "shell": [".sh", ".command"], "docker-compose": [".yml", ".yaml"], "ai-agent": [".json", ".yaml", ".yml", ".md", ".txt"], "generic": []}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
HOSTED_HISTORY_DIR = ".autovault-hosted-data"
lock = threading.Lock()

def init():
    LIB.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, description TEXT DEFAULT '', tags TEXT DEFAULT '', status TEXT DEFAULT 'Active', created TEXT NOT NULL, updated TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL, FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE)")
        c.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, command TEXT NOT NULL, returncode INTEGER, stdout TEXT DEFAULT '', stderr TEXT DEFAULT '', error TEXT DEFAULT '', started TEXT NOT NULL, duration_ms INTEGER NOT NULL, FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE)")

def now(): return dt.datetime.now().astimezone().isoformat(timespec="seconds")
def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
def path_for(wid):
    if not SAFE_ID.fullmatch(wid): raise ValueError("Invalid workflow id")
    p=(LIB/wid).resolve()
    if p.parent != LIB.resolve(): raise ValueError("Invalid workflow path")
    return p
def git(p,*args,check=True):
    r=subprocess.run(["git", "-C",str(p),*args],capture_output=True,text=True,timeout=20)
    if check and r.returncode: raise RuntimeError(r.stderr.strip() or "Git operation failed")
    return r
def ensure_git(p):
    if not (p/".git").exists():
        git(p,"init",check=True)
    git(p,"config","user.name","AutoVault",check=True); git(p,"config","user.email","autovault@localhost",check=True)
def snapshot(wid,message="Save snapshot"):
    p=path_for(wid); ensure_git(p); git(p,"add","-A");
    if git(p,"diff","--cached","--quiet",check=False).returncode: git(p,"commit","-m",message[:200])
    h=git(p,"rev-parse","HEAD",check=False)
    return h.stdout.strip()[:12] if h.returncode==0 else ""
def jsonout(handler,obj,code=200):
    b=json.dumps(obj,ensure_ascii=False).encode(); handler.send_response(code); handler.send_header("Content-Type","application/json; charset=utf-8"); handler.send_header("Content-Length",str(len(b))); handler.end_headers(); handler.wfile.write(b)
def body(handler):
    n=int(handler.headers.get("Content-Length",0));
    if n>250_000_000: raise ValueError("Request too large")
    return json.loads(handler.rfile.read(n) or b"{}")
def workflow_row(row):
    d=dict(row); d["tags"]=json.loads(d["tags"] or "[]"); return d
def new_id(name):
    base=re.sub("[^a-z0-9]+","-",str(name).lower()).strip("-")[:42] or "workflow"
    return base+"-"+uuid.uuid4().hex[:8]
def safe_extract_zip(archive: bytes, destination: Path):
    total=0; count=0
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for info in z.infolist():
            name=info.filename.replace("\\","/"); mode=(info.external_attr>>16)&0o170000
            if name.startswith("/") or any(part in ("..","") for part in Path(name).parts) or mode==0o120000:
                raise ValueError("Archive contains an unsafe path")
            if any(part==".git" for part in Path(name).parts): continue
            if info.is_dir(): continue
            count+=1; total+=info.file_size
            if count>500 or total>100_000_000: raise ValueError("Archive exceeds the 500-file or 100 MB import limit")
            target=(destination/name).resolve()
            if destination.resolve() not in target.parents: raise ValueError("Archive contains an unsafe path")
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst: shutil.copyfileobj(src,dst)
    if count==0: raise ValueError("Archive contains no importable files")
    return count

def restore_hosted_snapshots(wid: str, workflow_path: Path):
    history_dir=workflow_path/HOSTED_HISTORY_DIR
    history_file=history_dir/"history.json"
    if not history_file.is_file(): return 0
    if not SAFE_ID.fullmatch(wid): raise ValueError("Invalid hosted workflow id")
    data=json.loads(history_file.read_text(encoding="utf-8"))
    snapshots=data.get("snapshots") if isinstance(data,dict) else None
    if not isinstance(snapshots,list) or len(snapshots)>20000: raise ValueError("Hosted snapshot history is invalid")
    if not snapshots:
        shutil.rmtree(history_dir)
        return 0
    prepared=[]
    for snapshot in snapshots:
        if not isinstance(snapshot,dict): raise ValueError("Hosted snapshot history is invalid")
        revision=str(snapshot.get("revision","")); message=str(snapshot.get("message",""))[:200]
        created=str(snapshot.get("created",""))
        manifest=snapshot.get("manifest")
        if not re.fullmatch(r"[0-9a-f]{12}",revision) or not isinstance(manifest,dict): raise ValueError("Hosted snapshot history is invalid")
        timestamp=dt.datetime.fromisoformat(created).isoformat()
        files={}
        for raw_path,object_hash in manifest.items():
            path=str(raw_path).replace("\\","/")
            parts=path.split("/")
            if not path or path.startswith("/") or any(part in ("",".","..",".git",HOSTED_HISTORY_DIR) for part in parts): raise ValueError("Hosted snapshot contains an unsafe path")
            if not isinstance(object_hash,str) or not re.fullmatch(r"[0-9a-f]{64}",object_hash): raise ValueError("Hosted snapshot has an invalid object reference")
            obj=(history_dir/"objects"/object_hash).resolve()
            if (history_dir/"objects").resolve() not in obj.parents or not obj.is_file(): raise ValueError("Hosted snapshot object is missing")
            content=obj.read_bytes()
            if hashlib.sha256(content).hexdigest()!=object_hash: raise ValueError("Hosted snapshot object failed its integrity check")
            files[path]=content
        prepared.append((timestamp,message,files))
    for child in workflow_path.iterdir():
        if child.name in (".git",HOSTED_HISTORY_DIR): continue
        if child.is_dir(): shutil.rmtree(child)
        else: child.unlink()
    git_dir=workflow_path/".git"
    if git_dir.is_dir(): shutil.rmtree(git_dir)
    elif git_dir.exists(): git_dir.unlink()
    env={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"HOME":str(workflow_path),"LANG":"C","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":os.devnull,"GIT_TERMINAL_PROMPT":"0","GIT_PAGER":"cat","GIT_OPTIONAL_LOCKS":"0"}
    def safe_git(*args,environment=None):
        result=subprocess.run(["git","-c","core.hooksPath=/dev/null","-C",str(workflow_path),*args],capture_output=True,text=True,timeout=30,env=environment or env)
        if result.returncode: raise RuntimeError(result.stderr.strip() or "Could not restore hosted snapshot history")
        return result
    safe_git("init","-q")
    safe_git("config","user.name","AutoVault")
    safe_git("config","user.email","autovault@localhost")
    for timestamp,message,files in prepared:
        for relative,content in files.items():
            target=(workflow_path/relative).resolve()
            if workflow_path.resolve() not in target.parents: raise ValueError("Hosted snapshot contains an unsafe path")
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
        safe_git("add","-A","--",".",f":(exclude){HOSTED_HISTORY_DIR}")
        safe_git("commit","--allow-empty","--quiet","-m",message or "Imported hosted version",environment={**env,"GIT_AUTHOR_DATE":timestamp,"GIT_COMMITTER_DATE":timestamp})
    shutil.rmtree(history_dir)
    return len(prepared)

class Handler(BaseHTTPRequestHandler):
    server_version="AutoVault/1.0"
    def log_message(self,fmt,*args): print("[%s] %s"%(dt.datetime.now().strftime("%H:%M:%S"),fmt%args))
    def do_GET(self):
        u=urllib.parse.urlparse(self.path); q=urllib.parse.parse_qs(u.query)
        try:
            if u.path=="/": return self.static("index.html")
            if u.path.startswith("/static/"): return self.static(u.path.removeprefix("/static/"))
            if u.path=="/api/workflows":
                term=q.get("q",[""])[0].strip().lower(); kind=q.get("type",[""])[0]; status=q.get("status",[""])[0]
                with db() as c: rows=c.execute("SELECT * FROM workflows ORDER BY updated DESC").fetchall()
                vals=[workflow_row(r) for r in rows]
                vals=[w for w in vals if (not term or term in (w["name"]+" "+w["description"]+" "+" ".join(w["tags"])).lower()) and (not kind or w["kind"]==kind) and (not status or w["status"]==status)]
                return jsonout(self,{"workflows":vals,"home":str(ROOT)})
            m=re.fullmatch(r"/api/workflows/([a-z0-9-]+)(?:/(.+))?",u.path)
            if m:
                wid,action=m.groups(); p=path_for(wid)
                with db() as c: w=c.execute("SELECT * FROM workflows WHERE id=?",(wid,)).fetchone()
                if not w: return jsonout(self,{"error":"Workflow not found"},404)
                if action=="backup": return self.backup_one(wid,p)
                if action is None:
                    files=[str(x.relative_to(p)) for x in p.rglob("*") if x.is_file() and ".git" not in x.parts and HOSTED_HISTORY_DIR not in x.parts]
                    commits=git(p,"log","--pretty=format:%h%x09%ad%x09%s","--date=short","-n","30",check=False).stdout.splitlines()
                    return jsonout(self,{"workflow":workflow_row(w),"files":files,"history":commits})
                if action=="notes":
                    with db() as c: notes=[dict(x) for x in c.execute("SELECT * FROM notes WHERE workflow_id=? ORDER BY id DESC",(wid,))]
                    return jsonout(self,{"notes":notes})
                if action=="runs":
                    with db() as c: runs=[dict(x) for x in c.execute("SELECT * FROM runs WHERE workflow_id=? ORDER BY id DESC LIMIT 50",(wid,))]
                    return jsonout(self,{"runs":runs})
                if action=="backup/download":
                    f=ROOT/(wid+"-backup.tar.gz")
                    if not f.is_file(): return jsonout(self,{"error":"Create this workflow backup first"},404)
                    b=f.read_bytes(); self.send_response(200); self.send_header("Content-Type","application/gzip"); self.send_header("Content-Disposition","attachment; filename="+f.name); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
                if action=="diff":
                    a=q.get("a",[""])[0]; b=q.get("b",["HEAD"])[0]
                    out=git(p,"diff","--no-ext-diff",a,b,check=False)
                    return jsonout(self,{"diff":out.stdout,"error":out.stderr if out.returncode not in (0,1) else ""})
            if u.path.startswith("/api/file/"):
                bits=u.path.split("/",4); wid=bits[3]; rel=urllib.parse.unquote(bits[4]) if len(bits)>4 else ""
                p=(path_for(wid)/rel).resolve()
                if path_for(wid) not in p.parents or HOSTED_HISTORY_DIR in p.parts or not p.is_file(): return jsonout(self,{"error":"File not found"},404)
                return jsonout(self,{"path":rel,"content":p.read_text(errors="replace")[:500000]})
            if u.path=="/api/health": return jsonout(self,{"ok":True,"home":str(ROOT),"binding":"localhost"})
            if u.path=="/api/backup/download":
                backups=sorted(ROOT.glob("autovault-backup-*.tar.gz"),key=lambda x:x.stat().st_mtime,reverse=True)
                if not backups: return jsonout(self,{"error":"Create a backup first"},404)
                b=backups[0].read_bytes(); self.send_response(200); self.send_header("Content-Type","application/gzip"); self.send_header("Content-Disposition","attachment; filename="+backups[0].name); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
            return jsonout(self,{"error":"Not found"},404)
        except Exception as e: return jsonout(self,{"error":str(e)},400)
    def static(self,name):
        p=(STATIC/name).resolve()
        if STATIC.resolve() not in p.parents or not p.is_file(): self.send_error(404); return
        b=p.read_bytes(); self.send_response(200); self.send_header("Content-Type",mimetypes.guess_type(str(p))[0] or "application/octet-stream"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        u=urllib.parse.urlparse(self.path); parts=u.path.strip("/").split()
        try:
            data=body(self)
            if u.path=="/api/workflows":
                kind=data.get("kind","generic"); name=str(data.get("name","New workflow")).strip()[:120]
                if kind not in TYPES: raise ValueError("Unsupported workflow type")
                if not name: raise ValueError("Workflow name is required")
                wid=new_id(name)
                p=path_for(wid); p.mkdir(parents=True); filename=str(data.get("filename", "workflow"+ (TYPES[kind][0] if TYPES[kind] else ".md"))).replace("\\","/").split("/")[-1]
                if not filename or filename.startswith("."): filename="workflow.md"
                (p/filename).write_text(str(data.get("content","")),encoding="utf-8")
                with db() as c: c.execute("INSERT INTO workflows(id,name,kind,description,tags,status,created,updated) VALUES(?,?,?,?,?,?,?,?)",(wid,name,kind,str(data.get("description",""))[:1000],json.dumps(data.get("tags",[])),data.get("status","Active"),now(),now()))
                snapshot(wid,"Initial version")
                return jsonout(self,{"id":wid},201)
            if u.path=="/api/import":
                filename=str(data.get("filename","workflow.json")).replace("\\","/").split("/")[-1]; content=data.get("content","")
                if not filename: raise ValueError("Invalid import")
                ext=Path(filename).suffix.lower(); kind={".py":"python",".js":"javascript",".mjs":"javascript",".cjs":"javascript",".ts":"javascript",".sh":"shell",".command":"shell",".json":"n8n",".yml":"docker-compose",".yaml":"docker-compose"}.get(ext,"generic")
                if data.get("kind") in TYPES: kind=data["kind"]
                name=str(data.get("name") or Path(filename).stem).strip()[:120]; wid=new_id(name); p=path_for(wid); p.mkdir(parents=True)
                if data.get("archive"):
                    raw=base64.b64decode(data["archive"],validate=True)
                    if len(raw)>50_000_000: raise ValueError("Compressed import exceeds 50 MB")
                    safe_extract_zip(raw,p)
                else:
                    if len(content)>20_000_000: raise ValueError("Text import exceeds 20 MB")
                    (p/filename).write_text(content,encoding="utf-8")
                with db() as c: c.execute("INSERT INTO workflows(id,name,kind,description,tags,status,created,updated) VALUES(?,?,?,?,?,?,?,?)",(wid,name,kind,str(data.get("description",""))[:1000],json.dumps(data.get("tags",[])),data.get("status","Active"),now(),now()))
                snapshot(wid,"Imported workflow")
                return jsonout(self,{"id":wid,"kind":kind},201)
            m=re.fullmatch(r"/api/workflows/([a-z0-9-]+)/(save|update|notes|restore|run|backup)",u.path)
            if m:
                wid,action=m.groups(); p=path_for(wid)
                with db() as c: row=c.execute("SELECT * FROM workflows WHERE id=?",(wid,)).fetchone()
                if not row: return jsonout(self,{"error":"Workflow not found"},404)
                if action=="update":
                    allowed={"name","description","status"}; fields={k:v for k,v in data.items() if k in allowed}
                    if "name" in fields:
                        fields["name"]=str(fields["name"]).strip()[:120]
                        if not fields["name"]: raise ValueError("Workflow name is required")
                    if "description" in fields: fields["description"]=str(fields["description"])[:1000]
                    if "status" in fields and fields["status"] not in ("Active","Testing","Maintenance","Archived"): raise ValueError("Invalid workflow status")
                    if "tags" in data:
                        tags=data["tags"]
                        if not isinstance(tags,list): raise ValueError("Tags must be a list")
                        fields["tags"]=json.dumps([str(t).strip()[:50] for t in tags if str(t).strip()][:50])
                    if not fields: raise ValueError("No fields to update")
                    sql=", ".join(k+"=?" for k in fields)+",updated=?"; vals=list(fields.values())+[now(),wid]
                    with db() as c: c.execute("UPDATE workflows SET "+sql+" WHERE id=?",vals)
                    return jsonout(self,{"ok":True})
                if action=="save":
                    rel=str(data.get("path","")); target=(p/rel).resolve()
                    if p not in target.parents: raise ValueError("Invalid file path")
                    if not target.is_file() and data.get("newFile") is not True: raise ValueError("Choose an existing workflow file")
                    content=str(data.get("content",""));
                    if len(content)>1_000_000: raise ValueError("File too large")
                    target.write_text(content,encoding="utf-8"); h=snapshot(wid,str(data.get("message","Update workflow")));
                    with db() as c: c.execute("UPDATE workflows SET updated=? WHERE id=?",(now(),wid))
                    return jsonout(self,{"ok":True,"commit":h})
                if action=="notes":
                    text=str(data.get("body","")).strip()
                    if not text: raise ValueError("Note cannot be empty")
                    with db() as c: c.execute("INSERT INTO notes(workflow_id,body,created) VALUES(?,?,?)",(wid,text[:5000],now()))
                    return jsonout(self,{"ok":True},201)
                if action=="restore":
                    rev=str(data.get("revision",""))
                    if not re.fullmatch(r"[0-9a-f]{4,40}",rev): raise ValueError("Invalid revision")
                    if git(p,"cat-file","-e",rev+"^{commit}",check=False).returncode: raise ValueError("Snapshot not found")
                    snapshot(wid,"Before restore")
                    git(p,"reset","--hard",rev); git(p,"clean","-fd"); h=snapshot(wid,"Restore snapshot "+rev[:12]);
                    with db() as c: c.execute("UPDATE workflows SET updated=? WHERE id=?",(now(),wid))
                    return jsonout(self,{"ok":True,"commit":h})
                if action=="run": return self.run_workflow(wid,p,row,data)
                if action=="backup": return self.backup_one(wid,p)
            if u.path=="/api/backup": return self.backup_all()
            if u.path=="/api/restore": return self.restore_backup(data)
            return jsonout(self,{"error":"Not found"},404)
        except Exception as e: return jsonout(self,{"error":str(e)},400)
    def do_DELETE(self):
        m=re.fullmatch(r"/api/workflows/([a-z0-9-]+)",urllib.parse.urlparse(self.path).path)
        if not m: return jsonout(self,{"error":"Not found"},404)
        wid=m.group(1)
        try:
            p=path_for(wid)
            with db() as c:
                row=c.execute("SELECT id FROM workflows WHERE id=?",(wid,)).fetchone()
                if not row: return jsonout(self,{"error":"Workflow not found"},404)
                c.execute("DELETE FROM workflows WHERE id=?",(wid,))
            if p.exists(): shutil.rmtree(p)
            return jsonout(self,{"ok":True})
        except Exception as e: return jsonout(self,{"error":str(e)},400)
    def run_workflow(self,wid,p,row,data):
        if data.get("approved") is not True: return jsonout(self,{"error":"Execution requires explicit approval"},403)
        kind=row["kind"]; rel=str(data.get("path","")); target=(p/rel).resolve()
        if p not in target.parents or not target.is_file(): raise ValueError("Choose an existing file")
        if kind=="python" and target.suffix==".py": cmd=[sys.executable,str(target)]
        elif kind=="javascript" and target.suffix in (".js",".mjs",".cjs"): cmd=["node",str(target)]
        elif kind=="shell" and target.suffix in (".sh",".command"): cmd=["/bin/sh",str(target)]
        else: raise ValueError("This workflow type or file cannot be executed by AutoVault")
        timeout=max(1,min(int(data.get("timeout",30)),300))
        env={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"HOME":str(p),"LANG":"C.UTF-8"}
        started=now(); clock=dt.datetime.now().timestamp()
        try:
            r=subprocess.run(cmd,cwd=p,env=env,capture_output=True,text=True,timeout=timeout)
            out,err=r.stdout[-30000:],r.stderr[-30000:]; elapsed=int((dt.datetime.now().timestamp()-clock)*1000); command=cmd[0]+" "+target.name
            with db() as c: cur=c.execute("INSERT INTO runs(workflow_id,command,returncode,stdout,stderr,error,started,duration_ms) VALUES(?,?,?,?,?,?,?,?)",(wid,command,r.returncode,out,err,"",started,elapsed)); run_id=cur.lastrowid
            return jsonout(self,{"run_id":run_id,"returncode":r.returncode,"stdout":out,"stderr":err,"command":command,"duration_ms":elapsed})
        except subprocess.TimeoutExpired as e:
            out=e.stdout or ""; err=e.stderr or ""; out=out.decode(errors="replace") if isinstance(out,bytes) else out; err=err.decode(errors="replace") if isinstance(err,bytes) else err; out,err=out[-30000:],err[-30000:]; elapsed=int((dt.datetime.now().timestamp()-clock)*1000); command=cmd[0]+" "+target.name
            with db() as c: cur=c.execute("INSERT INTO runs(workflow_id,command,returncode,stdout,stderr,error,started,duration_ms) VALUES(?,?,?,?,?,?,?,?)",(wid,command,None,out,err,"Execution timed out",started,elapsed)); run_id=cur.lastrowid
            return jsonout(self,{"run_id":run_id,"error":"Execution timed out","stdout":out,"stderr":err,"command":command,"duration_ms":elapsed},408)
    def backup_one(self,wid,p):
        out=ROOT/(wid+"-backup.tar.gz")
        with tarfile.open(out,"w:gz") as t: t.add(p,arcname=wid)
        return jsonout(self,{"file":str(out),"message":"Workflow backup created"})
    def backup_all(self):
        name="autovault-backup-"+dt.datetime.now().strftime("%Y%m%d-%H%M%S")+".tar.gz"; out=ROOT/name
        with db() as c: c.execute("PRAGMA wal_checkpoint(FULL)")
        with tarfile.open(out,"w:gz") as t:
            t.add(DB,arcname="autovault.sqlite3")
            t.add(LIB,arcname="workflows")
        return jsonout(self,{"file":str(out)})
    def restore_backup(self,data):
        raw=base64.b64decode(data["content"],validate=True) if data.get("content") else Path(str(data.get("file",""))).expanduser().read_bytes()
        if len(raw)>200_000_000: raise ValueError("Backup exceeds the 200 MB compressed limit")
        stage=Path(tempfile.mkdtemp(prefix=".autovault-restore-",dir=str(ROOT))); conn=None; old_lib=None; swapped=False
        try:
            with tarfile.open(fileobj=io.BytesIO(raw),mode="r:gz") as t:
                members=t.getmembers(); total=0
                if len(members)>10000: raise ValueError("Backup contains too many entries")
                for m in members:
                    name=m.name.replace("\\","/"); dest=(stage/name).resolve()
                    if name.startswith("/") or any(part==".." for part in Path(name).parts) or stage.resolve() not in dest.parents: raise ValueError("Backup contains an unsafe path")
                    if not (m.isfile() or m.isdir()): raise ValueError("Backup contains an unsupported file type")
                    total+=m.size
                    if total>1_000_000_000: raise ValueError("Backup exceeds the 1 GB expanded limit")
                t.extractall(stage,members=members)
            source_db=stage/"autovault.sqlite3"; incoming_lib=stage/"workflows"
            if not source_db.is_file() or not incoming_lib.is_dir(): raise ValueError("Backup is missing its database or workflow library")
            src=sqlite3.connect(source_db); src.row_factory=sqlite3.Row
            try:
                if src.execute("PRAGMA quick_check").fetchone()[0]!="ok": raise ValueError("Backup database failed its integrity check")
                tables={r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "workflows" not in tables: raise ValueError("Backup database has no workflow catalog")
                source_workflows=src.execute("SELECT id FROM workflows").fetchall()
                for row in source_workflows:
                    wid=row["id"]
                    if not SAFE_ID.fullmatch(wid) or not (incoming_lib/wid).is_dir(): raise ValueError("Backup workflow folder does not match its catalog")
                conn=sqlite3.connect(DB); conn.execute("PRAGMA foreign_keys=ON"); conn.execute("BEGIN IMMEDIATE")
                for table in ("runs","notes","workflows"):
                    conn.execute("DELETE FROM "+table)
                    if table not in tables: continue
                    dest_cols=[r[1] for r in conn.execute("PRAGMA table_info("+table+")")]
                    src_cols=[r[1] for r in src.execute("PRAGMA table_info("+table+")")]
                    cols=[col for col in dest_cols if col in src_cols]
                    if cols:
                        col_sql=",".join('"'+c+'"' for c in cols); marks=",".join("?" for _ in cols)
                        rows=src.execute("SELECT "+col_sql+" FROM "+table)
                        conn.executemany("INSERT INTO "+table+" ("+col_sql+") VALUES ("+marks+")",([r[c] for c in cols] for r in rows))
                if LIB.exists(): old_lib=stage/"previous-workflows"; os.replace(LIB,old_lib)
                try:
                    os.replace(incoming_lib,LIB); swapped=True
                    for row in source_workflows: restore_hosted_snapshots(row["id"],LIB/row["id"])
                except Exception:
                    if old_lib and old_lib.exists(): os.replace(old_lib,LIB)
                    conn.rollback(); raise
                conn.commit()
            except Exception:
                if conn: conn.rollback()
                if swapped:
                    shutil.rmtree(LIB,ignore_errors=True)
                    if old_lib and old_lib.exists(): os.replace(old_lib,LIB)
                raise
            finally:
                src.close()
                if conn: conn.close()
            return jsonout(self,{"ok":True,"message":"Backup restored. Reload the app."})
        finally: shutil.rmtree(stage,ignore_errors=True)

def main():
    init(); ap=argparse.ArgumentParser(description="AutoVault local automation workflow manager"); container_mode=os.environ.get("AUTOVAULT_CONTAINER")=="1"; ap.add_argument("--host",default="0.0.0.0" if container_mode else "127.0.0.1",help="Bind address (localhost unless running behind Docker's loopback-only port mapping)"); ap.add_argument("--port",type=int,default=8765); args=ap.parse_args()
    if args.host not in ("127.0.0.1","localhost","::1") and not (container_mode and args.host=="0.0.0.0"): ap.error("AutoVault only permits localhost binding outside its loopback-published Docker container")
    print("AutoVault running at http://127.0.0.1:%d"%args.port); print("Data directory:",ROOT)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
if __name__=="__main__": main()
