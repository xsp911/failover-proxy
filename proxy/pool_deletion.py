"""Explicit pool deletion parks owned remote routes, retains local deny tombstone."""
from .platform_io import private_mode
import asyncio
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from .a6_client import A6Error
from .pool_manager import PoolError


class DeletedRoutes:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'a6_deleted_routes.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path) as c:c.execute('CREATE TABLE IF NOT EXISTS denied (model TEXT,key_hash TEXT,created REAL,PRIMARY KEY(model,key_hash))')
    def deny(self,model,secret):
        digest=hashlib.sha256(secret.removeprefix('sk-').encode()).hexdigest()
        with sqlite3.connect(self.path) as c:c.execute('INSERT OR REPLACE INTO denied VALUES(?,?,?)',(model,digest,time.time()))
    def blocked(self,model,secret):
        digest=hashlib.sha256(secret.removeprefix('sk-').encode()).hexdigest()
        with sqlite3.connect(self.path) as c:return c.execute('SELECT 1 FROM denied WHERE model=? AND key_hash=?',(model,digest)).fetchone() is not None
    def clear(self,model,secret):
        digest=hashlib.sha256(secret.removeprefix('sk-').encode()).hexdigest()
        with sqlite3.connect(self.path) as c:c.execute('DELETE FROM denied WHERE model=? AND key_hash=?',(model,digest))


async def remove_pool(manager,pool,confirm_remote):
    if confirm_remote is not True:
        if pool.get('last_sync_at') or pool.get('members'):raise PoolError('pool_delete_confirmation_required',409)
        return await asyncio.to_thread(manager.db.delete,pool['id'],pool['revision'])
    client=manager.client()
    try:
        # Check before changing schedules, token or remote route. External routes
        # and pins must never be silently taken over by a destructive operation.
        pins=await client.pins()
        if any(p['token_id']==pool['token_id'] and p['model_name']==pool['model'] for p in pins):raise PoolError('existing_pin_conflicts_with_pool',409)
        routes=await client.list_subscriptions(pool['token_id'],pool['model'])
        owner=await asyncio.to_thread(manager.auto.remote,pool['id'])
        if len(routes)>1:raise PoolError('ambiguous_existing_routes',409)
        before=await client.get_subscription(routes[0]['id']) if routes else None
        if before and (not owner or owner['subscription_id']!=before['id'] or owner['config_version']!=before['config_version'] or owner['state'] not in ('owned','parked')):
            raise PoolError('auto_remote_route_conflict',409)
        secret=await client.token_secret(pool['token_id'])
        await asyncio.to_thread(manager.deleted.deny,pool['model'],secret)
        # Disable the persisted policy itself too. Scheduler.configure() must
        # not reactivate paid maintenance if a subsequent remote park fails.
        await asyncio.to_thread(manager.db.disable_for_delete,pool['id'],pool['revision'])
        await asyncio.to_thread(manager.auto.configure,pool['id'],False,time.time())
        if before:
            active=[c['channel_id'] for c in before['channels'] if not c.get('removed_at')]
            if active:
                draft=manager.route_draft(pool,[active[0]])
                draft['caps']={k:1 if k!='per_request_price_micros' else 0 for k in draft['caps']};draft['min_cache_hit_rate_bps']=None
                await asyncio.to_thread(manager.auto.save_remote,pool['id'],before['id'],before['config_version'],draft,'uncertain')
                saved=await client.save_subscription(pool['token_id'],pool['model'],draft,before)
                await asyncio.to_thread(manager.auto.save_remote,pool['id'],saved['id'],saved['config_version'],draft,'parked')
            elif before.get('fallback_enabled') is not False or before.get('auto_pool_enabled') is not False:
                raise PoolError('remote_empty_route_not_safe_to_delete',409)
        # Retain immutable evidence/billing for diagnostics; remove policy only
        # after known remote work route has been safely parked/read back.
        await asyncio.to_thread(manager.membership_history.record,pool['id'],before=[m['channel_id'] for m in pool.get('members',[])],after=[],reason='pool_deleted',source='delete',subscription_id=before['id'] if before else None,config_version=None)
        return await asyncio.to_thread(manager.db.delete,pool['id'],pool['revision'],True)
    finally:await client.close()
