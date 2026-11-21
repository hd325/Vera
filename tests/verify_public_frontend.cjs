// Fully offline verification of the current complete public frontend.
// Every browser request is fulfilled or aborted; DNS is disabled as a second guard.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const ROOT=path.resolve(process.argv[2]||'.'),OUT=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'vera-browser-check-')),ORIGIN='https://frontend.invalid';
const html=fs.readFileSync(path.join(ROOT,'index.html'),'utf8');
const allowedStatic=new Set(['logo.png','dompurify.min.js','marked.js','font-awesome.css','task-manager.css','account-settings.css','wechat-connection.css','chat-interactions.css','answer-process.js','answer-process.css','speech-input.js','task-manager.js','wechat-connection.js','account-settings.js','chat-interactions.js','pdf.min.js','pdf.worker.min.js','fonts/fontawesome-webfont.woff2']);
const types={'.js':'text/javascript','.css':'text/css','.png':'image/png','.woff2':'font/woff2'};
const attack='\"><img src=x onerror="globalThis.__injected=1">';
const malicious='**保留加粗**\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n'+attack+'\n\n[危險](javascript:alert(1))';
const user={account:'offline-fixture',name:'離線驗證',class:'合成資料',role:'student'};
function makePdf(){
  const objects=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>','<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R /Resources << >> >>','<< /Length 0 >>\nstream\n\nendstream'];
  let value='%PDF-1.4\n',offsets=[0];
  for(let i=0;i<objects.length;i++){offsets.push(Buffer.byteLength(value));value+=(i+1)+' 0 obj\n'+objects[i]+'\nendobj\n';}
  value+='%'+('synthetic padding '.repeat(15000))+'\n';
  const xref=Buffer.byteLength(value);value+='xref\n0 5\n0000000000 65535 f \n'+offsets.slice(1).map(x=>String(x).padStart(10,'0')+' 00000 n \n').join('')+'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n'+xref+'\n%%EOF\n';
  return Buffer.from(value);
}
const pdf=makePdf();
(async()=>{
  const browser=await chromium.launch({headless:true,channel:process.env.VERA_BROWSER_CHANNEL||(process.platform==='win32'?'msedge':undefined),args:['--host-resolver-rules=MAP * ~NOTFOUND','--disable-background-networking','--disable-component-update','--disable-sync','--no-proxy-server']});
  const passed=[],requests=[],missingStatic=[],unexpectedApi=[],pageErrors=[];
  let sourceMode='allow',sourceCount=0;
  const context=await browser.newContext({viewport:{width:1440,height:900},serviceWorkers:'block'});
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    requests.push({host:url.hostname,path:url.pathname,method:request.method(),range:request.headers().range||null,identity:request.headers()['x-session-id']||null});
    if(url.origin!==ORIGIN)return route.abort();
    if(url.pathname==='/')return route.fulfill({status:200,contentType:'text/html',body:html});
    if(url.pathname.startsWith('/static/')){
      const relative=decodeURIComponent(url.pathname.slice(8));
      if(!allowedStatic.has(relative)){missingStatic.push(relative);return route.fulfill({status:404,body:'not in synthetic fixture allowlist'});}
      const file=path.join(ROOT,relative);
      if(!fs.existsSync(file)){missingStatic.push(relative);return route.fulfill({status:404,body:'missing public static asset'});}
      return route.fulfill({status:200,contentType:types[path.extname(relative)]||'application/octet-stream',body:fs.readFileSync(file)});
    }
    const json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
    if(url.pathname==='/api/captcha/config')return json({enabled:false});
    if(url.pathname==='/api/login')return json({code:200,data:{session_id:'fixture-a',user_info:user}});
    if(url.pathname==='/api/session')return json({code:200,data:{user_info:user,current_conversation:null}});
    if(url.pathname==='/api/tasks')return json({code:200,data:[]});
    if(url.pathname==='/api/files')return json({code:200,data:{files:[]}});
    if(url.pathname==='/api/conversations')return json({code:200,data:[]});
    if(url.pathname==='/api/account/settings')return json({code:200,data:{memory_enabled:true,use_memory:true,theme:'light',auto_web:false,auto_think:false}});
    if(url.pathname==='/api/memories')return json({code:200,data:{memories:[{id:'fixture-memory',content:attack,category:'preference'}]}});
    if(url.pathname==='/api/skills')return json({code:200,data:{skills:[]}});
    if(url.pathname==='/api/chat/models')return json({models:[{id:'offline-model',name:'離線模型'}],default:'offline-model'});
    if(url.pathname==='/api/rag/status'||url.pathname==='/api/metrics/summary')return json({code:200,data:{}});
    if(url.pathname==='/api/reviews')return json({code:200,data:{files:[]}});
    if(url.pathname.startsWith('/api/learning/'))return json({code:200,data:{available:true,total:0,correct:0,accuracy:null}});
    if(url.pathname.startsWith('/api/retrieval/'))return json({code:200,data:{}});
    if(url.pathname==='/api/knowledge/chunks/fixture-chunk'){
      sourceCount++;
      if(sourceMode==='deny')return json({detail:attack},403);
      return json({code:200,data:{document_id:'fixture-document',filename:attack+'.txt',file_type:'txt',content:'AUTHORIZED CURRENT CONTENT',context_before:[{content:attack,location:attack}],context_after:[]}});
    }
    if(url.pathname==='/api/files/fixture-markdown/preview')return json({code:200,data:{preview_type:'text',file_name:'synthetic.md',content:malicious}});
    if(url.pathname==='/api/files/fixture-office/preview')return json({code:200,data:{preview_type:'office',file_name:attack+'.docx',has_download:true}});
    if(url.pathname==='/api/documents/fixture-original/file')return route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><p>synthetic original</p><script>try{parent.document.body.dataset.compromised="true"}catch(e){document.body.dataset.isolated="true"}</script>'});
    if(url.pathname==='/api/documents/fixture-pdf/file'){
      const range=request.headers().range;
      if(range){const match=/bytes=(\d+)-(\d*)/.exec(range);const start=Number(match[1]),end=Math.min(pdf.length-1,match[2]?Number(match[2]):pdf.length-1);return route.fulfill({status:206,headers:{'Content-Type':'application/pdf','Accept-Ranges':'bytes','Content-Range':`bytes ${start}-${end}/${pdf.length}`,'Content-Length':String(end-start+1)},body:pdf.subarray(start,end+1)});}
      return route.fulfill({status:200,headers:{'Content-Type':'application/pdf','Accept-Ranges':'bytes','Content-Length':String(pdf.length)},body:pdf});
    }
    unexpectedApi.push(url.pathname);return json({detail:'unmapped synthetic endpoint'},404);
  });
  const page=await context.newPage();page.on('pageerror',e=>pageErrors.push(String(e)));
  try{
    await page.goto(ORIGIN,{waitUntil:'load'});
    await page.waitForFunction(()=>typeof window.AccountSettings==='object'&&typeof window.BackgroundTasks==='object'&&typeof marked!=='undefined');
    assert.equal(await page.title(),'知行小智 Vera');
    assert.equal(await page.locator('#login-page').isVisible(),true);
    assert.equal(await page.locator('#app').isVisible(),false);
    assert.equal(await page.locator('#ict-login-link,#ict-login-retry').count(),0);
    assert.equal(await page.locator('#account').inputValue(),'');assert.equal(await page.locator('#password').inputValue(),'');
    assert.equal(requests.some(x=>x.path.includes('ict-login')),false);
    await page.screenshot({path:path.join(OUT,'public-login-desktop.png')});
    passed.push('Complete production HTML loads with normal blank local-account login and Vera branding');
    await page.locator('#account').fill('offline-fixture');await page.locator('#password').fill('synthetic-only');
    await page.locator('#login-submit-btn').click();await page.locator('#app').waitFor({state:'visible'});
    assert.equal(await page.locator('#login-page').isVisible(),false);
    assert.ok(await page.evaluate(()=>!!window.BackgroundTasks&&!!window.AccountSettings&&!!window.ChatInteractions&&!!window.VeraMobileChat));
    assert.equal(await page.locator('#ai-voice-btn').count(),1);assert.equal(await page.locator('#ai-attach-btn').count(),1);
    passed.push('Mock login succeeds and task, settings/memory, speech, photo and mobile controllers remain present');
    const safe=await page.evaluate(({malicious,attack})=>{
      globalThis.__injected=0;
      const row=addMessage(malicious,false,{web_sources:[{id:'W1',title:attack,url:'javascript:alert(1)',content_status:'fetched'}]});
      const bubble=row.querySelector('.message-bubble');
      if(bubble.querySelector('img,iframe,script,svg,math,[onerror],[onclick]'))return false;
      if(!bubble.querySelector('strong')||!bubble.querySelector('table'))return false;
      const live=document.createElement('div');renderMessageMarkdown(live,malicious,[]);document.body.append(live);
      const preview=document.createElement('div');renderFileMarkdown(preview,malicious);document.body.append(preview);
      if(live.querySelector('img,script,iframe,[onerror]')||preview.querySelector('img,script,iframe,[onerror]'))return false;
      restoreCurrentConversation({current_conversation:{id:'fixture-history',messages:[{content:malicious,is_user:false,timestamp:'2026-01-01T00:00:00Z'}]}});
      if(document.querySelector('#messages-container img,#messages-container script,#messages-container [onerror]'))return false;
      live.remove();preview.remove();
      attachedFiles=[{id:attack,name:attack,audience:attack,status:'success'}];renderAttachedFiles();
      if(attachedFilesContainer.querySelector('img,[onerror]')||!attachedFilesContainer.textContent.includes('<img'))return false;
      conversationHistory=[{id:attack,title:attack,message_count:attack}];renderConversationHistory();
      if(document.querySelector('#history-list img,#history-list [onerror]'))return false;
      renderFiles([{id:attack,name:attack,audience:'personal',subject_custom:attack,material_custom:attack,size:12,has_original:true}]);
      return !document.querySelector('#all-files-list img,#all-files-list [onerror]')&&!globalThis.__injected;
    },{malicious,attack});assert.equal(safe,true);
    passed.push('Actual live/history/file Markdown and untrusted names/IDs/metadata remain inert while tables and emphasis render');
    const traceSafe=await page.evaluate(({attack,malicious})=>{
      const row=document.createElement('div');row.className='message-content';document.body.append(row);
      const widget=AnswerProcess.createLive(row,{});
      widget.upsertStep({id:'untrusted',kind:'retrieval',status:'done',title:attack,detail:attack,
        links:[{kind:'web',label:attack,url:'javascript:alert(1)'}]});
      widget.setThinking(malicious,false);widget.finish('done',null,malicious);
      const safe=!row.querySelector('img,script,iframe,[onerror],a[href^="javascript:"]')&&!globalThis.__injected;
      row.remove();return safe;
    },{attack,malicious});assert.equal(traceSafe,true);
    passed.push('Actual process trace and thinking Markdown reject injected names, script links and markup');
    await page.evaluate(()=>window.previewFile('fixture-office'));assert.ok((await page.locator('#file-preview-body').textContent()).includes('<img'));
    assert.equal(await page.locator('#file-preview-body img,#file-preview-body [onerror]').count(),0);
    await page.evaluate(()=>closePreviewModal());
    await page.evaluate(()=>window.previewFile('fixture-markdown'));assert.equal(await page.locator('#file-preview-body table').count(),1);
    assert.equal(await page.locator('#file-preview-body img,#file-preview-body [onerror]').count(),0);
    await page.evaluate(()=>closePreviewModal());passed.push('Actual modal Office filenames and Markdown preview are sanitized');
    await page.evaluate(()=>openSourceView('fixture-document','fixture-chunk'));
    assert.ok((await page.locator('#source-view-body').textContent()).includes('AUTHORIZED CURRENT CONTENT'));
    assert.equal(await page.locator('#source-view-title img,#source-view-body img,#source-view-body [onerror]').count(),0);
    sourceMode='deny';await page.evaluate(()=>openSourceView('fixture-document','fixture-chunk'));
    assert.ok((await page.locator('#source-view-body').textContent()).includes('403'));assert.equal(sourceCount,2);
    assert.equal(await page.locator('#source-view-body img,#source-view-body [onerror]').count(),0);
    await page.evaluate(()=>closeSourceView());passed.push('Actual source modal reauthorizes each open and removes previously authorized text on 403');
    const race=await page.evaluate(async()=>{
      const original=fetch;let release;
      fetch=()=>new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify({data:{content:'STALE PRIVATE',file_type:'txt'}}),{status:200}));});
      const opening=openSourceView('fixture-document','fixture-chunk');sessionId='fixture-b';release();await opening;fetch=original;
      const stale=!sourceViewBody.textContent.includes('STALE PRIVATE');sessionId='fixture-a';closeSourceView();return stale;
    });assert.equal(race,true);passed.push('Actual source response is discarded after session changes');
    const popupEvent=page.waitForEvent('popup');await page.evaluate(()=>viewFullFile('fixture-original'));const popup=await popupEvent;
    await popup.waitForFunction(()=>!!document.querySelector('iframe'));
    assert.equal(await popup.locator('iframe').getAttribute('sandbox'),'allow-scripts');
    assert.equal(await popup.evaluate(()=>window.opener),null);
    await popup.waitForTimeout(150);
    assert.equal(await popup.evaluate(()=>document.body.dataset.compromised||''),'');
    assert.equal(await popup.frames()[1].evaluate(()=>document.body.dataset.isolated),'true');await popup.close();
    passed.push('Original HTML executes inside an opaque sandbox, cannot access popup parent, and popup has no opener');
    const pdfSettings=await page.evaluate(async()=>{
      await ensurePdfLibrary();const original=pdfjsLib,seen=[];
      window.pdfjsLib={...original,getDocument:options=>{seen.push({isEvalSupported:options.isEvalSupported,disableStream:options.disableStream,disableAutoFetch:options.disableAutoFetch,headers:options.httpHeaders});return original.getDocument(options);}};
      const container=document.createElement('div');container.style.width='640px';document.body.append(container);
      const viewer=new LocalPdfViewer(container);const loaded=await viewer.load('fixture-pdf');
      const rendered=container.dataset.renderedPage;viewer.destroy();container.remove();return {seen,loaded,rendered};
    });assert.equal(pdfSettings.loaded,true);assert.equal(pdfSettings.rendered,'1');
    assert.equal(pdfSettings.seen[0].isEvalSupported,false);assert.equal(pdfSettings.seen[0].disableStream,true);assert.equal(pdfSettings.seen[0].headers['X-Session-ID'],'fixture-a');
    assert.ok(requests.some(x=>x.path==='/api/documents/fixture-pdf/file'&&x.range));
    passed.push('Real local PDF.js renders a synthetic PDF with isEvalSupported:false, session headers, and HTTP Range requests');
    await page.evaluate(()=>{restoreCurrentConversation({current_conversation:null});window.AccountSettings.open('memory');});
    await page.locator('#settings-memory-list .settings-memory-card').waitFor();
    assert.equal(await page.locator('#settings-memory-list img,#settings-memory-list [onerror]').count(),0);
    assert.ok((await page.locator('#settings-memory-list').textContent()).includes('<img'));
    await page.evaluate(()=>window.AccountSettings.close());passed.push('Memory settings remain usable and render untrusted memory as text');
    await page.setViewportSize({width:390,height:844});await page.waitForTimeout(150);
    const layout=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,voice:!!document.getElementById('ai-voice-btn'),camera:!!document.getElementById('ai-attach-btn'),mobile:window.VeraMobileChat.isMobileLayout()}));
    assert.equal(layout.overflow,false);assert.equal(layout.mobile,true);assert.equal(layout.voice,true);assert.equal(layout.camera,true);
    await page.screenshot({path:path.join(OUT,'public-app-mobile.png')});passed.push('390px mobile app layout has no horizontal overflow and retains voice/photo controls');
    assert.deepEqual(missingStatic,[]);assert.deepEqual(unexpectedApi,[]);assert.deepEqual(pageErrors,[]);
    const summary={passed:true,cases:passed,pageErrors,missingStatic,unexpectedApi,requestCount:requests.length,rangeRequests:requests.filter(x=>x.range),externalRequestsAborted:[...new Set(requests.filter(x=>x.host!=='frontend.invalid').map(x=>x.host))],networkPolicy:'All requests fulfilled/aborted; DNS disabled; service workers blocked; no real backend/provider contacted'};
    fs.writeFileSync(path.join(OUT,'verify_frontend_full_results.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary,null,2));
  }catch(error){console.error(error);console.error(JSON.stringify({passed,pageErrors,missingStatic,unexpectedApi,requests},null,2));process.exitCode=1;}
  finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
