"""Physical encoder budgets use actual token IDs, including special tokens."""

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from folder_memory.embeddings import Embedder, EmbeddingError


class TokenizerModel:
    enabled = True

    def __init__(self, path, **limits):
        self.config = {"dimensions": 2, "model_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "model": str(path), "context": 512, "batch_tokens": 512, "micro_batch_tokens": 512, **limits}
        self.lock = threading.RLock()
        self.tokenized, self.embedded = [], []

    def _start(self):
        pass

    def check_budget(self):
        pass

    def _tokens(self, text, add_special):
        raw = [1000 + byte for byte in text.encode()]
        return [1, *raw, 2] if add_special else raw

    def _request(self, path, body, timeout):
        if path == "/tokenize":
            self.tokenized.append(body)
            return {"tokens": self._tokens(body["content"], body["add_special"])}
        if path != "/v1/embeddings":
            raise AssertionError("Unexpected encoder endpoint")
        self.embedded.append(body)
        limit = min(self.config[k] for k in ["context", "batch_tokens", "micro_batch_tokens"])
        for item in body["input"]:
            tokens = self._tokens(item, True) if isinstance(item, str) else item
            if len(tokens) > limit:
                raise AssertionError("Physical batch overflow")
        return {"data": [{"index": i, "embedding": [3, 4]} for i in range(len(body["input"]))]}


class EmbeddingLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "model.gguf"
        self.path.write_bytes(b"fixture weights")

    def encoder(self, **limits):
        model = TokenizerModel(self.path, **limits)
        return Embedder(model), model

    def test_long_tracking_url_is_masked_before_truncation_and_cache_key(self):
        encoder, model = self.encoder()
        prefix = "Сравни цены на "
        encoder.query(prefix + "https://market.example/product?tracking=" + "a" * 6000 + " и покажи итог")
        encoder.query(prefix + "https://market.example/product?tracking=" + "b" * 6000 + " и покажи итог")
        self.assertEqual(len(model.embedded), 1)
        content = model.embedded[0]["input"][0]
        self.assertIsInstance(content, str)
        self.assertEqual(content, "query: Сравни цены на URL_1 (market.example) и покажи итог")
        self.assertNotIn("tracking", next(iter(encoder.cache)))

    def test_multibyte_inputs_fit_the_physical_batch_with_special_tokens(self):
        encoder, model = self.encoder()
        vectors = encoder.encode(["界" * 1200, "🙂" * 1200, "я" * 1200])
        self.assertEqual(len(vectors), 3)
        for tokens in model.embedded[0]["input"]:
            self.assertIsInstance(tokens, list)
            self.assertEqual(len(tokens), 512)
            self.assertEqual(tokens[0], 1)
            self.assertEqual(tokens[-1], 2)

    def test_query_and_passage_prefixes_remain_for_clipped_inputs(self):
        encoder, model = self.encoder(micro_batch_tokens=64)
        encoder.encode(["界" * 100], query=True)
        encoder.encode(["界" * 100])
        for body, prefix in zip(model.embedded, ["query: ", "passage: "]):
            tokens = body["input"][0]
            self.assertEqual(tokens[1:1 + len(prefix)], [1000 + byte for byte in prefix.encode()])
            self.assertEqual(len(tokens), 64)
            self.assertEqual(tokens[-1], 2)

    def test_limit_is_the_minimum_of_context_logical_and_physical_batch(self):
        for field in ["context", "batch_tokens", "micro_batch_tokens"]:
            with self.subTest(field=field):
                encoder, model = self.encoder(**{field: 80})
                encoder.encode(["界" * 100])
                self.assertEqual(len(model.embedded[0]["input"][0]), 80)

    def test_normal_inputs_retain_the_exact_old_string_encoding_contract(self):
        encoder, model = self.encoder()
        fingerprint=encoder.fingerprint
        result = encoder.encode(["Обычный запрос", "plain passage"])
        self.assertEqual(model.embedded[0]["input"], ["passage: Обычный запрос", "passage: plain passage"])
        self.assertEqual(result, [[0.6, 0.8], [0.6, 0.8]])
        self.assertEqual(encoder.fingerprint,fingerprint)

    def test_invalid_tokenizer_result_is_not_sent_to_the_encoder(self):
        for token in [True, -1, "12", None]:
            with self.subTest(token=token):
                encoder, model = self.encoder()
                original = model._request
                model._request = lambda path, body, timeout: {"tokens": [token]} if path == "/tokenize" else original(path, body, timeout)
                with self.assertRaises(EmbeddingError):
                    encoder.encode(["text"])
                self.assertEqual(model.embedded, [])

    def test_unusual_special_token_alignment_uses_a_verified_string_prefix(self):
        encoder, model = self.encoder(micro_batch_tokens=64)
        original = model._tokens
        model._tokens = lambda text, add_special: [1, *original(text, False), 2] if add_special else [9999, *original(text, False)]
        encoder.encode(["界" * 100])
        content = model.embedded[0]["input"][0]
        self.assertIsInstance(content, str)
        self.assertTrue(content.startswith("passage: "))
        self.assertLessEqual(len(model._tokens(content, True)), 64)

    def test_token_budget_does_not_rely_on_server_truncate_flag(self):
        encoder, model = self.encoder()
        encoder.encode(["界" * 1000])
        self.assertFalse(model.embedded[0]["truncate"])

    def test_invalid_input_and_budget_are_rejected_before_embedding(self):
        with self.assertRaises(EmbeddingError):
            self.encoder(micro_batch_tokens=0)
        encoder, _ = self.encoder()
        for texts in ["text", [1], [], ["text"] * 17]:
            with self.subTest(texts=texts), self.assertRaises(EmbeddingError):
                encoder.encode(texts)


if __name__ == "__main__":
    unittest.main()
