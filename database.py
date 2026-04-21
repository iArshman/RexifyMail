"""
Database models and operations
"""
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict
from config import MONGO_URI, DATABASE_NAME
import hashlib

class Database:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGO_URI)
        self.db = self.client[DATABASE_NAME]
        
        # Collections
        self.users = self.db['users']
        self.accounts = self.db['gmail_accounts']
        self.auth_users = self.db['auth_users']
        self.email_history = self.db['email_history']
        self.pagination_cache = self.db['pagination_cache']
        self.callback_data = self.db['callback_data']
    
    async def create_indexes(self):
        """Create database indexes for better performance"""
        await self.users.create_index('user_id', unique=True)
        # Unique compound index prevents duplicate email per user at DB level
        await self.accounts.create_index([('user_id', 1), ('email', 1)], unique=True)
        await self.email_history.create_index([('user_id', 1), ('account_id', 1), ('message_id', 1)], unique=True)
        await self.email_history.create_index([('user_id', 1), ('internal_date', -1)])
        await self.pagination_cache.create_index('cache_key', unique=True)
        await self.callback_data.create_index('created_at', expireAfterSeconds=604800)
        await self.auth_users.create_index('username', unique=True)
        await self.auth_users.create_index([('telegram_id', 1), ('is_logged_in', 1)])
    
    # ── User Operations ───────────────────────────────────────────────────────

    async def get_user(self, user_id: int) -> Optional[Dict]:
        return await self.users.find_one({'user_id': user_id})
    
    async def create_user(self, user_id: int, username: str = None, full_name: str = None):
        user_data = {
            'user_id': user_id,
            'username': username,
            'full_name': full_name,
            'default_account_id': None,
            'notifications_enabled': True,
            'notification_frequency': 60,
            'created_at': datetime.now(timezone.utc),
            'updated_at': datetime.now(timezone.utc)
        }
        await self.users.insert_one(user_data)
        return user_data
    
    async def update_user(self, user_id: int, update_data: Dict):
        update_data['updated_at'] = datetime.now(timezone.utc)
        await self.users.update_one({'user_id': user_id}, {'$set': update_data})
    
    # ── Gmail Account Operations ──────────────────────────────────────────────

    async def account_exists(self, user_id: int, email: str) -> Optional[Dict]:
        """Return the existing account doc if this email is already saved for the user."""
        return await self.accounts.find_one({'user_id': user_id, 'email': email})

    async def add_account(self, user_id: int, email: str, tokens: Dict, provider: str = "gmail"):
        """
        Gmail or Microsoft account add 
        """
        # Search using both email and provider to avoid confusion
        existing = await self.accounts.find_one({'user_id': user_id, 'email': email, 'provider': provider})

        if existing:
            account_id = str(existing['_id'])
            await self.update_account_tokens(account_id, tokens)
            return account_id, False

        account_data = {
            'user_id': user_id,
            'email': email,
            'provider': provider,  
            'access_token': tokens['access_token'],
            'refresh_token': tokens.get('refresh_token'),
            'expires_at': tokens['expires_at'],
            'notifications_enabled': True,
            'is_default': False,
            'token_valid': True,
            'last_checked': datetime.now(timezone.utc),
            'created_at': datetime.now(timezone.utc),
            'updated_at': datetime.now(timezone.utc)
        }
        result = await self.accounts.insert_one(account_data)
        account_id = str(result.inserted_id)

        accounts_count = await self.accounts.count_documents({'user_id': user_id})
        if accounts_count == 1:
            await self.set_default_account(user_id, account_id)

        return account_id, True
    
    async def get_account(self, account_id: str) -> Optional[Dict]:
        from bson import ObjectId
        return await self.accounts.find_one({'_id': ObjectId(account_id)})
    
    async def get_user_accounts(self, user_id: int) -> List[Dict]:
        accounts = []
        async for account in self.accounts.find({'user_id': user_id}):
            account['account_id'] = str(account['_id'])
            accounts.append(account)
        return accounts
    
    async def update_account_tokens(self, account_id: str, tokens: Dict):
        from bson import ObjectId
        await self.accounts.update_one(
            {'_id': ObjectId(account_id)},
            {'$set': {
                'access_token': tokens['access_token'],
                'refresh_token': tokens.get('refresh_token'),
                'expires_at': tokens['expires_at'],
                'token_valid': True,
                'last_checked': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }}
        )

    async def mark_account_invalid(self, account_id: str):
        """Call this when a Gmail API call fails due to auth errors."""
        from bson import ObjectId
        await self.accounts.update_one(
            {'_id': ObjectId(account_id)},
            {'$set': {'token_valid': False, 'updated_at': datetime.now(timezone.utc)}}
        )

    async def mark_account_valid(self, account_id: str):
        from bson import ObjectId
        await self.accounts.update_one(
            {'_id': ObjectId(account_id)},
            {'$set': {
                'token_valid': True,
                'last_checked': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }}
        )
    
    async def delete_account(self, account_id: str):
        from bson import ObjectId
        account = await self.get_account(account_id)
        if not account:
            return
        await self.accounts.delete_one({'_id': ObjectId(account_id)})
        await self.email_history.delete_many({'user_id': account['user_id'], 'account_id': account_id})
        user = await self.get_user(account['user_id'])
        if user and user.get('default_account_id') == account_id:
            remaining = await self.get_user_accounts(account['user_id'])
            if remaining:
                await self.set_default_account(account['user_id'], remaining[0]['account_id'])
            else:
                await self.update_user(account['user_id'], {'default_account_id': None})

    async def clear_inactive_accounts(self, user_id: int) -> int:
        """Deletes all inactive accounts for a user and safely cleans up their history."""
        inactive_accounts = await self.accounts.find({'user_id': user_id, 'token_valid': False}).to_list(length=None)
        count = 0
        for acc in inactive_accounts:
            await self.delete_account(str(acc['_id'])) 
            count += 1
        return count

    async def set_default_account(self, user_id: int, account_id: str):
        from bson import ObjectId
        await self.accounts.update_many({'user_id': user_id}, {'$set': {'is_default': False}})
        await self.accounts.update_one({'_id': ObjectId(account_id)}, {'$set': {'is_default': True}})
        await self.update_user(user_id, {'default_account_id': account_id})
    
    async def toggle_account_notifications(self, account_id: str) -> bool:
        from bson import ObjectId
        account = await self.get_account(account_id)
        new_state = not account.get('notifications_enabled', True)
        await self.accounts.update_one({'_id': ObjectId(account_id)}, {'$set': {'notifications_enabled': new_state}})
        return new_state
    
    # ── Email History ─────────────────────────────────────────────────────────

    async def add_email_to_history(self, user_id: int, account_id: str, message_id: str,
                                   thread_id: str = None, subject: str = None,
                                   from_addr: str = None, snippet: str = None,
                                   internal_date: int = None, unread: bool = True,
                                   account_email: str = None):
        """Store email notification metadata so the mailbox can be served from DB cache."""
        await self.email_history.update_one(
            {'user_id': user_id, 'account_id': account_id, 'message_id': message_id},
            {'$setOnInsert': {
                'user_id': user_id,
                'account_id': account_id,
                'message_id': message_id,
                'thread_id': thread_id,
                'subject': subject,
                'from_addr': from_addr,
                'snippet': snippet,
                'internal_date': internal_date,
                'unread': unread,
                'account_email': account_email,
                'notified_at': datetime.now(timezone.utc)
            }},
            upsert=True
        )

    async def get_mailbox_emails(self, user_id: int, limit: int = 50) -> List[Dict]:
        """Return recent notified emails from cache for the mailbox page (no Gmail API call)."""
        cursor = self.email_history.find(
            {'user_id': user_id},
            sort=[('internal_date', -1)],
            limit=limit
        )
        return await cursor.to_list(length=limit)
    
    async def get_mailbox_emails_24h(self, user_id: int, page: int = 1, per_page: int = 25) -> tuple:
        """Return last 24h emails with pagination. Returns (emails, total_count, total_pages)."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
        
        # Count total emails in last 24h
        total_count = await self.email_history.count_documents({
            'user_id': user_id,
            'internal_date': {'$gte': int(cutoff_time.timestamp() * 1000)}
        })
        
        # Calculate pagination
        skip = (page - 1) * per_page
        total_pages = (total_count + per_page - 1) // per_page
        
        # Fetch paginated emails
        cursor = self.email_history.find(
            {
                'user_id': user_id,
                'internal_date': {'$gte': int(cutoff_time.timestamp() * 1000)}
            },
            sort=[('internal_date', -1)],
            skip=skip,
            limit=per_page
        )
        emails = await cursor.to_list(length=per_page)
        
        return emails, total_count, total_pages

    async def is_email_notified(self, user_id: int, account_id: str, message_id: str) -> bool:
        result = await self.email_history.find_one({'user_id': user_id, 'account_id': account_id, 'message_id': message_id})
        return result is not None
    
    # ── Pagination Cache ──────────────────────────────────────────────────────

    async def save_pagination_cache(self, cache_key: str, message_ids: List[str], metadata: Dict = None):
        await self.pagination_cache.update_one(
            {'cache_key': cache_key},
            {'$set': {'message_ids': message_ids, 'metadata': metadata or {}, 'timestamp': datetime.now(timezone.utc)}},
            upsert=True
        )
    
    async def get_pagination_cache(self, cache_key: str) -> Optional[Dict]:
        return await self.pagination_cache.find_one({'cache_key': cache_key})
    
    async def clear_old_cache(self, hours: int = 24):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        await self.pagination_cache.delete_many({'timestamp': {'$lt': cutoff}})

    # ── Email Callback Data ───────────────────────────────────────────────────

    async def store_email_callback(self, user_id: int, account_id: str, message_id: str, thread_id: str = None) -> str:
        data_str = f"{user_id}:{account_id}:{message_id}:{datetime.now(timezone.utc).timestamp()}"
        cb_hash = hashlib.md5(data_str.encode()).hexdigest()[:16]
        await self.callback_data.update_one(
            {"hash": cb_hash},
            {"$set": {"hash": cb_hash, "user_id": user_id, "account_id": account_id,
                      "message_id": message_id, "thread_id": thread_id, "created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        return cb_hash

    async def get_email_callback(self, cb_hash: str) -> Optional[Dict]:
        return await self.callback_data.find_one({"hash": cb_hash})
        
    # ── Login system functions ───────────────────────────────────────────────────

    def _hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()
    
    async def register_user(self, telegram_id: int, username: str, password: str, full_name: str = None):
        username_taken = await self.auth_users.find_one({'username': username})

        if username_taken:
            return {'success': False, 'error': 'username_taken'}

        await self.auth_users.update_many(
            {'telegram_id': telegram_id, 'is_logged_in': True},
            {'$set': {'is_logged_in': False}}
        )

        hashed_pwd = self._hash_password(password)
        internal_id = int(hashlib.sha256(username.encode()).hexdigest(), 16) % (10 ** 15)

        user_data = {
            'telegram_id': telegram_id,
            'username': username,
            'password': hashed_pwd,
            'full_name': full_name,
            'is_logged_in': True,
            'internal_user_id': internal_id,
            'created_at': datetime.now(timezone.utc),
            'last_login': datetime.now(timezone.utc)
        }

        await self.auth_users.insert_one(user_data)
        existing_user = await self.users.find_one({'user_id': internal_id})

        if not existing_user:
            await self.create_user(internal_id, username, full_name)

        return {'success': True, 'internal_user_id': internal_id}

    async def login_user(self, telegram_id: int, username: str, password: str):
        hashed_pwd = self._hash_password(password)

        user = await self.auth_users.find_one({
            'username': username,
            'password': hashed_pwd
        })

        if user:
            await self.auth_users.update_many(
                {'telegram_id': telegram_id, 'is_logged_in': True},
                {'$set': {'is_logged_in': False}}
            )

            await self.auth_users.update_one(
                {'_id': user['_id']},
                {'$set': {
                    'telegram_id': telegram_id,
                    'is_logged_in': True,
                    'last_login': datetime.now(timezone.utc)
                }}
            )

            return {'success': True, 'internal_user_id': user['internal_user_id']}

        return {'success': False}

    async def logout_user(self, telegram_id: int):
        result = await self.auth_users.update_many(
            {'telegram_id': telegram_id, 'is_logged_in': True},
            {'$set': {'is_logged_in': False}}
        )
        return result.modified_count > 0
    
    async def get_internal_user_id(self, telegram_id: int):
        user = await self.auth_users.find_one({
            'telegram_id': telegram_id,
            'is_logged_in': True
        })
        if user:
            return user['internal_user_id']
        return None

    async def is_user_logged_in(self, telegram_id: int) -> bool:
        user = await self.auth_users.find_one({'telegram_id': telegram_id, 'is_logged_in': True})
        return user is not None

    async def get_active_telegram_id(self, internal_user_id: int) -> int:
        user = await self.auth_users.find_one({'internal_user_id': internal_user_id, 'is_logged_in': True})
        if user:
            return user['telegram_id']
        return None

# Global database instance
db = Database()
