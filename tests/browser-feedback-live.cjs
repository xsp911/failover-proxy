const {chromium}=require('/tmp/failover-browser-check/node_modules/playwright');
const fs=require('fs');
(async()=>{
 const values=Object.fromEntries(fs.readFileSync('/opt/failover-proxy/access.env','utf8').trim().split('\n').map(x=>{const i=x.indexOf('=');return [x.slice(0,i),x.slice(i+1)]}));
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('https://127.0.0.1:9097/');await page.locator('#username').fill(values.FP_ADMIN_USERNAME);await page.locator('#password').fill(values.FP_ADMIN_PASSWORD);await page.locator('#login-button').click();await page.locator('#app').waitFor({state:'visible'});
 await page.waitForFunction(()=>document.querySelector('#version').textContent.includes('0.2.0'));
 await page.locator('#tab-settings').click();await page.waitForFunction(()=>!document.querySelector('#config-fieldset').disabled);await page.locator('[data-settings="models"]').click();
 const context=page.getByLabel('Размер контекста, токенов');const output=page.getByLabel('Максимум выходных токенов');
 await context.waitFor({state:'visible'});await output.waitFor({state:'visible'});
 // Round-trip existing unknown metadata as null; don't invent provider limits.
 await context.fill('');await context.dispatchEvent('input');await output.fill('');await output.dispatchEvent('input');
 await page.locator('#save-config').click();await page.waitForFunction(()=>document.querySelector('#save-config').disabled);
 if(await page.locator('#config-error').isVisible())throw Error(await page.locator('#config-error').textContent());
 await page.locator('#reload-config').click();await page.waitForFunction(()=>!document.querySelector('#config-fieldset').disabled);
 if(await context.inputValue()!==''||await output.inputValue()!=='')throw Error('null metadata roundtrip failed');
 const info=await page.evaluate(async()=>{const h=await(await fetch('/admin/health')).json();const c=await(await fetch('/admin/config')).json();return {health:h,models:c.models.map(m=>({context:m.context_length,output:m.max_output_tokens})),providers:c.providers.map(p=>({id:p.id,priority:p.priority,has_api_key:p.has_api_key}))}});
 if(info.health.version!=='0.2.0'||info.health.queue_wait_s!==3||info.providers.some(p=>!p.has_api_key))throw Error('deployed metadata/queue health mismatch');
 await page.screenshot({path:'/home/xspider/failover-proxy/artifacts/feedback-models.png',fullPage:true});
 await page.locator('#logout').click();await page.locator('#login-view').waitFor({state:'visible'});if(errors.length)throw Error(errors.join(';'));
 console.log('PASS live v0.2.0 login, queue health, model metadata controls, null save/reload, provider secrets retained, logout');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
