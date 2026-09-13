const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function run(chunks, status=200) {
  let calls = 0; let seen;
  const encoder = new TextEncoder();
  const context = {
    URL, Headers, Request, Response, AbortController, EventTarget, MessageEvent, TextDecoder,
    location: {href:'https://sgo.example.com/', origin:'https://sgo.example.com', replace(){}},
    document: {addEventListener(){}},
    addEventListener(){},
    fetch: async (url, options) => {
      calls++; seen=options;
      if (status !== 200) return new Response(JSON.stringify({detail:'Quota reached'}), {status});
      return new Response(new ReadableStream({start(controller){for (const chunk of chunks) controller.enqueue(encoder.encode(chunk)); controller.close();}}));
    }
  };
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('web/static/access.js','utf8')+'\nthis.Source=SGOEventSource;',context);
  const source=new context.Source('/api/evaluate/stream/test');
  const messages=[];
  await new Promise((resolve,reject)=>{
    source.addEventListener('progress', event=>messages.push(JSON.parse(event.data)));
    source.addEventListener('complete', event=>{messages.push(JSON.parse(event.data));source.close();resolve();});
    source.onerror=event=>{source.close();status===200?reject(new Error(event.data)):resolve();};
  });
  assert.equal(calls,1,'stream must never automatically reconnect');
  assert.equal(seen.headers.get('X-SGO-Request'),'1');
  return messages;
}
(async()=>{
  const messages=await run(['event: progress\r','\ndata: {"n":1}\r\n\r','\nevent: complete\ndata: {"ok":true}\n\n']);
  assert.deepEqual(messages,[{n:1},{ok:true}]);
  await run([],429);
  console.log('Authenticated SSE chunk parsing, CSRF header and no reconnect: ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
