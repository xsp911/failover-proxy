const {chromium}=require('/tmp/failover-browser-check/node_modules/playwright');
const fs=require('fs');
(async()=>{
 const access=Object.fromEntries(fs.readFileSync('/opt/failover-proxy/access.env','utf8').trim().split('\n').map(x=>{const n=x.indexOf('=');return [x.slice(0,n),x.slice(n+1)]}));
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000},ignoreHTTPSErrors:true});
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('https://127.0.0.1:9097/');
 await page.locator('#login-view').waitFor({state:'visible'});
 await page.locator('#username').fill(access.FP_ADMIN_USERNAME);
 await page.locator('#password').fill(access.FP_ADMIN_PASSWORD);
 await page.locator('#login-button').click();
 await page.locator('#app').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.getElementById('stats-content').getAttribute('aria-busy')||document.getElementById('stats-content').getAttribute('aria-busy')==='false');
 await page.screenshot({path:'/home/xspider/failover-proxy/artifacts/dashboard.png',fullPage:true});
 await page.locator('#tab-settings').click();
 await page.waitForFunction(()=>!document.getElementById('config-fieldset').disabled);
 if(await page.locator('#provider-editor .editor-card').count()===0){console.log('Provider editors',await page.locator('#provider-editor').textContent());}
 // Exercise optimistic configuration save without changing the routing values.
 await page.locator('#provider-editor input[type="number"]').first().fill('1');
 await page.locator('#provider-editor input[type="number"]').first().dispatchEvent('input');
 if(await page.locator('#save-config').isEnabled()){
   await page.locator('#save-config').click();
   await page.waitForFunction(()=>document.getElementById('save-config').disabled);
   if(await page.locator('#config-error').isVisible())throw Error(await page.locator('#config-error').textContent());
 }
 await page.screenshot({path:'/home/xspider/failover-proxy/artifacts/settings.png',fullPage:true});
 await page.locator('[data-settings="models"]').click();
 if(!(await page.locator('#model-editor').textContent()).includes('deepseek'))throw Error('migrated model missing');
 await page.locator('[data-settings="keys"]').click();
 await page.locator('#key-name').fill('Browser verification key');
 await page.locator('#create-key').click();
 await page.locator('#token-dialog').waitFor({state:'visible'});
 if(!(await page.locator('#new-token').textContent()).startsWith('fp-'))throw Error('key token absent');
 await page.locator('#close-token').click();
 // Revoke the disposable browser-test key via authenticated API, not production keys.
 const result=await page.evaluate(async()=>{
   const s=await (await fetch('/admin/session')).json();
   const k=await (await fetch('/admin/keys')).json();
   const row=k.keys.find(x=>x.name==='Browser verification key');
   if(!row)return false;
   return (await fetch('/admin/keys/'+row.id,{method:'DELETE',headers:{'X-CSRF-Token':s.csrf_token}})).ok;
 });
 if(!result)throw Error('test key cleanup failed');
 await page.setViewportSize({width:390,height:844});
 await page.locator('#tab-stats').click();
 await page.screenshot({path:'/home/xspider/failover-proxy/artifacts/mobile.png',fullPage:true});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+2);
 if(overflow)throw Error('mobile page horizontal overflow');
 await page.locator('#logout').click();
 await page.locator('#login-view').waitFor({state:'visible'});
 if(errors.length)throw Error('browser exceptions: '+errors.join(';'));
 console.log('PASS browser login, stats, settings, migrated vision model, one-time key, revoke, mobile, logout; no JS exceptions');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
