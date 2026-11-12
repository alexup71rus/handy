import unittest

from folder_memory.embeddings import Embedder
from tests import test_embedding_limits as fixtures


class EmbeddingPolicyTests(unittest.TestCase):
    setUp=fixtures.EmbeddingLimitTests.setUp
    encoder=fixtures.EmbeddingLimitTests.encoder

    def test_prefix_and_pooling_policies_have_separate_indexes(self):
        model=fixtures.TokenizerModel(self.path,dimensions=384,context=511)
        baseline=Embedder(model)
        self.assertEqual(baseline.fingerprint,model.config['model_sha256'])
        model.config.update(query_prefix='Instruct: Retrieve relevant text\nQuery: ',passage_prefix='',pooling='last')
        changed=Embedder(model)
        self.assertNotEqual(changed.fingerprint,baseline.fingerprint)
        changed.dimension=2;model.config['dimensions']=2
        changed.encode(['document'])
        changed.encode(['question'],query=True)
        self.assertEqual(model.embedded[0]['input'],['document'])
        self.assertEqual(model.embedded[1]['input'],['Instruct: Retrieve relevant text\nQuery: question'])

    def test_policy_change_invalidates_cached_query(self):
        encoder,model=self.encoder()
        encoder.query('same question')
        old=encoder.fingerprint
        model.config['query_prefix']='Question: '
        encoder.query('same question')
        self.assertNotEqual(encoder.fingerprint,old)
        self.assertEqual(len(model.embedded),2)
        self.assertEqual(model.embedded[1]['input'],['Question: same question'])


if __name__=='__main__':unittest.main()
