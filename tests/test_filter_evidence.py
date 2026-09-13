import unittest,json
from types import SimpleNamespace
from web.app import extract_filters
class FilterEvidenceTests(unittest.TestCase):
 def client(self,payload):
  return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]))))
 def test_ungrounded_filters_are_dropped(self):
  value=extract_filters(self.client({'filters':{'sex':'Male','state':'CA','occupation':'not_in_workforce'},'evidence':{}}),'test','Venture capitalists investing in B2B SaaS',columns={'sex','state','occupation'})
  self.assertEqual(value,{})
 def test_wrong_occupation_mapping_is_dropped(self):
  value=extract_filters(self.client({'filters':{'occupation':'civil_engineer'},'evidence':{'occupation':'engineering managers'}}),'test','engineering managers',columns={'occupation'})
  self.assertEqual(value,{})
 def test_explicit_age_and_city_are_kept(self):
  value=extract_filters(self.client({'filters':{'age_min':25,'age_max':45,'city':'Chicago'},'evidence':{'age_min':'25–45','age_max':'25–45','city':'Chicago'}}),'test','Chicago residents aged 25–45',columns={'age','city'})
  self.assertEqual(value,{'age_min':25,'age_max':45,'city':'Chicago'})
 def test_team_size_is_not_age(self):
  value=extract_filters(self.client({'filters':{'age_min':5,'age_max':50},'evidence':{'age_min':'5-50','age_max':'5-50'}}),'test','Engineering managers of teams of 5-50 people',columns={'age'})
  self.assertEqual(value,{})
 def test_men_is_not_evidence_inside_women(self):
  value=extract_filters(self.client({'filters':{'sex':'Male'},'evidence':{'sex':'men'}}),'test','women in Chicago',columns={'sex'})
  self.assertEqual(value,{})
 def test_multiple_cities_and_occupations_are_supported(self):
  from datasets import Dataset
  from scripts.persona_loader import filter_personas
  ds=Dataset.from_list([{'city':'Chicago','occupation':'engineer','sex':'Female','state':'IL'}, {'city':'Boston','occupation':'designer','sex':'Male','state':'MA'}, {'city':'Austin','occupation':'manager','sex':'Male','state':'TX'}])
  found=filter_personas(ds,{'city':['Chicago','Boston'],'occupation':['engineer','designer'],'sex':['Male','Female'],'state':['IL','MA']})
  self.assertEqual(len(found),2)
 def test_invalid_model_response_does_not_silently_broaden(self):
  from fastapi import HTTPException
  with self.assertRaises(HTTPException):
   extract_filters(self.client({'unexpected':'payload'}),'test','Women in Chicago aged25-45',columns={'sex','city','age'})
