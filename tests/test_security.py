import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from web.auth import AuthStore
import web.app as appmod
from web.security import SecurityMiddleware


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100000.0
        self.sent = []
        self.auth = AuthStore(Path(self.tmp.name) / 'auth.db', 's'*48, lambda email, code: self.sent.append((email, code)), clock=lambda: self.now)
        self.auth.allow('owner@example.com', admin=True)
        self.auth.allow('member@example.com')
        self.env = patch.dict('os.environ', {'APP_ORIGIN': 'https://sgo.example.com'})
        self.env.start()
        self.store_patch = patch('web.security.store_from_env', return_value=self.auth)
        self.store_patch.start()
        appmod.app.middleware_stack = None
        self.client = TestClient(appmod.app, base_url='https://sgo.example.com')
        self.headers = {'X-SGO-Request': '1', 'Origin': 'https://sgo.example.com'}

    def tearDown(self):
        self.client.close()
        self.store_patch.stop()
        self.env.stop()
        appmod.app.middleware_stack = None
        appmod.sessions.clear()
        self.tmp.cleanup()

    def login(self, email='owner@example.com'):
        response = self.client.post('/auth/request-code', json={'email': email}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post('/auth/verify', json={'email': email, 'code': self.sent[-1][1]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_anonymous_all_business_routes_protected(self):
        for route in appmod.app.routes:
            if route.path.startswith('/api/'):
                path = route.path.replace('{sid}', 'unknown')
                for method in route.methods:
                    response = self.client.request(method, path, headers=self.headers)
                    self.assertEqual(response.status_code, 401, (path, response.text))
        self.assertIn(self.client.get('/docs').status_code, (401, 404))
        self.assertEqual(self.client.get('/', follow_redirects=False).status_code, 303)

    def test_login_cookie_and_absolute_expiration(self):
        response = self.login()
        cookie = response.headers['set-cookie']
        for value in ('HttpOnly', 'Secure', 'SameSite=strict', 'Max-Age=86400'):
            self.assertIn(value, cookie)
        self.now += 86399
        self.assertEqual(self.client.get('/api/config').status_code, 200)
        self.now += 1
        self.assertEqual(self.client.get('/api/config').status_code, 401)

    def test_csrf_and_host(self):
        self.login()
        for headers in ({}, {'X-SGO-Request': '1', 'Origin': 'https://evil.test'}):
            self.assertEqual(self.client.post('/api/session', json={'entity_text': 'x'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/config', headers={'Host': 'evil.test'}).status_code, 400)

    def test_owner_isolation_all_sid_routes(self):
        self.login()
        response = self.client.post('/api/session', json={'entity_text': 'private'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()['session_id']
        self.client.post('/auth/logout', headers=self.headers)
        self.login('member@example.com')
        for route in appmod.app.routes:
            if '{sid}' in route.path:
                for method in route.methods:
                    response = self.client.request(method, route.path.replace('{sid}', sid), headers=self.headers)
                    self.assertEqual(response.status_code, 404, route.path)

    def test_model_overrides_are_rejected(self):
        self.login()
        response = self.client.post('/api/infer-spec', json={'entity_text': 'x'}, headers={**self.headers, 'X-LLM-Base': 'http://169.254.169.254'})
        self.assertEqual(response.status_code, 400)

    def test_payload_and_workload_bounds(self):
        self.login()
        for body in ({'entity_text': 'x'*6001}, {'entity_text': 'x'*70000}):
            response = self.client.post('/api/session', json=body, headers=self.headers)
            self.assertIn(response.status_code, (413, 422))
        response = self.client.post('/api/cohort/generate', json={'description': 'x', 'segments': [{'label': 'x', 'count': 51}]}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        response = self.client.post('/api/cohort/generate', json={'description': 'x', 'segments': [{'label': 'x', 'count': 2}], 'parallel': 3}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_dataset_admin_and_fixed_path(self):
        self.login('member@example.com')
        self.assertEqual(self.client.post('/api/nemotron/setup', json={'dataset': 'USA'}, headers=self.headers).status_code, 403)
        self.client.post('/auth/logout', headers=self.headers)
        self.login()
        self.assertEqual(self.client.post('/api/nemotron/setup', json={'dataset': 'USA', 'path': '/tmp/other'}, headers=self.headers).status_code, 403)

    def test_revoke_is_immediate(self):
        self.login()
        self.auth.revoke('owner@example.com')
        self.assertEqual(self.client.get('/api/config').status_code, 401)

    def test_unknown_email_generic_response(self):
        response = self.client.post('/auth/request-code', json={'email': 'unknown@example.com'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent, [])

    def test_security_headers(self):
        response = self.client.get('/login')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])

    def test_duplicate_probe_parameter_rejected(self):
        from web.security import check_workload
        from web.auth import AuthError
        with self.assertRaises(AuthError):
            check_workload('/api/bias-audit/stream/x', None, {'probes': ['framing', 'framing,framing']})

    def test_expired_pending_tickets_do_not_block_new_ticket(self):
        import asyncio
        import time
        appmod.sessions['owned'] = {'owner': 'owner@example.com'}
        appmod._cf_pending.clear()
        appmod._cf_pending.update({str(i): {'ts': time.time()-601, 'sid': 'owned'} for i in range(100)})
        try:
            result = asyncio.run(appmod.prepare_counterfactual('owned', appmod.CounterfactualRequest(changes=[])))
            self.assertIn('ticket', result)
        finally:
            appmod._cf_pending.clear()

    def test_script_policy_rejects_arbitrary_inline_scripts(self):
        response = self.client.get('/login')
        directive = response.headers['content-security-policy'].split('script-src ')[1].split(';')[0]
        self.assertNotIn("'unsafe-inline'", directive)

    def test_missing_auth_configuration_fails_closed(self):
        with patch('web.security.store_from_env', side_effect=ValueError('missing secret')):
            appmod.app.middleware_stack = None
            response = self.client.get('/api/config')
            self.assertEqual(response.status_code, 503)

    def test_authenticated_stream_works_with_bounded_model_and_no_replay(self):
        self.login()
        sid = self.client.post('/api/session', json={'entity_text': 'hello'}, headers=self.headers).json()['session_id']
        self.client.post(f'/api/cohort/upload/{sid}', json=[{'name': 'Reviewer'}], headers=self.headers)
        fake = {'score': 5, 'action': 'neutral', 'reasoning': 'test', 'attractions': [], 'concerns': [], '_evaluator': {'name': 'Reviewer'}}
        with patch.object(appmod, 'evaluate_one', return_value=fake), patch.object(appmod, 'get_client', return_value=object()):
            response = self.client.get(f'/api/evaluate/stream/{sid}?parallel=2', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('event: complete', response.text)
        self.assertIn('event: start', response.text)

    def test_auth_body_is_bounded_even_without_content_length(self):
        def chunks():
            yield b'{"email":"'
            yield b'x' * 70000
            yield b'"}'
        response = self.client.post('/auth/request-code', content=chunks(), headers=self.headers)
        self.assertEqual(response.status_code, 413)

    def test_production_cookie_uses_host_prefix(self):
        response = self.login()
        cookie = response.headers['set-cookie']
        self.assertTrue(cookie.startswith('__Host-sgo_session='))
        self.assertNotIn('Domain=', cookie)

    def test_generated_long_profiles_are_saved_on_server_without_upload(self):
        self.login()
        sid = self.client.post('/api/session', json={'entity_text':'Short draft'}, headers=self.headers).json()['session_id']
        profiles = [{'name':f'Person {i}', 'persona':'x'*7001} for i in range(10)]
        with patch.object(appmod, 'generate_segment', return_value=profiles), patch.object(appmod, 'llm_from_request', return_value=(object(),'test')):
            response = self.client.post('/api/cohort/generate', json={'session_id':sid,'description':'Reviewers','dataset':'generated','segments':[{'label':'People','count':10}],'parallel':2}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get('cohort_saved'), 'generated profiles should be saved server-side')
        self.assertNotIn('cohort', response.json(), 'do not send a large panel to the browser only to re-upload it')
        self.assertEqual(len(appmod.sessions[sid]['cohort']),10)
        self.assertEqual(appmod.sessions[sid]['cohort'][0]['persona'],'x'*7001)
        self.assertTrue(self.client.get(f'/api/session/{sid}').json()['has_cohort'])
        fake={'score':5,'action':'neutral','reasoning':'test','attractions':[],'concerns':[],'_evaluator':{'name':'Reviewer'}}
        with patch.object(appmod,'evaluate_one',return_value=fake), patch.object(appmod,'get_client',return_value=object()):
            stream=self.client.get(f'/api/evaluate/stream/{sid}?parallel=2',headers=self.headers)
        self.assertEqual(stream.status_code,200)
        self.assertIn('event: complete',stream.text)

    def test_panel_generation_cannot_save_into_another_users_session(self):
        self.login()
        sid=self.client.post('/api/session',json={'entity_text':'Private'},headers=self.headers).json()['session_id']
        self.client.post('/auth/logout',headers=self.headers)
        self.login('member@example.com')
        with patch.object(appmod,'generate_segment') as generate, patch.object(appmod,'llm_from_request',return_value=(object(),'test')):
            response=self.client.post('/api/cohort/generate',json={'session_id':sid,'description':'Reviewers','dataset':'generated','segments':[{'label':'People','count':1}]},headers=self.headers)
        self.assertEqual(response.status_code,404)
        generate.assert_not_called()
        self.assertIsNone(appmod.sessions[sid]['cohort'])

    def test_generated_50_panel_uses_small_batches_with_token_cap(self):
        self.login()
        sid=self.client.post('/api/session',json={'entity_text':'Draft'},headers=self.headers).json()['session_id']
        sizes=[]
        def generate(client,model,label,count,description):
            sizes.append(count)
            return [{'name':f'{len(sizes)}-{i}','persona':'Short profile'} for i in range(count)]
        with patch.object(appmod,'generate_segment',side_effect=generate), patch.object(appmod,'llm_from_request',return_value=(object(),'test')):
            response=self.client.post('/api/cohort/generate',json={'session_id':sid,'description':'Reviewers','dataset':'generated','segments':[{'label':'People','count':50}],'parallel':2},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['cohort_size'],50)
        self.assertLessEqual(max(sizes),2,'large batches cannot fit the 2048-token output cap')

    def test_audit_budget_failure_returns_429_before_sse(self):
        from types import SimpleNamespace
        self.login()
        sid=self.client.post('/api/session',json={'entity_text':'draft'},headers=self.headers).json()['session_id']
        appmod.sessions[sid]['cohort']=[{'name':'A'}]
        for _ in range(99): self.auth.model_call('owner@example.com')
        model=SimpleNamespace(ensure_budget=lambda needed:self.auth.check_model_budget('owner@example.com',needed))
        with patch.object(appmod,'_llm_from_params',return_value=(model,'test-model')),patch.object(appmod,'run_paired_evaluation') as paired:
            response=self.client.get(f'/api/bias-audit/stream/{sid}?probes=order&sample=1',headers=self.headers)
        self.assertEqual(response.status_code,429)
        self.assertIn('Model-call',response.json()['detail'])
        self.assertIn('retry-after',response.headers)
        paired.assert_not_called()

    def test_bounded_concerns_reach_suggestion_handler(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.login()
        model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"changes": []}'))])))))
        with patch.object(appmod,'llm_from_request',return_value=(model,'test-model')):
            response=self.client.post('/api/suggest-changes',json={'entity_text':'draft','goal':'clarity','concerns':[f'Concern {i}' for i in range(15)]},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
