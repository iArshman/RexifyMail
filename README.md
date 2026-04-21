# 📬 Rexify Mail Bot

### The Ultimate Bridge Between Gmail, Outlook & Telegram

Rexify Mail Bot lets you securely manage **Gmail and Microsoft Outlook accounts directly from Telegram** using official OAuth2 authentication — no app passwords required.

Includes inbox browsing, search, compose, attachments, multi-account switching, notifications, and a lightweight web email viewer.

---

# 🌟 Key Features

* Private Telegram-bound login system
* Google OAuth2 (Gmail API)
* Microsoft OAuth2 (Graph API)
* Secure MongoDB token storage
* Automatic token refresh handling

No passwords are stored. Ever.

---

# 📥 Multi-Cloud Inbox Support

Supported providers:

| Provider         | API                 |
| ---------------- | ------------------- |
| Gmail            | Google Gmail API    |
| Google Workspace | Gmail API           |
| Outlook          | Microsoft Graph API |
| Hotmail          | Microsoft Graph API |
| Live             | Microsoft Graph API |

Features:

* connect multiple accounts
* switch default account
* remove accounts anytime
* unified Telegram control panel

---

# 🤖 Telegram Bot Features

## Inbox Management

* View latest emails
* Pagination support
* Mark read / unread
* Delete emails
* View sender preview

---

## Compose Emails

Send emails directly from Telegram:

* New message
* Reply
* Forward
* Attachment support

Works for:

* Gmail
* Outlook

---

## 🔍 Smart Search

Search across providers using:

* subject
* sender
* keywords

Supported on:

* Gmail API search
* Microsoft Graph search

---

## 🔔 Notifications

Real-time email alerts

Per-account controls:

* enable notifications
* disable notifications
* polling refresh support

---

# 🌐 Web Mail Viewer

Rexify runs a lightweight **aiohttp web interface**

Routes:

| Route             | Description             |
| ----------------- | ----------------------- |
| `/`               | Landing page            |
| `/oauth_callback` | Google OAuth return     |
| `/ms_callback`    | Microsoft OAuth return  |
| `/mailbox`        | Secure dashboard        |
| `/view/{hash}`    | Sandboxed email preview |

Features:

* safe iframe rendering
* HTML email preview
* attachment-safe display
* mobile friendly

Runs on:

```
http://localhost:3000
```

or your deployed domain

---

# 🏗 Project Structure

```
rexify-main/
│
├── main.py        Telegram bot logic
├── web.py         OAuth callbacks + web viewer
├── database.py    MongoDB token storage
├── config.py      environment loader
├── Dockerfile
├── requirements.txt
└── .env
```

---

# 🚀 Quick Start

## Requirements

| Tool                 | Version                 |
| -------------------- | ----------------------- |
| Python               | 3.9+                    |
| MongoDB              | Atlas / Local           |
| Google Cloud Project | Gmail API enabled       |
| Azure App            | Microsoft Graph enabled |

---

# 🛠 Installation

Clone repository

```bash
git clone https://github.com/iarshman/rexify-gmail-bot.git
cd rexify-gmail-bot
```

Install dependencies

```bash
pip install -r requirements.txt
```

Create environment file

```bash
cp .env.example .env
```

Run bot

```bash
python main.py
```

---

# ⚙️ Environment Variables

## TELEGRAM

```
BOT_TOKEN=your_bot_token
```

---

## GOOGLE (GMAIL)

```
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
REDIRECT_URI=https://your-domain.com/oauth_callback
```

---

## MICROSOFT (OUTLOOK)

```
MS_CLIENT_ID=your_azure_client_id
MS_CLIENT_SECRET=your_azure_secret
MS_REDIRECT_URI=https://your-domain.com/ms_callback
```

---

## DATABASE

```
MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/
```

---

# 📖 Bot Commands

| Command     | Action                   |
| ----------- | ------------------------ |
| /start      | Login or open dashboard  |
| /inbox      | View latest emails       |
| /compose    | Send new email           |
| /search     | Search Gmail & Outlook   |
| /addaccount | Connect Gmail account    |
| /addoutlook | Connect Outlook account  |
| /settings   | Notification preferences |
| /logout     | Logout session           |

---

# 🐳 Docker Deployment

Build image

```
docker build -t rexify-mail .
```

Run container

```
docker run -d \
--name rexify-instance \
-p 3000:3000 \
--env-file .env \
--restart unless-stopped \
rexify-mail
```

---

# 🔒 Security Model

## Token Safety

OAuth tokens stored securely inside MongoDB

Auto-refresh enabled

---

## Telegram Identity Binding

Each connected mailbox is locked to its Telegram user

Prevents spoofing

---

## Safe Email Rendering

HTML emails displayed inside sandbox viewer

Protects against:

* XSS
* malicious embeds
* tracking scripts

---

# ❤️ Built for Speed & Privacy

Created by **Arshman**
