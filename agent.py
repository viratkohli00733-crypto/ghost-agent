"""
Ghost Agent — agent.py v3.0
- Claude primary (diff format), Gemini fallback
- SSE streaming live logs
- Approval system — Sir approve kare tab deploy
- Local file sync support
- Smart file selector
"""

from flask import Flask, request, jsonify, Response, stream_with_context
import requests, json, os, base64, re, time
from datetime import datetime

app = Flask(__name__)

AGENT_SECRET   = os.environ.get('AGENT_SECRET', 'ghost_agent_2026')
GITHUB_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO    = os.environ.get('GITHUB_REPO', 'viratkohli00733-crypto/starcutters')
GITHUB_BRANCH  = os.environ.get('GITHUB_BRANCH', 'master')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY', '')

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
GITHUB_API = "https://api.github.com"

# In-memory pending approvals
_pending = {}

def _gh_headers():
    token = os.environ.get('GITHUB_TOKEN', GITHUB_TOKEN)
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

CONTEXT = """You are an expert Flask developer on StylStation — a barbershop booking platform.
STACK: Flask + PostgreSQL (psycopg2) + Jinja2 templates
KEY PATTERNS: get_subdomain(), get_db()/release_db(), shop_id for tenant isolation
DB TABLES: shops, artists, services, bookings, gallery, reviews
STRUCTURE: app.py (all routes), templates/, static/, requirements.txt"""

DIFF_FORMAT = """
STRICT OUTPUT FORMAT — follow exactly, no exceptions:

FILE: <filepath>
<<<OLD>>>
<exact existing code to replace — must match file exactly>
<<<NEW>>>
<new code to put in its place>
<<<END>>>

RULES:
- Return ONLY this format. No explanation. No markdown. No full file.
- OLD must be character-perfect match of existing code
- For new file creation: leave OLD section empty
- Multiple changes = multiple FILE blocks
- If no changes needed: return NOCHANGE
"""


# ── SSE ────────────────────────────────────────────────────────────────────────

def sse(event, data):
    payload = json.dumps({"event": event, "data": data})
    return f"data: {payload}\n\n"


# ── GitHub ─────────────────────────────────────────────────────────────────────

def gh_get(filepath):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=_gh_headers(), timeout=15)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d['content']).decode('utf-8'), d['sha']
    return None, None

def gh_put(filepath, content, sha, message):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in [200, 201], r.json()

def gh_list_recursive(path=""):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=_gh_headers(), timeout=15)
    result = []
    if r.status_code == 200:
        for item in r.json():
            if item['type'] == 'file':
                result.append(item['path'])
            elif item['type'] == 'dir':
                result.extend(gh_list_recursive(item['path']))
    return result


# ── AI ─────────────────────────────────────────────────────────────────────────

def call_claude(prompt):
    if not CLAUDE_API_KEY:
        return None
    try:
        r = requests.post(CLAUDE_URL, headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }, json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=60)
        if r.status_code == 200:
            return r.json()['content'][0]['text']
        print(f"Claude {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Claude error: {e}")
    return None

def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    try:
        r = requests.post(GEMINI_URL, json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096}
        }, timeout=60)
        if r.status_code == 200:
            return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Gemini error: {e}")
    return None

def ai_call(prompt, model="auto"):
    if model == "gemini":
        return call_gemini(prompt)
    if model == "claude":
        return call_claude(prompt)
    return call_claude(prompt) or call_gemini(prompt)


# ── Diff parser ────────────────────────────────────────────────────────────────

def parse_diff(text):
    changes = []
    blocks = re.split(r'^FILE:\s*', text, flags=re.MULTILINE)
    for block in blocks:
        if not block.strip():
            continue
        try:
            lines = block.strip().split('\n')
            filepath = lines[0].strip()
            old_m = re.search(r'<<<OLD>>>\n(.*?)<<<NEW>>>', block, re.DOTALL)
            new_m = re.search(r'<<<NEW>>>\n(.*?)<<<END>>>', block, re.DOTALL)
            if old_m and new_m:
                changes.append({
                    "filepath": filepath,
                    "old": old_m.group(1),
                    "new": new_m.group(1)
                })
        except Exception as e:
            print(f"Parse error: {e}")
    return changes

def apply_diff(content, old_code, new_code):
    if old_code.strip() == "":
        return new_code, True
    if old_code in content:
        return content.replace(old_code, new_code, 1), True
    # Try stripped lines match
    old_lines = [l.rstrip() for l in old_code.strip().split('\n')]
    content_lines = content.split('\n')
    for i in range(len(content_lines) - len(old_lines) + 1):
        chunk = [l.rstrip() for l in content_lines[i:i+len(old_lines)]]
        if chunk == old_lines:
            new_lines = content_lines[:i] + new_code.split('\n') + content_lines[i+len(old_lines):]
            return '\n'.join(new_lines), True
    return content, False


# ── Smart file selector ────────────────────────────────────────────────────────

def select_relevant_files(command, all_files):
    cmd = command.lower()
    relevant = []

    if 'app.py' in all_files:
        relevant.append('app.py')

    keyword_map = {
        'booking':    ['templates/booking.html', 'templates/booking_awaiting.html', 'templates/booking_confirmed.html'],
        'payment':    ['templates/payment.html', 'templates/payment_review.html'],
        'artist':     ['templates/artist.html', 'templates/artists.html'],
        'login':      ['templates/login.html', 'templates/partner_login.html'],
        'signup':     ['templates/signup.html'],
        'index':      ['templates/index.html'],
        'home':       ['templates/index.html'],
        'contact':    ['templates/contact.html'],
        'review':     ['templates/reviews.html'],
        'service':    ['templates/services.html'],
        'admin':      ['templates/admin.html'],
        'base':       ['templates/base.html'],
        'timeline':   ['templates/timeline.html'],
        'my booking': ['templates/my_bookings.html'],
        '404':        ['templates/404.html'],
    }

    for kw, files in keyword_map.items():
        if kw in cmd:
            for f in files:
                if f in all_files and f not in relevant:
                    relevant.append(f)

    if any(w in cmd for w in ['style', 'css', 'color', 'font', 'design']):
        for f in all_files:
            if f.startswith('static/') and f.endswith('.css') and f not in relevant:
                relevant.append(f)

    if any(w in cmd for w in ['database', 'schema', 'table', 'column', 'db']):
        if 'database/schema.sql' in all_files and 'database/schema.sql' not in relevant:
            relevant.append('database/schema.sql')

    # Always add base.html for template context if any template selected
    if any(f.startswith('templates/') for f in relevant):
        if 'templates/base.html' in all_files and 'templates/base.html' not in relevant:
            relevant.append('templates/base.html')

    return relevant[:6]


# ── Main executor (SSE) ────────────────────────────────────────────────────────

def execute_stream(command, model="auto", attached_files=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    approval_id = f"appr_{int(time.time())}"
    ai_name = "Claude" if (model == "claude" or (model == "auto" and CLAUDE_API_KEY)) else "Gemini"

    yield sse("log", f"[{ts}] ⚡ Ghost Agent v3 — Sir.")
    yield sse("log", f"[{ts}] 📋 Command: \"{command}\"")
    yield sse("log", f"[{ts}] 🤖 Engine: {ai_name}")
    time.sleep(0.1)

    # Step 1: File list
    yield sse("log", f"[{ts}] 📡 Fetching repo file list...")
    all_files = gh_list_recursive()
    if not all_files:
        yield sse("error", f"[{ts}] ❌ Cannot reach GitHub!")
        yield sse("done", {"status": "error", "message": "GitHub unreachable, Sir."})
        return
    yield sse("log", f"[{ts}] ✅ {len(all_files)} files found")

    # Step 2: File contents
    sha_map = {}
    if attached_files:
        file_contents = attached_files
        yield sse("log", f"[{ts}] 📎 Using attached files: {', '.join(attached_files.keys())}")
        for fp in attached_files:
            _, sha = gh_get(fp)
            sha_map[fp] = sha
    else:
        relevant = select_relevant_files(command, all_files)
        yield sse("log", f"[{ts}] 🎯 Selected: {', '.join(relevant)}")
        file_contents = {}
        for fp in relevant:
            content, sha = gh_get(fp)
            if content:
                file_contents[fp] = content
                sha_map[fp] = sha
                yield sse("log", f"[{ts}]    ✅ {fp} ({len(content.splitlines())} lines)")
            else:
                yield sse("error", f"[{ts}]    ⚠️  {fp} — fetch failed")

    if not file_contents:
        yield sse("error", f"[{ts}] ❌ No files loaded!")
        yield sse("done", {"status": "error", "message": "No files, Sir."})
        return

    # Step 3: AI prompt
    yield sse("log", f"[{ts}] 🤖 Asking {ai_name} for diff...")
    context = CONTEXT + "\n\nCURRENT FILES:\n"
    for fp, content in file_contents.items():
        context += f"\n--- {fp} ---\n{content}\n"
    prompt = context + f'\nCOMMAND: "{command}"\n{DIFF_FORMAT}'

    response = ai_call(prompt, model=model)
    if not response:
        yield sse("error", f"[{ts}] ❌ AI no response!")
        yield sse("done", {"status": "error", "message": "AI failed, Sir."})
        return

    yield sse("log", f"[{ts}] ✅ AI responded ({len(response)} chars)")

    if "NOCHANGE" in response:
        yield sse("log", f"[{ts}] ℹ️  AI says no changes needed")
        yield sse("done", {"status": "nochange", "message": "No changes needed, Sir."})
        return

    # Step 4: Parse & apply diff
    yield sse("log", f"[{ts}] 🔧 Parsing diff...")
    changes = parse_diff(response)
    if not changes:
        yield sse("error", f"[{ts}] ❌ Diff parse failed!")
        yield sse("error", f"[{ts}]    AI raw: {response[:300]}")
        yield sse("done", {"status": "error", "message": "Could not parse AI diff, Sir."})
        return

    yield sse("log", f"[{ts}] ✅ {len(changes)} change(s) parsed")

    preview_files = {}
    diff_preview = []

    for ch in changes:
        fp = ch['filepath']
        original = file_contents.get(fp, "")
        new_content, ok = apply_diff(original, ch['old'], ch['new'])
        diff_preview.append({
            "filepath": fp,
            "old": ch['old'],
            "new": ch['new'],
            "success": ok
        })
        if ok:
            preview_files[fp] = new_content
            yield sse("log", f"[{ts}]    ✅ {fp} patch applied")
        else:
            yield sse("error", f"[{ts}]    ⚠️  {fp} — OLD code not found! Manual check needed.")

    # Step 5: Store pending
    _pending[approval_id] = {
        "files":   preview_files,
        "sha_map": sha_map,
        "command": command,
        "ts":      ts,
        "diff":    diff_preview
    }

    yield sse("log", f"[{ts}] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    yield sse("log", f"[{ts}] ⏳ Awaiting Sir's approval...")

    yield sse("approval_required", {
        "approval_id": approval_id,
        "diff":        diff_preview,
        "file_count":  len(preview_files),
        "change_count": len(changes),
        "summary":     f"{len(changes)} change(s) across {len(preview_files)} file(s)",
        "message":     "Review and approve to deploy, Sir."
    })


# ── Routes ─────────────────────────────────────────────────────────────────────

def auth():
    secret = request.headers.get('X-Ghost-Secret', '') or (request.json or {}).get('secret', '')
    return secret == AGENT_SECRET

@app.route('/health')
def health():
    return jsonify({"status": "alive", "version": "v3", "pending": len(_pending)})

@app.route('/execute', methods=['POST'])
def execute():
    if not auth():
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    cmd     = data.get('command', '').strip()
    model   = data.get('model', 'auto')
    attached = data.get('attached_files', None)
    if not cmd:
        return jsonify({"error": "No command"}), 400

    def generate():
        try:
            for chunk in execute_stream(cmd, model=model, attached_files=attached):
                yield chunk
        except Exception as e:
            yield sse("error", f"[FATAL] {e}")
            yield sse("done", {"status": "error", "message": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

@app.route('/approve', methods=['POST'])
def approve():
    if not auth():
        return jsonify({"error": "Unauthorized"}), 401
    approval_id = (request.json or {}).get('approval_id', '')
    pending = _pending.get(approval_id)
    if not pending:
        return jsonify({"error": "Not found or expired"}), 404

    ts = pending['ts']
    results = []
    for fp, content in pending['files'].items():
        sha = pending['sha_map'].get(fp)
        ok, resp = gh_put(fp, content, sha,
                          f"Ghost: {pending['command'][:60]} [{ts}]")
        if ok:
            results.append(f"✅ {fp}")
        else:
            err = resp.get('message', '?') if isinstance(resp, dict) else str(resp)
            results.append(f"❌ {fp}: {err}")

    del _pending[approval_id]
    success = all(r.startswith("✅") for r in results)
    return jsonify({
        "success": success,
        "message": "Deployed! Render live in ~30s, Sir." if success else "Some files failed, Sir.",
        "details": results
    })

@app.route('/cancel', methods=['POST'])
def cancel():
    if not auth():
        return jsonify({"error": "Unauthorized"}), 401
    aid = (request.json or {}).get('approval_id', '')
    if aid in _pending:
        del _pending[aid]
    return jsonify({"success": True, "message": "Cancelled, Sir."})

@app.route('/sync', methods=['POST'])
def sync():
    """Full repo sync for Ghost local storage."""
    if not auth():
        return jsonify({"error": "Unauthorized"}), 401
    exts = {'.py', '.html', '.css', '.js', '.txt', '.sql'}
    all_files = gh_list_recursive()
    result = {}
    for fp in all_files:
        if os.path.splitext(fp)[1].lower() in exts:
            content, sha = gh_get(fp)
            if content:
                result[fp] = {"content": content, "sha": sha}
    return jsonify({"success": True, "files": result, "count": len(result)})

@app.route('/read', methods=['POST'])
def read():
    if not auth():
        return jsonify({"error": "Unauthorized"}), 401
    fp = (request.json or {}).get('filepath', '')
    content, sha = gh_get(fp)
    if content:
        return jsonify({"content": content, "sha": sha})
    return jsonify({"error": "Not found"}), 404

@app.route('/status')
def status():
    if request.args.get('secret') != AGENT_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "repo":    GITHUB_REPO,
        "branch":  GITHUB_BRANCH,
        "gemini":  bool(GEMINI_API_KEY),
        "claude":  bool(CLAUDE_API_KEY),
        "pending": len(_pending),
        "version": "v3"
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
