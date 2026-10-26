import asyncio
import copy
import json
import time

import httpx
import pytest

from proxy.pool_manager import PoolManager,PoolError,iso
from proxy.store import Store
from proxy.a6_settings import A6Settings
from proxy.model_monitor import ModelMonitor


class FakeClient:
    def __init__(self):
        self.rows=[];self.routes=[];self.pin_rows=[];self.writes=[]
    async def tokens(self):return [{'id':i,'name':str(i),'status':1} for i in (1,2,3,4)]
    async def close(self):pass
    async def pins(self):return self.pin_rows
    async def search(self,token,model):return [r for r in self.rows if r['model_name']==model]
    async def list_subscriptions(self,token,model):return [r for r in self.routes if r['token_id']==token and r['model_name']==model]
    async def get_subscription(self,id):return next(r for r in self.routes if r['id']==id)
    async def save_subscription(self,token,model,draft,existing=None):
        self.writes.append((token,model,copy.deepcopy(draft)))
        value={'id':existing['id'] if existing else 100+len(self.routes),'token_id':token,'model_name':model,'config_version':existing['config_version']+1 if existing else 1,**copy.deepcopy(draft)}
        if existing:self.routes.remove(existing)
        self.routes.append(value);return value
    async def token_secret(self,id):return 'not-a-real-key'


def policy(name='Pool A',model='deepseek-v4.1-flash-1017',token=1,test=2):
    return {'name':name,'token_id':token,'test_token_id':test,'model':model,'reference_model':'deepseek-v4.1-flash-1017','protocol':'chat',
        'max_input_usd':.002,'max_output_usd':.008,'max_cache_read_usd':.0001,'max_cache_write_usd':.001,'enabled':False}


def row(model='deepseek-v4.1-flash-1017',channel=123,**kw):
    return {'channel_id':channel,'listing_id':1000+channel,'supplier_name':'Supplier','model_name':model,'input_price_micros':1000,'output_price_micros':4000,'cache_read_price_micros':20,'cache_write_price_micros':0,
        'pricing_currency':'USD','pricing_unit':'per_1m_tokens','charge_type':'per_token','cache_hit_rate_24h':8000,'cache_hit_state':'rate','recent_success_rate':9900,'sample_count':100,'test_endpoint':'chat',**kw}


@pytest.fixture
def manager(tmp_path):
    store=Store(tmp_path);a6=A6Settings(store);a6.save('fake-secret-not-real-123',23984)
    monitor=ModelMonitor(store,tmp_path)
    m=PoolManager(store,a6,monitor,tmp_path)
    client=FakeClient();m.client=lambda:client
    return m,client


@pytest.mark.asyncio
async def test_multiple_model_policies_and_filtering(manager):
    m,c=manager
    one=await m.policy(policy());two=await m.policy(policy(name='Pool B',model='grok-4.7',token=3,test=4))
    assert len(m.db.list())==2 and one['id']!=two['id']
    c.rows=[row(),row(channel=124,input_price_micros=5000),row(channel=125,cache_hit_rate_24h=4000)]
    result=await m.discover(one,c)
    assert result[0]['eligible'] is True
    assert len(result)==1 and all(r['eligible'] for r in result)
    assert m.db.get(one['id'])['discovery_summary']=={'total':3,'eligible':1,'excluded':2,'shown':1}
    assert not c.writes
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_preview_refuses_unverified_channels_and_pin(manager):
    m,c=manager;p=await m.policy(policy());c.rows=[row()]
    result=await m.preview(p['id']);assert not result['can_publish'] and 'no_verified_eligible_members' in result['warnings']
    assert result['draft']['fallback_enabled'] is False and result['draft']['auto_pool_enabled'] is False
    assert result['draft']['caps']['input_price_micros']==2000
    c.pin_rows=[{'token_id':1,'model_name':p['model'],'channel_id':999}]
    result=await m.preview(p['id']);assert 'existing_pin_conflicts_with_pool' in result['warnings']
    with pytest.raises(PoolError):await m.publish(p['id'],result['preview_id'],True)
    assert not c.writes
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_verified_fresh_candidate_can_publish_explicitly(manager):
    m,c=manager;p=await m.policy({**policy(),'enabled':True});c.rows=[row()]
    candidates=await m.discover(p,c);candidate=candidates[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'verified_at':iso(time.time()),'actual_channel_id':123,'request_ids':['test-request'],'reference_version':'reference'})
    preview=await m.preview(p['id']);assert preview['can_publish']
    with pytest.raises(PoolError):await m.publish(p['id'],preview['preview_id'],False)
    result=await m.publish(p['id'],preview['preview_id'],True)
    assert result['status']=='published' and len(c.writes)==1
    assert c.writes[0][0]==1 and c.writes[0][2]['channels']==[{'channel_id':123,'priority':None}]
    with pytest.raises(PoolError):await m.delete(p['id'],p['revision'])
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_preview_price_change_and_version_rejects(manager):
    m,c=manager;p=await m.policy({**policy(),'enabled':True});c.rows=[row()]
    candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'verified_at':iso(time.time()),'actual_channel_id':123,'reference_version':'test'})
    preview=await m.preview(p['id']);c.rows=[row(input_price_micros=9000)]
    with pytest.raises(PoolError,match='pool_candidates_changed'):await m.publish(p['id'],preview['preview_id'],True)
    assert not c.writes
    await m.close();await m.monitor.close()
