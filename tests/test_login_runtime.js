const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
let now=100000; let tick; let responder;
const elements=new Map();
function el(id){if(!elements.has(id))elements.set(id,{value:'',hidden:true,disabled:false,readOnly:false,textContent:'',className:'',handlers:{},addEventListener(k,f){this.handlers[k]=f;},focus(){}});return elements.get(id);}
const context={document:{getElementById:el},fetch:(...args)=>responder(...args),Date:class extends Date{static now(){return now;}},setInterval:f=>{tick=f;},window:{location:{replace(){}}}};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/static/login.js','utf8'),context);
(async()=>{
 el('email').value='person@example.com';
 responder=async()=>({ok:true,json:async()=>({message:'sent'}),headers:new Headers()});
 await el('emailForm').handlers.submit({preventDefault(){}});
 el('code').value='00001234';
 now+=61000;
 let finish;
 responder=()=>new Promise(resolve=>{finish=resolve;});
 const pending=el('emailForm').handlers.submit({preventDefault(){}});
 tick();
 assert.equal(el('sendCode').disabled,true,'timer must not unlock an in-flight resend');
 finish({ok:true,json:async()=>({message:'sent'}),headers:new Headers()});
 await pending;
 assert.equal(el('code').value,'','successful resend must clear the previous code');
 now+=61000;
 responder=async()=>({ok:false,status:429,json:async()=>({detail:'limited'}),headers:new Headers({'Retry-After':'1800'})});
 await el('emailForm').handlers.submit({preventDefault(){}});
 assert.match(el('status').textContent,/30/,'rate limit should explain actual wait');
 tick();assert.equal(el('sendCode').disabled,true);
 console.log('Login resend in-flight guard, stale-code clearing and retry time: ok');
})().catch(e=>{console.error(e);process.exitCode=1;});
