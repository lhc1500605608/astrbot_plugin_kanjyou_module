'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../pages/common/panel.html'),'utf8');
function block(start,end) {return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
let checks=0;
function check(name,fn) {fn();checks++;console.log('ok - '+name);}
async function asyncCheck(name,fn) {await fn();checks++;console.log('ok - '+name);}
const sandbox = {URLSearchParams, Date, Intl, Map, Promise, setTimeout, clearTimeout, L:(a)=>a, DEMO:false, window:{}, TZ:"Asia/Shanghai", lang:"zh", t:(a)=>a};
vm.createContext(sandbox);
vm.runInContext(block('const esc =','const qs =')+block('const toDate =','const parseSid =')+block('const parseSid =','/* ---------------- API layer')+block('const normalize =','/* ---------------- demo')+block('const parseMaybe =','/* Skip rebuilding')+block('const mergeDecisions =','const applyStatus =')+block('const HHMM =','/* ---------------- events')+'globalThis.test={esc,fmtTime,parseSid,parseWl,normalize,ownSSE,makeApi,unwrapEvent,mergeDecisions};',sandbox);
const T=sandbox.test;
check('full thread suffix preserved',()=>assert.equal(T.parseSid('private:A:FriendMessage:thread:42').name,'thread:42'));
check('server wall time is not silently converted',()=>assert.equal(T.fmtTime('2026-10-14 08:15:30',true),'2026-10-14 08:15:30'));
check('instance case preserved',()=>assert.equal(T.parseSid('private:Bot-A:FriendMessage:42').platform,'Bot-A'));
check('markup is escaped',()=>assert.equal(T.esc('<img src=x onerror="attack()">'), '&lt;img src=x onerror=&quot;attack()&quot;&gt;'));
check('scoped private entry',()=>assert.equal(T.parseWl('{"kind":"private","platform":"A","id":"42"}','private').scoped,true));
check('wrong-kind entry is inert',()=>assert.equal(T.parseWl('{"kind":"group","platform":"A","id":"42"}','private').scoped,false));
check('legacy entry stays unbound',()=>assert.equal(T.parseWl('{"legacy_id":"42"}','private').legacy,true));
check('empty platform is not authorized',()=>assert.equal(T.parseWl('{"kind":"private","platform":"","id":"42"}','private').scoped,false));
const realStatus={sessions:[],global:{enabled:true,paused_until:null,fail_streak:0},whitelist:{private:[],group:[]},settings:{sleep_start:'23:30',sleep_end:'08:00',timezone:'Asia/Shanghai'},recent:[]};
check('actual SDK status payload is accepted',()=>{const r=T.normalize(realStatus,'status');assert.equal(r.ok,true);assert.equal(r.data,realStatus);});
check('actual SDK POST payload shapes are accepted',()=>{
 for (const [e,d] of [['toggle',{enabled:true}],['sleep',{start:'23:30',end:'08:00'}],['mood',{session_id:'A',mood:50}],['test',{triggered:true,session_id:'A'}],['reload',{reloaded:true}],['whitelist',{private:[],group:[]}]]) {
   const r=T.normalize(d,e);assert.equal(r.ok,true,`${e} should pass`);assert.equal(r.data,d);
 }
});
check('raw success envelope is retained',()=>{const r=T.normalize({status:'ok',data:realStatus},'status');assert.equal(r.ok,true);assert.deepEqual(r.data,realStatus);});
check('JSON string envelope is decoded',()=>{const r=T.normalize(JSON.stringify({status:'ok',data:{reloaded:true}}),'reload');assert.equal(r.ok,true);assert.equal(r.data.reloaded,true);});
check('API errors cannot look successful',()=>assert.equal(T.normalize({status:'error',message:'denied'},'status').ok,false));
check('unsupported status cannot look successful',()=>assert.equal(T.normalize({status:'weird'},'status').ok,false));
check('no response cannot look successful',()=>assert.equal(T.normalize(null,'status').ok,false));
check('malformed JSON cannot look successful',()=>assert.equal(T.normalize('bad','status').ok,false));
check('arbitrary object cannot look successful',()=>assert.equal(T.normalize({hello:'world'},'status').ok,false));
check('successful payload without envelope is not blanket-accepted',()=>assert.equal(T.normalize({data:{triggered:true}},'test').ok,false));
check('real envelope is decoded',()=>assert.equal(T.unwrapEvent({type:'status',data:{sessions:[]}}).type,'status'));
check('SDK string wrapper is decoded',()=>assert.equal(T.unwrapEvent({data:JSON.stringify({type:'decision',data:{session_id:'A'}})}).data.session_id,'A'));
check('malformed SSE is ignored',()=>assert.equal(T.unwrapEvent({data:'bad'}),null));
check('unknown SSE is ignored',()=>assert.equal(T.unwrapEvent({type:'ping',data:{}}),null));
check('helper without SDK is not a live host',()=>{sandbox.window.KANJYOU={apiGet:async()=>({status:'ok'})};assert.equal(T.makeApi().mode,'nohost');});
check('replayed decisions deduplicate',()=>{const row={session_id:'A',at:'2026-10-14T08:00:00Z',outcome:'skip',reason_codes:['cooldown']};assert.equal(T.mergeDecisions([{...row}],[{...row}]).length,1);});
check('decisions newest first',()=>{assert.equal(T.mergeDecisions([{at:1},{at:3},{at:2}])[0].at,3);});
check('decision history is bounded',()=>assert.equal(T.mergeDecisions(Array.from({length:250},(_,at)=>({at}))).length,200));
// The shared submit delegate must leave native method=dialog forms alone.
let handler;
const dialogScope={document:{},window:{addEventListener(){}},on:(_el,_event,fn)=>{handler=fn;},console};
vm.createContext(dialogScope);
vm.runInContext(block('on(document, "submit",','on(window, "pagehide",'),dialogScope);
(async()=>{
 await asyncCheck('native dialog submit is not prevented',async()=>{let prevented=false;await handler({target:{getAttribute:()=>null},preventDefault(){prevented=true;}});assert.equal(prevented,false);});
 await asyncCheck('pending SSE close unsubscribes late ID',async()=>{
  let resolve;const removed=[];const sdk={subscribeSSE(_endpoint,handlers){assert.equal(typeof handlers.onMessage,'function');return new Promise(r=>{resolve=r;});},unsubscribeSSE(id){removed.push(id);return Promise.resolve();}};
  const handle=T.ownSSE(sdk,()=>{},()=>{});await Promise.resolve();handle.close();resolve('late');await Promise.resolve();await Promise.resolve();assert.deepEqual(removed,['late']);
 });
 await asyncCheck('stale SSE handlers cannot update UI',async()=>{
  let handlers;const data=[];const sdk={subscribeSSE(_e,h){handlers=h;return Promise.resolve('id');},unsubscribeSSE(){return Promise.resolve();}};
  const handle=T.ownSSE(sdk,x=>data.push(x),()=>{});await Promise.resolve();await Promise.resolve();handle.close();handlers.onMessage({data:'late'});assert.equal(data.length,0);
 });
 await asyncCheck('closing before ready prevents subscription',async()=>{
  let ready;let count=0;const sdk={ready(){return new Promise(r=>{ready=r;});},subscribeSSE(){count++;return Promise.resolve('id');},unsubscribeSSE(){}};
  const handle=T.ownSSE(sdk,()=>{},()=>{});handle.close();ready();await Promise.resolve();await Promise.resolve();assert.equal(count,0);
 });
 await asyncCheck('shared helper makeApi accepts unwrapped SDK payloads',async()=>{
  const K={apiGet:async()=>realStatus,apiPost:async()=>({reloaded:true}),sdk:{ready:()=>Promise.resolve()}};
  sandbox.window.KANJYOU=K;sandbox.window.AstrBotPluginPage={apiGet:async()=>realStatus};
  const api=T.makeApi();assert.equal(api.mode,'live');
  assert.deepEqual((await api.get('status')).data,realStatus);
  assert.deepEqual((await api.post('reload',{})).data,{reloaded:true});
 });
 await asyncCheck('direct SDK makeApi accepts unwrapped payloads and preserves rejection',async()=>{
  delete sandbox.window.KANJYOU;sandbox.window.AstrBotPluginPage={ready:()=>Promise.resolve(),apiGet:async()=>realStatus,apiPost:async()=>({enabled:false})};
  const api=T.makeApi();assert.equal(api.mode,'live');
  assert.deepEqual((await api.get('status')).data,realStatus);
  assert.deepEqual((await api.post('toggle',{enabled:false})).data,{enabled:false});
  sandbox.window.AstrBotPluginPage={ready:()=>Promise.resolve(),apiGet:async()=>{throw new Error('denied');}};
  const failed=await T.makeApi().get('status');assert.equal(failed.ok,false);assert.equal(failed.message,'denied');
 });
 console.log(`WebUI panel regression tests passed (${checks} cases)`);
})().catch(err=>{console.error(err);process.exitCode=1;});
