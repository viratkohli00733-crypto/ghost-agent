"""
Ghost Agent v8.0 — Deploy + DB Manager
Architecture:
  - /deploy     : Receive files → staging push → approve → production
  - /approve    : Merge staging → master → production deploy
  - /cancel     : Cancel pending
  - /db         : Run SQL queries
  - /db_schema  : Get DB schema
  - /sync       : Fetch all repo files (for Qwen on phone)
  - /shop_create: New shop from template + DB entry
  - /shop_list  : List all shops
  - /shop_suspend / /shop_activate
  - /analytics  : Business analytics
  - /rollback   : Rollback a file
  - /render_logs: Render deploy logs
  - /health     : Status

No AI on agent — Qwen runs on phone only.
"""
from flask import Flask, request, jsonify, Response, stream_with_context
import requests, json, os, base64, re, time
from datetime import datetime

app = Flask(__name__)

AGENT_SECRET   = os.environ.get('AGENT_SECRET',      'ghost_agent_2026')
GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN',      '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO',       'viratkohli00733-crypto/starcutters')
GITHUB_BRANCH  = os.environ.get('GITHUB_BRANCH',     'master')
STAGING_BRANCH = os.environ.get('STAGING_BRANCH',    'staging')
RENDER_API_KEY = os.environ.get('RENDER_API_KEY',    '')
RENDER_SERVICE = os.environ.get('RENDER_SERVICE_ID', '')
RENDER_STAGING = os.environ.get('RENDER_STAGING_ID', '')
DB_URL         = os.environ.get('DATABASE_URL',      '')
GITHUB_API     = "https://api.github.com"
RENDER_API     = "https://api.render.com/v1"
_pending       = {}

def auth():
    s = request.headers.get('X-Ghost-Secret','') or (request.json or {}).get('secret','')
    return s == AGENT_SECRET

def sse(event, data):
    return f"data: {json.dumps({'event':event,'data':data})}\n\n"

def ts():
    return datetime.now().strftime("%H:%M:%S")

def _gh_h():
    return {"Authorization":f"token {GITHUB_TOKEN}","Accept":"application/vnd.github.v3+json","Content-Type":"application/json"}

def gh_get(fp, branch=None, repo=None):
    repo=repo or GITHUB_REPO; branch=branch or GITHUB_BRANCH
    r=requests.get(f"{GITHUB_API}/repos/{repo}/contents/{fp}?ref={branch}",headers=_gh_h(),timeout=15)
    if r.status_code==200:
        d=r.json(); return base64.b64decode(d['content']).decode('utf-8'),d['sha']
    return None,None

def gh_put(fp, content, sha, msg, branch=None, repo=None):
    repo=repo or GITHUB_REPO; branch=branch or GITHUB_BRANCH
    p={"message":msg,"content":base64.b64encode(content.encode()).decode(),"branch":branch}
    if sha: p["sha"]=sha
    r=requests.put(f"{GITHUB_API}/repos/{repo}/contents/{fp}",headers=_gh_h(),json=p,timeout=15)
    return r.status_code in[200,201],r.json()

def gh_list(path="",branch=None,repo=None):
    repo=repo or GITHUB_REPO; branch=branch or GITHUB_BRANCH
    r=requests.get(f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={branch}",headers=_gh_h(),timeout=15)
    result=[]
    if r.status_code==200:
        for item in r.json():
            if item['type']=='file': result.append(item['path'])
            elif item['type']=='dir': result.extend(gh_list(item['path'],branch,repo))
    return result

def gh_merge():
    r=requests.post(f"{GITHUB_API}/repos/{GITHUB_REPO}/merges",headers=_gh_h(),
        json={"base":GITHUB_BRANCH,"head":STAGING_BRANCH,
              "commit_message":f"Ghost Deploy: staging→master [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"},timeout=15)
    return r.status_code in[201,204],r.json() if r.content else {}

def _rh():
    return {"Authorization":f"Bearer {RENDER_API_KEY}","Content-Type":"application/json"}

def render_deploy(sid):
    if not sid or not RENDER_API_KEY: return False,"Not configured"
    try:
        r=requests.post(f"{RENDER_API}/services/{sid}/deploys",headers=_rh(),json={},timeout=15)
        return r.status_code==201,r.json()
    except Exception as e: return False,str(e)

def render_url(sid):
    if not sid or not RENDER_API_KEY: return None
    try:
        r=requests.get(f"{RENDER_API}/services/{sid}",headers=_rh(),timeout=10)
        if r.status_code==200: return r.json().get('serviceDetails',{}).get('url')
    except: pass
    return None

def db_q(sql,params=None):
    if not DB_URL: return None,"DATABASE_URL not configured"
    try:
        import psycopg2,psycopg2.extras
        conn=psycopg2.connect(DB_URL); cur=conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql,params or ())
        if sql.strip().upper().startswith('SELECT'):
            rows=cur.fetchall(); cur.close(); conn.close(); return [dict(r) for r in rows],None
        else:
            conn.commit(); n=cur.rowcount; cur.close(); conn.close(); return {"affected":n},None
    except Exception as e: return None,str(e)

@app.route('/health')
def health():
    return jsonify({"status":"alive","version":"v8","pending":len(_pending),
                    "render":bool(RENDER_API_KEY),"staging":bool(RENDER_STAGING),"db":bool(DB_URL)})

@app.route('/deploy',methods=['POST'])
def deploy():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    data=request.json or {}
    files=data.get('files',{}); direct=data.get('direct',False)
    commit_msg=data.get('commit_msg',f"Ghost deploy [{datetime.now().strftime('%H:%M')}]")
    if not files: return jsonify({"error":"No files"}),400

    def gen():
        t=ts(); aid=f"deploy_{int(time.time())}"
        branch=GITHUB_BRANCH if direct else STAGING_BRANCH
        yield sse("log",f"[{t}] [SYS] Ghost Agent v8 online, Sir.")
        yield sse("log",f"[{t}] [GO] Pushing {len(files)} file(s) → {branch}")
        sha_map={}; errs=[]
        for fp,content in files.items():
            _,sha=gh_get(fp,branch=branch); sha_map[fp]=sha
            ok,resp=gh_put(fp,content,sha,commit_msg,branch=branch)
            if ok: yield sse("log",f"[{t}]   [OK] {fp}")
            else:
                err=resp.get('message','?') if isinstance(resp,dict) else str(resp)
                yield sse("log",f"[{t}]   [X] {fp}: {err}"); errs.append(fp)
        if direct:
            if RENDER_SERVICE:
                ok,_=render_deploy(RENDER_SERVICE)
                yield sse("log",f"[{t}] [GO] Production deploy {'triggered' if ok else 'failed'}")
            yield sse("done",{"status":"done","message":"Deployed to production, Sir."}); return
        staging_url=None
        if RENDER_STAGING:
            ok,_=render_deploy(RENDER_STAGING)
            yield sse("log",f"[{t}] [GO] Staging deploy {'triggered' if ok else 'failed'}")
            staging_url=render_url(RENDER_STAGING)
            if staging_url: yield sse("log",f"[{t}] [INFO] Preview: {staging_url}")
        _pending[aid]={"type":"deploy","files":files,"sha_map":sha_map,"commit_msg":commit_msg,"ts":t}
        yield sse("log",f"[{t}] {'─'*36}")
        yield sse("approval_required",{
            "approval_id":aid,
            "diff":[{"filepath":fp,"success":fp not in errs} for fp in files],
            "summary":f"{len(files)} file(s) on staging. Approve to go live.",
            "staging_url":staging_url,
            "message":"Staging ready, Sir. Preview and approve to deploy to production."})
    return Response(stream_with_context(gen()),mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/approve',methods=['POST'])
def approve():
    """
    Approve staging — confirms staging push only.
    Does NOT merge to master/production.
    Use /promote for production deploy.
    """
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    aid=(request.json or {}).get('approval_id','')
    pending=_pending.get(aid)
    if not pending: return jsonify({"error":"Not found"}),404
    staging_url = render_url(RENDER_STAGING) or 'https://starcutters-staging.onrender.com'
    # Keep pending — needed for promote
    return jsonify({
        "success":   True,
        "message":   f"Staging confirmed, Sir. Preview at {staging_url}\nType CONFIRM PRODUCTION to go live or CANCEL to discard.",
        "staging_url": staging_url,
        "approval_id": aid,
        "status":    "staging_approved"
    })


@app.route('/promote', methods=['POST'])
def promote():
    """
    PROMOTE — merge staging → master → deploy to production.
    Only called after explicit CONFIRM PRODUCTION from user.
    """
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    aid=(request.json or {}).get('approval_id','')
    pending=_pending.get(aid)
    if not pending: return jsonify({"error":"Not found or already promoted"}),404
    details=[]; ok,resp=gh_merge()
    if ok:
        details.append("[OK] staging → master merged")
        if RENDER_SERVICE:
            dok,_=render_deploy(RENDER_SERVICE)
            details.append("[OK] Production deploy triggered" if dok else "[!] Deploy trigger failed")
    else:
        msg=resp.get('message','?') if isinstance(resp,dict) else str(resp)
        del _pending[aid]
        return jsonify({"success":False,"message":f"Merge failed: {msg}","details":[f"[X] {msg}"]})
    del _pending[aid]
    return jsonify({"success":True,"message":"Deployed to production, Sir. 🚀","details":details})


@app.route('/discard', methods=['POST'])
def discard():
    """
    DISCARD — reset staging branch back to master.
    Called when user types CANCEL after staging preview.
    """
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    aid=(request.json or {}).get('approval_id','')
    if aid in _pending: del _pending[aid]
    # Reset staging to master
    try:
        # Get master SHA
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs/heads/{GITHUB_BRANCH}",
            headers=_gh_h(), timeout=15)
        if r.status_code == 200:
            master_sha = r.json()['object']['sha']
            # Force update staging to master
            r2 = requests.patch(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs/heads/{STAGING_BRANCH}",
                headers=_gh_h(),
                json={"sha": master_sha, "force": True},
                timeout=15)
            if r2.status_code == 200:
                return jsonify({"success":True,"message":"Staging discarded. Master is safe, Sir."})
    except Exception as e:
        pass
    return jsonify({"success":True,"message":"Pending cleared. Master untouched, Sir."})

@app.route('/cancel',methods=['POST'])
def cancel():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    aid=(request.json or {}).get('approval_id','')
    if aid in _pending: del _pending[aid]
    return jsonify({"success":True,"message":"Cancelled, Sir."})

@app.route('/sync',methods=['POST'])
def sync():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    result={}; exts={'.py','.html','.css','.js','.txt','.sql','.json','.md'}
    for fp in gh_list():
        if os.path.splitext(fp)[1].lower() in exts:
            content,sha=gh_get(fp)
            if content: result[fp]={"content":content,"sha":sha}
    return jsonify({"success":True,"files":result,"count":len(result)})

@app.route('/read',methods=['POST'])
def read():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    fp=(request.json or {}).get('filepath','')
    branch=(request.json or {}).get('branch',GITHUB_BRANCH)
    content,sha=gh_get(fp,branch=branch)
    if content: return jsonify({"content":content,"sha":sha})
    return jsonify({"error":"Not found"}),404

@app.route('/db',methods=['POST'])
def db():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    data=request.json or {}; sql=data.get('sql','').strip(); params=data.get('params')
    if not sql: return jsonify({"error":"No SQL"}),400
    rows,err=db_q(sql,params)
    if err: return jsonify({"error":err}),500
    return jsonify({"success":True,"rows":rows})

@app.route('/db_schema',methods=['GET','POST'])
def db_schema():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    rows,err=db_q("""SELECT table_name,column_name,data_type,is_nullable
        FROM information_schema.columns WHERE table_schema='public'
        ORDER BY table_name,ordinal_position""")
    if err: return jsonify({"error":err}),500
    schema={}
    for r in rows:
        t=r['table_name']
        if t not in schema: schema[t]=[]
        schema[t].append({"column":r['column_name'],"type":r['data_type'],"nullable":r['is_nullable']})
    return jsonify({"success":True,"schema":schema})

@app.route('/render_logs',methods=['GET','POST'])
def render_logs():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    service=(request.json or {}).get('service','production')
    sid=RENDER_SERVICE if service=='production' else RENDER_STAGING
    logs=[]
    try:
        r=requests.get(f"{RENDER_API}/services/{sid}/deploys?limit=1",headers=_rh(),timeout=10)
        if r.status_code==200 and r.json():
            did=r.json()[0].get('deploy',{}).get('id','')
            if did:
                lr=requests.get(f"{RENDER_API}/services/{sid}/deploys/{did}/logs",headers=_rh(),timeout=15)
                if lr.status_code==200: logs=lr.json()
    except: pass
    return jsonify({"success":True,"logs":logs,"service":service})

@app.route('/shop_list',methods=['GET','POST'])
def shop_list():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    rows,err=db_q("SELECT * FROM shops ORDER BY created_at DESC")
    if err: return jsonify({"error":err}),500
    return jsonify({"success":True,"shops":rows,"count":len(rows)})

@app.route('/shop_create',methods=['POST'])
def shop_create():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    shop=request.json or {}
    if not shop.get('name') or not shop.get('subdomain'): return jsonify({"error":"name and subdomain required"}),400
    def gen():
        t=ts(); aid=f"shop_{int(time.time())}"; name=shop['name']; sub=shop['subdomain']
        yield sse("log",f"[{t}] [SYS] Creating shop: {name} ({sub}.stylstation.in)")
        yield sse("log",f"[{t}] [DIR] Fetching template files...")
        tfiles={}
        for fp in gh_list():
            if os.path.splitext(fp)[1].lower() in {'.py','.html','.css','.js','.sql'}:
                content,_=gh_get(fp)
                if content:
                    tfiles[fp]=content.replace('Starcutters',name).replace('starcutters',sub)
        yield sse("log",f"[{t}] [OK] {len(tfiles)} files customized")
        sha_map={}
        for fp,content in tfiles.items():
            _,sha=gh_get(fp,branch=STAGING_BRANCH); sha_map[fp]=sha
            gh_put(fp,content,sha,f"New shop: {name}",branch=STAGING_BRANCH)
        staging_url=None
        if RENDER_STAGING:
            render_deploy(RENDER_STAGING); staging_url=render_url(RENDER_STAGING)
        _,err=db_q("INSERT INTO shops (name,subdomain,status,created_at) VALUES (%s,%s,'pending',NOW())",(name,sub))
        yield sse("log",f"[{t}] {'[OK] DB entry created' if not err else f'[!] DB: {err}'}")
        _pending[aid]={"type":"shop_create","shop":shop,"files":tfiles,"sha_map":sha_map,"ts":t,"commit_msg":f"New shop: {name}"}
        yield sse("approval_required",{
            "approval_id":aid,
            "diff":[{"filepath":fp,"success":True} for fp in tfiles],
            "summary":f"Shop '{name}' ready on staging.",
            "staging_url":staging_url,
            "message":f"Approve to deploy {name} to production, Sir."})
    return Response(stream_with_context(gen()),mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.route('/shop_suspend',methods=['POST'])
def shop_suspend():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    sub=(request.json or {}).get('subdomain','')
    if not sub: return jsonify({"error":"subdomain required"}),400
    _,err=db_q("UPDATE shops SET status='suspended' WHERE subdomain=%s",(sub,))
    if err: return jsonify({"error":err}),500
    return jsonify({"success":True,"message":f"{sub} suspended, Sir."})

@app.route('/shop_activate',methods=['POST'])
def shop_activate():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    sub=(request.json or {}).get('subdomain','')
    if not sub: return jsonify({"error":"subdomain required"}),400
    _,err=db_q("UPDATE shops SET status='active' WHERE subdomain=%s",(sub,))
    if err: return jsonify({"error":err}),500
    return jsonify({"success":True,"message":f"{sub} activated, Sir."})

@app.route('/analytics',methods=['GET','POST'])
def analytics():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    overview={}
    for key,sql in [
        ("total_shops","SELECT COUNT(*) as c FROM shops"),
        ("active_shops","SELECT COUNT(*) as c FROM shops WHERE status='active'"),
        ("total_users","SELECT COUNT(*) as c FROM users"),
        ("total_bookings","SELECT COUNT(*) as c FROM bookings"),
        ("bookings_today","SELECT COUNT(*) as c FROM bookings WHERE DATE(created_at)=CURRENT_DATE"),
        ("revenue_today","SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE(created_at)=CURRENT_DATE"),
        ("revenue_month","SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE_TRUNC('month',created_at)=DATE_TRUNC('month',NOW())"),
    ]:
        rows,err=db_q(sql)
        if not err and rows: overview[key]=rows[0].get('c') or rows[0].get('total') or 0
    rows,err=db_q("""SELECT s.name,s.subdomain,COUNT(b.id) as bookings,COALESCE(SUM(p.amount),0) as revenue
        FROM shops s LEFT JOIN bookings b ON b.shop_id=s.id LEFT JOIN payments p ON p.booking_id=b.id
        GROUP BY s.id,s.name,s.subdomain ORDER BY bookings DESC LIMIT 10""")
    top=rows if not err else []
    return jsonify({"analytics":{"overview":overview,"top_shops":top},
                    "generated_at":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"message":"Analytics ready, Sir."})

@app.route('/rollback',methods=['POST'])
def rollback():
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    data=request.json or {}; fp=data.get('filepath',''); back=data.get('commits_back',1)
    if not fp: return jsonify({"error":"filepath required"}),400
    try:
        r=requests.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/commits?path={fp}&per_page={back+1}",headers=_gh_h(),timeout=15)
        if r.status_code!=200 or len(r.json())<back+1: return jsonify({"error":"Not enough history"}),404
        old_sha=r.json()[back]['sha']
        fr=requests.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{fp}?ref={old_sha}",headers=_gh_h(),timeout=15)
        if fr.status_code!=200: return jsonify({"error":"Could not fetch old version"}),404
        old=base64.b64decode(fr.json()['content']).decode('utf-8')
        _,cur_sha=gh_get(fp); ok,_=gh_put(fp,old,cur_sha,f"Ghost rollback: {fp} to {old_sha[:7]}")
        if ok: return jsonify({"success":True,"message":f"Rolled back {fp}, Sir."})
        return jsonify({"error":"Push failed"}),500
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route('/rag_sync',methods=['POST'])
def rag_sync():
    """Fetch all repo files and return for RAG knowledge base building."""
    if not auth(): return jsonify({"error":"Unauthorized"}),401
    result={}
    exts={'.py','.html','.css','.js','.txt','.sql','.json','.md'}
    try:
        for fp in gh_list():
            if os.path.splitext(fp)[1].lower() in exts:
                content,sha=gh_get(fp)
                if content:
                    result[fp]=content
        return jsonify({
            "success": True,
            "files":   result,
            "count":   len(result),
            "message": f"RAG sync: {len(result)} files fetched"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__=='__main__':
    app.run(debug=False,host='0.0.0.0',port=int(os.environ.get('PORT',5001)))
