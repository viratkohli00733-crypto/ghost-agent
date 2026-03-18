"""
Ghost Agent — agent.py
Deploy on Render as separate Flask service.
"""

from flask import Flask, request, jsonify
import requests, json, os, base64, re
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
GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}

CONTEXT = """You are an expert Flask developer on StylStation — a barbershop booking platform.

STACK: Flask + PostgreSQL (psycopg2) + Jinja2 templates
KEY PATTERNS:
- get_subdomain() for multi-tenant detection
- get_db() / release_db() for DB connections
- shop_id for tenant isolation
- JSON responses for /api/ routes, render_template for pages

DB TABLES: shops, artists, services, bookings, gallery, reviews
STRUCTURE: app.py (all routes), templates/, static/, requirements.txt"""


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh_get(filepath):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=GH_HEADERS, timeout=15)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d['content']).decode('utf-8'), d['sha']
    return None, None

def gh_put(filepath, content, sha, message):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": GITHUB_BRANCH
    }
    if sha: payload["sha"] = sha
    r = requests.put(url, headers=GH_HEADERS, json=payload, timeout=15)
    return r.status_code in [200, 201], r.json()

def gh_list(path=""):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=GH_HEADERS, timeout=15)
    return [f['path'] for f in r.json()] if r.status_code == 200 else []


# ── AI helpers ─────────────────────────────────────────────────────────────────

def gemini(prompt):
    try:
        r = requests.post(GEMINI_URL, json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}
        }, timeout=60)
        if r.status_code == 200:
            return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Gemini error: {e}")
    return None

def claude(prompt):
    if not CLAUDE_API_KEY: return None
    try:
        r = requests.post(CLAUDE_URL, headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }, json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=60)
        if r.status_code == 200:
            return r.json()['content'][0]['text']
    except Exception as e:
        print(f"Claude error: {e}")
    return None

def ai(prompt):
    return claude(prompt) or gemini(prompt)

def clean_code(code, lang="python"):
    code = re.sub(rf'^```{lang}\n?', '', code, flags=re.MULTILINE)
    code = re.sub(r'^```\n?', '', code, flags=re.MULTILINE)
    return code.strip()


# ── Main executor ──────────────────────────────────────────────────────────────

def execute_task(command):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Read current app.py
    app_content, app_sha = gh_get('app.py')
    if not app_content:
        return {"success": False, "message": "Cannot read app.py from GitHub, Sir."}

    templates = gh_list('templates')

    # Step 1: Plan
    plan_raw = ai(f"""{CONTEXT}

CURRENT app.py (summary):
{app_content[:2000]}

TEMPLATES: {templates}

COMMAND: "{command}"

Return JSON plan:
{{"files_to_modify": ["app.py"], "files_to_create": [], "summary": "one line", "safe": true}}
JSON only, no text.""")

    try:
        plan = json.loads(re.search(r'\{.*\}', plan_raw or '{}', re.DOTALL).group())
    except Exception:
        plan = {"files_to_modify": ["app.py"], "files_to_create": [], "summary": command, "safe": True}

    if not plan.get('safe', True):
        return {"success": False, "message": f"Unsafe operation, Sir. Manual review needed."}

    results = []

    # Step 2: Modify app.py
    if 'app.py' in plan.get('files_to_modify', []):
        new_code = ai(f"""{CONTEXT}

CURRENT app.py:
{app_content}

TASK: {command}

Return COMPLETE updated app.py.
Add comment: # GHOST AGENT [{ts}]: {command[:50]}
Python code only, no markdown.""")

        if not new_code:
            return {"success": False, "message": "AI could not generate code, Sir."}

        new_code = clean_code(new_code)
        ok, resp = gh_put('app.py', new_code, app_sha,
                          f"Ghost Agent: {command[:60]} [{ts}]")
        if ok:
            results.append("app.py updated ✅")
        else:
            return {"success": False, "message": f"GitHub error: {resp.get('message', 'Unknown')}"}

    # Step 3: Create new templates
    for f in plan.get('files_to_create', []):
        if f.startswith('templates/'):
            html = ai(f"""{CONTEXT}
Create {f} for: {command}
Return complete HTML with Jinja2. HTML only.""")
            if html:
                ok, _ = gh_put(f, clean_code(html, 'html'), None,
                               f"Ghost Agent: Create {f} [{ts}]")
                if ok: results.append(f"{f} created ✅")

    # Step 4: Modify existing templates
    for f in plan.get('files_to_modify', []):
        if f.startswith('templates/'):
            content, sha = gh_get(f)
            if content:
                new_html = ai(f"""{CONTEXT}
CURRENT {f}:
{content}
TASK: {command}
Return complete updated HTML.""")
                if new_html:
                    ok, _ = gh_put(f, clean_code(new_html, 'html'), sha,
                                   f"Ghost Agent: Update {f} [{ts}]")
                    if ok: results.append(f"{f} updated ✅")

    return {
        "success": True,
        "message": f"{plan.get('summary', command)} Done, Sir. Render deploying in ~30 seconds.",
        "details": results,
        "timestamp": ts
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

def auth():
    secret = request.headers.get('X-Ghost-Secret', '') or (request.json or {}).get('secret', '')
    return secret == AGENT_SECRET

@app.route('/health')
def health():
    return jsonify({"status": "alive", "agent": "Ghost Agent"})

@app.route('/execute', methods=['POST'])
def execute():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    cmd = (request.json or {}).get('command', '').strip()
    if not cmd: return jsonify({"error": "No command"}), 400
    print(f"Agent: {cmd}")
    return jsonify(execute_task(cmd))

@app.route('/read', methods=['POST'])
def read():
    if not auth(): return jsonify({"error": "Unauthorized"}), 401
    fp = (request.json or {}).get('filepath', '')
    content, _ = gh_get(fp)
    return jsonify({"content": content}) if content else (jsonify({"error": "Not found"}), 404)

@app.route('/status')
def status():
    if request.args.get('secret') != AGENT_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "repo": GITHUB_REPO, "branch": GITHUB_BRANCH,
        "gemini": bool(GEMINI_API_KEY), "claude": bool(CLAUDE_API_KEY),
        "files": gh_list()
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
  
