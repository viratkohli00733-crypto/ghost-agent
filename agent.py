"""
Ghost Agent v7.0 — Complete Site Manager
Features: deploy, execute, monitor, ask, db, shops, analytics, rollback
Brain: Qwen Coder-3B (phone) — offline only, no cloud (V70)
"""

from flask import Flask, request, jsonify, Response, stream_with_context
import requests, json, os, base64, re, time
from datetime import datetime

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
AGENT_SECRET   = os.environ.get('AGENT_SECRET',      'ghost_agent_2026')
GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN',      '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO',       'viratkohli00733-crypto/starcutters')
GITHUB_BRANCH  = os.environ.get('GITHUB_BRANCH',     'master')
STAGING_BRANCH = os.environ.get('STAGING_BRANCH',    'staging')
RENDER_API_KEY = os.environ.get('RENDER_API_KEY',    '')
RENDER_SERVICE = os.environ.get('RENDER_SERVICE_ID', '')   # production
RENDER_STAGING = os.environ.get('RENDER_STAGING_ID', '')   # staging
DB_URL         = os.environ.get('DATABASE_URL',      '')   # PostgreSQL URL

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"

_pending = {}   # approval_id → pending data

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════
def auth():
    secret = request.headers.get('X-Ghost-Secret', '') or (request.json or {}).get('secret', '')
    return secret == AGENT_SECRET

# ══════════════════════════════════════════════════════════════════════════════
#  GITHUB HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def _gh_headers():
    return {
        "Authorization": f"token {os.environ.get('GITHUB_TOKEN', GITHUB_TOKEN)}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json"
    }

def gh_get(filepath, branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{filepath}?ref={branch}",
        headers=_gh_headers(), timeout=15)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d['content']).decode('utf-8'), d['sha']
    return None, None

def gh_put(filepath, content, sha, message, branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  branch
    }
    if sha: payload["sha"] = sha
    r = requests.put(
        f"{GITHUB_API}/repos/{repo}/contents/{filepath}",
        headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [200, 201], r.json()

def gh_delete(filepath, sha, message, branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    payload = {"message": message, "sha": sha, "branch": branch}
    r = requests.delete(
        f"{GITHUB_API}/repos/{repo}/contents/{filepath}",
        headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code == 200

def gh_list_recursive(path="", branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    r = requests.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={branch}",
        headers=_gh_headers(), timeout=15)
    result = []
    if r.status_code == 200:
        for item in r.json():
            if   item['type'] == 'file': result.append(item['path'])
            elif item['type'] == 'dir':  result.extend(gh_list_recursive(item['path'], branch, repo))
    return result

def gh_merge_to_master():
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/merges"
    payload = {
        "base":           GITHUB_BRANCH,
        "head":           STAGING_BRANCH,
        "commit_message": f"Ghost Deploy: staging → master [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
    }
    r = requests.post(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [201, 204], r.json() if r.content else {}

# ══════════════════════════════════════════════════════════════════════════════
#  SSE
# ══════════════════════════════════════════════════════════════════════════════
def sse(event, data):
    return f"data: {json.dumps({'event': event, 'data': data})}\n\n"

def ts():
    return datetime.now().strftime("%H:%M:%S")

# ══════════════════════════════════════════════════════════════════════════════
#  RENDER HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def _rh():
    return {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}

def render_deploy(service_id):
    if not service_id or not RENDER_API_KEY: return False, "Not configured"
    try:
        r = requests.post(f"{RENDER_API}/services/{service_id}/deploys",
                          headers=_rh(), json={}, timeout=15)
        return r.status_code == 201, r.json()
    except Exception as e:
        return False, str(e)

def render_get_url(service_id):
    if not service_id or not RENDER_API_KEY: return None
    try:
        r = requests.get(f"{RENDER_API}/services/{service_id}", headers=_rh(), timeout=10)
        if r.status_code == 200:
            return r.json().get('serviceDetails', {}).get('url')
    except: pass
    return None

def render_get_deploys(service_id, limit=5):
    if not service_id or not RENDER_API_KEY: return []
    try:
        r = requests.get(f"{RENDER_API}/services/{service_id}/deploys?limit={limit}",
                         headers=_rh(), timeout=10)
        if r.status_code == 200: return r.json()
    except: pass
    return []

# ══════════════════════════════════════════════════════════════════════════════
#  AI — Qwen only (V70 — no cloud)
#  Qwen Coder-3B runs on phone via llama-server (127.0.0.1:8080)
#  Agent receives pre-planned files + command from Ghost app (main.py)
#  No Gemini, No Claude — offline only
# ══════════════════════════════════════════════════════════════════════════════

def ai_call(prompt, model_hint='auto', max_tokens=4096):
    """
    V70: Qwen only. model_hint ignored — always uses Qwen.
    Returns (response_text, 'qwen') or (None, None) if unavailable.
    Note: In V70 architecture, Qwen runs on phone. Agent receives
    pre-processed file plans from Ghost app. This function is kept
    for future extensibility but agent primarily uses files sent by Ghost.
    """
    return None, None  # Agent relies on Ghost app's Qwen for planning

# ══════════════════════════════════════════════════════════════════════════════
#  DB HELPER
# ══════════════════════════════════════════════════════════════════════════════
def db_query(sql, params=None):
    """Run a SQL query on the production DB. Returns rows or error."""
    if not DB_URL:
        return None, "DATABASE_URL not configured"
    try:
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(DB_URL)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        if sql.strip().upper().startswith('SELECT'):
            rows = cur.fetchall()
            cur.close(); conn.close()
            return [dict(r) for r in rows], None
        else:
            conn.commit()
            affected = cur.rowcount
            cur.close(); conn.close()
            return {"affected": affected}, None
    except Exception as e:
        return None, str(e)

# ══════════════════════════════════════════════════════════════════════════════
#  EXECUTE — AI coding + staging flow
# ══════════════════════════════════════════════════════════════════════════════
def execute_stream(command, model, attached_files, planned_files=None):
    t = ts()
    approval_id = f"exec_{int(time.time())}"

    yield sse("log", f"[{t}] [SYS] Ghost Agent v6 online, Sir.")
    yield sse("log", f"[{t}] [SYS] Command: {command}")
    yield sse("log", f"[{t}] [SYS] Model: {model.upper()}")
    yield sse("log", f"[{t}] {'─'*36}")

    # Step 1 — fetch files
    yield sse("log", f"[{t}] [DIR] Fetching repo files...")
    repo_files = {}
    try:
        if planned_files:
            yield sse("log", f"[{t}] [SYS] Qwen planned {len(planned_files)} files")
            relevant = planned_files
        else:
            all_paths = gh_list_recursive(branch=GITHUB_BRANCH)
            relevant  = [p for p in all_paths
                         if os.path.splitext(p)[1].lower()
                         in {'.py','.html','.css','.js','.txt','.sql'}]
            yield sse("log", f"[{t}] [DIR] Found {len(relevant)} files")
        for fp in relevant:
            content, _ = gh_get(fp, branch=GITHUB_BRANCH)
            if content: repo_files[fp] = content
        yield sse("log", f"[{t}] [OK] {len(repo_files)} files loaded")
    except Exception as e:
        yield sse("log", f"[{t}] [X] Fetch error: {e}")
        yield sse("done", {"status": "error", "message": str(e)}); return

    if attached_files:
        repo_files.update(attached_files)
        yield sse("log", f"[{t}] [FILE] {len(attached_files)} attached files added")

    # Step 2 — AI generates
    yield sse("log", f"[{t}] [AI] Sending to {model.upper()}...")
    file_summary  = "\n".join([f"- {fp} ({len(c)} chars)" for fp, c in repo_files.items()])
    file_contents = "\n\n".join([f"=== {fp} ===\n{content}"
                                  for fp, content in list(repo_files.items())[:15]])

    prompt = f"""You are Ghost Agent — AI developer for StylStation (Flask barbershop aggregator).

TASK: {command}

FILES IN REPO:
{file_summary}

FILE CONTENTS:
{file_contents}

RULES:
- Make ONLY the changes needed
- Keep all Jinja2 syntax, Flask routes, DB logic intact
- Return ONLY a JSON object: {{"filepath": "full content", ...}}
- No explanation, no markdown, pure JSON only
- New HTML pages must extend base.html using Jinja2 blocks"""

    response, used_model = ai_call(prompt, model)

    if not response:
        yield sse("log", f"[{t}] [X] AI returned no response")
        yield sse("done", {"status": "error", "message": "AI failed, Sir."}); return

    yield sse("log", f"[{t}] [AI] Response from {used_model}, parsing...")

    # Step 3 — parse
    changed_files = {}
    try:
        clean = response.strip()
        if clean.startswith("```"):
            clean = re.sub(r'^```[a-z]*\n?', '', clean)
            clean = re.sub(r'\n?```$', '', clean)
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m: changed_files = json.loads(m.group())
        yield sse("log", f"[{t}] [OK] {len(changed_files)} file(s) generated")
        for fp in changed_files:
            yield sse("log", f"[{t}]   → {fp}")
    except Exception as e:
        yield sse("log", f"[{t}] [X] Parse error: {e}")
        yield sse("done", {"status": "error", "message": str(e)}); return

    if not changed_files:
        yield sse("done", {"status": "error", "message": "AI made no changes, Sir."}); return

    # Step 4 — push to staging
    yield sse("log", f"[{t}] [GO] Pushing to staging branch...")
    sha_map = {}; push_errors = []
    for fp, content in changed_files.items():
        _, sha = gh_get(fp, branch=STAGING_BRANCH)
        sha_map[fp] = sha
        ok, resp = gh_put(fp, content, sha, f"Ghost staging: {command[:50]}", branch=STAGING_BRANCH)
        if ok: yield sse("log", f"[{t}]   [OK] {fp}")
        else:
            err = resp.get('message','?') if isinstance(resp, dict) else str(resp)
            yield sse("log", f"[{t}]   [X] {fp}: {err}")
            push_errors.append(fp)

    # Step 5 — trigger staging deploy
    staging_url = None
    if RENDER_STAGING:
        yield sse("log", f"[{t}] [GO] Triggering staging deploy...")
        ok, _ = render_deploy(RENDER_STAGING)
        yield sse("log", f"[{t}]   {'[OK] Deploy triggered' if ok else '[!] Trigger failed'}")
        staging_url = render_get_url(RENDER_STAGING)
        if staging_url: yield sse("log", f"[{t}] [INFO] Preview: {staging_url}")

    # Step 6 — approval
    _pending[approval_id] = {
        "type": "execute", "command": command,
        "files": changed_files, "sha_map": sha_map,
        "staging_url": staging_url, "ts": t,
    }
    yield sse("log", f"[{t}] {'─'*36}")
    yield sse("approval_required", {
        "approval_id":  approval_id,
        "diff":         [{"filepath": fp, "success": fp not in push_errors} for fp in changed_files],
        "summary":      f"{len(changed_files)} file(s) on staging. Preview → approve to go live.",
        "staging_url":  staging_url,
        "message":      "Staging ready, Sir. Preview and approve to deploy to production."
    })


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/health')
def health():
    return jsonify({
        "status":  "alive", "version": "v6",
        "pending": len(_pending),
        "render":  bool(RENDER_API_KEY),
        "staging": bool(RENDER_STAGING),
        "db":      bool(DB_URL),
    })

@app.route('/execute', methods=['POST'])
def execute():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data           = request.json or {}
    command        = data.get('command', '').strip()
    model          = data.get('model', 'auto')
    attached_files = data.get('attached_files') or {}
    planned_files  = data.get('planned_files')  or None
    if not command: return jsonify({"error": "No command"}), 400

    def generate():
        try:
            for chunk in execute_stream(command, model, attached_files, planned_files):
                yield chunk
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")
            yield sse("done",  {"status": "error", "message": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/deploy', methods=['POST'])
def deploy():
    """Direct deploy — no AI, just push files straight to staging then approve."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data       = request.json or {}
    files_dict = data.get('files', {})
    direct     = data.get('direct', False)   # True = skip staging, go straight to master
    commit_msg = data.get('commit_msg', f"Ghost deploy [{datetime.now().strftime('%H:%M')}]")
    if not files_dict: return jsonify({"error": "No files"}), 400

    def generate():
        try:
            t           = ts()
            approval_id = f"deploy_{int(time.time())}"
            branch      = GITHUB_BRANCH if direct else STAGING_BRANCH
            yield sse("log", f"[{t}] [GO] Deploying {len(files_dict)} file(s) → {branch}")
            sha_map = {}
            for fp, content in files_dict.items():
                _, sha = gh_get(fp, branch=branch)
                sha_map[fp] = sha
                ok, _ = gh_put(fp, content, sha, commit_msg, branch=branch)
                yield sse("log", f"[{t}]   {'[OK]' if ok else '[X]'} {fp}")
            if direct:
                # Trigger production deploy immediately
                if RENDER_SERVICE:
                    ok, _ = render_deploy(RENDER_SERVICE)
                    yield sse("log", f"[{t}] [GO] Production deploy {'triggered' if ok else 'failed'}")
                yield sse("done", {"status": "done", "message": "Deployed directly, Sir."})
            else:
                _pending[approval_id] = {
                    "type": "upload", "files": files_dict,
                    "sha_map": sha_map, "commit_msg": commit_msg, "ts": t
                }
                staging_url = render_get_url(RENDER_STAGING)
                if RENDER_STAGING: render_deploy(RENDER_STAGING)
                yield sse("approval_required", {
                    "approval_id": approval_id,
                    "diff":        [{"filepath": fp, "success": True} for fp in files_dict],
                    "summary":     f"{len(files_dict)} file(s) on staging.",
                    "staging_url": staging_url,
                    "message":     "Approve to push to production, Sir."
                })
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/approve', methods=['POST'])
def approve():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    aid     = (request.json or {}).get('approval_id', '')
    pending = _pending.get(aid)
    if not pending: return jsonify({"error": "Not found"}), 404

    details = []
    ptype   = pending.get('type', 'upload')

    if ptype == 'execute':
        ok, resp = gh_merge_to_master()
        if ok:
            details.append("[OK] staging → master merged")
            if RENDER_SERVICE:
                dok, _ = render_deploy(RENDER_SERVICE)
                details.append("[OK] Production deploy triggered" if dok else "[!] Deploy trigger failed")
        else:
            msg = resp.get('message','?') if isinstance(resp, dict) else str(resp)
            del _pending[aid]
            return jsonify({"success": False, "message": f"Merge failed: {msg}", "details": [f"[X] {msg}"]})
    else:
        for fp, content in pending['files'].items():
            ok, resp = gh_put(fp, content, pending['sha_map'].get(fp), pending.get('commit_msg','Ghost'))
            details.append(f"{'[OK]' if ok else '[X]'} {fp}")
        if RENDER_SERVICE:
            dok, _ = render_deploy(RENDER_SERVICE)
            details.append("[OK] Production deploy triggered" if dok else "[!] Deploy trigger failed")

    del _pending[aid]
    success = not any(d.startswith("[X]") for d in details)
    return jsonify({
        "success": success,
        "message": "Deployed to production! Live in ~30s, Sir." if success else "Some steps failed, Sir.",
        "details": details
    })


@app.route('/cancel', methods=['POST'])
def cancel():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    aid = (request.json or {}).get('approval_id', '')
    if aid in _pending: del _pending[aid]
    return jsonify({"success": True, "message": "Cancelled, Sir."})


@app.route('/rollback', methods=['POST'])
def rollback():
    """Rollback a file to previous commit."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data     = request.json or {}
    filepath = data.get('filepath', '')
    steps    = data.get('steps', 1)
    if not filepath: return jsonify({"error": "filepath required"}), 400
    try:
        # Get commit history for file
        r = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/commits?path={filepath}&per_page={steps+1}",
            headers=_gh_headers(), timeout=15)
        if r.status_code != 200:
            return jsonify({"error": f"GitHub error: {r.status_code}"}), 500
        commits = r.json()
        if len(commits) <= steps:
            return jsonify({"error": "Not enough history"}), 400
        target_sha = commits[steps]['sha']
        # Get file at that commit
        r2 = requests.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}?ref={target_sha}",
            headers=_gh_headers(), timeout=15)
        if r2.status_code != 200:
            return jsonify({"error": "Could not fetch old version"}), 500
        old_content = base64.b64decode(r2.json()['content']).decode('utf-8')
        # Get current SHA
        _, current_sha = gh_get(filepath)
        # Push old content as new commit
        ok, _ = gh_put(filepath, old_content, current_sha,
                       f"Ghost rollback: {filepath} ({steps} step{'s' if steps>1 else ''})")
        if ok:
            if RENDER_SERVICE: render_deploy(RENDER_SERVICE)
            return jsonify({"success": True, "message": f"Rolled back {filepath}, Sir."})
        return jsonify({"success": False, "message": "Rollback push failed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/monitor', methods=['GET', 'POST'])
def monitor():
    """Check health of all Render services + GitHub."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    results = {}

    # Production site
    prod_url = render_get_url(RENDER_SERVICE)
    if prod_url:
        try:
            r = requests.get(prod_url, timeout=10)
            results['production'] = {
                "url": prod_url, "status": r.status_code,
                "healthy": r.status_code == 200,
                "response_ms": int(r.elapsed.total_seconds() * 1000)
            }
        except Exception as e:
            results['production'] = {"url": prod_url, "healthy": False, "error": str(e)}

    # Staging site
    staging_url = render_get_url(RENDER_STAGING)
    if staging_url:
        try:
            r = requests.get(staging_url, timeout=10)
            results['staging'] = {
                "url": staging_url, "status": r.status_code,
                "healthy": r.status_code == 200,
                "response_ms": int(r.elapsed.total_seconds() * 1000)
            }
        except Exception as e:
            results['staging'] = {"url": staging_url, "healthy": False, "error": str(e)}

    # Ghost agent itself
    results['agent'] = {"healthy": True, "version": "v6", "pending": len(_pending)}

    # Recent deploys
    deploys = render_get_deploys(RENDER_SERVICE, limit=3)
    if deploys:
        results['last_deploy'] = {
            "status": deploys[0].get('status', '?'),
            "commit": deploys[0].get('commit', {}).get('message', '?')[:60],
            "time":   deploys[0].get('createdAt', '?')
        }

    # Database
    if DB_URL:
        rows, err = db_query("SELECT COUNT(*) as c FROM bookings")
        results['database'] = {"healthy": err is None, "bookings": rows[0]['c'] if rows else 0}

    all_healthy = all(v.get('healthy', True) for v in results.values() if isinstance(v, dict))
    return jsonify({
        "healthy": all_healthy,
        "message": "All systems nominal, Sir." if all_healthy else "Issues detected, Sir.",
        "services": results,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


@app.route('/ask', methods=['POST'])
def ask():
    """Answer a question — V70: Qwen only via Ghost app planning."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data     = request.json or {}
    question = data.get('question', '').strip()
    model    = data.get('model', 'auto')
    context  = data.get('context', '')   # extra context like error logs, screenshot text
    if not question: return jsonify({"error": "No question"}), 400

    prompt = f"""You are Ghost — AI assistant for StylStation barbershop platform (Flask/Python).
Answer concisely and practically. If code is needed, provide it.

Question: {question}"""
    if context:
        prompt += f"\n\nContext/Logs:\n{context}"

    response, used_model = ai_call(prompt, model, max_tokens=2048)

    if not response:
        return jsonify({"error": "AI unavailable", "message": "No AI responded, Sir."}), 503

    return jsonify({
        "answer":     response,
        "model_used": used_model,
        "question":   question
    })


@app.route('/db_query', methods=['POST'])
def route_db_query():
    """Run a SELECT query and return results."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    sql  = data.get('sql', '').strip()
    if not sql: return jsonify({"error": "No SQL"}), 400
    if not sql.upper().startswith('SELECT'):
        return jsonify({"error": "Only SELECT queries allowed here"}), 400
    rows, err = db_query(sql)
    if err: return jsonify({"error": err}), 500
    return jsonify({"rows": rows, "count": len(rows)})


@app.route('/db_report', methods=['GET', 'POST'])
def db_report():
    """Business analytics report from DB."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    report = {}

    queries = {
        "total_bookings":    "SELECT COUNT(*) as c FROM bookings",
        "bookings_today":    "SELECT COUNT(*) as c FROM bookings WHERE DATE(created_at) = CURRENT_DATE",
        "total_customers":   "SELECT COUNT(*) as c FROM users WHERE role='customer'",
        "total_shops":       "SELECT COUNT(*) as c FROM shops",
        "active_shops":      "SELECT COUNT(*) as c FROM shops WHERE status='active'",
        "revenue_today":     "SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE(created_at) = CURRENT_DATE",
        "revenue_month":     "SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)",
        "popular_services":  "SELECT service_name, COUNT(*) as bookings FROM bookings GROUP BY service_name ORDER BY bookings DESC LIMIT 5",
        "top_shops":         "SELECT shop_name, COUNT(*) as bookings FROM bookings GROUP BY shop_name ORDER BY bookings DESC LIMIT 5",
    }

    for key, sql in queries.items():
        rows, err = db_query(sql)
        if err:
            report[key] = {"error": err}
        elif key in ('popular_services', 'top_shops'):
            report[key] = rows
        else:
            report[key] = rows[0] if rows else {}

    return jsonify({"report": report, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route('/db_backup', methods=['POST'])
def db_backup():
    """Backup key DB tables to GitHub as JSON."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    backup = {}
    tables = ['users', 'shops', 'bookings', 'payments', 'services']
    for table in tables:
        rows, err = db_query(f"SELECT * FROM {table} LIMIT 1000")
        if not err and rows:
            backup[table] = rows
    if not backup:
        return jsonify({"error": "No data or DB not configured"}), 500
    content = json.dumps(backup, indent=2, default=str)
    fname   = f"backups/db_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    _, sha  = gh_get(fname)
    ok, _   = gh_put(fname, content, sha, f"DB backup {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if ok:
        return jsonify({"success": True, "message": f"Backed up to {fname}, Sir.", "tables": list(backup.keys())})
    return jsonify({"success": False, "message": "Backup push failed"}), 500


@app.route('/shop_status', methods=['GET', 'POST'])
def shop_status():
    """Get status of all shops."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    rows, err = db_query("SELECT id, name, subdomain, status, created_at FROM shops ORDER BY created_at DESC")
    if err: return jsonify({"error": err}), 500
    return jsonify({"shops": rows, "count": len(rows)})


@app.route('/shop_create', methods=['POST'])
def shop_create():
    """Create a new shop — customize template + deploy to subdomain."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    shop_data = request.json or {}
    if not shop_data.get('name') or not shop_data.get('subdomain'):
        return jsonify({"error": "name and subdomain required"}), 400

    def generate():
        t           = ts()
        approval_id = f"shop_{int(time.time())}"
        name        = shop_data['name']
        subdomain   = shop_data['subdomain']

        yield sse("log", f"[{t}] [SYS] Creating shop: {name}")
        yield sse("log", f"[{t}] [SYS] Subdomain: {subdomain}.stylstation.in")
        yield sse("log", f"[{t}] [DIR] Fetching templates...")

        template_files = {}
        for fp in gh_list_recursive():
            if os.path.splitext(fp)[1].lower() in {'.py', '.html', '.css', '.js', '.sql'}:
                content, _ = gh_get(fp)
                if content: template_files[fp] = content
        yield sse("log", f"[{t}] [OK] {len(template_files)} templates fetched")
        yield sse("log", f"[{t}] [AI] Customizing for {name}...")

        prompt = f"""You are Ghost Agent. Customize this barbershop website template for a new shop.

Shop details:
- Name: {name}
- Owner: {shop_data.get('owner', '')}
- Phone: {shop_data.get('phone', '')}
- Address: {shop_data.get('address', '')}
- Services: {shop_data.get('services', '')}
- Subdomain: {subdomain}

Current templates:
{chr(10).join([f'- {fp}' for fp in template_files.keys()])}

Return ONLY a JSON object with the customized files.
Replace all instances of 'Starcutters' with '{name}'.
Keep all Jinja2 syntax, Flask routes, and DB logic intact.
JSON only, no explanation."""

        response, used_model = ai_call(prompt, 'auto')
        customized = {}
        if response:
            try:
                clean = response.strip()
                if clean.startswith("```"):
                    clean = re.sub(r'^```[a-z]*\n?', '', clean)
                    clean = re.sub(r'\n?```$', '', clean)
                m = re.search(r'\{.*\}', clean, re.DOTALL)
                if m: customized = json.loads(m.group())
                yield sse("log", f"[{t}] [OK] {used_model} customized {len(customized)} files")
            except Exception as e:
                yield sse("log", f"[{t}] [!] Parse failed: {e} — using base templates")

        final = {**template_files, **customized}
        _pending[approval_id] = {
            "type": "shop_create", "shop_data": shop_data,
            "files": final, "sha_map": {}, "ts": t,
            "commit_msg": f"New shop: {name} [{subdomain}]"
        }
        yield sse("log", f"[{t}] {'─'*36}")
        yield sse("approval_required", {
            "approval_id": approval_id,
            "diff":        [{"filepath": fp, "success": True} for fp in final],
            "summary":     f"Shop '{name}' ready — {len(final)} files.",
            "staging_url": None,
            "message":     f"Approve to deploy {name}.stylstation.in, Sir."
        })

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/shop_suspend', methods=['POST'])
def shop_suspend():
    """Suspend a shop (set status=suspended in DB)."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data      = request.json or {}
    subdomain = data.get('subdomain', '')
    if not subdomain: return jsonify({"error": "subdomain required"}), 400
    _, err = db_query("UPDATE shops SET status='suspended' WHERE subdomain=%s", (subdomain,))
    if err: return jsonify({"error": err}), 500
    return jsonify({"success": True, "message": f"{subdomain} suspended, Sir."})


@app.route('/shop_activate', methods=['POST'])
def shop_activate():
    """Activate a suspended shop."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data      = request.json or {}
    subdomain = data.get('subdomain', '')
    if not subdomain: return jsonify({"error": "subdomain required"}), 400
    _, err = db_query("UPDATE shops SET status='active' WHERE subdomain=%s", (subdomain,))
    if err: return jsonify({"error": err}), 500
    return jsonify({"success": True, "message": f"{subdomain} activated, Sir."})


@app.route('/analytics', methods=['GET', 'POST'])
def analytics():
    """Full business analytics for StylStation."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401

    data = {}

    # Platform overview
    overview_queries = [
        ("total_shops",     "SELECT COUNT(*) as c FROM shops"),
        ("active_shops",    "SELECT COUNT(*) as c FROM shops WHERE status='active'"),
        ("total_users",     "SELECT COUNT(*) as c FROM users"),
        ("total_bookings",  "SELECT COUNT(*) as c FROM bookings"),
        ("bookings_today",  "SELECT COUNT(*) as c FROM bookings WHERE DATE(created_at)=CURRENT_DATE"),
        ("bookings_week",   "SELECT COUNT(*) as c FROM bookings WHERE created_at >= NOW() - INTERVAL '7 days'"),
        ("revenue_today",   "SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE(created_at)=CURRENT_DATE"),
        ("revenue_month",   "SELECT COALESCE(SUM(amount),0) as total FROM payments WHERE DATE_TRUNC('month',created_at)=DATE_TRUNC('month',NOW())"),
    ]
    overview = {}
    for key, sql in overview_queries:
        rows, err = db_query(sql)
        if not err and rows:
            overview[key] = rows[0].get('c') or rows[0].get('total') or 0
    data['overview'] = overview

    # Top performing shops
    rows, err = db_query("""
        SELECT s.name, s.subdomain, COUNT(b.id) as bookings,
               COALESCE(SUM(p.amount),0) as revenue
        FROM shops s
        LEFT JOIN bookings b ON b.shop_id = s.id
        LEFT JOIN payments p ON p.booking_id = b.id
        GROUP BY s.id, s.name, s.subdomain
        ORDER BY bookings DESC LIMIT 10
    """)
    if not err: data['top_shops'] = rows

    # Popular services
    rows, err = db_query("""
        SELECT service_name, COUNT(*) as bookings
        FROM bookings GROUP BY service_name
        ORDER BY bookings DESC LIMIT 10
    """)
    if not err: data['popular_services'] = rows

    return jsonify({
        "analytics": data,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": "Analytics ready, Sir."
    })


@app.route('/sync', methods=['POST'])
def sync():
    """Sync all repo files locally."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    result = {}
    for fp in gh_list_recursive():
        if os.path.splitext(fp)[1].lower() in {'.py','.html','.css','.js','.txt','.sql'}:
            content, sha = gh_get(fp)
            if content: result[fp] = {"content": content, "sha": sha}
    return jsonify({"success": True, "files": result, "count": len(result)})


@app.route('/read', methods=['POST'])
def read():
    """Read a single file from GitHub."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    fp = (request.json or {}).get('filepath', '')
    content, sha = gh_get(fp)
    if content: return jsonify({"content": content, "sha": sha})
    return jsonify({"error": "Not found"}), 404


@app.route('/status')
def status():
    if request.args.get('secret') != AGENT_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "version":  "v6",
        "repo":     GITHUB_REPO,
        "branch":   GITHUB_BRANCH,
        "staging":  STAGING_BRANCH,
        "render":   bool(RENDER_API_KEY),
        "db":       bool(DB_URL),
        "pending":  len(_pending),
    })


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
