import asyncio,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from starlette.requests import Request
from web.auth import AuthStore,AuthError
import web.app as appmod
import bias_audit

class AuditFailureTests(unittest.TestCase):
    def test_model_budget_preflight_does_not_consume_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            auth=AuthStore(Path(folder)/'auth.db','x'*48,lambda *a:None)
            auth.allow('member@example.com')
            for _ in range(99): auth.model_call('member@example.com')
            with self.assertRaises(AuthError) as error:
                auth.check_model_budget('member@example.com',2)
            self.assertEqual(error.exception.status,429)
            self.assertIn('Model-call',str(error.exception))
            auth.model_call('member@example.com')

    def request(self):
        r=Request({'type':'http','method':'GET','path':'/','headers':[],'client':('127.0.0.1',1),'query_string':b'','scheme':'http','server':('test',80)})
        r.state.api_key=r.state.base_url=r.state.model=''
        return r

    def test_audit_checks_full_cost_before_starting_stream(self):
        appmod.sessions['audit-test']={'cohort':[{'name':'A'}]*3,'entity_text':'draft'}
        client=SimpleNamespace(ensure_budget=Mock(side_effect=AuthError('Model-call limit reached',429)))
        try:
            with patch.object(appmod,'_llm_from_params',return_value=(client,'model')):
                with self.assertRaises(AuthError):
                    asyncio.run(appmod.bias_audit_stream('audit-test',self.request(),sample=3))
            client.ensure_budget.assert_called_once_with(20)
        finally: appmod.sessions.pop('audit-test',None)

    def test_failed_audit_is_not_reported_complete(self):
        appmod.sessions['audit-test']={'cohort':[{'name':'A'}],'entity_text':'draft'}
        async def collect():
            response=await appmod.bias_audit_stream('audit-test',self.request(),probes='authority,order',sample=1)
            return [json.loads(e['data']) async for e in response.body_iterator if e['event']=='complete']
        try:
            with patch.object(appmod,'_llm_from_params',return_value=(object(),'model')),patch.object(appmod,'run_paired_evaluation',return_value=[{'error':'Model-call limit reached'}]):
                result=asyncio.run(collect())[0]
            self.assertEqual(result.get('status'),'failed')
            self.assertIn('Model-call',result.get('error',''))
            self.assertEqual(len(result['analyses']),1,'Stop when no valid pairs remain')
        finally: appmod.sessions.pop('audit-test',None)

    def test_probe_preserves_failure_reason(self):
        result=bias_audit.analyze_probe([{'error':'Model-call limit reached'}],'order','a','b')
        self.assertIn('Model-call',result['error'])

    def test_missing_score_is_not_a_zero_delta_success(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"reasoning":"no score"}'))])))))
        result=bias_audit.evaluate_one(client,'model',{'name':'A'},'draft')
        self.assertIn('error',result)

    def test_fractional_pair_scores_are_supported(self):
        with patch.object(bias_audit,'evaluate_one',side_effect=[{'score':5.5},{'score':6}]):
            rows=bias_audit.run_paired_evaluation(object(),'model',[{'name':'A'}],'a','b','a','b',1)
        self.assertEqual(rows[0]['delta'],0.5)

    def test_suggest_changes_preserves_quota_status(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=AuthError('Model-call limit reached',429,60)))))
        with patch.object(appmod,'llm_from_request',return_value=(client,'model')):
            with self.assertRaises(AuthError):
                asyncio.run(appmod.suggest_changes(appmod.SuggestChangesInput(entity_text='draft',goal='goal',concerns=['a']),self.request()))

    def test_partial_pairs_are_disclosed_without_calibration_claim(self):
        analysis=bias_audit.analyze_probe([{'delta':0},{'error':'Model-call limit reached'}],'order','a','b')
        self.assertEqual(analysis.get('failed_pairs'),1)
        report=bias_audit.generate_report([analysis],'test-model')
        self.assertIn('Incomplete sample',report)
        self.assertNotIn('WELL-CALIBRATED',report)

    def test_partial_probe_marks_stream_partial(self):
        appmod.sessions['audit-test']={'cohort':[{'name':'A'},{'name':'B'}],'entity_text':'draft'}
        async def collect():
            response=await appmod.bias_audit_stream('audit-test',self.request(),probes='order',sample=2)
            return [json.loads(e['data']) async for e in response.body_iterator if e['event']=='complete']
        try:
            with patch.object(appmod,'_llm_from_params',return_value=(object(),'model')),patch.object(appmod,'run_paired_evaluation',return_value=[{'delta':0},{'error':'Model-call limit reached'}]):
                result=asyncio.run(collect())[0]
            self.assertEqual(result.get('status'),'partial')
        finally: appmod.sessions.pop('audit-test',None)
