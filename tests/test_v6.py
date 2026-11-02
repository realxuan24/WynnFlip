import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scanner
from scripts.import_weights import normalize

class V6Tests(unittest.TestCase):
    def test_profile_quality_prefers_correct_id(self):
        profiles={'exampleitem':{'Main':{'manaregen':.8,'walkspeed':.2}}}
        x=scanner.profile_quality({'name':'Example Item','stat_rolls':{'manaRegen':90,'walkSpeed':40}},profiles)
        self.assertEqual(x['profile'],'Main')
        self.assertAlmostEqual(x['score'],80)
    def test_negative_id_weight_prefers_low(self):
        p={'exampleitem':{'Low HP':{'health':-1.0}}}
        self.assertEqual(scanner.profile_quality({'name':'Example Item','stat_rolls':{'health':10}},p)['score'],90)
    def test_missing_profile_is_unverified(self):
        self.assertIsNone(scanner.profile_quality({'name':'Unknown','stat_rolls':{'hp':99}},{}))
    def test_low_coverage_rejected(self):
        p={'example':{'Main':{'mana':.8,'walk':.2}}}
        self.assertIsNone(scanner.profile_quality({'name':'Example','stat_rolls':{'walk':99}},p))
    def test_import_normalized(self):
        obj={'Example':{'Main':{'mana':.8,'walk':.2}}}
        self.assertEqual(normalize(obj),obj)
    def test_zero_price_rejected(self):
        self.assertIsNone(scanner.price_e({'listing_price':1,'amount':100}))
if __name__=='__main__':unittest.main()
