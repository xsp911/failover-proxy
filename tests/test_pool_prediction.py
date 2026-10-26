import asyncio

import pytest
from proxy.a6_probe import TargetProbe
from test_pool_manager import policy


class Monitor:
    def __init__(self,probability):
        self.catalog={'challenges':[{'id':'query-'+str(i),'prompt':'numbers','expected_count':100} for i in range(3)]}
        self.scorer=asyncio.Semaphore(1);self.probability=probability
    async def score(self,outputs,model):
        return {'decision':'ranked','probability_status':'reference_calibrated','used_outputs':3,'target_probability':self.probability,'prediction':'alternative-reference','probability':.9876}


@pytest.mark.asyncio
@pytest.mark.parametrize('expected_score,status',[(.99,'verified'),(.001,'suspect')])
async def test_probe_persists_detector_prediction_without_claiming_identity(expected_score,status):
    probe=TargetProbe(None,Monitor(expected_score));calls=[]
    async def challenge(pool,channel,secret,ch):
        calls.append(ch['id'])
        return {'text':'1,2,3','expected_count':100},{'parsed_numbers':100,'request_id':'request-'+str(len(calls))}
    probe.challenge=challenge
    async def reserve(*_):return None
    result=await probe.run({**policy(),'threshold_percent':90},{'channel_id':123},'fake',reserve)
    assert result['verification_status']==status
    assert result['predicted_model']=='alternative-reference' and result['predicted_probability']==.9876
    assert len(calls)==(3 if status=='verified' else 6)
