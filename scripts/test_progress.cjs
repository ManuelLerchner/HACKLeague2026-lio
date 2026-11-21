// Run: node scripts/test_progress.cjs
// Exercise refresh safety without a browser, network, or third-party dependencies.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(path.join(__dirname, '../src/supplier_chaser/web/static/progress.js'),'utf8');
class Element {
  constructor(tag='input') {this.tag=tag;this.type='text';this.value='original';this.checked=false;this.children=[];this.listeners={};this.hidden=true;}
  matches(){return ['input','textarea','select'].includes(this.tag);}
  querySelectorAll(){return this.children;}
  replaceChildren(...items){this.children=items;}
  append(...items){this.children.push(...items);}
  addEventListener(name, fn){this.listeners[name]=fn;}
}
async function setup(){
  const handlers={}, timers=[], intervals=[], control=new Element(), notification=new Element('div');
  let state={pending:true,revision:'initial',status:'pending'}, calls=0,reloads=0;
  const document={hidden:false,activeElement:null,dialogOpen:false,querySelector:selector=>selector==='dialog[open]' ? document.dialogOpen : notification,querySelectorAll:()=>[control],
    addEventListener:(name,fn)=>{(handlers[name]??=[]).push(fn)},createTextNode:text=>text,createElement:tag=>new Element(tag)};
  const context={document,Element,Map,Set,JSON,Boolean,location:{pathname:'/sources/00000000-0000-0000-0000-000000000000',reload:()=>reloads++},
    window:{setTimeout:fn=>timers.push(fn),setInterval:fn=>intervals.push(fn),confirm:()=>true},
    fetch:async()=>{calls++;return {ok:true,status:200,json:async()=>state}}};
  vm.runInNewContext(code,context);
  await new Promise(setImmediate);
  return {document,control,notification,handlers,timers,intervals,set:value=>state=value,calls:()=>calls,reloads:()=>reloads};
}
(async()=>{
  let s=await setup();
  s.set({pending:false,revision:'done',status:'complete'});await s.intervals[0]();
  assert.equal(s.timers.length,1);s.timers.shift()();assert.equal(s.reloads(),1);
  s=await setup();s.control.value='unsaved';s.handlers.input[0]({target:s.control});
  s.set({pending:false,revision:'done',status:'complete'});await s.intervals[0]();
  assert.equal(s.timers.length,0);assert.equal(s.reloads(),0);
  assert(s.notification.children.some(x=>x.textContent==='Discard edits & refresh'));
  s=await setup();s.document.hidden=true;await s.intervals[0]();assert.equal(s.calls(),1);
  s=await setup();const form=new Element('form');form.children=[s.control];
  s.control.value='submitted';s.handlers.input[0]({target:s.control});s.handlers.submit[0]({target:form});
  s.control.value='edited during request';s.handlers.input[0]({target:s.control});
  s.handlers['supplier-chaser:action-queued'][0]({detail:{form}});await new Promise(setImmediate);
  s.set({pending:false,revision:'done',status:'complete'});await s.intervals[0]();
  assert.equal(s.timers.length,0);assert.equal(s.reloads(),0);
  s=await setup();s.document.dialogOpen=true;
  s.set({pending:false,revision:'done',status:'complete'});await s.intervals[0]();
  assert.equal(s.timers.length,0);assert.equal(s.reloads(),0);
  s.document.dialogOpen=false;await s.intervals[0]();
  assert.equal(s.timers.length,1);s.timers.shift()();assert.equal(s.reloads(),1);
  console.log('5 progress scenarios passed: auto refresh, edit preservation, hidden pause, edits during submission, open preview protection');
})().catch(error => { console.error(error); process.exitCode = 1; });
