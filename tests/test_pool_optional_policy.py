import time

import pytest

from proxy.a6_probe import TargetProbe
from proxy.pool_manager import micros
from test_pool_parallel import setup
from test_pool_manager import row


@pytest.mark.asyncio
async def test_only_output_price_filters_and_serializes_unconstrained_fields(tmp_path):
    m,c,p=await setup(tmp_path,1)
    p=await m.policy({**p,'max_input_usd':None,'max_cache_read_usd':None,'max_cache_write_usd':None,'max_output_usd':.008},p['id'])
    assert m.eligibility(p,row(input_price_micros=999999,cache_read_price_micros=99999,cache_write_price_micros=99999))[0]
    assert not m.eligibility(p,row(output_price_micros=9000))[0]
    draft=m.route_draft(p,[123])
    assert draft['caps']['input_price_micros']==0 and draft['caps']['cache_read_price_micros']==0 and draft['caps']['output_price_micros']==8000
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_optional_prices_paid_tests_freeze_known_seller_prices(tmp_path,monkeypatch):
    m,c,p=await setup(tmp_path,1)
    p=await m.policy({**p,'max_input_usd':None,'max_output_usd':.008,'max_cache_read_usd':None,'max_cache_write_usd':None},p['id'])
    async def passed(self,pool,candidate,secret,reserve):
        await reserve(candidate,{'prompt':'test'})
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req-test'],'predicted_model':'deepseek-v4.1-flash-1017','predicted_probability':.99}
    monkeypatch.setattr(TargetProbe,'run',passed)
    job={'id':'optional-check','progress':0};await m.verify(p,c,job)
    testdraft=c.writes[0][2]
    assert testdraft['caps']['input_price_micros']==1000
    assert testdraft['caps']['cache_read_price_micros']==20
    assert testdraft['caps']['cache_write_price_micros']==1
    assert job['reserved_cost_usd']>0
    result=m.db.get(p['id'])['candidates'][0]
    assert result['predicted_model']=='deepseek-v4.1-flash-1017'
    await m.close();await m.monitor.close()
