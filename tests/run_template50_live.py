"""Explicit live-model batch test. Run manually, never discovered as unit tests.
Uses an isolated ASGI client/auth database and strictly bounded test-only budgets.
Does not alter production users, their quotas, or public authentication.
"""
import json, logging, tempfile, time, secrets, os
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from web.auth import AuthStore
import web.app as appmod

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('sgo').setLevel(logging.WARNING)
ORIGIN=os.environ.setdefault('APP_ORIGIN','https://sgo.example.com')
ROOT=Path(os.getenv('SGO_TEST_OUTPUT','/tmp/sgo50-results'))
ROOT.mkdir(exist_ok=True)
class BatchAuth(AuthStore):
    def charge(self,email,heavy=False):
        with self.db() as db:
            buckets=[('batch-requests',30,7200)]
            if heavy: buckets.append(('batch-heavy',6,7200))
            self._limits(db,buckets)
    def model_call(self,email):
        with self.db() as db:
            self._limits(db,[('batch-model-calls',int(os.getenv('SGO_TEST_MODEL_LIMIT','400')),7200)])

def events(text):
    parsed=[]
    for block in text.replace('\r\n','\n').split('\n\n'):
        kind='message'; data=[]
        for line in block.splitlines():
            if line.startswith('event:'):kind=line[6:].strip()
            if line.startswith('data:'):data.append(line[5:].lstrip())
        if data:parsed.append((kind,json.loads('\n'.join(data))))
    return parsed

def require(response):
    if response.status_code!=200:raise RuntimeError(f'HTTP {response.status_code}: {response.text[:400]}')
    return response.json()

out=[]
with tempfile.TemporaryDirectory(prefix='sgo50-auth-') as folder:
    codes=[]
    auth=BatchAuth(Path(folder)/'auth.db',secrets.token_urlsafe(48),lambda email,code:codes.append(code))
    auth.allow('qa-sgo@example.com')
    dataset=os.getenv('SGO_TEST_DATASET','USA')
    templates=json.loads(Path('/tmp/sgo50-templates.json').read_text())
    appmod.app.middleware_stack=None
    with patch('web.security.store_from_env',return_value=auth), TestClient(appmod.app,base_url=ORIGIN,raise_server_exceptions=False) as client:
        client.headers.update({'X-SGO-Request':'1','Origin':ORIGIN})
        require(client.post('/auth/request-code',json={'email':'qa-sgo@example.com'}))
        require(client.post('/auth/verify',json={'email':'qa-sgo@example.com','code':codes.pop()}))
        for name,text in templates.items():
            selected=os.getenv("SGO_TEST_TEMPLATES", "").split(",")
            if selected != [""] and name not in selected: continue
            start=time.monotonic(); record={'template':name,'requested':50,'dataset':dataset,'status':'running'}
            print(json.dumps(record),flush=True)
            try:
                sid=require(client.post('/api/session',json={'entity_text':text,'dataset':dataset}))['session_id']
                spec=require(client.post('/api/infer-spec',json={'entity_text':text}))
                require(client.patch(f'/api/session/{sid}',json={'goal':spec.get('goal',''),'audience':spec.get('audience','')}))
                segments=require(client.post('/api/suggest-segments',json={'entity_text':text,'audience_context':spec.get('audience','')})).get('segments',[])[:10]
                if not segments:raise RuntimeError('No segments returned')
                for i,segment in enumerate(segments):segment['count']=50//len(segments)+(i<50%len(segments))
                panel=require(client.post('/api/cohort/generate',json={'session_id':sid,'description':spec.get('audience',''),'audience_context':spec.get('audience',''),'dataset':dataset,'parallel':2,'segments':segments}))
                record.update(actual=panel['cohort_size'],saved=panel.get('cohort_saved'),filters=panel.get('filters'),audience=spec.get('audience'))
                if panel['cohort_size']!=50 or not panel.get('cohort_saved'):raise RuntimeError('Panel size/save mismatch')
                stream=client.get(f'/api/evaluate/stream/{sid}?parallel=2&bias_calibration=true')
                if stream.status_code!=200:raise RuntimeError(f'Evaluation HTTP {stream.status_code}: {stream.text[:200]}')
                ev=events(stream.text)
                complete=next((data for kind,data in ev if kind=='complete'),None)
                if complete is None:raise RuntimeError('Stream missing complete event')
                if complete.get('error'):raise RuntimeError(complete['error'])
                results=appmod.sessions[sid]['eval_results']
                valid=[r for r in results if isinstance(r.get('score'),(int,float)) and 1<=r['score']<=10]
                record.update(completed=len(results),valid_scores=len(valid),errors=len(results)-len(valid),average=round(sum(r['score'] for r in valid)/len(valid),2) if valid else None)
                report=client.get(f'/api/report/{sid}')
                record['report_ok']=report.status_code==200 and len(report.content)>100
                if report.status_code==200:(ROOT/(name+'.md')).write_text(report.text)
                (ROOT/(name+'.json')).write_text(json.dumps({'summary':record,'results':results,'complete':complete},ensure_ascii=False,indent=2))
                if len(valid)!=50 or not record['report_ok']:raise RuntimeError('Incomplete scores or report')
                record['status']='pass'
            except Exception as error:
                record['status']='fail';record['error']=str(error)
            record['seconds']=round(time.monotonic()-start,1)
            out.append(record)
            (ROOT/'summary.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
            print(json.dumps(record,ensure_ascii=False),flush=True)
        with auth.db() as db:
            row=db.execute("SELECT count FROM limits WHERE key='batch-model-calls'").fetchone()
            print(json.dumps({'actual_model_calls':row['count'] if row else 0}),flush=True)

raise SystemExit(0 if len(out) > 0 and all(r["status"] == "pass" for r in out) else 1)
