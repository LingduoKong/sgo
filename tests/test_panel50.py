import unittest
from scripts.stratified_sampler import stratified_sample
from web.security import check_workload
from web.auth import AuthError

class Panel50Tests(unittest.TestCase):
    def test_exact_50_despite_rounding(self):
        profiles=[{'id':i,'group':i%7} for i in range(700)]
        result=stratified_sample(profiles,[lambda p:p['group']],total=50)
        self.assertEqual(len(result),50)
        self.assertEqual(len({p['id'] for p in result}),50)
    def test_no_more_than_50_with_many_strata(self):
        profiles=[{'id':i} for i in range(100)]
        self.assertEqual(len(stratified_sample(profiles,[lambda p:p['id']],total=50)),50)
    def test_50_allowed_but_51_rejected(self):
        check_workload('/api/cohort/generate',{'segments':[{'label':'a','count':50}],'parallel':2},{})
        with self.assertRaises(AuthError):
            check_workload('/api/cohort/generate',{'segments':[{'label':'a','count':51}]},{})
