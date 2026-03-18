"""
Ghost Agent — agent.py v4.0
Role: File manager + Deployer + Render monitor + New shop generator
"""

from flask import Flask, request, jsonify, Response, stream_with_context
import requests, json, os, base64, re, time
from datetime import datetime

app = Flask(__name__)

AGENT_SECRET   = os.environ.get('AGENT_SECRET',      'ghost_agent_2026')
GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN',      '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO',       'viratkohli00733-crypto/starcutters')
GITHUB_BRANCH  = os.environ.get('GITHUB_BRANCH',     'master')
RENDER_API_KEY = os.environ.get('RENDER_API_KEY',    '')
RENDER_SERVICE = os.environ.get('RENDER_SERVICE_ID', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY',    '')
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY',    '')

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"

_pending = {}

def auth():
    secret = request.headers.get('X-Ghost-Secret', '') or (request.json or {}).get('secret', '')
    return secret == AGENT_SECRET

def _gh_headers():
    token = os.environ.get('GITHUB_TOKEN', GITHUB_TOKEN)
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}

def gh_get(filepath, repo=None):
    repo = repo or GITHUB_REPO
    url  = f"{GITHUB_API}/repos/{repo}/contents/{filepath}?ref={GITHUB_BRANCH}"
    r    = requests.get(url, headers=_gh_headers(), timeout=15)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d['content']).decode('utf-8'), d['sha']
    return None, None

def gh_put(filepath, content, sha, message, repo=None):
    repo = repo or GITHUB_REPO
    url  = f"{GITHUB_API}/repos/{repo}/contents/{filepath}"
    payload = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": GITHUB_BRANCH}
    if sha: payload["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [200, 201], r.json()

def gh_list_recursive(path="", repo=None):
    repo = repo or GITHUB_REPO
    url  = f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={GITHUB_BRANCH}"
    r    = requests.get(url, headers=_gh_headers(), timeout=15)
    result = []
    if r.status_code == 200:
        for item in r.json():
            if item['type'] == 'file': result.append(item['path'])
            elif item['type'] == 'dir': result.extend(gh_list_recursive(item['path'], repo))
    return result

def sse(event, data):
    return f"data: {json.dumps({'event': event, 'data': data})}\n\n"

def render_headers():
    return {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}

def get_render_logs(limit=10):
    if not RENDER_SERVICE or not RENDER_API_KEY:
        return None, "Render API key or Service ID not configured"
    try:
        r = requests.get(f"{RENDER_API}/services/{RENDER_SERVICE}/deploys?limit={limit}", headers=render_headers(), timeout=15)
        if r.status_code == 200: return r.json(), None
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

def ai_call(prompt):
    if CLAUDE_API_KEY:
        try:
            r = requests.post(CLAUDE_URL, headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            if r.status_code == 200: return r.json()['content'][0]['text']
        except Exception as e: print(f"Claude: {e}")
    if GEMINI_API_KEY:
        try:
            r = requests.post(GEMINI_URL, json={"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}}, timeout=60)
            if r.status_code == 200: return r.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as e: print(f"Gemini: {e}")
    return None

def upload_stream(files_dict, commit_msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    approval_id = f"appr_{int(time.time())}"
    yield sse("log", f"[{ts}] Ghost Agent v4 ready, Sir.")
    yield sse("log", f"[{ts}] Files: {len(files_dict)}")
    sha_map = {}
    for fp in files_dict:
        _, sha = gh_get(fp)
        sha_map[fp] = sha
        yield sse("log", f"[{ts}]   {'[exists]' if sha else '[new]'} {fp}")
    _pending[approval_id] = {"files": files_dict, "sha_map": sha_map, "commit_msg": commit_msg, "ts": ts}
    yield sse("log", f"[{ts}] ----------------------------------")
    yield sse("approval_required", {"approval_id": approval_id, "files": list(files_dict.keys()), "message": f"{len(files_dict)} file(s) ready. Approve to deploy, Sir."})

def new_shop_stream(shop_data):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    approval_id = f"shop_{int(time.time())}"
    name = shop_data.get('name', 'NewShop')
    subdomain = shop_data.get('subdomain', 'newshop')
    yield sse("log", f"[{ts}] New shop: {name}")
    yield sse("log", f"[{ts}] Subdomain: {subdomain}.stylstation.in")
    yield sse("log", f"[{ts}] Fetching templates...")
    template_files = {}
    for fp in gh_list_recursive():
        if os.path.splitext(fp)[1].lower() in {'.py','.html','.css','.js','.txt','.sql'}:
            content, _ = gh_get(fp)
            if content: template_files[fp] = content
    yield sse("log", f"[{ts}] [OK] {len(template_files)} templates fetched")
    yield sse("log", f"[{ts}] [AI] Customizing for {name}...")
    prompt = f"""Customize barbershop website for:
Name: {name}, Owner: {shop_data.get('owner','')}, Phone: {shop_data.get('phone','')}, 
Address: {shop_data.get('address','')}, Services: {shop_data.get('services','')}
Return JSON: {{"templates/index.html": "<html>", "templates/base.html": "<html>"}}
Replace 'Starcutters' with '{name}'. Keep all Jinja2 syntax. JSON only."""
    response = ai_call(prompt)
    customized = {}
    if response:
        try:
            m = re.search(r'\{.*\}', response, re.DOTALL)
            if m: customized = json.loads(m.group())
            yield sse("log", f"[{ts}] [OK] AI customized {len(customized)} files")
        except: yield sse("log", f"[{ts}] [!] AI parse failed, using base templates")
    final = {**template_files, **customized}
    _pending[approval_id] = {"files": final, "sha_map": {}, "commit_msg": f"New shop: {name} [{ts}]", "ts": ts}
    yield sse("log", f"[{ts}] ----------------------------------")
    yield sse("approval_required", {"approval_id": approval_id, "files": list(final.keys()), "shop": shop_data, "message": f"Shop '{name}' ready. Approve to deploy, Sir."})

@app.route('/health')
def health():
    return jsonify({"status": "alive", "version": "v4", "pending": len(_pending), "claude": bool(CLAUDE_API_KEY), "gemini": bool(GEMINI_API_KEY), "render": bool(RENDER_API_KEY)})

@app.route('/upload', methods=['POST'])
def upload():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    files_dict = data.get('files', {})
    commit_msg = data.get('commit_msg', f"Ghost upload [{datetime.now().strftime('%H:%M')}]")
    if not files_dict: return jsonify({"error": "No files"}), 400
    def generate():
        try:
            for chunk in upload_stream(files_dict, commit_msg): yield chunk
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/approve', methods=['POST'])
def approve():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    aid = (request.json or {}).get('approval_id', '')
    pending = _pending.get(aid)
    if not pending: return jsonify({"error": "Not found"}), 404
    results = []
    for fp, content in pending['files'].items():
        ok, resp = gh_put(fp, content, pending['sha_map'].get(fp), pending['commit_msg'])
        results.append(f"{'[OK]' if ok else '[X]'} {fp}" + (f": {resp.get('message','?')}" if not ok and isinstance(resp,dict) else ""))
    del _pending[aid]
    success = all(r.startswith("[OK]") for r in results)
    return jsonify({"success": success, "message": "Deployed! Live in ~30s, Sir." if success else "Some failed, Sir.", "details": results})

@app.route('/cancel', methods=['POST'])
def cancel():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    aid = (request.json or {}).get('approval_id', '')
    if aid in _pending: del _pending[aid]
    return jsonify({"success": True, "message": "Cancelled, Sir."})

@app.route('/render_logs', methods=['GET', 'POST'])
def render_logs():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    deploys, err = get_render_logs()
    if err: return jsonify({"error": err}), 500
    return jsonify({"deploys": deploys, "count": len(deploys)})

@app.route('/sync', methods=['POST'])
def sync():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    result = {}
    for fp in gh_list_recursive():
        if os.path.splitext(fp)[1].lower() in {'.py','.html','.css','.js','.txt','.sql'}:
            content, sha = gh_get(fp)
            if content: result[fp] = {"content": content, "sha": sha}
    return jsonify({"success": True, "files": result, "count": len(result)})

@app.route('/read', methods=['POST'])
def read():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    fp = (request.json or {}).get('filepath', '')
    content, sha = gh_get(fp)
    if content: return jsonify({"content": content, "sha": sha})
    return jsonify({"error": "Not found"}), 404

@app.route('/new_shop', methods=['POST'])
def new_shop():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    shop_data = request.json or {}
    if not shop_data.get('name') or not shop_data.get('subdomain'):
        return jsonify({"error": "name and subdomain required"}), 400
    def generate():
        try:
            for chunk in new_shop_stream(shop_data): yield chunk
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/status')
def status():
    if request.args.get('secret') != AGENT_SECRET: return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"repo": GITHUB_REPO, "branch": GITHUB_BRANCH, "claude": bool(CLAUDE_API_KEY), "gemini": bool(GEMINI_API_KEY), "render": bool(RENDER_API_KEY), "pending": len(_pending), "version": "v4"})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
