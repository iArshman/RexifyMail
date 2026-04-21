import os
import logging
import asyncio
import html
import time
from datetime import datetime, timezone
from typing import Optional, Dict
from html.parser import HTMLParser
import hashlib
from bson import ObjectId
import re
import base64

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web, ClientSession

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import msal
from email.mime.text import MIMEText

# Ensure MS variables are imported from config
from config import (
    BOT_TOKEN, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES, 
    NOTIFICATION_CHECK_INTERVAL, PORT,
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_REDIRECT_URI, MS_SCOPES
)
from database import Database
import web as web_module

# ============= GLOBAL VARS =============
bot: Optional[Bot] = None
db: Optional[Database] = None
dp: Optional[Dispatcher] = None

oauth_states: Dict[str, dict] = {}
user_states: Dict[int, dict] = {}


# ============= LOGGING =============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= HELPERS =============

async def get_current_user_id(telegram_id: int) -> int:
    return await db.get_internal_user_id(telegram_id)

def html_to_telegram(html_content: str) -> str:
    if not html_content:
        return ""
    text = html.unescape(html_content)
    text = re.sub(r'</?(td|th)[^>]*>', ' ', text, flags=re.IGNORECASE)
    block_tags = r'</?(p|div|section|article|header|footer|li|tr|blockquote|h[1-6])[^>]*>'
    text = re.sub(block_tags, '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr\s*/?>', '\n─────────────\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()

def escape_html(text: str) -> str:
    decoded_text = html.unescape(str(text))
    return html.escape(decoded_text)

# ============= API CLIENTS =============

def get_gmail_service(access_token: str, refresh_token: str = None, expires_at: float = None):
    creds = Credentials.from_authorized_user_info({
        'token': access_token,
        'refresh_token': refresh_token,
        'token_uri': 'https://oauth2.googleapis.com/token',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scopes': SCOPES
    }, SCOPES)
    return build('gmail', 'v1', credentials=creds)

async def get_valid_ms_token(account: dict) -> str:
    """Checks if Microsoft token is expired and refreshes it automatically."""
    current_time = time.time()
    # 120 seconds buffer
    if current_time >= account.get('expires_at', 0) - 120:
        if not account.get('refresh_token'):
            raise Exception("No refresh token available to renew session.")
        
        logger.info(f"Refreshing MS token for {account.get('email')}")
        msal_app = msal.ConfidentialClientApplication(
            MS_CLIENT_ID, 
            client_credential=MS_CLIENT_SECRET,
            authority="https://login.microsoftonline.com/common"
        )
        
        result = msal_app.acquire_token_by_refresh_token(
            account['refresh_token'], 
            scopes=MS_SCOPES
        )
        
        if "access_token" in result:
            new_tokens = {
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token", account['refresh_token']),
                "expires_at": time.time() + result.get("expires_in", 3600)
            }
            # Update DB
            await db.accounts.update_one({"_id": account["_id"]}, {"$set": new_tokens})
            # Update local dictionary
            account.update(new_tokens)
            return result["access_token"]
        else:
            raise Exception(f"Token refresh failed: {result.get('error_description', 'Unknown error')}")
            
    return account['access_token']

async def call_ms_graph(endpoint: str, account: dict, method: str = "GET", json_data: dict = None):
    """Helper to call Microsoft Graph API (Now auto-refreshes token)"""
    token = await get_valid_ms_token(account)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    url = f"https://graph.microsoft.com/v1.0/{endpoint}"
    async with ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json_data) as resp:
            if resp.status in [200, 201, 202]:
                return await resp.json()
            if resp.status == 204:
                return True
            resp_text = await resp.text()
            raise Exception(f"Graph API Error {resp.status}: {resp_text}")

def get_header(headers, name):
    for h in headers:
        if h['name'].lower() == name.lower():
            return h['value']
    return ''

def get_email_body(payload) -> str:
    body_plain = ""
    body_html = ""

    def _extract(parts):
        nonlocal body_plain, body_html
        for part in parts:
            mime = part.get('mimeType', '')
            data = part.get('body', {}).get('data', '')
            sub_parts = part.get('parts', [])
            if sub_parts:
                _extract(sub_parts)
            if not data:
                continue
            decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if mime == 'text/plain' and not body_plain:
                body_plain = decoded
            elif mime == 'text/html' and not body_html:
                body_html = decoded

    if 'parts' in payload:
        _extract(payload['parts'])
    else:
        data = payload.get('body', {}).get('data', '')
        if data:
            decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
            if payload.get('mimeType') == 'text/html':
                body_html = decoded
            else:
                body_plain = decoded

    if body_plain:
        return body_plain.strip()
    elif body_html:
        return html_to_telegram(body_html)
    return ""

async def store_email_data(account_id: str, message_id: str, thread_id: str = None) -> str:
    hash_value = hashlib.md5(f"{account_id}:{message_id}:{thread_id or ''}".encode()).hexdigest()[:16]
    await db.callback_data.update_one(
        {"hash": hash_value},
        {"$set": {"hash": hash_value, "account_id": account_id, "message_id": message_id,
                  "thread_id": thread_id, "created_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    return hash_value

async def get_email_data(hash_value: str) -> dict:
    result = await db.callback_data.find_one({"hash": hash_value})
    if result:
        return {"account_id": result["account_id"], "message_id": result["message_id"],
                "thread_id": result.get("thread_id")}
    return None

# ============= ACCOUNT STATUS HELPERS =============

async def check_token_validity(account: dict) -> bool:
    account_id = str(account['_id'])
    provider = account.get('provider', 'gmail')
    try:
        if provider == 'gmail':
            service = get_gmail_service(
                account['access_token'],
                account.get('refresh_token'),
                account.get('expires_at')
            )
            service.users().getProfile(userId='me').execute()
        else: # Microsoft
            await call_ms_graph("me", account)
            
        await db.mark_account_valid(account_id)
        return True
    except Exception:
        await db.mark_account_invalid(account_id)
        return False

# ============= COMMAND HANDLERS =============

async def cmd_start(message: Message):
    telegram_id = message.from_user.id
    
    if await db.is_user_logged_in(telegram_id):
        await message.answer(
            "👋 Welcome to <b>Rexify Multi-Mail Bot</b>!\n\n"
            "Manage Gmail and Outlook accounts right inside Telegram.\n\n"
            "<b>Commands:</b>\n"
            "/inbox — View latest emails\n"
            "/compose — Compose new email\n"
            "/search — Search emails\n"
            "/addaccount — Add Gmail account\n"
            "/addoutlook — Add Outlook/Hotmail account\n"
            "/settings — Settings & manage accounts\n"
            "/logout — Logout",
            parse_mode="HTML"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Register", callback_data="auth_register")],
            [InlineKeyboardButton(text="Login", callback_data="auth_login")]
        ])
        await message.answer(
            "<b>Welcome to Rexify Mail</b>\n\n"
            "Please register or login to continue.",
            reply_markup=kb,
            parse_mode="HTML"
        )

async def cmd_logout(message: Message):
    telegram_id = message.from_user.id
    if await db.logout_user(telegram_id):
        if telegram_id in user_states:
            del user_states[telegram_id]
        await message.answer("<b>Logged out successfully.</b>\n\nUse /start to login again.", parse_mode="HTML")
    else:
        await message.answer("You are not logged in.")

async def cmd_inbox(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_id = await get_current_user_id(telegram_id)
    default = await db.accounts.find_one({"user_id": user_id, "is_default": True}) \
              or await db.accounts.find_one({"user_id": user_id})
    if not default:
        await message.answer("No accounts connected. Use /addaccount or /addoutlook to add one.")
        return

    if not await check_token_validity(default):
        await message.answer(
            f"⚠️ The token for <b>{escape_html(default['email'])}</b> has expired or been revoked.\n\n"
            "Please re-connect this account.",
            parse_mode="HTML"
        )
        return

    provider = default.get('provider', 'gmail')
    
    try:
        await message.answer(f"📬 Latest 5 emails from <b>{escape_html(default['email'])}</b>", parse_mode="HTML")

        if provider == 'gmail':
            service = get_gmail_service(default['access_token'], default.get('refresh_token'), default.get('expires_at'))
            results = service.users().messages().list(userId='me', maxResults=5).execute()
            messages = results.get('messages', [])

            if not messages:
                return await message.answer(f"📭 No emails in <b>{escape_html(default['email'])}</b>", parse_mode="HTML")

            for msg in messages:
                msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()
                headers = msg_detail.get('payload', {}).get('headers', [])
                from_addr = escape_html(get_header(headers, 'From'))
                subject = escape_html(get_header(headers, 'Subject'))
                snippet = escape_html(msg_detail.get('snippet', '')[:60])
                cb_hash = await store_email_data(str(default['_id']), msg['id'], msg.get('threadId'))
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📖 View", callback_data=f"email:{cb_hash}")
                ]])
                text = f"<b>From:</b> {from_addr}\n<b>Subject:</b> {subject}\n<b>Preview:</b> {snippet}…"
                await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
                
        else: # Microsoft
            data = await call_ms_graph("me/mailFolders/inbox/messages?$top=5&$select=id,subject,from,bodyPreview", default)
            messages = data.get('value', [])
            
            if not messages:
                return await message.answer(f"📭 No emails in <b>{escape_html(default['email'])}</b>", parse_mode="HTML")
                
            for msg in messages:
                from_addr = escape_html(msg.get('from', {}).get('emailAddress', {}).get('name', 'Unknown'))
                subject = escape_html(msg.get('subject', '(No Subject)'))
                snippet = escape_html(msg.get('bodyPreview', '')[:60])
                cb_hash = await store_email_data(str(default['_id']), msg['id'], msg.get('conversationId'))
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📖 View", callback_data=f"email:{cb_hash}")
                ]])
                text = f"<b>From:</b> {from_addr}\n<b>Subject:</b> {subject}\n<b>Preview:</b> {snippet}…"
                await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Inbox error: {e}")
        await message.answer(f"Failed to load inbox: {str(e)}")

async def cmd_settings(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_id = await get_current_user_id(telegram_id)
    all_accounts = await db.accounts.find({"user_id": user_id}).to_list(length=None)

    if not all_accounts:
        await message.answer("No accounts connected.\n\nUse /addaccount or /addoutlook to connect.")
        return

    active_accounts = [acc for acc in all_accounts if acc.get('token_valid', True)]
    inactive_accounts = [acc for acc in all_accounts if not acc.get('token_valid', True)]

    total_active = len(active_accounts)
    default = next((acc for acc in active_accounts if acc.get('is_default')), None)
    if not default and active_accounts:
        default = active_accounts[0]

    lines = [f"⚙️ <b>Account Settings</b>\n"]
    if default:
        lines.append(f"<b>⭐ Default:</b> <code>{escape_html(default['email'])}</code>")
    
    lines.append(f"<b>🟢 Active Connected:</b> {total_active}\n")
    
    if active_accounts:
        lines.append("<i>Tap an account below to manage:</i>")
    else:
        lines.append("No active accounts right now. Please re-connect.")

    text = "\n".join(lines)
    keyboard = []

    for acc in active_accounts[:10]:
        is_default = (default and acc.get('_id') == default.get('_id'))
        notifs_off = not acc.get('notifications_enabled', True)
        
        prefix = "⭐" if is_default else "📧"
        suffix = " 🔕" if notifs_off else ""
        label = f"{prefix} {acc['email']}{suffix}"
        
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"acc:{str(acc['_id'])}")])

    if total_active > 10:
        keyboard.append([InlineKeyboardButton(text="Next ➡️", callback_data="acc_page:1")])

    if inactive_accounts:
        keyboard.append([InlineKeyboardButton(
            text=f"⚠️ Action Required: {len(inactive_accounts)} Inactive", 
            callback_data="inactive_list"
        )])

    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    
async def cmd_add(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_id = await get_current_user_id(telegram_id)
    state_key = f"{telegram_id}_{int(datetime.now().timestamp())}"

    flow = Flow.from_client_config(
        {"web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true",
                                          state=state_key, prompt="consent")
    oauth_states[state_key] = {"user_id": user_id, "telegram_id": telegram_id, "flow": flow, "provider": "gmail"}

    await message.answer(
        "Click below to connect your Gmail account:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔐 Connect Gmail", url=auth_url)
        ]])
    )

async def cmd_add_outlook(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_id = await get_current_user_id(telegram_id)
    state_key = f"ms_{telegram_id}_{int(datetime.now().timestamp())}"

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
    
    oauth_states[state_key] = {"user_id": user_id, "telegram_id": telegram_id, "provider": "microsoft"}

    await message.answer(
        "Click below to connect your <b>Microsoft (Outlook/Hotmail)</b> account:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔐 Connect Microsoft", url=auth_url)
        ]]),
        parse_mode="HTML"
    )

async def cmd_search(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_states[telegram_id] = {"action": "search"}
    await message.answer("🔍 <b>Search Emails</b>\n\nEnter your search query:", parse_mode="HTML")

async def cmd_compose(message: Message):
    telegram_id = message.from_user.id
    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")

    user_states[telegram_id] = {"action": "compose_to"}
    await message.answer("📧 <b>Compose New Email</b>\n\nEnter recipient email address:", parse_mode="HTML")

# ============= MESSAGE HANDLER =============

async def handle_user_input(message: Message):
    telegram_id = message.from_user.id
    if telegram_id not in user_states:
        return

    state = user_states[telegram_id]
    action = state.get("action")

    if action == "register_username":
        username = message.text.strip()
        if len(username) < 3:
            return await message.answer("Username must be at least 3 characters long.")
        user_states[telegram_id] = {"action": "register_password", "username": username}
        await message.answer("Enter your password (min 6 characters):")
        return
    
    elif action == "register_password":
        password = message.text.strip()
        if len(password) < 6:
            return await message.answer("Password must be at least 6 characters long.")
        
        result = await db.register_user(telegram_id, state['username'], password, message.from_user.full_name)
        if result['success']:
            del user_states[telegram_id]
            await message.answer(f"<b>Registration successful!</b>\n\nAccount: <b>{state['username']}</b>\n\nYou are now logged in. Use /start to see available commands.", parse_mode="HTML")
        else:
            del user_states[telegram_id]
            if result['error'] == 'username_taken': 
                await message.answer("Username is already taken. Try /start again.")
            else: 
                await message.answer("Registration failed. Try /start again.")
        return
    
    elif action == "login_username":
        user_states[telegram_id] = {"action": "login_password", "username": message.text.strip()}
        await message.answer("Enter your password:")
        return
    
    elif action == "login_password":
        result = await db.login_user(telegram_id, state['username'], message.text.strip())
        if result['success']:
            del user_states[telegram_id]
            await message.answer(f"<b>Login successful!</b>\n\nAccount: <b>{state['username']}</b>\n\nUse /start to see available commands.", parse_mode="HTML")
        else:
            del user_states[telegram_id]
            await message.answer("Login failed. Invalid username or password. Try /start again.")
        return

    if not await db.is_user_logged_in(telegram_id):
        return await message.answer("Please login first using /start")
    
    user_id = await get_current_user_id(telegram_id)

    if action == "search":
        query = message.text
        default = await db.accounts.find_one({"user_id": user_id, "is_default": True}) \
                  or await db.accounts.find_one({"user_id": user_id})
        if not default:
            await message.answer("No account connected. Use /addaccount")
            del user_states[telegram_id]
            return
        
        provider = default.get('provider', 'gmail')
        try:
            if provider == 'gmail':
                service = get_gmail_service(default['access_token'], default.get('refresh_token'), default.get('expires_at'))
                results = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
                messages = results.get('messages', [])
                if not messages:
                    await message.answer(f"No results for '<b>{escape_html(query)}</b>'", parse_mode="HTML")
                else:
                    await message.answer(f"🔍 Found <b>{len(messages)}</b> result(s) for '<b>{escape_html(query)}</b>'", parse_mode="HTML")
                    for idx, msg in enumerate(messages, 1):
                        msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()
                        headers = msg_detail.get('payload', {}).get('headers', [])
                        from_addr = escape_html(get_header(headers, 'From'))
                        subject = escape_html(get_header(headers, 'Subject'))
                        cb_hash = await store_email_data(str(default['_id']), msg['id'], msg.get('threadId'))
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text=f"📖 View #{idx}", callback_data=f"email:{cb_hash}")
                        ]])
                        await message.answer(f"<b>{idx}.</b> <b>From:</b> {from_addr}\n<b>Subject:</b> {subject}",
                                             parse_mode="HTML", reply_markup=keyboard)
            else: # Microsoft
                data = await call_ms_graph(f"me/messages?$search=\"{query}\"&$top=10&$select=id,subject,from", default)
                messages = data.get('value', [])
                if not messages:
                    await message.answer(f"No results for '<b>{escape_html(query)}</b>'", parse_mode="HTML")
                else:
                    await message.answer(f"🔍 Found <b>{len(messages)}</b> result(s) for '<b>{escape_html(query)}</b>'", parse_mode="HTML")
                    for idx, msg in enumerate(messages, 1):
                        from_addr = escape_html(msg.get('from', {}).get('emailAddress', {}).get('name', 'Unknown'))
                        subject = escape_html(msg.get('subject', '(No Subject)'))
                        cb_hash = await store_email_data(str(default['_id']), msg['id'], msg.get('conversationId'))
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text=f"📖 View #{idx}", callback_data=f"email:{cb_hash}")
                        ]])
                        await message.answer(f"<b>{idx}.</b> <b>From:</b> {from_addr}\n<b>Subject:</b> {subject}",
                                             parse_mode="HTML", reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Search error: {e}")
            await message.answer(f"Search failed: {str(e)}")
        del user_states[telegram_id]

    elif action == "compose_to":
        user_states[telegram_id] = {"action": "compose_subject", "to": message.text}
        await message.answer("📝 Enter email subject:")

    elif action == "compose_subject":
        user_states[telegram_id].update({"action": "compose_body", "subject": message.text})
        await message.answer("✍️ Enter email body:")

    elif action == "compose_body":
        default = await db.accounts.find_one({"user_id": user_id, "is_default": True}) \
                  or await db.accounts.find_one({"user_id": user_id})
        if not default:
            await message.answer("No account connected. Use /addaccount")
            del user_states[telegram_id]
            return
            
        provider = default.get('provider', 'gmail')
        try:
            if provider == 'gmail':
                service = get_gmail_service(default['access_token'], default.get('refresh_token'), default.get('expires_at'))
                msg = MIMEText(message.text)
                msg['to'] = state['to']
                msg['subject'] = state['subject']
                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                service.users().messages().send(userId='me', body={'raw': raw}).execute()
            else: # Microsoft
                payload = {
                    "message": {
                        "subject": state['subject'],
                        "body": {"contentType": "Text", "content": message.text},
                        "toRecipients": [{"emailAddress": {"address": state['to']}}]
                    }
                }
                await call_ms_graph("me/sendMail", default, method="POST", json_data=payload)
                
            await message.answer(f"✅ Email sent to <b>{escape_html(state['to'])}</b>", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Send error: {e}")
            await message.answer(f"Failed to send: {str(e)}")
        del user_states[telegram_id]

    elif action == "reply":
        account = await db.accounts.find_one({"_id": ObjectId(state['account_id'])})
        if not account:
            await message.answer("Account not found")
            del user_states[telegram_id]
            return
            
        provider = account.get('provider', 'gmail')
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                email_message = MIMEText(message.text)
                email_message['to'] = state['reply_to']
                email_message['subject'] = state['subject']
                raw = base64.urlsafe_b64encode(email_message.as_bytes()).decode()
                send_body = {'raw': raw}
                if state.get('thread_id'):
                    send_body['threadId'] = state['thread_id']
                service.users().messages().send(userId='me', body=send_body).execute()
            else: # Microsoft
                payload = {
                    "message": {
                        "toRecipients": [{"emailAddress": {"address": state['reply_to']}}],
                        "comment": message.text
                    }
                }
                await call_ms_graph(f"me/messages/{state['message_id']}/reply", account, method="POST", json_data=payload)

            await message.answer(f"✅ Reply sent to <b>{escape_html(state['reply_to'])}</b>!", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Reply error: {e}")
            await message.answer(f"Failed to send reply: {str(e)}")
        del user_states[telegram_id]

    elif action == "forward_to":
        user_states[telegram_id].update({"action": "forward_body", "forward_recipient": message.text})
        await message.answer("✍️ Add a note (or type 'skip'):")

    elif action == "forward_body":
        account = await db.accounts.find_one({"_id": ObjectId(state['account_id'])})
        if not account:
            await message.answer("Account not found")
            del user_states[telegram_id]
            return
            
        provider = account.get('provider', 'gmail')
        additional = message.text if message.text.lower() != 'skip' else ''
        
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                original_msg = service.users().messages().get(userId='me', id=state['forward_msg_id']).execute()
                headers = original_msg.get('payload', {}).get('headers', [])
                subject = get_header(headers, 'Subject')
                original_body = get_email_body(original_msg.get('payload', {}))
                full_body = f"{additional}\n\n---------- Forwarded message ----------\n{original_body}"
                email_message = MIMEText(full_body)
                email_message['to'] = state['forward_recipient']
                email_message['subject'] = f"Fwd: {subject}"
                raw = base64.urlsafe_b64encode(email_message.as_bytes()).decode()
                service.users().messages().send(userId='me', body={'raw': raw}).execute()
            else: # Microsoft
                payload = {
                    "toRecipients": [{"emailAddress": {"address": state['forward_recipient']}}],
                    "comment": additional
                }
                await call_ms_graph(f"me/messages/{state['forward_msg_id']}/forward", account, method="POST", json_data=payload)

            await message.answer(f"✅ Forwarded to <b>{escape_html(state['forward_recipient'])}</b>!", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Forward error: {e}")
            await message.answer(f"Failed to forward: {str(e)}")
        del user_states[telegram_id]

# ============= CALLBACK HANDLERS =============

async def handle_callback(callback: CallbackQuery):
    await callback.answer()
    data = callback.data
    telegram_id = callback.from_user.id

    if data == "auth_register":
        user_states[telegram_id] = {"action": "register_username"}
        await callback.message.edit_text("<b>Registration</b>\n\nEnter your desired username:", parse_mode="HTML")
        return
    elif data == "auth_login":
        user_states[telegram_id] = {"action": "login_username"}
        await callback.message.edit_text("<b>Login</b>\n\nEnter your username:", parse_mode="HTML")
        return

    if not await db.is_user_logged_in(telegram_id):
        await callback.answer("Please login first using /start", show_alert=True)
        return
    
    user_id = await get_current_user_id(telegram_id)

    if data.startswith("acc:"):
        account_id = data.split(":", 1)[1]
        account = await db.accounts.find_one({"_id": ObjectId(account_id)})
        if not account:
            await callback.message.edit_text("Account not found.")
            return

        is_default = account.get('is_default', False)
        notif_enabled = account.get('notifications_enabled', True)
        token_valid = account.get('token_valid', True)
        provider = account.get('provider', 'gmail')

        status_line = "🟢 Active" if token_valid else "🔴 Token expired — use /addaccount or /addoutlook to re-connect"
        prov_icon = "Ⓜ️" if provider == "microsoft" else "📧"
        
        text = (
            f"{prov_icon} <b>Account Details</b>\n\n"
            f"<b>Email:</b> {escape_html(account['email'])}\n"
            f"<b>Type:</b> {provider.capitalize()}\n"
            f"<b>Status:</b> {status_line}\n"
            f"<b>Default:</b> {'Yes ⭐' if is_default else 'No'}\n"
            f"<b>Notifications:</b> {'On 🔔' if notif_enabled else 'Off 🔕'}"
        )

        keyboard = []
        if not is_default:
            keyboard.append([InlineKeyboardButton(text="⭐ Set as Default", callback_data=f"default:{account_id}")])
        keyboard.append([InlineKeyboardButton(
            text=f"{'🔕 Disable' if notif_enabled else '🔔 Enable'} Notifications",
            callback_data=f"notif:{account_id}"
        )])
        if not token_valid:
            keyboard.append([InlineKeyboardButton(text="🔄 Re-check Token", callback_data=f"recheck:{account_id}")])
        keyboard.append([InlineKeyboardButton(text="🗑 Delete Account", callback_data=f"del_acc:{account_id}")])
        keyboard.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back_settings")])

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

    elif data.startswith("recheck:"):
        account_id = data.split(":", 1)[1]
        account = await db.accounts.find_one({"_id": ObjectId(account_id)})
        if account:
            is_valid = await check_token_validity(account)
            await callback.answer(f"{'✅ Token is valid!' if is_valid else '❌ Still invalid — re-connect'}", show_alert=True)
        callback.data = f"acc:{account_id}"
        await handle_callback(callback)

    elif data.startswith("default:"):
        account_id = data.split(":", 1)[1]
        await db.accounts.update_many({"user_id": user_id}, {"$set": {"is_default": False}})
        await db.accounts.update_one({"_id": ObjectId(account_id)}, {"$set": {"is_default": True}})
        await callback.answer("✅ Default account updated!")
        callback.data = f"acc:{account_id}"
        await handle_callback(callback)

    elif data.startswith("notif:"):
        account_id = data.split(":", 1)[1]
        account = await db.accounts.find_one({"_id": ObjectId(account_id)})
        new_state = not account.get('notifications_enabled', True)
        await db.accounts.update_one({"_id": ObjectId(account_id)}, {"$set": {"notifications_enabled": new_state}})
        callback.data = f"acc:{account_id}"
        await handle_callback(callback)

    elif data.startswith("del_acc:"):
        account_id = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, Delete", callback_data=f"del_yes:{account_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"del_no:{account_id}")
        ]])
        await callback.message.edit_text("⚠️ Are you sure you want to delete this account?", reply_markup=keyboard)

    elif data.startswith("del_yes:"):
        account_id = data.split(":", 1)[1]
        await db.delete_account(account_id)
        await callback.message.edit_text("✅ Account deleted successfully!")

    elif data.startswith("del_no:"):
        await callback.message.edit_text("Deletion cancelled.")

    elif data == "back_settings":
        await callback.message.delete()
        fake_msg = callback.message
        fake_msg.from_user = callback.from_user
        await cmd_settings(fake_msg)

    elif data.startswith("acc_page:"):
        page = int(data.split(":", 1)[1])
        skip = page * 10
        
        active_accounts = await db.accounts.find({"user_id": user_id, "token_valid": True}).skip(skip).to_list(length=10)
        total_active = await db.accounts.count_documents({"user_id": user_id, "token_valid": True})
        default = await db.accounts.find_one({"user_id": user_id, "is_default": True})
        
        if not default and active_accounts:
            default = active_accounts[0]

        lines = [f"⚙️ <b>Account Settings</b>\n"]
        if default:
            lines.append(f"<b>⭐ Default:</b> <code>{escape_html(default['email'])}</code>")
        lines.append(f"<b>🟢 Active Connected:</b> {total_active} (Page {page + 1})\n")
        lines.append("<i>Tap an account below to manage:</i>")
        text = "\n".join(lines)

        keyboard = []
        for acc in active_accounts:
            is_default = (default and acc.get('_id') == default.get('_id'))
            notifs_off = not acc.get('notifications_enabled', True)
            
            prefix = "⭐" if is_default else ("Ⓜ️" if acc.get('provider') == 'microsoft' else "📧")
            suffix = " 🔕" if notifs_off else ""
            label = f"{prefix} {acc['email']}{suffix}"
            
            keyboard.append([InlineKeyboardButton(text=label, callback_data=f"acc:{str(acc['_id'])}")])

        pagination = []
        if page > 0:
            pagination.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"acc_page:{page-1}"))
        if skip + 10 < total_active:
            pagination.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"acc_page:{page+1}"))
        if pagination:
            keyboard.append(pagination)

        inactive_count = await db.accounts.count_documents({"user_id": user_id, "token_valid": False})
        if inactive_count > 0:
            keyboard.append([InlineKeyboardButton(text=f"⚠️ Action Required: {inactive_count} Inactive", callback_data="inactive_list")])

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

    elif data == "inactive_list":
        accounts = await db.accounts.find({"user_id": user_id, "token_valid": False}).to_list(length=None)
        if not accounts:
            await callback.answer("No inactive accounts found.", show_alert=True)
            return

        text = (
            f"❌ <b>Inactive Accounts ({len(accounts)})</b>\n\n"
            "The tokens for these accounts have expired or been revoked. "
            "You can clear them and reconnect to resume service.\n\n"
        )
        for acc in accounts:
            text += f"• <code>{escape_html(acc['email'])}</code>\n"

        keyboard = [
            [InlineKeyboardButton(text="🗑 Clear All Inactive", callback_data="clear_inactive")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="back_settings")]
        ]
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

    elif data == "clear_inactive":
        try:
            deleted_count = await db.clear_inactive_accounts(user_id)
            await callback.answer(f"✅ Cleared {deleted_count} inactive accounts!", show_alert=True)
        except AttributeError:
            logger.error("clear_inactive_accounts not found in database.py")
            await callback.answer("⚠️ Error: Please update database.py first!", show_alert=True)
            return
            
        await callback.message.delete()
        fake_msg = callback.message
        fake_msg.from_user = callback.from_user
        await cmd_settings(fake_msg)

    elif data.startswith("email:"):
        cb_hash = data.split(":", 1)[1]
        email_data = await get_email_data(cb_hash)
        if not email_data:
            await callback.message.answer("Email data not found or expired.")
            return

        account = await db.accounts.find_one({"_id": ObjectId(email_data['account_id'])})
        if not account:
            await callback.message.answer("Account not found.")
            return

        provider = account.get('provider', 'gmail')
        
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                msg = service.users().messages().get(userId='me', id=email_data['message_id']).execute()
                headers = msg.get('payload', {}).get('headers', [])

                from_addr = escape_html(get_header(headers, 'From'))
                to_addr = escape_html(get_header(headers, 'To'))
                subject = escape_html(get_header(headers, 'Subject'))
                date = escape_html(get_header(headers, 'Date'))

                body = get_email_body(msg.get('payload', {}))
                is_unread = 'UNREAD' in msg.get('labelIds', [])
                
            else: # Microsoft
                msg = await call_ms_graph(f"me/messages/{email_data['message_id']}", account)
                
                from_addr = escape_html(msg.get('from', {}).get('emailAddress', {}).get('name', '') + " <" + msg.get('from', {}).get('emailAddress', {}).get('address', '') + ">")
                to_addr = escape_html(", ".join([r.get('emailAddress', {}).get('address', '') for r in msg.get('toRecipients', [])]))
                subject = escape_html(msg.get('subject', '(No Subject)'))
                date = escape_html(msg.get('receivedDateTime', ''))
                
                body_content = msg.get('body', {}).get('content', '')
                if msg.get('body', {}).get('contentType') == 'html':
                    body = html_to_telegram(body_content)
                else:
                    body = body_content
                    
                is_unread = not msg.get('isRead', True)

            body_escaped = escape_html(body[:1500] + ('…' if len(body) > 1500 else ''))

            text = (
                f"📧 <b>Email Details</b>\n\n"
                f"<b>From:</b> {from_addr}\n"
                f"<b>To:</b> {to_addr}\n"
                f"<b>Subject:</b> {subject}\n"
                f"<b>Date:</b> {date}\n\n"
                f"{body_escaped}"
            )

            keyboard = [
                [
                    InlineKeyboardButton(text="📖 Mark Read" if is_unread else "📧 Mark Unread", callback_data=f"mr:{cb_hash}"),
                    InlineKeyboardButton(text="🗑 Delete", callback_data=f"del:{cb_hash}")
                ],
                [
                    InlineKeyboardButton(text="🌐 View on Web", url=f"{REDIRECT_URI.replace('/oauth_callback', '')}/view/{cb_hash}"),
                    InlineKeyboardButton(text="↩️ Reply", callback_data=f"rep:{cb_hash}")
                ],
                [InlineKeyboardButton(text="➡️ Forward", callback_data=f"fwd:{cb_hash}")],
                [InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data="back_inbox")]
            ]

            await callback.message.edit_text(text, parse_mode="HTML",
                                              reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        except Exception as e:
            logger.error(f"Error loading email: {e}")
            await callback.message.answer(f"Error loading email: {str(e)}")

    elif data.startswith("mr:"):
        cb_hash = data.split(":", 1)[1]
        email_data = await get_email_data(cb_hash)
        if not email_data: return
        account = await db.accounts.find_one({"_id": ObjectId(email_data['account_id'])})
        provider = account.get('provider', 'gmail')
        
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                msg = service.users().messages().get(userId='me', id=email_data['message_id']).execute()
                is_unread = 'UNREAD' in msg.get('labelIds', [])
                if is_unread:
                    service.users().messages().modify(userId='me', id=email_data['message_id'], body={'removeLabelIds': ['UNREAD']}).execute()
                else:
                    service.users().messages().modify(userId='me', id=email_data['message_id'], body={'addLabelIds': ['UNREAD']}).execute()
            else: # Microsoft
                msg = await call_ms_graph(f"me/messages/{email_data['message_id']}?$select=isRead", account)
                is_unread = not msg.get('isRead', True)
                await call_ms_graph(f"me/messages/{email_data['message_id']}", account, method="PATCH", json_data={"isRead": is_unread})
                
            callback.data = f"email:{cb_hash}"
            await handle_callback(callback)
        except Exception as e:
            logger.error(f"Mark error: {e}")

    elif data.startswith("del:"):
        cb_hash = data.split(":", 1)[1]
        email_data = await get_email_data(cb_hash)
        if not email_data: return
        account = await db.accounts.find_one({"_id": ObjectId(email_data['account_id'])})
        provider = account.get('provider', 'gmail')
        
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                service.users().messages().trash(userId='me', id=email_data['message_id']).execute()
            else: # Microsoft
                await call_ms_graph(f"me/messages/{email_data['message_id']}", account, method="DELETE")
                
            await callback.message.edit_text("✅ Email moved to trash")
        except Exception as e:
            logger.error(f"Delete error: {e}")

    elif data.startswith("rep:"):
        cb_hash = data.split(":", 1)[1]
        email_data = await get_email_data(cb_hash)
        if not email_data: return
        account = await db.accounts.find_one({"_id": ObjectId(email_data['account_id'])})
        provider = account.get('provider', 'gmail')
        
        try:
            if provider == 'gmail':
                service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                msg = service.users().messages().get(userId='me', id=email_data['message_id']).execute()
                headers = msg.get('payload', {}).get('headers', [])
                from_addr = get_header(headers, 'From')
                subject = get_header(headers, 'Subject')
            else: # Microsoft
                msg = await call_ms_graph(f"me/messages/{email_data['message_id']}?$select=subject,from", account)
                from_addr = msg.get('from', {}).get('emailAddress', {}).get('address', '')
                subject = msg.get('subject', '')
                
            user_states[telegram_id] = {
                "action": "reply",
                "account_id": email_data['account_id'],
                "reply_to": from_addr,
                "subject": f"Re: {subject}",
                "thread_id": email_data.get('thread_id'),
                "message_id": email_data['message_id']
            }
            await callback.message.answer(f"↩️ Replying to: <b>{escape_html(from_addr)}</b>\n\n✍️ Enter your reply:", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Reply setup error: {e}")

    elif data.startswith("fwd:"):
        cb_hash = data.split(":", 1)[1]
        email_data = await get_email_data(cb_hash)
        if not email_data: return
        user_states[telegram_id] = {
            "action": "forward_to",
            "account_id": email_data['account_id'],
            "forward_msg_id": email_data['message_id']
        }
        await callback.message.answer("➡️ Enter recipient email address:")

    elif data == "back_inbox":
        await callback.message.delete()
        fake_msg = callback.message
        fake_msg.from_user = callback.from_user
        await cmd_inbox(fake_msg)

# ============= NOTIFICATION SYSTEMS =============

async def check_new_emails():
    """Checks for new emails for all VALID accounts with notifications enabled."""
    while True:
        try:
            await asyncio.sleep(NOTIFICATION_CHECK_INTERVAL)
            # CRITICAL FIX: Only check accounts where token_valid is True
            accounts = await db.accounts.find({
                "notifications_enabled": True, 
                "token_valid": True
            }).to_list(length=None)

            for account in accounts:
                try:
                    account_id = str(account['_id'])
                    internal_user_id = int(account['user_id'])
                    telegram_id = await db.get_active_telegram_id(internal_user_id)
                    provider = account.get('provider', 'gmail')
                    
                    if not telegram_id: continue

                    if provider == 'gmail':
                        service = get_gmail_service(account['access_token'], account.get('refresh_token'), account.get('expires_at'))
                        results = service.users().messages().list(userId='me', maxResults=5, q='is:unread').execute()

                        await db.mark_account_valid(account_id)

                        for msg in results.get('messages', []):
                            if await db.is_email_notified(internal_user_id, account_id, msg['id']):
                                continue

                            msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()
                            headers = msg_detail.get('payload', {}).get('headers', [])
                            from_addr = escape_html(get_header(headers, 'From'))
                            subject = escape_html(get_header(headers, 'Subject'))
                            snippet = escape_html(msg_detail.get('snippet', '')[:100])

                            text = (
                                f"📧 <b>New Email — {escape_html(account['email'])}</b>\n\n"
                                f"<b>From:</b> {from_addr}\n"
                                f"<b>Subject:</b> {subject}\n"
                                f"<b>Preview:</b> {snippet}…"
                            )
                            cb_hash = await store_email_data(account_id, msg['id'], msg.get('threadId'))
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="📖 View", callback_data=f"email:{cb_hash}")
                            ]])
                            await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
                            
                            await db.add_email_to_history(
                                internal_user_id, account_id, msg['id'],
                                thread_id=msg.get('threadId'),
                                subject=get_header(msg_detail.get('payload', {}).get('headers', []), 'Subject'),
                                from_addr=get_header(msg_detail.get('payload', {}).get('headers', []), 'From'),
                                snippet=msg_detail.get('snippet', '')[:100],
                                internal_date=int(msg_detail.get('internalDate') or 0),
                                unread='UNREAD' in msg_detail.get('labelIds', []),
                                account_email=account['email']
                            )
                            
                    else: # Microsoft
                        data = await call_ms_graph("me/messages?$filter=isRead eq false&$top=5&$select=id,conversationId,subject,from,bodyPreview,receivedDateTime", account)
                        
                        await db.mark_account_valid(account_id)
                        
                        for msg in data.get('value', []):
                            if await db.is_email_notified(internal_user_id, account_id, msg['id']):
                                continue
                                
                            from_addr = escape_html(msg.get('from', {}).get('emailAddress', {}).get('name', 'Unknown'))
                            subject = escape_html(msg.get('subject', '(No Subject)'))
                            snippet = escape_html(msg.get('bodyPreview', '')[:100])
                            
                            text = (
                                f"Ⓜ️ <b>New Outlook Email — {escape_html(account['email'])}</b>\n\n"
                                f"<b>From:</b> {from_addr}\n"
                                f"<b>Subject:</b> {subject}\n"
                                f"<b>Preview:</b> {snippet}…"
                            )
                            cb_hash = await store_email_data(account_id, msg['id'], msg.get('conversationId'))
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="📖 View", callback_data=f"email:{cb_hash}")
                            ]])
                            await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
                            
                            try:
                                dt_obj = datetime.strptime(msg.get('receivedDateTime')[:19], "%Y-%m-%dT%H:%M:%S")
                                ms_internal_date = int(dt_obj.replace(tzinfo=timezone.utc).timestamp() * 1000)
                            except:
                                ms_internal_date = int(time.time() * 1000)
                                
                            await db.add_email_to_history(
                                internal_user_id, account_id, msg['id'],
                                thread_id=msg.get('conversationId'),
                                subject=msg.get('subject', ''),
                                from_addr=msg.get('from', {}).get('emailAddress', {}).get('address', ''),
                                snippet=msg.get('bodyPreview', '')[:100],
                                internal_date=ms_internal_date,
                                unread=True,
                                account_email=account['email']
                            )

                except Exception as e:
                    error_str = str(e).lower()
                    if 'invalid_grant' in error_str or 'token' in error_str or '401' in error_str:
                        await db.mark_account_invalid(str(account['_id']))
                        logger.warning(f"Token invalid for {account.get('email')}, marked invalid.")
                        try:
                            if telegram_id:
                                await bot.send_message(
                                    telegram_id, 
                                    f"⚠️ <b>Account Disconnected</b>\n\nThe token for <b>{escape_html(account.get('email', 'your account'))}</b> has expired.\n\nIt has been moved to your Inactive list. Please reconnect.",
                                    parse_mode="HTML"
                                )
                        except Exception as send_err:
                            logger.error(f"Failed to alert user {internal_user_id}: {send_err}")
                    else:
                        logger.error(f"Email check error for {account.get('email')}: {e}")

        except Exception as e:
            logger.error(f"Notification loop error: {e}")

async def daily_token_check():
    """Runs every 24 hours to check validity of ALL active accounts."""
    while True:
        try:
            await asyncio.sleep(86400) # 24 hours
            logger.info("Running 24h background token check...")
            
            # CRITICAL FIX
            accounts = await db.accounts.find({"token_valid": True}).to_list(length=None)
            
            for account in accounts:
                try:
                    account_id = str(account['_id'])
                    internal_user_id = int(account['user_id'])
                    telegram_id = await db.get_active_telegram_id(internal_user_id)
                    provider = account.get('provider', 'gmail')
                    
                    if not telegram_id: continue
                    
                    if provider == 'gmail':
                        service = get_gmail_service(
                            account['access_token'], 
                            account.get('refresh_token'), 
                            account.get('expires_at')
                        )
                        service.users().getProfile(userId='me').execute()
                    else: # Microsoft
                        await call_ms_graph("me", account)
                    
                except Exception as e:
                    await db.mark_account_invalid(account_id)
                    try:
                        if telegram_id:
                            await bot.send_message(
                                telegram_id, 
                                f"⚠️ <b>Account Disconnected</b>\n\nAuthentication failed for <b>{escape_html(account.get('email', 'your account'))}</b>.\nIt has been moved to your Inactive list. Please reconnect.",
                                parse_mode="HTML"
                            )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Daily token loop error: {e}")

# ============= MAIN =============

async def main():
    global bot, db, dp, oauth_states, user_states

    logger.info("Starting bot...")
    bot = Bot(token=BOT_TOKEN)
    db = Database()
    dp = Dispatcher()

    await bot.set_my_commands([
        BotCommand(command="start", description="Start bot"),
        BotCommand(command="inbox", description="View latest emails"),
        BotCommand(command="compose", description="Compose new email"),
        BotCommand(command="search", description="Search emails"),
        BotCommand(command="addaccount", description="Add Gmail account"),
        BotCommand(command="addoutlook", description="Add Outlook/Hotmail account"),
        BotCommand(command="settings", description="Settings & manage accounts"),
        BotCommand(command="logout", description="Logout from account"),
    ])

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_inbox, Command("inbox"))
    dp.message.register(cmd_settings, Command("settings"))
    dp.message.register(cmd_add, Command("addaccount"))
    dp.message.register(cmd_add_outlook, Command("addoutlook"))
    dp.message.register(cmd_search, Command("search"))
    dp.message.register(cmd_compose, Command("compose"))
    dp.message.register(cmd_logout, Command("logout"))
    dp.message.register(handle_user_input, F.text)
    dp.callback_query.register(handle_callback)

    web_module.setup_web_module(bot, db, oauth_states, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
    app = web_module.create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

    asyncio.create_task(check_new_emails())
    asyncio.create_task(daily_token_check())
    
    logger.info("Bot started successfully!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
