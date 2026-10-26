// Isolated browser API mocks: no real credentials/provider requests/config writes.
const {chromium}=require('/tmp/failover-browser-check/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),origin='http://feedback.invalid';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 for(const copyWorks of [true,false]){
  const page=await browser.newPage({ignoreHTTPSErrors:true});
  const errors=[],external=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin){external.push(route.request().url());return route.abort()}
    const asset={'/':['web/index.html','text/html'],'/app.js':['web/app.js','application/javascript'],'/styles.css':['web/styles.css','text/css']}[url.pathname];
    if(!asset)return route.abort();
    return route.fulfill({body:fs.readFileSync(path.join(root,asset[0])),contentType:asset[1]});
   });
  const config={revision:1,settings:{request_timeout_s:180,connect_timeout_s:10,read_timeout_s:60,cooldown_s:60,failure_threshold:1,max_request_bytes:33554432,max_response_bytes:16777216,max_buffer_events:32768,max_concurrent_requests:8,max_attempts:3,trust_private_upstreams:false},providers:[{id:'p1',name:'Example',base_url:'https://example.invalid/v1',priority:1,enabled:true,has_api_key:true}],models:[{id:'m1',name:'test-model',enabled:true,supports_vision:true,routes:[{provider_id:'p1',upstream_model:'upstream',enabled:true}],context_length:null,max_output_tokens:null}]};
  let saved=null;
  await page.addInitScript(({copyWorks})=>{
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined});
    document.execCommand=(command)=>{window.copyCommand=command;return copyWorks};
  },{copyWorks});
  await page.route('**/admin/**',async route=>{
   const req=route.request(),url=new URL(req.url());let value;
   if(url.origin!==origin){external.push(req.url());return route.abort()}
   if(req.method()==='GET'&&url.pathname==='/admin/observability')return route.fulfill({json:{},status:404});
   if(url.pathname==='/admin/session')value={username:'test',csrf_token:'test-csrf'};
   else if(url.pathname==='/admin/health')value={status:'ok',version:'0.2.0',active_requests:0,queued_requests:2,buffered_mode:true};
   else if(url.pathname==='/admin/config'){
    if(req.method()==='PUT'){saved=req.postDataJSON();Object.assign(config,saved);config.revision++}
    value=config;
   } else if(url.pathname==='/admin/stats')value={generated_at:new Date().toISOString(),summary:{requests:0,final_requests:0,final_errors:0},providers:[],timeline:[],recent_errors:[],recent_requests:[],coverage:{note:'test'}};
   else if(url.pathname==='/admin/keys')value=req.method()==='POST'?{key:{id:'test-key',name:'Browser test',prefix:'fp-test',models:[],enabled:true},token:'fp-example-not-real'}:{keys:[]};
   else value={ok:true};
   await route.fulfill({json:value});
  });
  await page.goto(origin+'/');await page.locator('#app').waitFor({state:'visible'});
  await page.locator('#tab-settings').click();await page.waitForFunction(()=>!document.querySelector('#config-fieldset').disabled);
  await page.locator('[data-settings="models"]').click();
  const ctx=page.getByLabel('Размер контекста, токенов');
  const output=page.getByLabel('Максимум выходных токенов');
  await ctx.fill('900000');await output.fill('32768');
  await page.locator('#save-config').click();await page.waitForFunction(()=>document.querySelector('#save-config').disabled);
  if(!saved||saved.models[0].context_length!==900000||saved.models[0].max_output_tokens!==32768)throw Error('metadata not saved');
  await page.locator('#reload-config').click();await page.waitForFunction(()=>!document.querySelector('#config-fieldset').disabled);
  if(await ctx.inputValue()!=='900000'||await output.inputValue()!=='32768')throw Error('metadata reload failed');
  await page.locator('[data-settings="keys"]').click();await page.locator('#key-name').fill('Browser test');await page.locator('#create-key').click();
  await page.locator('#token-dialog').waitFor({state:'visible'});await page.locator('#copy-token').click();
  await page.waitForFunction(()=>!!document.querySelector('#copy-status').textContent);
  const status=await page.locator('#copy-status').textContent();
  if(await page.evaluate(()=>window.copyCommand)!=='copy')throw Error('legacy clipboard not attempted');
  if(copyWorks&&!status.toLowerCase().includes('скопир'))throw Error('copy success not reported');
  if(!copyWorks&&!status.includes('Ctrl+C'))throw Error('manual copy fallback missing');
  if(errors.length)throw Error(errors.join(';'));
  assert.deepEqual(external,[]);
  console.log('PASS metadata input/save/reload and clipboard fallback result='+copyWorks);
  await page.close();
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
