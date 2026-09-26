/* Execute the real controller with controlled network timing and a minimal DOM. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const S = require('../../static/js/cloud-state.js');
const D = require('../../static/js/durable-work.js');
const source = fs.readFileSync(require.resolve('../../static/js/cloud-app.js'), 'utf8');

function data(name) {
  return { profile:{name,email:name.toLowerCase()+'@example.invalid'}, context_id:name,
    csrf:'synthetic-csrf', todos:[{id:name,title:name+' private task'}],
    next_cursor:null, conversation_id:null, capabilities:{chat_execute:false,gmail_connect:false} };
}
function response(value,status=200) {
  return {ok:status<400,status,type:'basic',json:async()=>value};
}
function harness(fetch, withChannel=false, pathname='/settings') {
  const elements=new Map(), writes=[], intervals=[], events={}, docEvents={};
  function element(id) {
    const listeners={}, item={hidden:false,disabled:false,value:'',dataset:{},children:[],listeners,
      addEventListener:(name,fn)=>{listeners[name]=fn;},append(...nodes){this.children.push(...nodes);},
      replaceChildren(...nodes){this.children=nodes;}};
    let text='';
    Object.defineProperty(item,'textContent',{get:()=>text,set:value=>{text=value;writes.push([id,value]);}});
    return item;
  }
  const get=id=>{if(!elements.has(id)) elements.set(id,element(id)); return elements.get(id);};
  const document={hidden:false,getElementById:get,createElement:tag=>element(tag),
    addEventListener:(name,fn)=>{docEvents[name]=fn;}};
  const location={pathname,assign(){}};
  let channel;
  class Channel { constructor(){channel=this;} postMessage(){} }
  const window={CloudState:S,DurableWork:D,location,addEventListener:(name,fn)=>{events[name]=fn;}};
  if(withChannel) window.BroadcastChannel=Channel;
  vm.runInNewContext(source,{window,document,fetch,AbortController,crypto:globalThis.crypto,
    BroadcastChannel:Channel,setInterval:fn=>{intervals.push(fn);},setTimeout:()=>1,clearTimeout(){},console});
  return {get,writes,intervals,events,docEvents,channel,document};
}
async function flush(){for(let i=0;i<15;i++) await new Promise(resolve=>setImmediate(resolve));}

async function staleBootstrap() {
  let release, count=0;
  const held=new Promise(resolve=>{release=resolve;});
  const h=harness(async path=>{
    assert.equal(path,'/api/cloud/bootstrap');
    if(++count===1) return held;
    return response(data('Bob'));
  });
  // No visibility/page events or channel signal: another visible window changes the cookie.
  release(response(data('Alice')));
  await flush();
  assert(!h.writes.some(([,value])=>String(value).includes('Alice')), 'delayed bootstrap rendered previous account');
  assert.equal(h.get('cloud-profile').textContent,'Bob · bob@example.invalid');
  assert(count>=2,'bootstrap must recheck authority without a channel');
}

async function failedLogout() {
  let count=0;
  const h=harness(async path=>{
    if(path==='/logout') return response({},503);
    assert.equal(path,'/api/cloud/bootstrap'); count++;
    return response(data('Alice'));
  },true);
  await flush();
  assert.equal(h.get('cloud-account').hidden,false);
  h.get('cloud-logout').listeners.click();
  await flush();
  assert.equal(h.get('cloud-account').hidden,true);
  const warning=h.get('cloud-status').textContent, previous=count;
  assert(warning.includes('could not confirm'));
  for(const trigger of [
    ()=>h.intervals[0](),
    ()=>h.events.pageshow(),
    ()=>h.docEvents.visibilitychange(),
    ()=>h.channel.onmessage({data:'session-changed'})
  ]) {
    trigger(); await flush();
    assert.equal(h.get('cloud-account').hidden,true,'failed logout restored private content');
    assert.equal(h.get('cloud-status').textContent,warning,'failed logout warning disappeared');
    assert.equal(count,previous,'failed logout revalidated automatically');
  }
}

async function staleError() {
  let account='Alice';
  const h=harness(async path=>{
    if(path==='/api/cloud/bootstrap') return response(data(account));
    if(path==='/api/cloud/conversations/ensure') return response({id:account+'-thread'});
    if(path==='/api/work/conversations/Alice-thread') {
      account='Bob'; return response({error:'conversation_not_found'},404);
    }
    if(path==='/api/work/conversations/Bob-thread') return response({generation:1,messages:[],jobs:[]});
    throw new Error('unexpected path');
  },false,'/chat');
  await flush();
  assert.equal(h.get('cloud-profile').textContent,'Bob · bob@example.invalid',
    'error response after cookie switch did not revalidate account');
}

(async()=>{
  const failures=[];
  for(const [name,test] of [['stale bootstrap',staleBootstrap],['failed logout latch',failedLogout],['stale error response',staleError]]) {
    try { await test(); console.log('PASS '+name); }
    catch(error){failures.push(name);console.error('FAIL '+name+': '+error.message);}
  }
  if(failures.length) process.exitCode=1;
})();
