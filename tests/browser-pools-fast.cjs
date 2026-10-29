// Offline delayed-catalogue regression; no real servers, credentials or model calls.
const fs=require('fs'),assert=require('node:assert/strict'),{chromium}=require('/tmp/failover-browser-check/node_modules/playwright');
(async()=>{const b=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 const p=await b.newPage();let reads=0,tokenReads=0,release;const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.route('**/*',async r=>{
  const u=new URL(r.request().url());assert.equal(u.origin,'http://fast-pools.invalid');assert.equal(r.request().method(),'GET');const j=x=>r.fulfill({json:x});
  if(u.pathname==='/')return r.fulfill({body:fs.readFileSync('web/index.html'),contentType:'text/html'});
  if(['/app.js','/styles.css'].includes(u.pathname))return r.fulfill({body:fs.readFileSync('web'+u.pathname),contentType:u.pathname.endsWith('.js')?'application/javascript':'text/css'});
  if(u.pathname==='/admin/session')return j({username:'offline',csrf_token:'csrf'});
  if(u.pathname==='/admin/config')return j({revision:1,settings:{},providers:[],models:[]});
  if(u.pathname==='/admin/stats')return j({summary:{},providers:[],timeline:[],recent_requests:[],recent_errors:[]});
  if(u.pathname==='/admin/health')return j({status:'ok'});
  if(u.pathname==='/admin/a6/pools'){reads++;assert.equal(u.searchParams.get('include_tokens'),'false');return j({pools:[{id:'p',name:'Local pool',model:'m',protocol:'chat',revision:1,members:[],candidates:[],enabled:false,auto_publish:false}],jobs:[],catalog:[],tokens:[],tokens_included:false,capabilities:{}})}
  if(u.pathname==='/admin/a6/pools/tokens'){tokenReads++;await new Promise(resolve=>{release=resolve});return j({tokens:[{id:11,name:'Work',status:1},{id:22,name:'Test',status:1}]})}
  return j({});
 });
 await p.goto('http://fast-pools.invalid/');await p.locator('#app').waitFor({state:'visible'});await p.locator('#tab-market').click();await p.locator('.pool-card').waitFor({state:'visible'});assert.equal(tokenReads,0);
 const initialRefresh=p.waitForResponse(r=>new URL(r.url()).pathname==='/admin/a6/pools');await p.locator('#pools-refresh').click();await initialRefresh;
 assert.equal(tokenReads,0,'refresh must not read external catalogue');
 const request=p.waitForRequest(r=>new URL(r.url()).pathname==='/admin/a6/pools/tokens');await p.locator('#pool-new').click();await request;
 await p.locator('#pool-name').fill('Typed during slow token fetch');assert(await p.locator('.pool-card').isVisible());
 // Delay is deterministic: release intercepted response only after verifying usable list/form.
 assert.equal(tokenReads,1);assert.equal(typeof release,'function');release();
 await p.waitForFunction(()=>document.querySelector('#pool-token_id option[value="11"]'));
 assert.equal(await p.locator('#pool-name').inputValue(),'Typed during slow token fetch','late token response must not reset typed fields');
 await p.locator('#pool-token_id').selectOption('11');
 const refresh=p.waitForResponse(r=>new URL(r.url()).pathname==='/admin/a6/pools');await p.locator('#pools-refresh').click();await refresh;
 assert.equal(await p.locator('#pool-name').inputValue(),'Typed during slow token fetch');assert.equal(await p.locator('#pool-token_id').inputValue(),'11');assert.equal(tokenReads,1);
 assert.deepEqual(errors,[]);console.log('PASS fast pool list/no remote catalogue on landing or polling/lazy token fetch/typed draft retained');
}finally{await b.close()}})().catch(e=>{console.error(e);process.exit(1)});
