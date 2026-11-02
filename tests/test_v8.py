import unittest
import scanner

class V8(unittest.TestCase):
    def test_fixed_id_not_roll_scored(self):
        r={'stat_rolls':[{'displayName':'Intelligence','apiName':'rawIntelligence','statRoll':20,'rollPercentage':'NaN','statRange':{'low':20,'high':20,'fixed':True}}, {'displayName':'Mana Steal','apiName':'manaSteal','statRoll':15,'rollPercentage':90,'statRange':{'low':4,'high':16,'fixed':False}}]}
        self.assertEqual(scanner.roll_numbers(r), {'manaSteal':90.0})
    def test_no_false_potential_without_profile(self):
        r={'name':'Sample','listing_price':100,'amount':1,'stat_rolls':{'walkSpeed':95}}
        peers=[{'name':'Sample','listing_price':200+i,'amount':1,'hash_code':str(i),'stat_rolls':{'walkSpeed':95}} for i in range(12)]
        result=scanner.estimate(r,peers,[])
        if result: self.assertNotEqual(result['category'],'potential')
