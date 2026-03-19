"""
Ghost Agent — agent.py v5.0
Staging flow: /execute → AI generates code → staging branch → approval → production
"""

from flask import Flask, request, jsonify, Response, stream_with_context
import requests, json, os, base64, re, time
from datetime import datetime

app = Flask(__name__)

AGENT_SECRET    = os.environ.get('AGENT_SECRET',       'ghost_agent_2026')
GITHUB_TOKEN    = os.environ.get('GITHUB_TOKEN',       '')
GITHUB_REPO     = os.environ.get('GITHUB_REPO',        'viratkohli00733-crypto/starcutters')
GITHUB_BRANCH   = os.environ.get('GITHUB_BRANCH',      'master')
STAGING_BRANCH  = os.environ.get('STAGING_BRANCH',     'staging')
RENDER_API_KEY  = os.environ.get('RENDER_API_KEY',     '')
RENDER_SERVICE  = os.environ.get('RENDER_SERVICE_ID',  '')
RENDER_STAGING  = os.environ.get('RENDER_STAGING_ID',  '')   # staging service ID
GEMINI_API_KEY  = os.environ.get('GEMINI_API_KEY',     '')
CLAUDE_API_KEY  = os.environ.get('CLAUDE_API_KEY',     '')

GITHUB_API  = "https://api.github.com"
RENDER_API  = "https://api.render.com/v1"
GEMINI_URL  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
CLAUDE_URL  = "https://api.anthropic.com/v1/messages"

_pending = {}   # approval_id → pending data

# ── Auth ──────────────────────────────────────────────────────────────────────
def auth():
    secret = request.headers.get('X-Ghost-Secret','') or (request.json or {}).get('secret','')
    return secret == AGENT_SECRET

# ── GitHub helpers ────────────────────────────────────────────────────────────
def _gh_headers():
    return {
        "Authorization": f"token {os.environ.get('GITHUB_TOKEN', GITHUB_TOKEN)}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json"
    }

def gh_get(filepath, branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    url    = f"{GITHUB_API}/repos/{repo}/contents/{filepath}?ref={branch}"
    r      = requests.get(url, headers=_gh_headers(), timeout=15)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d['content']).decode('utf-8'), d['sha']
    return None, None

def gh_put(filepath, content, sha, message, branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    url     = f"{GITHUB_API}/repos/{repo}/contents/{filepath}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  branch
    }
    if sha: payload["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [200, 201], r.json()

def gh_list_recursive(path="", branch=None, repo=None):
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    url    = f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={branch}"
    r      = requests.get(url, headers=_gh_headers(), timeout=15)
    result = []
    if r.status_code == 200:
        for item in r.json():
            if   item['type'] == 'file': result.append(item['path'])
            elif item['type'] == 'dir':  result.extend(gh_list_recursive(item['path'], branch, repo))
    return result

def gh_merge_staging_to_master():
    """Merge staging branch into master via GitHub API."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/merges"
    payload = {
        "base":           GITHUB_BRANCH,
        "head":           STAGING_BRANCH,
        "commit_message": f"Ghost Deploy: staging → master [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
    }
    r = requests.post(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [201, 204], r.json() if r.content else {}

# ── SSE helper ────────────────────────────────────────────────────────────────
def sse(event, data):
    return f"data: {json.dumps({'event': event, 'data': data})}\n\n"

# ── Render helpers ────────────────────────────────────────────────────────────
def render_headers():
    return {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}

def render_trigger_deploy(service_id):
    """Trigger a manual deploy on Render service."""
    if not service_id or not RENDER_API_KEY:
        return False, "Render not configured"
    try:
        r = requests.post(
            f"{RENDER_API}/services/{service_id}/deploys",
            headers=render_headers(), json={}, timeout=15)
        return r.status_code == 201, r.json()
    except Exception as e:
        return False, str(e)

def get_staging_url():
    """Get staging service URL from Render."""
    if not RENDER_STAGING or not RENDER_API_KEY:
        return None
    try:
        r = requests.get(f"{RENDER_API}/services/{RENDER_STAGING}",
                         headers=render_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get('serviceDetails', {}).get('url')
    except: pass
    return None

# ── AI helper ─────────────────────────────────────────────────────────────────
def ai_call(prompt):
    if CLAUDE_API_KEY:
        try:
            r = requests.post(CLAUDE_URL,
                headers={"x-api-key": CLAUDE_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514",
                      "max_tokens": 8192,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=90)
            if r.status_code == 200:
                return r.json()['content'][0]['text']
            print(f"Claude error: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"Claude: {e}")
    if GEMINI_API_KEY:
        try:
            r = requests.post(GEMINI_URL,
                json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}},
                timeout=90)
            if r.status_code == 200:
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            print(f"Gemini error: {r.status_code}")
        except Exception as e:
            print(f"Gemini: {e}")
    return None

# ── /execute stream ───────────────────────────────────────────────────────────
def execute_stream(command, model, attached_files):
    """
    Full staging flow:
    1. Fetch current repo files
    2. AI generates changes
    3. Push to staging branch
    4. Trigger staging deploy
    5. Ask for approval
    6. On approve → merge staging → master → production deploy
    """
    ts = datetime.now().strftime("%H:%M:%S")
    approval_id = f"exec_{int(time.time())}"

    yield sse("log", f"[{ts}] [SYS] Ghost Agent v5 online, Sir.")
    yield sse("log", f"[{ts}] [SYS] Command: {command}")
    yield sse("log", f"[{ts}] [SYS] Model: {model.upper()}")
    yield sse("log", f"[{ts}] --------------------------------")

    # Step 1 — fetch repo files
    yield sse("log", f"[{ts}] [DIR] Fetching repo files from GitHub...")
    repo_files = {}
    try:
        all_paths = gh_list_recursive(branch=GITHUB_BRANCH)
        relevant  = [p for p in all_paths
                     if os.path.splitext(p)[1].lower()
                     in {'.py','.html','.css','.js','.txt','.sql'}]
        yield sse("log", f"[{ts}] [DIR] Found {len(relevant)} files")
        for fp in relevant:
            content, _ = gh_get(fp, branch=GITHUB_BRANCH)
            if content: repo_files[fp] = content
        yield sse("log", f"[{ts}] [OK] {len(repo_files)} files loaded")
    except Exception as e:
        yield sse("log", f"[{ts}] [X] Fetch error: {e}")
        yield sse("done", {"status": "error", "message": f"Failed to fetch repo: {e}"})
        return

    # Step 2 — attach extra files if any
    if attached_files:
        yield sse("log", f"[{ts}] [FILE] {len(attached_files)} attached files added")
        repo_files.update(attached_files)

    # Step 3 — AI generates changes
    yield sse("log", f"[{ts}] [AI] Sending to {model.upper()}...")
    file_summary = "\n".join([f"- {fp} ({len(c)} chars)" for fp, c in repo_files.items()])
    file_contents = "\n\n".join([
        f"=== {fp} ===\n{content}"
        for fp, content in list(repo_files.items())[:20]  # limit to avoid token overflow
    ])

    prompt = f"""You are Ghost Agent — an AI developer managing a Flask barbershop website.

TASK: {command}

CURRENT REPO FILES:
{file_summary}

FILE CONTENTS:
{file_contents}

INSTRUCTIONS:
- Make ONLY the changes needed for the task
- Keep all existing Jinja2 templates, Flask routes, and logic intact
- Return ONLY a JSON object with changed/new files
- Format: {{"filepath": "full file content", ...}}
- JSON only, no explanation, no markdown code blocks
- If creating new HTML, extend base.html using Jinja2 blocks"""

    response = ai_call(prompt)

    if not response:
        yield sse("log", f"[{ts}] [X] AI returned no response")
        yield sse("done", {"status": "error", "message": "AI failed to respond, Sir."})
        return

    yield sse("log", f"[{ts}] [AI] Response received, parsing...")

    # Step 4 — parse AI response
    changed_files = {}
    try:
        # Strip markdown code blocks if present
        clean = response.strip()
        if clean.startswith("```"):
            clean = re.sub(r'^```[a-z]*\n?', '', clean)
            clean = re.sub(r'\n?```$', '', clean)
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            changed_files = json.loads(m.group())
        yield sse("log", f"[{ts}] [OK] AI generated {len(changed_files)} file(s)")
        for fp in changed_files:
            yield sse("log", f"[{ts}]   → {fp}")
    except Exception as e:
        yield sse("log", f"[{ts}] [X] Parse error: {e}")
        yield sse("done", {"status": "error", "message": f"Could not parse AI response: {e}"})
        return

    if not changed_files:
        yield sse("log", f"[{ts}] [!] AI made no changes")
        yield sse("done", {"status": "error", "message": "AI made no file changes, Sir."})
        return

    # Step 5 — push to staging branch
    yield sse("log", f"[{ts}] [GO] Pushing to staging branch...")
    sha_map = {}
    push_errors = []
    for fp, content in changed_files.items():
        _, sha = gh_get(fp, branch=STAGING_BRANCH)
        sha_map[fp] = sha
        ok, resp = gh_put(fp, content, sha,
                          f"Ghost staging: {command[:60]}",
                          branch=STAGING_BRANCH)
        if ok:
            yield sse("log", f"[{ts}]   [OK] {fp}")
        else:
            err = resp.get('message', '?') if isinstance(resp, dict) else str(resp)
            yield sse("log", f"[{ts}]   [X] {fp}: {err}")
            push_errors.append(fp)

    if push_errors:
        yield sse("log", f"[{ts}] [!] {len(push_errors)} file(s) failed to push")

    # Step 6 — trigger staging deploy
    staging_url = None
    if RENDER_STAGING:
        yield sse("log", f"[{ts}] [GO] Triggering staging deploy on Render...")
        ok, _ = render_trigger_deploy(RENDER_STAGING)
        if ok:
            yield sse("log", f"[{ts}] [OK] Staging deploy triggered")
            staging_url = get_staging_url()
            if staging_url:
                yield sse("log", f"[{ts}] [INFO] Staging URL: {staging_url}")
        else:
            yield sse("log", f"[{ts}] [!] Staging deploy trigger failed")
    else:
        yield sse("log", f"[{ts}] [!] No staging Render ID — skipping deploy trigger")

    # Step 7 — store pending and ask approval
    _pending[approval_id] = {
        "type":       "execute",
        "command":    command,
        "files":      changed_files,
        "sha_map":    sha_map,
        "staging_url": staging_url,
        "ts":         ts,
    }

    yield sse("log", f"[{ts}] --------------------------------")
    yield sse("approval_required", {
        "approval_id":  approval_id,
        "diff":         [{"filepath": fp, "success": fp not in push_errors}
                         for fp in changed_files],
        "summary":      f"{len(changed_files)} file(s) on staging. Preview → approve to go live.",
        "staging_url":  staging_url,
        "message":      f"Staging ready, Sir. Preview and approve to deploy to production."
    })


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/health')
def health():
    return jsonify({
        "status":  "alive",
        "version": "v5",
        "pending": len(_pending),
        "claude":  bool(CLAUDE_API_KEY),
        "gemini":  bool(GEMINI_API_KEY),
        "render":  bool(RENDER_API_KEY),
        "staging": bool(RENDER_STAGING),
    })

@app.route('/execute', methods=['POST'])
def execute():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data           = request.json or {}
    command        = data.get('command', '').strip()
    model          = data.get('model',   'auto')
    attached_files = data.get('attached_files') or {}
    if not command: return jsonify({"error": "No command"}), 400

    def generate():
        try:
            for chunk in execute_stream(command, model, attached_files):
                yield chunk
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")
            yield sse("done",  {"status": "error", "message": str(e)})

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/approve', methods=['POST'])
def approve():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    aid     = (request.json or {}).get('approval_id', '')
    pending = _pending.get(aid)
    if not pending: return jsonify({"error": "Not found"}), 404

    details = []

    if pending.get('type') == 'execute':
        # Merge staging → master
        ok, resp = gh_merge_staging_to_master()
        if ok:
            details.append("[OK] staging merged to master")
            # Trigger production deploy
            if RENDER_SERVICE:
                dok, _ = render_trigger_deploy(RENDER_SERVICE)
                details.append(f"[OK] Production deploy triggered" if dok
                                else "[!] Production deploy trigger failed")
        else:
            msg = resp.get('message', '?') if isinstance(resp, dict) else str(resp)
            details.append(f"[X] Merge failed: {msg}")
            del _pending[aid]
            return jsonify({"success": False, "message": f"Merge failed: {msg}", "details": details})
    else:
        # Legacy upload flow
        for fp, content in pending['files'].items():
            ok, resp = gh_put(fp, content, pending['sha_map'].get(fp), pending.get('commit_msg','Ghost upload'))
            details.append(f"{'[OK]' if ok else '[X]'} {fp}")

    del _pending[aid]
    success = all(d.startswith("[OK]") for d in details)
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

@app.route('/upload', methods=['POST'])
def upload():
    """Legacy direct upload route."""
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    data       = request.json or {}
    files_dict = data.get('files', {})
    commit_msg = data.get('commit_msg', f"Ghost upload [{datetime.now().strftime('%H:%M')}]")
    if not files_dict: return jsonify({"error": "No files"}), 400

    def generate():
        try:
            ts          = datetime.now().strftime("%H:%M:%S")
            approval_id = f"appr_{int(time.time())}"
            yield sse("log", f"[{ts}] Uploading {len(files_dict)} file(s)...")
            sha_map = {}
            for fp in files_dict:
                _, sha = gh_get(fp)
                sha_map[fp] = sha
                yield sse("log", f"[{ts}]   {'[exists]' if sha else '[new]'} {fp}")
            _pending[approval_id] = {"files": files_dict, "sha_map": sha_map, "commit_msg": commit_msg, "ts": ts}
            yield sse("approval_required", {
                "approval_id": approval_id,
                "diff": [{"filepath": fp, "success": True} for fp in files_dict],
                "summary": f"{len(files_dict)} file(s) ready.",
                "message": "Approve to deploy, Sir."
            })
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

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

@app.route('/render_logs', methods=['GET', 'POST'])
def render_logs():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    if not RENDER_SERVICE or not RENDER_API_KEY:
        return jsonify({"error": "Render not configured"}), 500
    try:
        r = requests.get(f"{RENDER_API}/services/{RENDER_SERVICE}/deploys?limit=10",
                         headers=render_headers(), timeout=15)
        if r.status_code == 200:
            return jsonify({"deploys": r.json(), "count": len(r.json())})
        return jsonify({"error": f"HTTP {r.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/new_shop', methods=['POST'])
def new_shop():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    shop_data = request.json or {}
    if not shop_data.get('name') or not shop_data.get('subdomain'):
        return jsonify({"error": "name and subdomain required"}), 400

    def generate():
        try:
            ts          = datetime.now().strftime("%H:%M:%S")
            approval_id = f"shop_{int(time.time())}"
            name        = shop_data.get('name', 'NewShop')
            yield sse("log", f"[{ts}] New shop: {name}")
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
Return JSON only: {{"templates/index.html": "...", "templates/base.html": "..."}}
Replace 'Starcutters' with '{name}'. Keep all Jinja2 syntax."""
            response  = ai_call(prompt)
            customized = {}
            if response:
                try:
                    m = re.search(r'\{.*\}', response, re.DOTALL)
                    if m: customized = json.loads(m.group())
                    yield sse("log", f"[{ts}] [OK] AI customized {len(customized)} files")
                except:
                    yield sse("log", f"[{ts}] [!] AI parse failed")
            final = {**template_files, **customized}
            _pending[approval_id] = {"files": final, "sha_map": {}, "commit_msg": f"New shop: {name}", "ts": ts}
            yield sse("approval_required", {
                "approval_id": approval_id,
                "diff":        [{"filepath": fp, "success": True} for fp in final],
                "summary":     f"Shop '{name}' ready.",
                "message":     "Approve to deploy, Sir."
            })
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/status')
def status():
    if request.args.get('secret') != AGENT_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "repo":     GITHUB_REPO,
        "branch":   GITHUB_BRANCH,
        "staging":  STAGING_BRANCH,
        "claude":   bool(CLAUDE_API_KEY),
        "gemini":   bool(GEMINI_API_KEY),
        "render":   bool(RENDER_API_KEY),
        "pending":  len(_pending),
        "version":  "v5"
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
