import unittest
import scanner

class TestIDAware(unittest.TestCase):
    def test_no_weights_with_tiny_sample(self):
        rows=[{'listing_price':10000+i*500,'amount':1,'stat_rolls':{'speed':i,'spell':i}} for i in range(6)]
        self.assertEqual(scanner.learn_id_weights(rows)[0],{})
    def test_weighting_can_change_similarity(self):
        a={'stat_rolls':{'speed':90,'spell':50}}
        b={'stat_rolls':{'speed':40,'spell':50}}
        self.assertLess(scanner.comparable(a,b,{'speed':1.8}),scanner.comparable(a,b,{'speed':1.0}))
    def test_invalid_prices(self):
        self.assertIsNone(scanner.price_e({'listing_price':1,'amount':1000}))
    def test_detect_association_when_data_sufficient(self):
        rows=[{'listing_price':5000+1000*i,'amount':1,'stat_rolls':{'speed':i*3,'spell':(i*7)%29}} for i in range(24)]
        weights,evidence=scanner.learn_id_weights(rows)
        self.assertIn('speed',weights)
        self.assertGreater(evidence['speed']['sample_size'],9)
    def test_estimate_handles_no_price(self):
        self.assertIsNone(scanner.estimate({'listing_price':0,'amount':1},[],[]))
if __name__=='__main__':unittest.main()
