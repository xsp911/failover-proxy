const {chromium}=require('/tmp/failover-browser-check/node_modules/playwright');const fs=require('fs');const assert=require('node:assert/strict');
(async()=>{
 const b=await chromium.launch({headless:true,args:['--no-sandbox']});const p=await b.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
 const base={id:'p',revision:1,name:'Existing',token_id:11,test_token_id:22,test_token_ids:[22],model:'m',reference_model:'m',protocol:'chat',max_input_usd:1,max_output_usd:1,max_cache_read_usd:1,max_cache_write_usd:1,min_cache_hit_percent:40,min_success_percent:90,threshold_percent:90,verification_ttl_hours:24,refresh_minutes:10,max_candidates:10,max_members:3,max_probe_requests:60,max_probe_usd:1,enabled:false};let code='test_token_in_use';let status=400;
 await p.route('**/*',async route=>{const u=new URL(route.request().url());if(u.pathname.startsWith('/admin/')){
  if(route.request().method()==='GET'&&u.pathname==='/admin/observability')return route.fulfill({json:{},status:404});
   let data;if(u.pathname==='/admin/session')data={username:'test',csrf_token:'csrf'};
  else if(u.pathname==='/admin/stats')data={summary:{},providers:[],timeline:[],recent_requests:[],recent_errors:[]};
  else if(u.pathname==='/admin/health')data={status:'ok'};
  else if(u.pathname==='/admin/a6/tokens')data={items:[]};
  else if(u.pathname==='/admin/a6/pools')data={pools:[base],catalog:[{id:'m',display_name:'M'}],tokens:[11,22,33,44,55].map(id=>({id,name:'Key'+id,status:1})),jobs:[],capabilities:{}};
  else if(u.pathname==='/admin/a6/pools/p')return route.fulfill({status,json:{error:{code,message:'DO NOT ECHO RAW SECRET'}}});
  else data={};return route.fulfill({json:data});}
  const name={'/':'web/index.html','/app.js':'web/app.js','/styles.css':'web/styles.css'}[u.pathname];return name?route.fulfill({body:fs.readFileSync(name),contentType:u.pathname.endsWith('.js')?'application/javascript':u.pathname.endsWith('.css')?'text/css':'text/html'}):route.abort();});
 await p.goto('http://errors.invalid/');await p.locator('#app').waitFor({state:'visible'});await p.locator('#tab-market').click();await p.locator('#pools-list button').first().click();
 await p.locator('#pool-section-tab-settings').click();
 if(!await p.locator('#pool-settings').evaluate(n=>n.open))await p.locator('#pool-settings > summary').click();
  for(const [value,expected,http] of [['test_token_in_use','занят другим пулом',400],['pool_token_unavailable','отключён',400],['test_token_has_conflicting_pin','закреплён продавец',409]]){
  code=value;status=http;await p.locator('#pool-name').fill('Changed '+value);await p.locator('#pool-save').click();await p.waitForFunction(expected=>document.querySelector('#pools-error').textContent.includes(expected),expected);assert(!(await p.locator('#pools-error').textContent()).includes('RAW SECRET'));
 }
 assert.deepEqual(errors,[]);console.log('PASS exact key conflict/disabled/pin error displayed, no raw server error exposed');await b.close();
})().catch(e=>{console.error(e);process.exit(1)});
