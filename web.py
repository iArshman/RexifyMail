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

# Cached bot info to avoid repeated Telegram API calls on every page load
_bot_info_cache = None
_ms_session = None  # Shared aiohttp session for Microsoft Graph calls

def setup_web_module(bot_instance, db_instance, oauth_states_dict, client_id, client_secret, redirect_uri):
    global bot, db, oauth_states, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    bot = bot_instance
    db = db_instance
    oauth_states = oauth_states_dict
    CLIENT_ID = client_id
    CLIENT_SECRET = client_secret
    REDIRECT_URI = redirect_uri

async def get_bot_info():
    """Return cached bot info — only calls Telegram API once per process lifetime."""
    global _bot_info_cache
    if _bot_info_cache is None:
        _bot_info_cache = await bot.get_me()
    return _bot_info_cache

async def get_ms_session() -> ClientSession:
    """Return a shared persistent aiohttp session for MS Graph calls."""
    global _ms_session
    if _ms_session is None or _ms_session.closed:
        _ms_session = ClientSession()
    return _ms_session

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_gmail_service(access_token, refresh_token=None):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    # NOTE: do NOT pass `scopes` here. When set, google-auth sends the scope list
    # to Google's refresh endpoint, which rejects with `invalid_scope` if the user
    # originally consented to a different set of scopes. Omitting it makes Google
    # refresh with the originally-granted scopes.
    creds = Credentials.from_authorized_user_info({
        'token': access_token,
        'refresh_token': refresh_token,
        'token_uri': 'https://oauth2.googleapis.com/token',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
    })
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)

async def call_ms_graph(endpoint: str, token: str, method: str = "GET", json_data: dict = None):
    """Helper to call Microsoft Graph API in Web Module — uses a shared persistent session."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://graph.microsoft.com/v1.0/{endpoint}"
    session = await get_ms_session()
    async with session.request(method, url, headers=headers, json=json_data) as resp:
        if resp.status in [200, 201]:
            return await resp.json()
        if resp.status in [202, 204]:
            # 202 Accepted (e.g., sendMail) and 204 No Content return no body
            return True
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
            body = part.get('body', {})
            data = body.get('data', '')
            attachment_id = body.get('attachmentId', '')
            if fn:
                attachments.append({
                    'name': fn,
                    'type': mime,
                    'size': body.get('size', 0),
                    'attachment_id': attachment_id  # Gmail attachment ID for download
                })
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

def render_attachments(attachments, cb_hash=""):
    if not attachments: return ""
    chips = ""
    for i, att in enumerate(attachments):
        sz = att.get('size', 0)
        sz_str = f"{sz/1024:.1f} KB" if sz < 1024*1024 else f"{sz/(1024*1024):.1f} MB"
        mime = att.get('type', '')
        if 'image' in mime:
            icon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>'
        elif 'pdf' in mime:
            icon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>'
        elif 'sheet' in mime or 'excel' in mime or 'spreadsheet' in mime:
            icon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/><line x1="8" y1="9" x2="10" y2="9"/></svg>'
        else:
            icon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'

        # Build download URL using attachment_id (Gmail) or index (Microsoft)
        att_id = att.get('attachment_id') or f"ms_{i}"
        download_url = f"/api/attachment/{cb_hash}/{att_id}" if cb_hash else "#"

        chips += (
            f'<a href="{download_url}" download="{html_module.escape(att["name"])}" class="att-chip" title="Download {html_module.escape(att["name"])}">'
            f'<span class="att-icon">{icon}</span>'
            f'<div><div class="att-name">{html_module.escape(att["name"])}</div><div class="att-size">{sz_str}</div></div>'
            f'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-left:4px;flex-shrink:0;opacity:.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
            f'</a>'
        )
    return f'<div class="att-bar"><div class="att-label">Attachments ({len(attachments)})</div><div class="att-chips">{chips}</div></div>'

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
    bot_user = await get_bot_info()
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
    <p>© 2026 Rexify.</p>
</footer>
</body></html>"""
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
        from_web = state_data.get("from_web", False)
        oauth_states.pop(state, None)

        if from_web:
            # Redirect back to mailbox for web-initiated OAuth
            return web.HTTPFound('/mailbox')
        
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
        from_web = state_data.get("from_web", False)
        oauth_states.pop(state, None)

        if from_web:
            # Redirect back to mailbox for web-initiated OAuth
            return web.HTTPFound('/mailbox')

        try:
            await bot.send_message(telegram_id, f"{'✅ Connected' if is_new else '🔄 Refreshed'} Microsoft: {email}")
        except: pass

        return web.Response(text=f"<html><head><style>{BASE_CSS}</style></head><body style='text-align:center;padding-top:80px'><h2>Microsoft Account connected!</h2><p>Return to Telegram.</p></body></html>", content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {str(e)}", status=500)

# ── EMAIL WEB VIEWER ─────────────────����────────────────────────────────────────

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
                atts_data = await call_ms_graph(f"me/messages/{callback_data['message_id']}/attachments?$select=id,name,contentType,size", account['access_token'])
                for a in atts_data.get('value', []):
                    attachments.append({
                        'name': a.get('name', 'Attachment'),
                        'type': a.get('contentType', ''),
                        'size': a.get('size', 0),
                        'attachment_id': a.get('id', '')  # MS attachment ID for download
                    })

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
.att-chip{{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:.82rem;text-decoration:none;color:var(--text);transition:background .15s,border-color .15s}}
.att-chip:hover{{background:var(--primary-light);border-color:var(--primary)}}
.att-icon{{font-size:1.2rem}}
.att-name{{font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.att-size{{color:var(--text-3);font-size:.75rem;margin-top:1px}}
.reply-bar{{padding:24px 32px;border-top:2px solid var(--border);background:var(--surface)}}
.reply-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}}
.reply-title{{font-size:.95rem;font-weight:600;color:var(--text)}}
.reply-to-chip{{font-size:.8rem;color:var(--text-2);background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:3px 10px}}
.reply-textarea{{width:100%;min-height:120px;resize:vertical;padding:12px 14px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:10px;font-size:.9rem;font-family:inherit;line-height:1.5;outline:none;transition:border-color .2s;box-sizing:border-box}}
.reply-textarea:focus{{border-color:var(--primary)}}
.reply-actions{{display:flex;align-items:center;gap:10px;margin-top:12px}}
.reply-send-btn{{display:inline-flex;align-items:center;gap:7px;padding:9px 20px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;font-family:inherit;transition:background .15s}}
.reply-send-btn:hover{{background:var(--primary-hover)}}
.reply-send-btn:disabled{{opacity:.6;cursor:not-allowed}}
.reply-status{{font-size:.85rem;color:var(--text-2)}}
.reply-status.ok{{color:#22c55e}}
.reply-status.err{{color:#ef4444}}
</style></head><body>
<div class="topbar">
    <a href="/mailbox" class="brand">Rexify Mail</a>
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
{render_attachments(attachments, cb_hash)}
<div class="reply-bar">
    <div class="reply-header">
        <span class="reply-title">Reply</span>
        <span class="reply-to-chip">To: {html_module.escape(sender_email)}</span>
    </div>
    <textarea class="reply-textarea" id="replyBody" placeholder="Write your reply..."></textarea>
    <div class="reply-actions">
        <button class="reply-send-btn" id="replyBtn" onclick="sendReply()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            Send Reply
        </button>
        <span class="reply-status" id="replyStatus"></span>
    </div>
</div>
</div></div>
<script>
var f=document.getElementById('ef');
f.srcdoc={iframe_json};
f.onload=function(){{
    try{{f.style.height=Math.max(f.contentDocument.body.scrollHeight+40,200)+'px';}}catch(e){{f.style.height='600px';}}
}};

function sendReply() {{
    var body = document.getElementById('replyBody').value.trim();
    if (!body) {{ document.getElementById('replyStatus').textContent = 'Please enter a reply.'; document.getElementById('replyStatus').className = 'reply-status err'; return; }}
    var btn = document.getElementById('replyBtn');
    var status = document.getElementById('replyStatus');
    btn.disabled = true;
    btn.textContent = 'Sending...';
    status.textContent = '';
    status.className = 'reply-status';
    fetch('/api/reply/{cb_hash}', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{body: body}})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
        if (d.success) {{
            status.textContent = 'Reply sent!';
            status.className = 'reply-status ok';
            document.getElementById('replyBody').value = '';
            btn.disabled = false;
            btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Reply';
        }} else {{
            status.textContent = d.error || 'Failed to send.';
            status.className = 'reply-status err';
            btn.disabled = false;
            btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Reply';
        }}
    }})
    .catch(function() {{
        status.textContent = 'Network error.';
        status.className = 'reply-status err';
        btn.disabled = false;
        btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Reply';
    }});
}}
</script></body></html>"""
        return web.Response(text=page_html, content_type='text/html')
    except Exception as e:
        logger.error(f"Email view error: {e}")
        return web.Response(text=f"Error: {e}", status=500)

# ── LOGIN PAGE ────────────────────────────────────────────────────────────────

def serve_login_page(error=False):
    err_msg = "<div class='err-msg'>Invalid username or password.</div>" if error else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0b">
<title>Sign in — RexifyMail</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
    --bg:#0a0a0b;--surface:#131316;--surface-2:#1a1a1d;
    --border:#26262a;--border-strong:#3a3a40;
    --primary:#2563eb;--primary-hover:#1d4ed8;
    --text:#f4f4f5;--text-2:#a1a1aa;--text-3:#71717a;--red:#ef4444;
}}
html,body{{height:100%;background:var(--bg);color:var(--text);font-family:'Inter',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}}
body{{display:flex;align-items:center;justify-content:center;padding:20px}}
a{{color:inherit;text-decoration:none}}
.login-card{{background:var(--surface);padding:36px;border-radius:18px;width:100%;max-width:380px;border:1px solid var(--border)}}
.brand-row{{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:8px}}
.brand-logo{{width:36px;height:36px;border-radius:10px;background:var(--primary);display:flex;align-items:center;justify-content:center;color:#fff}}
.brand-name{{font-size:1.1rem;font-weight:700;color:var(--text)}}
.login-title{{font-size:1.5rem;font-weight:700;text-align:center;margin-top:18px}}
.login-sub{{color:var(--text-2);text-align:center;font-size:.9rem;margin-top:6px;margin-bottom:24px}}
.field-label{{display:block;font-size:.82rem;font-weight:500;color:var(--text-2);margin-bottom:6px}}
input[type="text"], input[type="password"]{{width:100%;padding:12px 14px;margin-bottom:14px;border:1px solid var(--border);border-radius:10px;background:var(--bg);color:var(--text);outline:none;font-size:.95rem;font-family:inherit;transition:border-color .2s}}
input[type="text"]:focus, input[type="password"]:focus{{border-color:var(--primary)}}
input::placeholder{{color:var(--text-3)}}
.btn-submit{{width:100%;padding:13px;background:var(--primary);color:#fff;border-radius:10px;font-weight:600;font-size:.95rem;transition:background .15s;border:none;cursor:pointer;font-family:inherit;margin-top:6px}}
.btn-submit:hover{{background:var(--primary-hover)}}
.err-msg{{background:rgba(239,68,68,.12);color:#fca5a5;border:1px solid rgba(239,68,68,.3);padding:10px 14px;border-radius:10px;font-size:.85rem;margin-bottom:14px;text-align:center}}
.back-link{{display:block;text-align:center;margin-top:20px;color:var(--text-2);font-size:.85rem;font-weight:500;transition:color .15s}}
.back-link:hover{{color:var(--text)}}
</style>
</head>
<body>
<div class="login-card">
    <div class="brand-row">
        <div class="brand-logo">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
        </div>
        <div class="brand-name">RexifyMail</div>
    </div>
    <div class="login-title">Welcome back</div>
    <div class="login-sub">Sign in with your bot credentials</div>
    {err_msg}
    <form method="POST">
        <label class="field-label" for="username">Username</label>
        <input id="username" type="text" name="username" placeholder="your_username" required autofocus autocomplete="username">
        <label class="field-label" for="password">Password</label>
        <input id="password" type="password" name="password" placeholder="••••••••" required autocomplete="current-password">
        <button class="btn-submit" type="submit">Sign in</button>
    </form>
    <a href="/" class="back-link">← Back to home</a>
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
    folder = request.query.get('folder', 'inbox').lower()  # inbox, sent, spam, trash
    if folder not in ('inbox', 'sent', 'spam', 'trash'):
        folder = 'inbox'
    next_token_param = request.query.get('token', None)  # Gmail pageToken from URL

    # Fire DB queries and bot info fetch in parallel to avoid sequential waits
    accounts_future = db.accounts.find({"user_id": internal_user_id}).to_list(None)
    auth_user_future = db.auth_users.find_one({"internal_user_id": internal_user_id})
    bot_info_future = get_bot_info()

    accounts, auth_user, bot_user = await asyncio.gather(
        accounts_future, auth_user_future, bot_info_future,
        return_exceptions=True
    )
    if isinstance(accounts, Exception): accounts = []
    if isinstance(auth_user, Exception): auth_user = None
    if isinstance(bot_user, Exception): bot_user = None

    active_accounts = [a for a in accounts if a.get('token_valid', True)]
    display_username = (auth_user or {}).get('username', 'user')
    bot_link = f"https://t.me/{bot_user.username}" if bot_user else "#"
    
    all_emails = []
    total_count, total_pages = 0, 1
    current_account = None
    fetch_error = None
    default_account = active_accounts[0] if active_accounts else None
    next_page_token = None  # For Gmail page-token based pagination

    # Map folder name to Gmail label and MS Graph mailFolder
    GMAIL_LABEL_MAP = {'inbox': 'INBOX', 'sent': 'SENT', 'spam': 'SPAM', 'trash': 'TRASH'}
    MS_FOLDER_MAP = {'inbox': 'inbox', 'sent': 'sentitems', 'spam': 'junkemail', 'trash': 'deleteditems'}

    if view_mode == 'account' and selected_account_id:
        # Account is already loaded in the accounts list — no extra DB call needed
        current_account = next((a for a in accounts if str(a['_id']) == selected_account_id and a.get('user_id') == internal_user_id), None)
        
        if current_account and current_account.get('token_valid', True):
            provider = current_account.get('provider', 'gmail')
            try:
                if provider == 'gmail':
                    service = get_gmail_service(current_account['access_token'], current_account.get('refresh_token'))
                    label_id = GMAIL_LABEL_MAP.get(folder, 'INBOX')

                    # Run heavy sync Gmail calls inside an executor so we don't block the event loop
                    def _fetch_gmail_page():
                        list_params = {
                            'userId': 'me',
                            'maxResults': 100,
                            'labelIds': [label_id],
                        }
                        if next_token_param:
                            list_params['pageToken'] = next_token_param
                        list_resp = service.users().messages().list(**list_params).execute()
                        msgs = list_resp.get('messages', [])
                        next_tok = list_resp.get('nextPageToken')
                        total_est = list_resp.get('resultSizeEstimate', len(msgs))

                        if not msgs:
                            return [], next_tok, total_est

                        # Batch-fetch metadata for all 50 messages in a SINGLE HTTP call
                        details = {}
                        def _cb(request_id, response, exception):
                            if exception is None:
                                details[request_id] = response

                        batch = service.new_batch_http_request(callback=_cb)
                        for i, m in enumerate(msgs):
                            batch.add(
                                service.users().messages().get(
                                    userId='me', id=m['id'], format='metadata',
                                    metadataHeaders=['From', 'Subject', 'Date']
                                ),
                                request_id=str(i)
                            )
                        batch.execute()

                        ordered = []
                        for i, m in enumerate(msgs):
                            d = details.get(str(i))
                            if d:
                                ordered.append((m, d))
                        return ordered, next_tok, total_est

                    loop = asyncio.get_event_loop()
                    ordered, next_page_token, total_est = await loop.run_in_executor(None, _fetch_gmail_page)

                    # Build emails and collect callback data for batch insert
                    callbacks_to_store = []
                    account_id_str = str(current_account['_id'])
                    for msg, msg_detail in ordered:
                        headers = msg_detail.get('payload', {}).get('headers', [])
                        from_addr = get_header(headers, 'From')
                        subject = get_header(headers, 'Subject')
                        timestamp = int(msg_detail.get('internalDate', 0))
                        snippet = msg_detail.get('snippet', '')

                        sender_name, sender_email_addr = extract_sender_name(from_addr)
                        ts = datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else datetime.now(timezone.utc)

                        # Generate hash without DB write (fast)
                        cb_hash = db.generate_callback_hash(internal_user_id, account_id_str, msg['id'])
                        callbacks_to_store.append({'hash': cb_hash, 'user_id': internal_user_id, 'account_id': account_id_str, 'message_id': msg['id'], 'thread_id': msg.get('threadId')})

                        all_emails.append({
                            "acc": current_account['email'], "sub": subject or "(No subject)",
                            "from": sender_name or sender_email_addr or 'Unknown',
                            "from_email": sender_email_addr,
                            "initial": (sender_name or sender_email_addr or '?')[0].upper(),
                            "color": avatar_color(sender_name or sender_email_addr),
                            "time": get_time_ago(ts), "link": f"/view/{cb_hash}", "ts": ts,
                            "unread": 'UNREAD' in msg_detail.get('labelIds', []), "snippet": snippet[:150],
                        })

                    # Batch store callbacks in background (non-blocking for page render)
                    asyncio.create_task(db.store_email_callbacks_batch(callbacks_to_store))
                    total_count = total_est
                    total_pages = 0  # Use pageToken-based "Next" only
                else:
                    # Microsoft - fetch single page with $top=100 and $skip for pagination
                    ms_folder = MS_FOLDER_MAP.get(folder, 'inbox')
                    skip_count = (page - 1) * 100
                    query_str = (
                        f"me/mailFolders/{ms_folder}/messages"
                        f"?$top=100&$skip={skip_count}"
                        "&$select=id,conversationId,subject,from,bodyPreview,receivedDateTime,isRead"
                        "&$orderby=receivedDateTime desc"
                    )
                    data = await call_ms_graph(query_str, current_account['access_token'])
                    messages = data.get('value', [])
                    has_more = '@odata.nextLink' in data

                    # Build emails and collect callback data for batch insert
                    callbacks_to_store = []
                    account_id_str = str(current_account['_id'])
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

                        # Generate hash without DB write (fast)
                        cb_hash = db.generate_callback_hash(internal_user_id, account_id_str, msg['id'])
                        callbacks_to_store.append({'hash': cb_hash, 'user_id': internal_user_id, 'account_id': account_id_str, 'message_id': msg['id'], 'thread_id': msg.get('conversationId')})

                        all_emails.append({
                            "acc": current_account['email'], "sub": subject,
                            "from": display_name, "from_email": sender_email_addr,
                            "initial": display_name[0].upper(), "color": avatar_color(display_name),
                            "time": get_time_ago(ts), "link": f"/view/{cb_hash}", "ts": ts,
                            "unread": not msg.get('isRead', True), "snippet": msg.get('bodyPreview', '')[:150],
                        })

                    # Batch store callbacks in background (non-blocking for page render)
                    asyncio.create_task(db.store_email_callbacks_batch(callbacks_to_store))
                    # For MS, pages numbered. If has_more then there's at least one more page.
                    total_pages = (page + 1) if has_more else page

                # No further sort needed; provider already returns most-recent-first

            except Exception as e:
                fetch_error = f"Failed to fetch emails: {str(e)}"
                logger.error(f"Fetch error: {e}", exc_info=True)
        else:
            fetch_error = "Account token is invalid or expired"
    
    elif view_mode != 'accounts':
        cached, total_count, total_pages = await db.get_mailbox_emails_24h(internal_user_id, page=page, per_page=100)
        # Build emails and collect callback data for batch insert
        callbacks_to_store = []
        for e in cached:
            # Generate hash without DB write (fast)
            cb_hash = db.generate_callback_hash(e['user_id'], e['account_id'], e['message_id'])
            callbacks_to_store.append({'hash': cb_hash, 'user_id': e['user_id'], 'account_id': e['account_id'], 'message_id': e['message_id'], 'thread_id': e.get('thread_id')})
            
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
        # Batch store callbacks in background (non-blocking for page render)
        asyncio.create_task(db.store_email_callbacks_batch(callbacks_to_store))

    def email_row(e):
        unread_class = 'unread' if e['unread'] else ''
        return f"""<a href='{e['link']}' class="mail-row {unread_class}">
            <div class="mail-avatar" style="background:{e['color']}">{e['initial']}</div>
            <div class="mail-body">
                <div class="mail-line1">
                    <span class="mail-from">{html_module.escape(e['from'])}</span>
                    <span class="mail-time">{e['time']}</span>
                </div>
                <div class="mail-subject">{html_module.escape(e['sub'])}</div>
                <div class="mail-snippet">{html_module.escape(e['snippet'])}</div>
                <div class="mail-account">{html_module.escape(e['acc'])}</div>
            </div></a>"""

    rows_html = "\n".join(email_row(e) for e in all_emails)

    # Build pagination URLs. For Gmail uses pageToken; for MS/cached uses page numbers.
    def _build_url(extra_params):
        params = {}
        if view_mode == 'account' and selected_account_id:
            params['view'] = 'account'
            params['account'] = selected_account_id
            params['folder'] = folder
        params.update({k: v for k, v in extra_params.items() if v is not None})
        return '/mailbox?' + '&'.join(f"{k}={v}" for k, v in params.items())

    pagination_html = ""
    if view_mode == 'account' and current_account and current_account.get('provider', 'gmail') == 'gmail':
        # Gmail: pageToken-style. Show Next if we have nextPageToken.
        if next_page_token or next_token_param:
            next_btn = f"<a href='{_build_url({'token': next_page_token})}' class='page-btn'>Next</a>" if next_page_token else ""
            # No back-token for prev; show Refresh to start.
            prev_btn = f"<a href='{_build_url({})}' class='page-btn'>Back to start</a>" if next_token_param else ""
            pagination_html = f"<div class='pagination'>{prev_btn}{next_btn}</div>"
    elif total_pages > 1:
        prev_btn = f"<a href='{_build_url({'page': page - 1})}' class='page-btn'>Previous</a>" if page > 1 else ""
        next_btn = f"<a href='{_build_url({'page': page + 1})}' class='page-btn'>Next</a>" if page < total_pages else ""
        pagination_html = f"<div class='pagination'>{prev_btn}<span class='page-info'>Page {page}</span>{next_btn}</div>"

    # Sidebar account items
    if active_accounts:
        accounts_list_html = ""
        for acc in active_accounts:
            acc_id = str(acc['_id'])
            is_active = (view_mode == 'account' and selected_account_id == acc_id)
            active_cls = ' active' if is_active else ''
            email_initial = acc['email'][0].upper()
            accounts_list_html += f"""<a href="/mailbox?view=account&account={acc_id}" class="account-item{active_cls}">
                <div class="account-dot" style="background:{avatar_color(acc['email'])}">{email_initial}</div>
                <div class="account-info"><div class="account-email">{html_module.escape(acc['email'])}</div><div class="account-provider">{acc.get('provider', 'gmail').capitalize()}</div></div>
            </a>"""
    else:
        accounts_list_html = ""

    all_accounts_active = ' active' if (view_mode == 'all' and not selected_account_id) else ''
    inbox_active = ' active' if (view_mode == 'all' and not selected_account_id) else ''

    # Page title in main area
    if view_mode == 'account' and current_account:
        folder_label = folder.capitalize()
        page_title = f"{folder_label} &middot; <span style='font-weight:500;color:var(--text-2);font-size:1.1rem'>{html_module.escape(current_account['email'])}</span>"
    else:
        page_title = "All Inbox"

    # Empty state HTML
    if not active_accounts:
        empty_state_html = f"""
        <div class="empty-card">
            <div class="empty-icon">
                <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
            </div>
            <div class="empty-title">Connect an account to get started</div>
            <div class="empty-subtitle">Add a Gmail or Outlook account to view your emails.</div>
            <button onclick="openAddAccountModal()" class="btn-primary empty-cta">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
                Connect an account
            </button>
        </div>"""
    elif not all_emails:
        empty_state_html = f"""
        <div class="empty-card">
            <div class="empty-icon">
                <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
            </div>
            <div class="empty-title">Your inbox is empty</div>
            <div class="empty-subtitle">No emails to display from the last 24 hours.</div>
        </div>"""
    else:
        empty_state_html = ""

    user_initial = display_username[0].upper() if display_username else 'U'

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0b">
<title>RexifyMail — Inbox</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
    --bg:#0a0a0b;
    --sidebar-bg:#0a0a0b;
    --surface:#131316;
    --surface-2:#1a1a1d;
    --surface-hover:#1f1f23;
    --border:#26262a;
    --border-strong:#3a3a40;
    --primary:#2563eb;
    --primary-hover:#1d4ed8;
    --primary-bg:rgba(37,99,235,0.14);
    --primary-bg-strong:rgba(37,99,235,0.22);
    --text:#f4f4f5;
    --text-2:#a1a1aa;
    --text-3:#71717a;
    --red:#ef4444;
}}
html,body{{height:100%;background:var(--bg);color:var(--text);font-family:'Inter',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}}
a{{color:inherit;text-decoration:none}}
button{{cursor:pointer;border:none;background:none;font-family:inherit;color:inherit}}
input{{font-family:inherit}}
::-webkit-scrollbar{{width:10px;height:10px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:#2a2a2f;border-radius:10px;border:2px solid var(--bg)}}
::-webkit-scrollbar-thumb:hover{{background:#3a3a40}}

.layout{{display:flex;height:100vh;overflow:hidden;background:var(--bg)}}

/* ── Sidebar ─────────────────────────────────────────────────────── */
.sidebar{{width:260px;background:var(--sidebar-bg);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;transition:transform .25s ease}}
.sidebar-brand{{display:flex;align-items:center;gap:10px;padding:18px 18px 14px;font-weight:700;font-size:1rem;color:var(--text)}}
.brand-logo{{width:32px;height:32px;border-radius:9px;background:var(--primary);display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0}}
.sidebar-content{{padding:6px 14px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px}}

.btn-compose{{display:flex;align-items:center;justify-content:center;gap:10px;background:var(--primary);color:#fff;border-radius:14px;padding:14px 16px;font-weight:600;font-size:.95rem;transition:background .15s;margin-bottom:14px;border:none;cursor:pointer;width:100%}}
.btn-compose:hover{{background:var(--primary-hover)}}

.nav-section{{display:flex;flex-direction:column;gap:2px}}
.nav-item{{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;font-size:.92rem;color:var(--text-2);transition:background .15s,color .15s;cursor:pointer;font-weight:500}}
.nav-item:hover{{background:var(--surface-2);color:var(--text)}}
.nav-item.active{{background:var(--primary-bg);color:var(--primary)}}
.nav-item svg{{flex-shrink:0}}

.section-label{{font-size:.7rem;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:.08em;padding:18px 12px 8px}}

.account-item{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:10px;color:var(--text-2);transition:background .15s,color .15s;font-size:.9rem;font-weight:500}}
.account-item:hover{{background:var(--surface-2);color:var(--text)}}
.account-item.active{{background:var(--surface-2);color:var(--text)}}
.account-dot{{width:22px;height:22px;border-radius:6px;color:#fff;display:flex;align-items:center;justify-content:center;font-size:.7rem;font-weight:700;flex-shrink:0}}
.account-info{{flex:1;min-width:0;overflow:hidden}}
.account-email{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.88rem}}
.account-provider{{font-size:.7rem;color:var(--text-3);margin-top:1px}}

.all-accounts{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:10px;color:var(--text);background:var(--surface-2);font-size:.9rem;font-weight:600;cursor:pointer}}
.all-accounts:hover{{background:var(--surface-hover)}}
.hash-icon{{width:22px;height:22px;border-radius:6px;background:var(--surface-hover);display:flex;align-items:center;justify-content:center;color:var(--text-2);flex-shrink:0}}

.add-account{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:10px;color:var(--primary);font-size:.88rem;font-weight:600;transition:background .15s;margin-top:2px}}
.add-account:hover{{background:var(--primary-bg)}}

/* ── User footer ──────────────────────────────────────���──────────── */
.sidebar-footer{{padding:12px 16px;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px}}
.user-avatar{{width:32px;height:32px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:.85rem;flex-shrink:0}}
.user-name{{font-size:.9rem;font-weight:500;color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.user-actions{{display:flex;align-items:center;gap:2px}}
.icon-btn{{width:28px;height:28px;border-radius:7px;display:flex;align-items:center;justify-content:center;color:var(--text-2);transition:background .15s,color .15s;cursor:pointer;border:none;background:none}}
.icon-btn:hover{{background:var(--surface-2);color:var(--text)}}

/* ── Main ────────────────────────────────────────────────────────── */
.main{{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;background:var(--bg)}}

.main-header{{display:flex;align-items:center;justify-content:space-between;padding:24px 32px 16px;gap:16px;flex-wrap:wrap}}
.page-title{{font-size:1.65rem;font-weight:700;color:var(--text);letter-spacing:-.02em}}
.header-actions{{display:flex;align-items:center;gap:10px}}
.btn-outline{{display:flex;align-items:center;gap:8px;padding:9px 16px;border-radius:10px;border:1px solid var(--border);background:transparent;color:var(--text);font-size:.88rem;font-weight:500;transition:background .15s,border-color .15s;cursor:pointer}}
.btn-outline:hover{{background:var(--surface-2);border-color:var(--border-strong)}}

.btn-primary{{display:inline-flex;align-items:center;gap:8px;padding:11px 20px;border-radius:10px;background:var(--primary);color:#fff;font-size:.92rem;font-weight:600;transition:background .15s;cursor:pointer;border:none}}
.btn-primary:hover{{background:var(--primary-hover)}}

.search-wrap{{padding:0 32px 16px}}
.search-bar{{display:flex;align-items:center;gap:12px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:13px 20px;transition:border-color .2s,background .2s}}
.search-bar:focus-within{{border-color:var(--border-strong);background:var(--surface-2)}}
.search-bar svg{{color:var(--text-3);flex-shrink:0}}
.search-bar input{{border:none;background:none;outline:none;width:100%;font-size:.95rem;color:var(--text)}}
.search-bar input::placeholder{{color:var(--text-3)}}

.content-area{{flex:1;overflow-y:auto;padding:0 32px 32px;min-height:0}}

.hamburger{{display:none;align-items:center;justify-content:center;width:36px;height:36px;border-radius:8px;color:var(--text);background:none;border:1px solid var(--border);cursor:pointer}}

/* ─��� Empty state card ────────────────────────────────────────────── */
.empty-card{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:80px 32px;display:flex;flex-direction:column;align-items:center;text-align:center;min-height:460px;justify-content:center;gap:6px}}
.empty-icon{{color:var(--text-3);margin-bottom:14px}}
.empty-title{{font-size:1.15rem;font-weight:600;color:var(--text);margin-bottom:4px}}
.empty-subtitle{{font-size:.92rem;color:var(--text-2);margin-bottom:22px}}
.empty-cta{{margin-top:4px}}

/* ─�� Mail rows ───────────────────────────────────────────────────── */
.mail-list{{background:var(--surface);border:1px solid var(--border);border-radius:18px;overflow:hidden}}
.mail-toolbar{{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border);font-size:.85rem;color:var(--text-2)}}
.mail-toolbar b{{color:var(--text);font-weight:600}}

.mail-row{{display:flex;align-items:flex-start;gap:14px;padding:14px 20px;border-bottom:1px solid var(--border);transition:background .15s;cursor:pointer}}
.mail-row:last-child{{border-bottom:none}}
.mail-row:hover{{background:var(--surface-2)}}
.mail-row.unread{{background:var(--primary-bg)}}
.mail-row.unread:hover{{background:var(--primary-bg-strong)}}
.mail-avatar{{width:38px;height:38px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:.92rem;flex-shrink:0}}
.mail-body{{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}}
.mail-line1{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
.mail-from{{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.92rem}}
.mail-time{{font-size:.78rem;color:var(--text-3);white-space:nowrap;flex-shrink:0}}
.mail-subject{{font-size:.92rem;color:var(--text);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.mail-snippet{{font-size:.85rem;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.mail-account{{font-size:.72rem;color:var(--text-3);margin-top:2px}}

.pagination{{display:flex;justify-content:center;align-items:center;gap:12px;padding:20px}}
.page-btn{{padding:9px 16px;border-radius:10px;background:var(--surface);border:1px solid var(--border);color:var(--text);font-size:.85rem;font-weight:500;transition:background .15s}}
.page-btn:hover{{background:var(--surface-2)}}
.page-info{{font-size:.85rem;color:var(--text-2)}}

.error-bar{{background:rgba(239,68,68,.12);color:#fca5a5;border:1px solid rgba(239,68,68,.3);padding:12px 16px;border-radius:12px;font-size:.88rem;margin-bottom:16px}}

.sidebar-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:998}}

/* ── Compose Modal ───────────────────────────────────────────────── */
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;align-items:center;justify-content:center}}
.modal-overlay.show{{display:flex}}
.compose-modal{{background:var(--surface);border:1px solid var(--border);border-radius:16px;width:90%;max-width:600px;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.3)}}
.modal-header{{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;border-bottom:1px solid var(--border)}}
.modal-title{{font-size:1.2rem;font-weight:700;color:var(--text)}}
.modal-close{{font-size:1.5rem;color:var(--text-2);cursor:pointer;background:none;border:none;padding:0;width:24px;height:24px;display:flex;align-items:center;justify-content:center}}
.modal-close:hover{{color:var(--text)}}
.modal-body{{flex:1;overflow-y:auto;padding:24px}}
.compose-field{{margin-bottom:20px}}
.field-label{{font-size:.82rem;font-weight:600;color:var(--text-2);margin-bottom:8px;display:block}}
.field-input{{width:100%;padding:11px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);outline:none;font-size:.95rem;font-family:inherit;transition:border-color .2s}}
.field-input:focus{{border-color:var(--primary)}}
textarea.field-input{{resize:vertical;min-height:200px;font-family:inherit}}
.from-select{{display:flex;align-items:center;gap:8px;padding:11px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);cursor:pointer;font-size:.95rem;font-family:inherit}}
.from-select:hover{{border-color:var(--border-strong)}}

.modal-toolbar{{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-top:1px solid var(--border)}}
.modal-icons{{display:flex;align-items:center;gap:8px}}
.modal-icon-btn{{width:36px;height:36px;border-radius:8px;background:var(--surface-2);border:none;color:var(--text-2);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1rem;transition:background .15s,color .15s}}
.modal-icon-btn:hover{{background:var(--surface-hover);color:var(--text)}}
.btn-send{{display:flex;align-items:center;gap:8px;padding:10px 20px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-weight:600;font-size:.92rem;cursor:pointer;transition:background .15s;font-family:inherit}}
.btn-send:hover{{background:var(--primary-hover)}}

/* ── Default Account Section ────────────────────────���────────────── */
.default-account-section{{background:var(--surface-2);border-radius:10px;padding:14px;margin-bottom:10px}}
.default-account-email{{font-size:.88rem;font-weight:600;color:var(--text);margin-bottom:10px;display:flex;align-items:center;gap:8px}}
.default-account-avatar{{width:20px;height:20px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:.65rem;font-weight:700;color:#fff;flex-shrink:0}}
.account-folders{{display:flex;flex-direction:column;gap:4px}}
.folder-btn{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;color:var(--text-2);font-size:.85rem;font-weight:500;cursor:pointer;transition:background .15s,color .15s;background:none;border:none;text-align:left;text-decoration:none}}
.folder-btn:hover{{background:rgba(37,99,235,.08);color:var(--text)}}
.folder-btn.active{{background:var(--primary-bg);color:var(--primary)}}
.folder-icon{{width:16px;height:16px;flex-shrink:0}}
.default-account-email span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}}

@media (max-width:768px){{
    .sidebar{{position:fixed;left:0;top:0;height:100%;z-index:999;transform:translateX(-100%)}}
    .sidebar.open{{transform:translateX(0)}}
    .sidebar-overlay.show{{display:block}}
    .hamburger{{display:flex}}
    .main-header{{padding:16px 20px 12px}}
    .search-wrap{{padding:0 20px 12px}}
    .content-area{{padding:0 20px 20px}}
    .page-title{{font-size:1.3rem}}
    .compose-modal{{width:95%;max-height:90vh}}
}}
</style>
</head>
<body>
<div class="layout">
<div class="sidebar-overlay" id="overlay" onclick="closeSidebar()"></div>

<aside class="sidebar" id="sidebar">
    <div class="sidebar-brand">
        <div class="brand-logo">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
        </div>
        RexifyMail
    </div>

    <div class="sidebar-content">
        <button class="btn-compose" onclick="openCompose()">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/></svg>
            Compose
        </button>

        <a href="/mailbox" class="nav-item{' active' if (view_mode == 'all' and not selected_account_id) else ''}">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
            All Inbox
        </a>

        {(lambda: (lambda da_id, is_da_view: f'''<div class="default-account-section">
            <div class="default-account-email">
                <div class="default-account-avatar" style="background:{avatar_color(default_account['email'])}">{default_account['email'][0].upper()}</div>
                <span title="{html_module.escape(default_account['email'])}">{html_module.escape(default_account['email'])}</span>
            </div>
            <div class="account-folders">
                <a class="folder-btn{' active' if (is_da_view and folder == 'inbox') else ''}" href="/mailbox?view=account&account={da_id}&folder=inbox">
                    <svg class="folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
                    Inbox
                </a>
                <a class="folder-btn{' active' if (is_da_view and folder == 'sent') else ''}" href="/mailbox?view=account&account={da_id}&folder=sent">
                    <svg class="folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></svg>
                    Sent
                </a>
                <a class="folder-btn{' active' if (is_da_view and folder == 'spam') else ''}" href="/mailbox?view=account&account={da_id}&folder=spam">
                    <svg class="folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/><polyline points="17 7 17 2 7 2 7 7"/><path d="M9 12v3"/><path d="M15 12v3"/></svg>
                    Spam
                </a>
                <a class="folder-btn{' active' if (is_da_view and folder == 'trash') else ''}" href="/mailbox?view=account&account={da_id}&folder=trash">
                    <svg class="folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    Trash
                </a>
            </div>
        </div>''')(str(default_account['_id']), (view_mode == 'account' and selected_account_id == str(default_account['_id']))))() if default_account else ''}

        <button class="add-account" onclick="openAddAccountModal()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
            Add account
        </button>

        <div class="section-label">Other Accounts</div>

        {accounts_list_html if active_accounts else '<div style="font-size:.82rem;color:var(--text-3);padding:8px 12px">No other accounts</div>'}
    </div>

    <div class="sidebar-footer">
        <div class="user-avatar">{user_initial}</div>
        <div class="user-name">{html_module.escape(display_username)}</div>
        <div class="user-actions">
            <a href="/mailbox?logout=1" class="icon-btn" title="Sign out" aria-label="Sign out">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" x2="9" y1="12" y2="12"/></svg>
            </a>
        </div>
    </div>
</aside>

<main class="main">
    <div class="main-header">
        <div style="display:flex;align-items:center;gap:12px">
            <button class="hamburger" onclick="toggleSidebar()" aria-label="Toggle sidebar">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/></svg>
            </button>
            <h1 class="page-title">{page_title}</h1>
        </div>
        <div class="header-actions">
            <button class="btn-outline" onclick="location.reload()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>
                Refresh
            </button>
        </div>
    </div>

    <div class="search-wrap">
        <div class="search-bar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
            <input type="text" placeholder="Search emails by sender, subject or content..." id="searchInput" oninput="filterEmails()">
        </div>
    </div>

    <div class="content-area">
        {f'<div class="error-bar">{html_module.escape(fetch_error)}</div>' if fetch_error else ''}
        {empty_state_html if (not all_emails or not active_accounts) else f'''
        <div class="mail-list">
            <div class="mail-toolbar"><span><b>{len(all_emails)}</b> messages</span></div>
            <div id="emailTable">{rows_html}</div>
        </div>
        {pagination_html}
        '''}
    </div>
</main>
</div>

<!-- Compose Modal -->
<div class="modal-overlay" id="composeModal" onclick="if(event.target===this) closeCompose()">
    <div class="compose-modal">
        <div class="modal-header">
            <h2 class="modal-title">New Message</h2>
            <button class="modal-close" onclick="closeCompose()">✕</button>
        </div>
        <div class="modal-body">
            <div class="compose-field">
                <label class="field-label">From</label>
                <select class="field-input" id="fromSelect">
                    {''.join(f'<option value="{str(acc["_id"])}">{html_module.escape(acc["email"])}</option>' for acc in active_accounts) if active_accounts else '<option value="">No account connected</option>'}
                </select>
            </div>
            <div class="compose-field">
                <label class="field-label">To</label>
                <input type="email" class="field-input" id="toInput" placeholder="recipient@example.com">
            </div>
            <div class="compose-field">
                <label class="field-label">Subject</label>
                <input type="text" class="field-input" id="subjectInput" placeholder="Subject">
            </div>
            <div class="compose-field">
                <textarea class="field-input" id="messageInput" placeholder="Write your message..."></textarea>
            </div>
        </div>
        <div class="modal-toolbar">
            <div class="modal-icons">
                <button class="modal-icon-btn" title="Emoji">😊</button>
                <button class="modal-icon-btn" title="Attachment">📎</button>
                <button class="modal-icon-btn" title="Link">🔗</button>
            </div>
            <button class="btn-send" onclick="sendMessage()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19.51 2.36a1.23 1.23 0 0 0-1.28-.12l-17 10a1.25 1.25 0 0 0 0 2.24l17 10a1.23 1.23 0 0 0 1.28-.12A1.2 1.2 0 0 0 21 21.25V2.75a1.2 1.2 0 0 0-.49-.39z"/></svg>
                Send
            </button>
        </div>
    </div>
</div>

<!-- Add Account Modal -->
<div class="modal-overlay" id="addAccountModal" onclick="if(event.target===this) closeAddAccountModal()">
    <div class="compose-modal">
        <div class="modal-header">
            <h2 class="modal-title">Add Account</h2>
            <button class="modal-close" onclick="closeAddAccountModal()">✕</button>
        </div>
        <div class="modal-body">
            <p style="color:var(--text-2);margin-bottom:16px">Choose your email provider to connect your account:</p>
            <a href="/web/add/gmail" style="display:inline-flex;align-items:center;gap:12px;padding:14px 16px;border:1px solid var(--border);border-radius:10px;background:var(--surface-2);color:var(--text);text-decoration:none;transition:background .15s;width:100%;margin-bottom:10px" onmouseover="this.style.background='var(--border)'" onmouseout="this.style.background='var(--surface-2)'">
                <span style="font-size:1.5rem">📧</span>
                <div>
                    <div style="font-weight:600">Gmail / Google Workspace</div>
                    <div style="font-size:.8rem;color:var(--text-2)">Connect via OAuth</div>
                </div>
            </a>
            <a href="/web/add/microsoft" style="display:inline-flex;align-items:center;gap:12px;padding:14px 16px;border:1px solid var(--border);border-radius:10px;background:var(--surface-2);color:var(--text);text-decoration:none;transition:background .15s;width:100%" onmouseover="this.style.background='var(--border)'" onmouseout="this.style.background='var(--surface-2)'">
                <span style="font-size:1.5rem">🔵</span>
                <div>
                    <div style="font-weight:600">Outlook / Microsoft</div>
                    <div style="font-size:.8rem;color:var(--text-2)">Connect via OAuth</div>
                </div>
            </a>
        </div>
    </div>
</div>

<script>
function toggleSidebar() {{ document.getElementById('sidebar').classList.toggle('open'); document.getElementById('overlay').classList.toggle('show'); }}
function closeSidebar() {{ document.getElementById('sidebar').classList.remove('open'); document.getElementById('overlay').classList.remove('show'); }}
function filterEmails() {{
    var q = document.getElementById('searchInput').value.toLowerCase();
    document.querySelectorAll('.mail-row').forEach(function(card) {{
        card.style.display = card.textContent.toLowerCase().includes(q) ? '' : 'none';
    }});
}}
function openCompose() {{ document.getElementById('composeModal').classList.add('show'); }}
function closeCompose() {{ document.getElementById('composeModal').classList.remove('show'); clearComposeFields(); }}
async function sendMessage() {{
    var fromSel = document.getElementById('fromSelect');
    var account_id = fromSel ? fromSel.value : '';
    var to = document.getElementById('toInput').value.trim();
    var subject = document.getElementById('subjectInput').value.trim();
    var body = document.getElementById('messageInput').value.trim();
    if(!account_id) {{ alert('Please connect an account first'); return; }}
    if(!to || !subject || !body) {{ alert('Please fill in To, Subject, and Message'); return; }}

    var sendBtn = document.querySelector('.btn-send');
    var oldHtml = sendBtn.innerHTML;
    sendBtn.disabled = true;
    sendBtn.style.opacity = '.65';
    sendBtn.innerHTML = 'Sending...';

    try {{
        var res = await fetch('/api/send', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ account_id: account_id, to: to, subject: subject, body: body }})
        }});
        var data = await res.json();
        if(res.ok && data.success) {{
            sendBtn.innerHTML = 'Sent!';
            setTimeout(function() {{ closeCompose(); sendBtn.innerHTML = oldHtml; sendBtn.disabled = false; sendBtn.style.opacity = '1'; }}, 800);
        }} else {{
            alert('Failed to send: ' + (data.error || ('HTTP ' + res.status)));
            sendBtn.disabled = false;
            sendBtn.style.opacity = '1';
            sendBtn.innerHTML = oldHtml;
        }}
    }} catch(e) {{
        alert('Network error: ' + e.message);
        sendBtn.disabled = false;
        sendBtn.style.opacity = '1';
        sendBtn.innerHTML = oldHtml;
    }}
}}
function clearComposeFields() {{
    document.getElementById('toInput').value = '';
    document.getElementById('subjectInput').value = '';
    document.getElementById('messageInput').value = '';
}}
function openAddAccountModal() {{ document.getElementById('addAccountModal').classList.add('show'); }}
function closeAddAccountModal() {{ document.getElementById('addAccountModal').classList.remove('show'); }}
</script>
</body></html>"""
    return web.Response(text=page_html, content_type="text/html")

# ── ATTACHMENT DOWNLOAD API ───────────────────────────────────────────────────

async def attachment_download_handler(request):
    """GET /api/attachment/{hash}/{attachment_id} — streams the attachment file"""
    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str:
        return web.Response(text="Not authenticated", status=401)
    try:
        internal_user_id = int(user_id_str)
    except ValueError:
        return web.Response(text="Invalid session", status=401)

    cb_hash = request.match_info.get('hash')
    attachment_id = request.match_info.get('attachment_id')

    callback_data = await db.get_email_callback(cb_hash)
    if not callback_data:
        return web.Response(text="Email not found", status=404)

    account = await db.get_account(callback_data['account_id'])
    if not account or account.get('user_id') != internal_user_id:
        return web.Response(text="Forbidden", status=403)

    provider = account.get('provider', 'gmail')
    try:
        if provider == 'gmail':
            service = get_gmail_service(account['access_token'], account.get('refresh_token'))
            loop = asyncio.get_event_loop()
            att = await loop.run_in_executor(
                None,
                lambda: service.users().messages().attachments().get(
                    userId='me',
                    messageId=callback_data['message_id'],
                    id=attachment_id
                ).execute()
            )
            file_data = base64.urlsafe_b64decode(att['data'])
            # Get filename by re-fetching message headers
            msg_meta = await loop.run_in_executor(
                None,
                lambda: service.users().messages().get(
                    userId='me', id=callback_data['message_id'], format='full'
                ).execute()
            )
            _, _, attachments = get_email_content(msg_meta.get('payload', {}))
            filename = next((a['name'] for a in attachments if a.get('attachment_id') == attachment_id), 'attachment')
            content_type = next((a['type'] for a in attachments if a.get('attachment_id') == attachment_id), 'application/octet-stream')

        else:  # Microsoft — attachment_id starts with 'ms_' for index-based, otherwise real ID
            att = await call_ms_graph(
                f"me/messages/{callback_data['message_id']}/attachments/{attachment_id}",
                account['access_token']
            )
            import base64 as _b64
            file_data = _b64.b64decode(att.get('contentBytes', ''))
            filename = att.get('name', 'attachment')
            content_type = att.get('contentType', 'application/octet-stream')

        return web.Response(
            body=file_data,
            content_type=content_type,
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f"attachment_download_handler error: {e}", exc_info=True)
        return web.Response(text=f"Error: {str(e)}", status=500)


# ── REPLY EMAIL API ───────────────────────────────────────────────────────────

async def reply_email_handler(request):
    """POST /api/reply/{hash} — JSON {body}"""
    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        internal_user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "Invalid session"}, status=401)

    cb_hash = request.match_info.get('hash')
    callback_data = await db.get_email_callback(cb_hash)
    if not callback_data:
        return web.json_response({"error": "Email not found"}, status=404)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    body = (data.get('body') or '').strip()
    if not body:
        return web.json_response({"error": "Reply body is required"}, status=400)

    try:
        account = await db.get_account(callback_data['account_id'])
    except Exception:
        return web.json_response({"error": "Invalid account"}, status=400)

    if not account or account.get('user_id') != internal_user_id:
        return web.json_response({"error": "Account not found"}, status=404)
    if not account.get('token_valid', True):
        return web.json_response({"error": "Account token expired — please reconnect"}, status=400)

    provider = account.get('provider', 'gmail')
    try:
        if provider == 'gmail':
            service = get_gmail_service(account['access_token'], account.get('refresh_token'))
            # Fetch original to get reply headers
            loop = asyncio.get_event_loop()
            orig = await loop.run_in_executor(
                None,
                lambda: service.users().messages().get(userId='me', id=callback_data['message_id'], format='metadata',
                                                        metadataHeaders=['From','To','Subject','Message-ID','References','In-Reply-To']).execute()
            )
            headers = orig.get('payload', {}).get('headers', [])
            reply_to_addr = get_header(headers, 'From')
            orig_subject = get_header(headers, 'Subject')
            orig_msg_id = get_header(headers, 'Message-ID')
            orig_refs = get_header(headers, 'References')

            from email.mime.text import MIMEText
            msg = MIMEText(body)
            msg['to'] = reply_to_addr
            msg['from'] = account['email']
            msg['subject'] = orig_subject if orig_subject.lower().startswith('re:') else f"Re: {orig_subject}"
            if orig_msg_id:
                msg['In-Reply-To'] = orig_msg_id
                msg['References'] = f"{orig_refs} {orig_msg_id}".strip() if orig_refs else orig_msg_id
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            send_body = {'raw': raw}
            if callback_data.get('thread_id'):
                send_body['threadId'] = callback_data['thread_id']
            await loop.run_in_executor(
                None,
                lambda: service.users().messages().send(userId='me', body=send_body).execute()
            )
        else:  # Microsoft
            payload = {
                "message": {
                    "toRecipients": [{"emailAddress": {"address": account.get('email', '')}}],
                    "comment": body
                }
            }
            # Fetch original to get the real sender address for reply-to
            orig = await call_ms_graph(
                f"me/messages/{callback_data['message_id']}?$select=from,toRecipients",
                account['access_token']
            )
            reply_addr = orig.get('from', {}).get('emailAddress', {}).get('address', '')
            payload['message']['toRecipients'] = [{"emailAddress": {"address": reply_addr}}]
            await call_ms_graph(
                f"me/messages/{callback_data['message_id']}/reply",
                account['access_token'],
                method="POST",
                json_data=payload
            )

        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"reply_email_handler error: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


# ── SEND EMAIL API ────────────────────────────────────────────────────────────

async def send_email_handler(request):
    """POST /api/send — JSON {account_id, to, subject, body}"""
    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        internal_user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "Invalid session"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    account_id = (data.get('account_id') or '').strip()
    to_addr = (data.get('to') or '').strip()
    subject = (data.get('subject') or '').strip()
    body = (data.get('body') or '').strip()

    if not account_id or not to_addr or not subject or not body:
        return web.json_response({"error": "All fields are required"}, status=400)

    try:
        account = await db.accounts.find_one({"_id": ObjectId(account_id), "user_id": internal_user_id})
    except Exception:
        return web.json_response({"error": "Invalid account id"}, status=400)

    if not account:
        return web.json_response({"error": "Account not found"}, status=404)
    if not account.get('token_valid', True):
        return web.json_response({"error": "Account token expired — please reconnect"}, status=400)

    provider = account.get('provider', 'gmail')
    try:
        if provider == 'gmail':
            from email.mime.text import MIMEText
            service = get_gmail_service(account['access_token'], account.get('refresh_token'))
            msg = MIMEText(body)
            msg['to'] = to_addr
            msg['from'] = account['email']
            msg['subject'] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: service.users().messages().send(userId='me', body={'raw': raw}).execute()
            )
        else:
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": to_addr}}]
                }
            }
            await call_ms_graph("me/sendMail", account['access_token'], method="POST", json_data=payload)

        return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"send_email_handler error: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)


# ── WEB OAUTH HANDLERS (Add accounts from web) ────────────────────────────────

async def web_add_gmail_handler(request):
    """GET /web/add/gmail — Initiate Gmail OAuth from web mailbox"""
    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str:
        return web.HTTPFound('/mailbox')
    
    try:
        internal_user_id = int(user_id_str)
    except ValueError:
        return web.HTTPFound('/mailbox')
    
    from google_auth_oauthlib.flow import Flow
    state_key = f"web_{internal_user_id}_{int(datetime.now(timezone.utc).timestamp())}"
    
    flow = Flow.from_client_config(
        {"web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=["https://mail.google.com/"],
        redirect_uri=REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true",
                                          state=state_key, prompt="consent")
    oauth_states[state_key] = {"user_id": internal_user_id, "telegram_id": None, "flow": flow, "provider": "gmail", "from_web": True}
    
    return web.HTTPFound(auth_url)

async def web_add_microsoft_handler(request):
    """GET /web/add/microsoft — Initiate Microsoft OAuth from web mailbox"""
    user_id_str = request.cookies.get('rexify_uid')
    if not user_id_str:
        return web.HTTPFound('/mailbox')
    
    try:
        internal_user_id = int(user_id_str)
    except ValueError:
        return web.HTTPFound('/mailbox')
    
    from config import MS_CLIENT_ID, MS_CLIENT_SECRET, MS_REDIRECT_URI, MS_SCOPES
    import msal
    
    state_key = f"web_ms_{internal_user_id}_{int(datetime.now(timezone.utc).timestamp())}"
    
    msal_app = msal.ConfidentialClientApplication(
        MS_CLIENT_ID, 
        client_credential=MS_CLIENT_SECRET,
        authority="https://login.microsoftonline.com/common"
    )
    
    auth_url = msal_app.get_authorization_request_url(
        MS_SCOPES,
        redirect_uri=MS_REDIRECT_URI,
        state=state_key
    )
    
    oauth_states[state_key] = {"user_id": internal_user_id, "telegram_id": None, "provider": "microsoft", "from_web": True}
    
    return web.HTTPFound(auth_url)

# ── APP FACTORY ───────────────────────────────────────────────────────────────

def create_web_app():
    app = web.Application()
    app.router.add_get('/', main_page_handler)
    app.router.add_get('/oauth_callback', oauth_callback_handler)
    app.router.add_get('/ms_callback', ms_callback_handler)
    app.router.add_get('/view/{hash}', get_email_handler)
    app.router.add_get('/mailbox', mailbox_handler)
    app.router.add_post('/mailbox', mailbox_handler)
    app.router.add_post('/api/send', send_email_handler)
    app.router.add_post('/api/reply/{hash}', reply_email_handler)
    app.router.add_get('/api/attachment/{hash}/{attachment_id}', attachment_download_handler)
    # Web OAuth routes for adding accounts directly
    app.router.add_get('/web/add/gmail', web_add_gmail_handler)
    app.router.add_get('/web/add/microsoft', web_add_microsoft_handler)
    return app
