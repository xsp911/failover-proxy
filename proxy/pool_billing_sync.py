"""Request-specific A6 consumption reconciliation; never infer spend from caps."""
import asyncio
import time
from datetime import datetime,timezone

from .a6_client import A6Error


def today():return datetime.now(timezone.utc).date().isoformat()


async def settle_request(store,client,reservation_id,request_id,token_id,attempts=3):
    if not request_id:return False
    await asyncio.to_thread(store.bind,reservation_id,request_id)
    last_reason='awaiting_consumption'
    for delay in (0,.5,1)[:max(1,min(attempts,3))]:
        if delay:await asyncio.sleep(delay)
        try:
            rows=await client.logs(request_id,token_id)
            matched=[r for r in rows if r.get('request_id')==request_id and r.get('token_id')==token_id and r.get('type') in (2,5) and type(r.get('quota')) is int and r['quota']>=0]
            # Ambiguous logs are unresolved, not summed or guessed. A consume
            # record wins over an error row for the exact same request.
            consume=[r for r in matched if r['type']==2]
            # Error events are not billing finality: A6 may publish a consume
            # record later for the same request. Never release its reserve on
            # a provisional type5/quota0 row; keep polling for type2 evidence.
            choices=consume
            if not choices:
                last_reason='error_log_only' if matched else 'consume_log_missing'
                continue
            if len({r['quota'] for r in choices})!=1:
                last_reason='ambiguous_consumption';break
            unit=await client.quota_per_unit()
            await asyncio.to_thread(store.settle,reservation_id,request_id,token_id,choices[0]['quota'],unit)
            return True
        except A6Error as error:last_reason=error.kind
    await asyncio.to_thread(store.defer_reconcile,reservation_id,last_reason)
    return False


async def reconcile_pending(manager):
    pending=await asyncio.to_thread(manager.billing.due_reconcile,20)
    if not pending:return
    client=manager.client()
    try:
        for item in pending:
            if not item.get('request_id'):continue
            try:await settle_request(manager.billing,client,item['reservation_id'],item['request_id'],item['token_id'],attempts=1)
            except (ValueError,A6Error):
                await asyncio.to_thread(manager.billing.defer_reconcile,item['reservation_id'],'billing_reconcile_failed')
    finally:await client.close()
