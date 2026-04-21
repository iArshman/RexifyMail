"""
Web server module: OAuth, Email View, User Mailbox, Landing & Compliance pages
"""
import os
from aiohttp import web, ClientSession
import logging
import time
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import base64
import json
import html as html_module
import re
import hashlib
import asyncio

logger = logging.getLogger(__name__)

# Globals set by main.py
bot = None
db = None
oauth_states = None
CLIENT_ID = None
CLIENT_SECRET = None
REDIRECT_URI = None

def setup_web_module(bot_instance, db_instance, oauth_states_dict, client_id, client_secret, redirect_uri):
    global bot, db, oauth_states, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    bot = bot_instance
    db = db_instance
    oauth_states = oauth_states_dict
    CLIENT_ID = client_id
    CLIENT_SECRET = client_secret
    REDIRECT_URI = redirect_uri

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_gmail_service(access_token, refresh_token=None):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_info({
        'token': access_token,
        'refresh_token': refresh_token,
        'token_uri': 'https://oauth2.googleapis.com/token',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly',
                   'https://www.googleapis.com/auth/gmail.send',
                   'https://www.googleapis.com/auth/gmail.modify']
    })
    return build('gmail', 'v1', credentials=creds)

async def call_ms_graph(endpoint: str, token: str, method: str = "GET", json_data: dict = None):
    """Helper to call Microsoft Graph API in Web Module"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://graph.microsoft.com/v1.0/{endpoint}"
    async with ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json_data) as resp:
            if resp.status in [200, 201, 202]: return await resp.json()
            if resp.status == 204: return True
            resp_text = await resp.text()
            raise Exception(f"Graph API Error {resp.status}: {resp_text}")

async def get_user_email(access_token):
    try:
        async with ClientSession() as session:
            async with session.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                                   headers={'Authorization': f'Bearer {access_token}'}) as r:
                if r.status == 200:
                    return (await r.json()).get('emailAddress')
    except Exception as e:
        logger.error(f"Error getting user email: {e}")
    return None

def get_header(headers, name):
    for h in headers:
        if h['name'].lower() == name.lower():
            return h['value']
    return ''

def get_time_ago(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    if diff < 60: return "Just now"
    if diff < 3600: return f"{int(diff/60)}m ago"
    if diff < 86400: return f"{int(diff/3600)}h ago"
    if diff < 86400*7: return f"{int(diff/86400)}d ago"
    return dt.strftime("%b %d")

def get_email_content(payload):
    html_body, plain_body, attachments = None, None, []
    def parse(parts):
        nonlocal html_body, plain_body
        for part in parts:
            mime = part.get('mimeType', '')
            fn = part.get('filename', '')
            data = part.get('body', {}).get('data', '')
            if fn:
                attachments.append({'name': fn, 'type': mime, 'size': part.get('body', {}).get('size', 0)})
            if mime == 'text/html' and not fn and not html_body and data:
                html_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            elif mime == 'text/plain' and not fn and not plain_body and data:
                plain_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if 'parts' in part:
                parse(part['parts'])
    if 'parts' in payload:
        parse(payload['parts'])
    else:
        data = payload.get('body', {}).get('data', '')
        if data:
            decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if payload.get('mimeType') == 'text/html':
                html_body = decoded
            else:
                plain_body = decoded
    return html_body, plain_body, attachments

AVATAR_COLORS = ['#d93025','#0b8043','#039be5','#8430ce','#e37400','#188038','#c5221f','#1a73e8']

def avatar_color(name):
    return AVATAR_COLORS[len(name) % len(AVATAR_COLORS)] if name else AVATAR_COLORS[0]

def extract_sender_name(from_str):
    if not from_str: return '', ''
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_str.strip())
    if m: return m.group(1).strip(), m.group(2).strip()
    return from_str.strip(), from_str.strip()

def render_attachments(attachments):
    if not attachments: return ""
    chips = ""
    for att in attachments:
        sz = att['size']
        sz_str = f"{sz/1024:.1f} KB" if sz < 1024*1024 else f"{sz/(1024*1024):.1f} MB"
        icon = "🖼️" if "image" in att.get('type','') else ("📊" if "sheet" in att.get('type','') else "📄")
        chips += f'<div class="att-chip"><span class="att-icon">{icon}</span><div><div class="att-name">{html_module.escape(att["name"])}</div><div class="att-size">{sz_str}</div></div></div>'
    return f'<div class="att-bar"><div class="att-label">📎 Attachments</div><div class="att-chips">{chips}</div></div>'

# ── SHARED CSS ────────────────────────────────────────────────────────────────

BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Roboto:wght@300;400;500&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
    --primary:#1a73e8;--primary-dark:#1557b0;--primary-light:#e8f0fe;
    --red:#d93025;--green:#188038;--yellow:#f9ab00;
    --bg:#f6f8fc;--surface:#fff;--sidebar-bg:#f6f8fc;
    --text:#202124;--text-2:#5f6368;--text-3:#80868b;
    --border:#e0e0e0;--shadow:0 1px 3px rgba(0,0,0,.1),0 1px 2px rgba(0,0,0,.06);
    --radius:8px;--radius-lg:16px;
}
@media (prefers-color-scheme: dark) {
    :root {
        --primary-light:rgba(26,115,232,0.15);
        --bg:#121212;--surface:#1e1e1e;--sidebar-bg:#1a1a1a;
        --text:#e8eaed;--text-2:#9aa0a6;--text-3:#80868b;
        --border:#3c4043;--shadow:0 1px 3px rgba(0,0,0,.4);
    }
}
body{font-family:'Roboto',sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
a{color:inherit;text-decoration:none}
button{cursor:pointer;border:none;background:none;font-family:inherit;color:inherit}
"""

# ── LANDING PAGE ──────────────────────────────────────────────────────────────

async def main_page_handler(request):
    bot_user = await bot.get_me()
    bot_link = f"https://t.me/{bot_user.username}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rexify Mail — Mail Bot for Telegram</title>
<style>
{BASE_CSS}
body{{background:var(--surface)}}
.hero{{background:linear-gradient(135deg,#1a73e8 0%,#34a853 100%);color:#fff;padding:100px 20px;text-align:center}}
.hero h1{{font-family:'Google Sans',sans-serif;font-size:clamp(2rem,5vw,3.5rem);font-weight:700;margin-bottom:16px}}
.hero p{{font-size:1.2rem;opacity:.9;max-width:560px;margin:0 auto 32px}}
.btn-hero{{display:inline-block;background:#fff;color:var(--primary);font-family:'Google Sans',sans-serif;font-weight:600;font-size:1rem;padding:14px 36px;border-radius:24px;box-shadow:0 4px 15px rgba(0,0,0,.15);transition:transform .2s,box-shadow .2s}}
.btn-hero:hover{{transform:translateY(-2px);box-shadow:0 8px 25px rgba(0,0,0,.2)}}
.nav{{display:flex;align-items:center;justify-content:space-between;padding:16px 32px;position:sticky;top:0;background:var(--surface);box-shadow:var(--shadow);z-index:100}}
.brand{{font-family:'Google Sans',sans-serif;font-size:1.3rem;font-weight:700;color:var(--primary);display:flex;align-items:center;gap:8px}}
.nav-links a{{color:var(--text-2);font-size:.9rem;margin-left:20px;transition:color .2s}}
.nav-links a:hover{{color:var(--primary)}}
.features{{padding:80px 20px;max-width:1100px;margin:0 auto}}
.features h2{{font-family:'Google Sans',sans-serif;text-align:center;font-size:2rem;margin-bottom:48px;color:var(--text)}}
.feat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}}
.feat-card{{background:var(--surface);border-radius:var(--radius-lg);padding:32px;box-shadow:var(--shadow);transition:transform .2s;border:1px solid var(--border)}}
.feat-card:hover{{transform:translateY(-4px);box-shadow:0 8px 25px rgba(0,0,0,.1)}}
.feat-icon{{font-size:2.5rem;margin-bottom:16px}}
.feat-card h3{{font-family:'Google Sans',sans-serif;font-size:1.1rem;margin-bottom:8px}}
.feat-card p{{color:var(--text-2);font-size:.9rem;line-height:1.6}}
.cta-section{{background:var(--bg);text-align:center;padding:80px 20px}}
.cta-section h2{{font-family:'Google Sans',sans-serif;font-size:1.8rem;margin-bottom:16px}}
footer{{background:#1a1a1a;color:#9aa0a6;padding:40px 20px;text-align:center;font-size:.85rem}}
footer a{{color:#e8eaed;margin:0 10px}}
</style>
</head>
<body>
<nav class="nav">
    <div class="brand">📬 Rexify Mail</div>
    <div class="nav-links">
        <a href="/mailbox" style="color:var(--primary);font-weight:500;margin-right:15px;">Login</a>
        <a href="/privacy">Privacy</a>
        <a href="/terms">Terms</a>
        <a href="{bot_link}" style="color:var(--primary);font-weight:500">Open Bot ↗</a>
    </div>
</nav>
<div class="hero">
    <div>📬</div>
    <h1>Gmail & Outlook in Telegram</h1>
    <p>Rexify connects your email accounts to Telegram so you can read, reply, and compose emails without leaving the app.</p>
    <a href="{bot_link}" class="btn-hero">🚀 Launch @{bot_user.username}</a>
</div>
<section class="features">
    <h2>Everything you need</h2>
    <div class="feat-grid">
        <div class="feat-card"><div class="feat-icon">📬</div><h3>Smart Notifications</h3><p>Get instant Telegram alerts the moment a new email arrives.</p></div>
        <div class="feat-card"><div class="feat-icon">🔐</div><h3>Secure OAuth 2.0</h3><p>Connect securely via official APIs.</p></div>
        <div class="feat-card"><div class="feat-icon">👥</div><h3>Multiple Accounts</h3><p>Manage all your addresses from a single bot.</p></div>
    </div>
</section>
<section class="cta-section">
    <h2>Ready to simplify your email?</h2>
    <a href="{bot_link}" class="btn-hero" style="box-shadow:0 4px 15px rgba(26,115,232,.3)">Get Started</a>
</section>
<footer>
    <p>© 2026 Rexify. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>
</footer>
</body></html>"""
    return web.Response(text=html, content_type='text/html')

async def privacy_policy_handler(request):
    html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Privacy Policy — Rexify</title><style>""" + BASE_CSS + """</style></head><body style="padding:40px;text-align:center"><h2>Privacy Policy</h2><p>We respect your privacy.</p></body></html>"""
    return web.Response(text=html, content_type='text/html')

async def terms_handler(request):
    html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Terms — Rexify</title><style>""" + BASE_CSS + """</style></head><body style="padding:40px;text-align:center"><h2>Terms of Service</h2><p>By using this bot you agree to our terms.</p></body></html>"""
    return web.Response(text=html, content_type='text/html')

# ── OAUTH CALLBACKS ───────────────────────────────────────────────────────────

async def oauth_callback_handler(request):
    try:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state: return web.Response(text="Missing data", status=400)

        state_data = oauth_states.get(state)
        if not state_data: return web.Response(text="Invalid session", status=400)

        user_id = state_data["user_id"]
        telegram_id = state_data.get("telegram_id", user_id)
        flow = state_data["flow"]
        flow.fetch_token(code=code)
        credentials = flow.credentials

        tokens_data = {"access_token": credentials.token, "refresh_token": credentials.refresh_token, "expires_at": credentials.expiry.timestamp()}
        email = await get_user_email(credentials.token)

        account_id, is_new = await db.add_account(user_id, email, tokens_data, provider="gmail")
        oauth_states.pop(state, None)

        try:
            await bot.send_message(telegram_id, f"{'✅ Connected:' if is_new else '🔄 Tokens refreshed:'} {email}")
        except: pass

        return web.Response(text=f"<html><head><style>{BASE_CSS}</style></head><body style='text-align:center;padding-top:80px'><h2>Google Account connected!</h2><p>Return to Telegram.</p></body></html>", content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {str(e)}", status=500)

async def ms_callback_handler(request):
    try:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state: return web.Response(text="Missing Microsoft auth data", status=400)

        state_data = oauth_states.get(state)
        if not state_data: return web.Response(text="Invalid session or state expired", status=400)

        user_id = state_data["user_id"]
        telegram_id = state_data.get("telegram_id")

        from config import MS_CLIENT_ID, MS_CLIENT_SECRET, MS_REDIRECT_URI, MS_SCOPES
        import msal
        
        msal_app = msal.ConfidentialClientApplication(
            MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET,
            authority="https://login.microsoftonline.com/common"
        )
        result = msal_app.acquire_token_by_authorization_code(code, scopes=MS_SCOPES, redirect_uri=MS_REDIRECT_URI)

        if "error" in result:
            return web.Response(text=f"Auth Error: {result.get('error_description')}", status=500)

        email = result.get("id_token_claims", {}).get("preferred_username")
        tokens_data = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token"),
            "expires_at": time.time() + result.get("expires_in", 3600)
        }

        account_id, is_new = await db.add_account(user_id, email, tokens_data, provider="microsoft")
        oauth_states.pop(state, None)

        try:
            await bot.send_message(telegram_id, f"{'✅ Connected' if is_new else '🔄 Refreshed'} Microsoft: {email}")
        except: pass

        return web.Response(text=f"<html><head><style>{BASE_CSS}</style></head><body style='text-align:center;padding-top:80px'><h2>Microsoft Account connected!</h2><p>Return to Telegram.</p></body></html>", content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {str(e)}", status=500)

# ── EMAIL WEB VIEWER ──────────────────────────────────────────────────────────

async def get_email_handler(request):
    try:
        cb_hash = request.match_info.get('hash')
        callback_data = await db.get_email_callback(cb_hash)
        if not callback_data: return web.Response(text="Not found", status=404)

        account = await db.get_account(callback_data['account_id'])
        provider = account.get('provider', 'gmail')

        html_body, plain_body, attachments = None, None, []
        subject, sender_name, sender_email, to_addr, date_str = "", "", "", "", ""

        if provider == 'gmail':
            service = get_gmail_service(account['access_token'], account.get('refresh_token'))
            msg = service.users().messages().get(userId='me', id=callback_data['message_id']).execute()
            headers = msg.get('payload', {}).get('headers', [])

            subject = get_header(headers, 'Subject') or '(No subject)'
            sender_full = get_header(headers, 'From')
            sender_name, sender_email = extract_sender_name(sender_full)
            to_addr = get_header(headers, 'To')
            date_str = get_header(headers, 'Date')

            html_body, plain_body, attachments = get_email_content(msg.get('payload', {}))
            
        else: # Microsoft
            msg = await call_ms_graph(f"me/messages/{callback_data['message_id']}", account['access_token'])
            subject = msg.get('subject', '(No subject)')
            sender_name = msg.get('from', {}).get('emailAddress', {}).get('name', '')
            sender_email = msg.get('from', {}).get('emailAddress', {}).get('address', '')
            to_addr = ", ".join([r.get('emailAddress', {}).get('address', '') for r in msg.get('toRecipients', [])])
            
            dt_str = msg.get('receivedDateTime', '')
            if dt_str:
                try:
                    dt_obj = datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
                    date_str = dt_obj.strftime("%a, %d %b %Y %H:%M:%S")
                except:
                    date_str = dt_str

            body_content = msg.get('body', {}).get('content', '')
            if msg.get('body', {}).get('contentType', '').lower() == 'html':
                html_body = body_content
            else:
                plain_body = body_content
                
            if msg.get('hasAttachments'):
                atts_data = await call_ms_graph(f"me/messages/{callback_data['message_id']}/attachments?$select=name,contentType,size", account['access_token'])
                for a in atts_data.get('value', []):
                    attachments.append({'name': a.get('name', 'Attachment'), 'type': a.get('contentType', ''), 'size': a.get('size', 0)})

        iframe_content = html_body if html_body else f"<pre style='white-space:pre-wrap;padding:20px'>{html_module.escape(plain_body or '')}</pre>"
        iframe_json = json.dumps(f"<base target='_blank'><style>body{{font-family:Roboto,sans-serif;padding:16px;line-height:1.6;color:#202124;background:#ffffff;}}img{{max-width:100%}}a{{color:#1a73e8}}</style>{iframe_content}")
        
        bg = avatar_color(sender_name or sender_email)
        initial = (sender_name[0] if sender_name else (sender_email[0] if sender_email else '?')).upper()

        page_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_module.escape(subject)}</title>
<style>
{BASE_CSS}
.topbar{{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:64px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}}
.brand{{font-family:'Google Sans',sans-serif;font-size:1.2rem;font-weight:700;color:var(--primary);display:flex;align-items:center;gap:8px}}
.back-btn{{color:var(--text-2);font-size:.9rem;padding:8px 16px;border-radius:20px;background:var(--bg);border:1px solid var(--border);transition:background .15s}}
.back-btn:hover{{background:var(--border)}}
.email-wrap{{max-width:860px;margin:32px auto;padding:0 16px 60px}}
.email-card{{background:var(--surface);border-radius:var(--radius-lg);box-shadow:var(--shadow);overflow:hidden;border:1px solid var(--border)}}
.email-header{{padding:28px 32px 20px;border-bottom:1px solid var(--border)}}
.subject-line{{font-size:1.5rem;font-weight:500;color:var(--text);margin-bottom:20px;font-family:'Google Sans',sans-serif;line-height:1.3}}
.sender-row{{display:flex;align-items:flex-start;gap:14px}}
.avatar{{width:42px;height:42px;border-radius:50%;background:{bg};color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:1.1rem;flex-shrink:0}}
.meta-name{{font-weight:500;color:var(--text)}}
.meta-addr{{color:var(--text-2);font-size:.82rem;margin-top:2px}}
.meta-date{{margin-left:auto;color:var(--text-3);font-size:.82rem;white-space:nowrap;padding-top:2px}}
.email-body-frame{{width:100%;min-height:400px;border:none;display:block;background:#fff}}
.att-bar{{padding:16px 32px;border-top:1px solid var(--border);background:var(--bg)}}
.att-label{{font-size:.82rem;font-weight:500;color:var(--text-2);margin-bottom:10px}}
.att-chips{{display:flex;flex-wrap:wrap;gap:8px}}
.att-chip{{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:.82rem}}
.att-icon{{font-size:1.2rem}}
.att-name{{font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.att-size{{color:var(--text-3);font-size:.75rem;margin-top:1px}}
</style></head><body>
<div class="topbar">
    <a href="/mailbox" class="brand">📬 Rexify Mail</a>
    <a href="javascript:history.back()" class="back-btn">← Back to Inbox</a>
</div>
<div class="email-wrap"><div class="email-card">
<div class="email-header">
    <div class="subject-line">{html_module.escape(subject)}</div>
    <div class="sender-row">
        <div class="avatar">{initial}</div>
        <div>
            <div class="meta-name">{html_module.escape(sender_name or sender_email)}</div>
            <div class="meta-addr">
                <span style="color:var(--text-3)">from</span> {html_module.escape(sender_email)}
                &nbsp;·&nbsp; <span style="color:var(--text-3)">to</span> {html_module.escape(to_addr)}
            </div>
        </div>
        <div class="meta-date">{html_module.escape(date_str)}</div>
    </div>
</div>
<iframe class="email-body-frame" id="ef" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe>
{render_attachments(attachments)}
</div></div>
<script>
var f=document.getElementById('ef');
f.srcdoc={iframe_json};
f.onload=function(){{
    try{{f.style.height=Math.max(f.contentDocument.body.scrollHeight+40,200)+'px';}}catch(e){{f.style.height='600px';}}
}};
</script></body></html>"""
        return web.Response(text=page_html, content_type='text/html')
    except Exception as e:
        logger.error(f"Email view error: {e}")
        return web.Response(text=f"Error: {e}", status=500)

# ── LOGIN PAGE ────────────────────────────────────────────────────────────────

def serve_login_page(error=False):
    err_msg = "<p style='color:var(--red);margin-bottom:16px;font-size:0.9rem'>Invalid username or password.</p>" if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mailbox Login — Rexify</title>
<style>
{BASE_CSS}
body{{display:flex;align-items:center;justify-content:center;height:100vh;background:var(--bg)}}
.login-card{{background:var(--surface);padding:40px;border-radius:16px;box-shadow:var(--shadow);text-align:center;width:90%;max-width:360px;border:1px solid var(--border)}}
input[type="text"], input[type="password"]{{width:100%;padding:14px;margin-bottom:16px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);outline:none;font-size:1rem;transition:border-color .2s}}
input[type="text"]:focus, input[type="password"]:focus{{border-color:var(--primary)}}
button{{width:100%;padding:14px;background:var(--primary);color:#fff;border-radius:8px;font-weight:600;font-size:1rem;transition:opacity .2s;border:none;cursor:pointer}}
button:hover{{opacity:.9}}
</style>
</head>
<body>
<div class="login-card">
    <div style="font-size:3.5rem;margin-bottom:16px">🔐</div>
    <h2 style="margin-bottom:8px;font-family:'Google Sans',sans-serif">Rexify Mailbox</h2>
    <p style="color:var(--text-2);margin-bottom:24px;font-size:.9rem">Sign in with your bot credentials</p>
    {err_msg}
    <form method="POST">
        <input type="text" name="username" placeholder="Username" required autofocus>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Sign In</button>
    </form>
    <div style="margin-top:20px">
        <a href="/" style="color:var(--primary);font-size:.9rem;font-weight:500">← Back to Home</a>
    </div>
</div>
</body></html>"""
    return web.Response(text=html, content_type='text/html')

# ── MAILBOX ───────────────────────────────────────────────────────────────────

async def mailbox_handler(request):
    if request.query.get('logout') == '1':
        resp = web.HTTPFound('/mailbox')
        resp.del_cookie('rexify_uid')
        return resp

    if request.method == 'POST':
        data = await request.post()
        username = data.get('username', '').strip()
        password = data.get('password', '')
        hashed_pwd = hashlib.sha256(password.encode()).hexdigest()
        user = await db.auth_users.find_one({"username": username, "password": hashed_pwd})
        if user:
            resp = web.HTTPFound('/mailbox')
            resp.set_cookie('rexify_uid', str(user['internal_user_id']), max_age=86400*7)
            return resp
        else:
            return serve_login_page(error=True)

    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str: return serve_login_page()
    try: internal_user_id = int(user_id_str)
    except ValueError: return serve_login_page()

    try: page = max(1, int(request.query.get('page', '1')))
    except ValueError: page = 1
    
    view_mode = request.query.get('view', 'all')
    selected_account_id = request.query.get('account', None)

    accounts = await db.accounts.find({"user_id": internal_user_id}).to_list(None)
    active_accounts = [a for a in accounts if a.get('token_valid', True)]
    
    all_emails = []
    total_count, total_pages = 0, 1
    current_account = None
    fetch_error = None

    if view_mode == 'account' and selected_account_id:
        current_account = await db.accounts.find_one({"_id": ObjectId(selected_account_id), "user_id": internal_user_id})
        
        if current_account and current_account.get('token_valid', True):
            provider = current_account.get('provider', 'gmail')
            try:
                if provider == 'gmail':
                    service = get_gmail_service(current_account['access_token'], current_account.get('refresh_token'))
                    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
                    query = f'after:{int(cutoff_time.timestamp())}'
                    results = service.users().messages().list(userId='me', q=query, maxResults=100).execute()
                    messages = results.get('messages', [])
                    
                    for msg in messages:
                        msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()
                        headers = msg_detail.get('payload', {}).get('headers', [])
                        from_addr = get_header(headers, 'From')
                        subject = get_header(headers, 'Subject')
                        timestamp = int(msg_detail.get('internalDate', 0))
                        snippet = msg_detail.get('snippet', '')
                        
                        sender_name, sender_email_addr = extract_sender_name(from_addr)
                        ts = datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else datetime.now(timezone.utc)
                        
                        cb_hash = await db.store_email_callback(internal_user_id, str(current_account['_id']), msg['id'], msg.get('threadId'))
                        
                        all_emails.append({
                            "acc": current_account['email'], "sub": subject or "(No subject)",
                            "from": sender_name or sender_email_addr or 'Unknown',
                            "from_email": sender_email_addr,
                            "initial": (sender_name or sender_email_addr or '?')[0].upper(),
                            "color": avatar_color(sender_name or sender_email_addr),
                            "time": get_time_ago(ts), "link": f"/view/{cb_hash}", "ts": ts,
                            "unread": 'UNREAD' in msg_detail.get('labelIds', []), "snippet": snippet[:150],
                        })
                else: # Microsoft Live Fetch for Mailbox
                    data = await call_ms_graph("me/mailFolders/inbox/messages?$top=100&$select=id,conversationId,subject,from,bodyPreview,receivedDateTime,isRead", current_account['access_token'])
                    messages = data.get('value', [])
                    
                    for msg in messages:
                        subject = msg.get('subject', '(No subject)')
                        sender_name = msg.get('from', {}).get('emailAddress', {}).get('name', '')
                        sender_email_addr = msg.get('from', {}).get('emailAddress', {}).get('address', '')
                        display_name = sender_name or sender_email_addr or 'Unknown'
                        
                        dt_str = msg.get('receivedDateTime', '')
                        if dt_str:
                            try: ts = datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                            except: ts = datetime.now(timezone.utc)
                        else: ts = datetime.now(timezone.utc)
                            
                        cb_hash = await db.store_email_callback(internal_user_id, str(current_account['_id']), msg['id'], msg.get('conversationId'))
                        
                        all_emails.append({
                            "acc": current_account['email'], "sub": subject,
                            "from": display_name, "from_email": sender_email_addr,
                            "initial": display_name[0].upper(), "color": avatar_color(display_name),
                            "time": get_time_ago(ts), "link": f"/view/{cb_hash}", "ts": ts,
                            "unread": not msg.get('isRead', True), "snippet": msg.get('bodyPreview', '')[:150],
                        })
                        
                all_emails.sort(key=lambda x: x["ts"], reverse=True)
                total_count = len(all_emails)
                skip = (page - 1) * 25
                all_emails = all_emails[skip:skip+25]
                total_pages = (total_count + 24) // 25
                
            except Exception as e:
                fetch_error = f"Failed to fetch emails: {str(e)}"
                logger.error(f"Fetch error: {e}")
        else:
            fetch_error = "Account token is invalid or expired"
    
    elif view_mode != 'accounts':
        cached, total_count, total_pages = await db.get_mailbox_emails_24h(internal_user_id, page=page, per_page=25)
        for e in cached:
            cb_hash = await db.store_email_callback(e['user_id'], e['account_id'], e['message_id'], e.get('thread_id'))
            ts = datetime.fromtimestamp(int(e['internal_date']) / 1000, timezone.utc) if e.get('internal_date') else e.get('notified_at', datetime.now(timezone.utc))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)

            sender_name, sender_email_addr = extract_sender_name(e.get('from_addr', ''))
            display_name = sender_name or sender_email_addr or 'Unknown'

            all_emails.append({
                "acc": e.get('account_email', ''), "sub": e.get('subject') or "(No subject)",
                "from": display_name, "from_email": sender_email_addr,
                "initial": display_name[0].upper() if display_name else '?', "color": avatar_color(display_name),
                "time": get_time_ago(ts), "link": f"/view/{cb_hash}", "ts": ts,
                "unread": e.get('unread', False), "snippet": (e.get('snippet') or '')[:150],
            })

    def email_row(e):
        unread_class = 'unread' if e['unread'] else ''
        return f"""<a href='{e['link']}' class="email-card {unread_class}">
            <div class="email-avatar" style="background:{e['color']}">{e['initial']}</div>
            <div class="email-content">
                <div class="email-header"><span class="email-from">{html_module.escape(e['from'])}</span><span class="email-meta">{e['time']} • {html_module.escape(e['acc'])}</span></div>
                <div class="email-subject">{html_module.escape(e['sub'])}</div>
                <div class="email-snippet">{html_module.escape(e['snippet'])}</div>
            </div></a>"""

    rows_html = "\n".join(email_row(e) for e in all_emails)

    pagination_html = ""
    if total_pages > 1:
        pagination_html = f'<div class="pagination">{"<a href=\'/mailbox?page=" + str(page - 1) + "\' class=\'pagination-btn\'>← Previous</a>" if page > 1 else ""}<span class="pagination-info">Page {page} of {total_pages}</span>{"<a href=\'/mailbox?page=" + str(page + 1) + "\' class=\'pagination-btn\'>Next →</a>" if page < total_pages else ""}</div>'

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rexify Mail — Inbox</title>
<style>
{BASE_CSS}
.layout{{display:flex;height:100vh;overflow:hidden}}
.sidebar{{width:240px;background:var(--sidebar-bg);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto;transition:transform .25s}}
.main{{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}}
.sidebar-brand{{display:flex;align-items:center;gap:10px;padding:18px 16px;font-family:'Google Sans',sans-serif;font-size:1.15rem;font-weight:700;color:var(--primary)}}
.sidebar-nav{{padding:8px 0}}
.nav-item{{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:0 24px 24px 0;margin-right:16px;font-size:.9rem;color:var(--text);cursor:pointer;transition:background .15s;text-decoration:none}}
.nav-item:hover{{background:rgba(0,0,0,.05)}}
.nav-item.active{{background:var(--primary-light);color:var(--primary);font-weight:600}}
.badge-count{{margin-left:auto;background:var(--primary);color:#fff;font-size:.7rem;font-weight:700;padding:3px 8px;border-radius:12px;min-width:22px;text-align:center}}
.sidebar-footer{{margin-top:auto;padding:12px 8px;border-top:1px solid var(--border)}}
.sidebar-logout{{display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:0 24px 24px 0;margin-right:16px;font-size:.88rem;color:var(--red);cursor:pointer;text-decoration:none}}
.topbar{{display:flex;align-items:center;gap:12px;padding:12px 20px;height:64px;background:var(--surface);border-bottom:1px solid var(--border)}}
.hamburger{{display:none;font-size:1.4rem;color:var(--text);padding:4px 8px;border-radius:50%;cursor:pointer;background:none;border:none}}
.search-wrap{{flex:1;max-width:500px}}
.search-bar{{display:flex;align-items:center;gap:10px;background:var(--bg);border-radius:24px;padding:9px 16px;border:1px solid transparent;transition:border-color .2s}}
.search-bar:focus-within{{border-color:var(--primary);background:var(--surface)}}
.search-bar input{{border:none;background:none;outline:none;width:100%;font-size:.95rem;color:var(--text)}}
.topbar-right{{display:flex;align-items:center;gap:8px;margin-left:auto}}
.inbox-toolbar{{display:flex;align-items:center;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--surface);min-height:48px}}
.emails-container{{flex:1;overflow-y:auto;background:var(--surface);padding:8px 0}}
.email-card{{display:flex;align-items:flex-start;gap:14px;padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;text-decoration:none;color:inherit;background:var(--surface)}}
.email-card:hover{{background:var(--bg)}}
.email-card.unread{{background:var(--primary-light)}}
.email-avatar{{width:42px;height:42px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:1rem;flex-shrink:0}}
.email-content{{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}}
.email-header{{display:flex;align-items:center;gap:8px;justify-content:space-between}}
.email-from{{font-weight:600;color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.email-meta{{font-size:.82rem;color:var(--text-2);white-space:nowrap}}
.email-subject{{font-weight:500;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.95rem}}
.email-snippet{{font-size:.85rem;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.pagination{{display:flex;justify-content:center;gap:12px;padding:20px;border-top:1px solid var(--border);background:var(--surface)}}
.pagination-btn{{padding:8px 14px;border-radius:6px;background:var(--primary);color:#fff;text-decoration:none;font-size:.85rem;font-weight:500}}
.empty-state{{text-align:center;padding:80px 20px;color:var(--text-2);font-size:1.1rem}}
.accounts-container{{flex:1;overflow-y:auto;background:var(--surface);padding:20px}}
.account-item{{display:flex;align-items:center;gap:16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px;text-decoration:none;color:inherit}}
.account-item:hover{{background:var(--primary-light);border-color:var(--primary)}}
.error-bar{{background:#fce8e6;color:#d9302d;padding:12px 16px;border-bottom:1px solid var(--border);font-size:.9rem}}
.sidebar-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:998}}
@media (max-width:768px){{
    .sidebar{{position:fixed;left:0;top:0;height:100%;z-index:999;transform:translateX(-100%)}}
    .sidebar.open{{transform:translateX(0)}}
    .sidebar-overlay.show{{display:block}}
    .hamburger{{display:flex}}
}}
</style>
</head>
<body>
<div class="layout">
<div class="sidebar-overlay" id="overlay" onclick="closeSidebar()"></div>

<div class="sidebar" id="sidebar">
    <div class="sidebar-brand">📬 Rexify Mail</div>
    <nav class="sidebar-nav">
        <a href="/mailbox" class="nav-item {'active' if view_mode != 'accounts' else ''}">
            <span class="nav-icon">📬</span> All Mails
        </a>
        <a href="/mailbox?view=accounts" class="nav-item {'active' if view_mode == 'accounts' else ''}">
            <span class="nav-icon">👤</span> Accounts
            <span class="badge-count">{len(active_accounts)}</span>
        </a>
    </nav>
    <div class="sidebar-footer">
        <a href="/mailbox?logout=1" class="sidebar-logout">🚪 Sign Out</a>
    </div>
</div>

<div class="main">
    <div class="topbar">
        <button class="hamburger" onclick="toggleSidebar()">☰</button>
        <div style="font-weight:600;margin-right:auto">{'👤 Accounts' if view_mode == 'accounts' else '📬 ' + (current_account['email'] if current_account else 'All Mails')}</div>
        <div class="search-wrap">
            <div class="search-bar">
                <span>🔍</span><input type="text" placeholder="Search emails" id="searchInput" oninput="filterEmails()">
            </div>
        </div>
        <div class="topbar-right">
            <button onclick="location.reload()" style="font-size:1.2rem;background:none;border:none">🔄</button>
        </div>
    </div>

    {'<div class="accounts-container">' if view_mode == 'accounts' else '<div style="display:none">'}
        <div style="color:var(--text-2);margin-bottom:20px">Click an account to view its live emails</div>
        {''.join(f'''<a href="/mailbox?view=account&account={str(acc['_id'])}" class="account-item">
            <div class="email-avatar" style="background:{avatar_color(acc['email'])}">{acc['email'][0].upper()}</div>
            <div style="flex:1"><div style="font-weight:500">{html_module.escape(acc['email'])}</div><div style="font-size:.8rem;color:var(--text-2)">{acc.get('provider', 'gmail').capitalize()}</div></div>
        </a>''' for acc in active_accounts) if active_accounts else '<div class="empty-state">No accounts connected</div>'}
    </div>

    {'<div style="display:none">' if view_mode == 'accounts' else '<div'} style="display:flex;flex-direction:column;flex:1;overflow:hidden">
        {f'<div class="error-bar">{html_module.escape(fetch_error)}</div>' if fetch_error else ''}
        <div class="inbox-toolbar">
            <b>{len(all_emails)}</b> messages
        </div>
        <div class="emails-container" id="emailTable">
            {rows_html if all_emails else '<div class="empty-state">No emails found</div>'}
        </div>
        {pagination_html}
    </div>
</div></div>
<script>
function toggleSidebar() {{ document.getElementById('sidebar').classList.toggle('open'); document.getElementById('overlay').classList.toggle('show'); }}
function closeSidebar() {{ document.getElementById('sidebar').classList.remove('open'); document.getElementById('overlay').classList.remove('show'); }}
function filterEmails() {{
    var q = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.email-card').forEach(function(card) {{
        card.style.display = card.textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
}}
</script>
</body></html>"""
    return web.Response(text=page_html, content_type="text/html")

# ── APP FACTORY ───────────────────────────────────────────────────────────────

def create_web_app():
    app = web.Application()
    app.router.add_get('/', main_page_handler)
    app.router.add_get('/oauth_callback', oauth_callback_handler)
    app.router.add_get('/ms_callback', ms_callback_handler)
    app.router.add_get('/privacy', privacy_policy_handler)
    app.router.add_get('/terms', terms_handler)
    app.router.add_get('/view/{hash}', get_email_handler)
    app.router.add_get('/mailbox', mailbox_handler)
    app.router.add_post('/mailbox', mailbox_handler)
    return app
