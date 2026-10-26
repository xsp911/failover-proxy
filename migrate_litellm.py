"""One-time local migration; credentials travel only in captured pipes, never stdout.

Existing LiteLLM stays unchanged. New API token/admin password written to a
private access.env outside this repository, not printed by this script.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess

from proxy.config import validate_config
from proxy.store import Store

DATA=Path('/opt/failover-proxy/data')
ACCESS=Path('/opt/failover-proxy/access.env')


def main():
    store=Store(DATA)
    if store.get_config()['providers']:
        raise SystemExit('Destination already contains providers; refusing to overwrite')
    sql='SELECT json_agg(json_build_object(\'id\', model_id, \'name\', model_name, \'params\', litellm_params)) FROM "LiteLLM_ProxyModelTable";'
    output=subprocess.run(['sudo','-n','docker','compose','-f','/opt/litellm/compose.yaml','exec','-T','db','psql','-U','litellm','-d','litellm','-tA','-c',sql],capture_output=True,text=True,check=True)
    encrypted=json.loads(output.stdout)
    script='''import json,sys
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
rows=json.load(sys.stdin)
for row in rows:
 p=row['params']
 for key in ['api_key','api_base','model','custom_llm_provider']:
  if isinstance(p.get(key),str):
   p[key]=decrypt_value_helper(p[key],key,exception_type='debug',return_original_value=True)
print(json.dumps(rows))
'''
    # Code passed as -c leaves stdin exclusively for encrypted records.
    result=subprocess.run(['sudo','-n','docker','exec','-i','litellm-litellm-1','/app/.venv/bin/python','-c',script],input=json.dumps(encrypted),capture_output=True,text=True,check=True)
    rows=json.loads(result.stdout)
    config=store.get_config()
    routes=[]
    for row in rows:
        if row['name']!='deepseek-v4.1-flash-1017':
            continue
        p=row['params']
        if not isinstance(p.get('api_key'),str) or not p['api_key']:
            raise RuntimeError('provider_key_missing')
        from urllib.parse import urlsplit
        base=p['api_base'].rstrip('/')
        config['providers'].append({'id':row['id'],'name':urlsplit(base).hostname,'base_url':base,'api_key':p['api_key'],'enabled':True,'priority':int(p['order'])})
        routes.append({'provider_id':row['id'],'upstream_model':p['model'].removeprefix('custom_openai/').removeprefix('openai/'),'enabled':True})
    if len(routes)!=3:
        raise RuntimeError('expected_three_providers')
    config['models']=[{'id':'deepseek-flash','name':'deepseek-v4.1-flash-1017','enabled':True,'supports_vision':True,'routes':routes}]
    store.save_config(validate_config(config),config['revision'])
    password=secrets.token_urlsafe(24)
    store.initialize_admin('admin',password)
    key=store.create_key('Initial agent key',['deepseek-v4.1-flash-1017'])
    fd=os.open(ACCESS,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('FP_ADMIN_USERNAME=admin\nFP_ADMIN_PASSWORD='+password+'\nFP_API_KEY='+key['token']+'\n')
    print('Migrated 3 providers, 1 vision model; created private admin and API credentials. Source unchanged.')


if __name__=='__main__':main()
