import importlib.util
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
spec=importlib.util.spec_from_file_location('scanner',Path(__file__).resolve().parents[1]/'scanner.py')
scanner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)
class TestV4(unittest.TestCase):
    def test_price_exact(self):
        self.assertEqual(scanner.price_e({'listing_price':100000,'amount':1}),100000)
    def test_identity(self):
        r={'hash_code':'abc'}
        self.assertEqual(scanner.identity(r),'abc')
    def test_age(self):
        now=datetime(2026,11,2,8,0,tzinfo=timezone.utc)
        self.assertEqual(scanner.age_minutes('2026-11-02T07:55:00Z',now),5)
        self.assertIsNone(scanner.age_minutes(None,now))
    def test_unknown_timestamp(self):
        now=datetime.now(timezone.utc)
        a={'name':'Example','listing_price':4096,'amount':1,'hash_code':'a'}
        output,_,_=scanner.build([a],{}, {},now)
        self.assertEqual(output[0]['freshness'],'unknown')
        self.assertEqual(output[0]['category'],'unrated')
    def test_volatile_not_safe(self):
        peers=[{'name':'Example','hash_code':str(i),'listing_price':x,'amount':1} for i,x in enumerate([4096,5000,12000,50000,60000,65000,70000])]
        result=scanner.estimate(peers[0],peers,[{'median_e':50000}]*4)
        self.assertIsNotNone(result)
        self.assertNotEqual(result['category'],'candidate')
if __name__=='__main__':unittest.main()
