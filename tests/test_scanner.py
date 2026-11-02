import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scanner

class ScannerTests(unittest.TestCase):
    def test_price(self):
        self.assertEqual(scanner.unit_price({'listing_price': 4096, 'amount': 1}),4096)
        self.assertEqual(scanner.unit_price({'listing_price': 8192, 'amount': 2}),4096)
        self.assertIsNone(scanner.int_emeralds('NaN'))
    def test_all_listings_exported_even_without_comparables(self):
        entries=[{'name':'Example','listing_price':4096,'amount':1,'rarity':'rare'}]
        rows,_=scanner.process(entries,[],{'initialized':False,'ids':[]},'2026-11-02T00:00:00+00:00')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['category'],'unrated')
        self.assertEqual(rows[0]['price_e'],4096)
    def test_volatile_candidate_not_promoted(self):
        group=[{'name':'Paradox','listing_price':int(x*4096),'amount':1} for x in [4,12,20,21,22,23,24]]
        result=scanner.classify(group[0],group,[])
        self.assertNotEqual(result['category'],'candidate')
if __name__=='__main__': unittest.main()
