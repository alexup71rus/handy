"""Small multilingual encoder using the same bounded local model lifecycle."""

import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from collections import OrderedDict

from .literals import PromptLiterals


class EmbeddingError(ValueError):
    pass


class Embedder:
    def __init__(self, model):
        self.model = model
        self.cache = OrderedDict()
        self.cache_lock = threading.Lock()
        self.dimension = int(model.config.get("dimensions", 384))
        self.fingerprint = self._fingerprint()
        self._verified_state = None
        self._verified_ok = False

    def _policy(self):
        config=self.model.config
        prefixes={key:config.get(key,default) for key,default in [('query_prefix','query: '),('passage_prefix','passage: ')]}
        if any(not isinstance(value,str) or len(value)>1000 for value in prefixes.values()):
            raise EmbeddingError('Invalid encoder prefix policy')
        limit=config.get('max_input_chars',1200)
        if type(limit) is not int or not 1<=limit<=16000:raise EmbeddingError('Invalid encoder input limit')
        return {**prefixes,'pooling':config.get('pooling','mean'),'max_input_chars':limit,'dimensions':int(config.get('dimensions',384)),'token_limit':self._token_limit()}

    def _fingerprint(self):
        policy=self._policy();sha=self.model.config.get('model_sha256','')
        if self.model.config.get('backend') == 'openai':
            identity = {'base_url': self.model.config.get('base_url', '').rstrip('/'),
                        'model_id': self.model.config.get('model_id', self.model.config.get('model')),
                        'revision': self.model.config.get('revision')}
            return hashlib.sha256(json.dumps({'remote': identity, 'policy': policy, 'version': 1},
                                            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Preserve existing E5 snapshots only for the original encoding policy.
        # Custom pooling, prompts, dimensions or clipping get a separate index.
        legacy={'query_prefix':'query: ','passage_prefix':'passage: ','pooling':'mean','max_input_chars':1200,'dimensions':384,'token_limit':511}
        if policy==legacy:return sha
        return hashlib.sha256(json.dumps({'model_sha256':sha,'policy':policy,'version':1},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def _verify_model(self):
        # Serialize verification, process lifecycle and query-cache invalidation.
        with self.model.lock:
            if self.model.config.get('backend') == 'openai':
                fingerprint = self._fingerprint()
                if self.fingerprint != fingerprint:
                    with self.cache_lock:
                        self.cache.clear()
                self.fingerprint = fingerprint
                self.dimension = int(self.model.config.get('dimensions', 384))
                return self.model.enabled
            expected = self.model.config.get("model_sha256", "")
            name = self.model.config.get("model", "")
            try:
                path = Path(name).resolve(strict=True)
                stat = path.stat()
                state = (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                         stat.st_ctime_ns, stat.st_size, expected,self._fingerprint())
            except (OSError, ValueError, TypeError):
                state = None
            if state == self._verified_state and state is not None:
                return self._verified_ok and self.model.enabled
            if self._verified_state is not None:
                # A running worker retains its old mapped weights after replacement.
                close = getattr(self.model, "close", None)
                if close:
                    close()
            with self.cache_lock:
                self.cache.clear()
            self._verified_state, self._verified_ok = state, False
            self.fingerprint = self._fingerprint()
            self.dimension=int(self.model.config.get('dimensions',384))
            if state is None or not self.model.enabled or not isinstance(expected, str) or len(expected) != 64:
                self._verified_state = None
                return False
            try:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                current = path.stat()
                signature = lambda s: (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size)
                if signature(before) != state[1:6] or signature(after) != state[1:6] or signature(current) != state[1:6]:
                    self._verified_state = None
                    return False
                self._verified_ok = digest.hexdigest() == expected.lower()
            except OSError:
                self._verified_state = None
            return self._verified_ok

    @property
    def enabled(self):
        return self._verify_model()

    def _token_limit(self):
        try:
            if self.model.config.get('backend') == 'openai':
                limit = int(self.model.config.get('context', 512))
                if not 32 <= limit <= 32768:
                    raise ValueError()
                return limit
            limit = min(512, *(int(self.model.config.get(key, 512)) for key in ("context", "batch_tokens", "micro_batch_tokens")))
        except (ValueError, TypeError):
            raise EmbeddingError("Invalid encoder token budget") from None
        if limit < 1:
            raise EmbeddingError("Invalid encoder token budget")
        return limit

    def _tokenize(self, text, *, add_special):
        result = self.model._request("/tokenize", {"content": text, "add_special": add_special, "parse_special": True}, timeout=60)
        tokens = result.get("tokens") if isinstance(result, dict) else None
        if not isinstance(tokens, list) or not tokens or any(type(token) is not int or token < 0 for token in tokens):
            raise EmbeddingError("Encoder did not return valid token IDs")
        return tokens

    def _bounded_input(self, text, limit, minimum):
        if self.model.config.get('backend') == 'openai':
            # No remote tokenizer is assumed. UTF-8 bytes leave a conservative
            # token budget; send strings, never invented tokenizer IDs.
            prefix = text[:minimum]
            available = limit - 16 - len(prefix.encode('utf-8'))
            if available < 1:
                raise EmbeddingError('Encoder token budget cannot fit its required prefix')
            return prefix + text[minimum:].encode('utf-8')[:available].decode('utf-8', errors='ignore')
        # b12202 adds/parses special tokens for string inputs. Character caps
        # bound RPC size; physical batch limits require the actual token count.
        full = self._tokenize(text, add_special=True)
        if len(full) <= limit:
            return text
        bare = self._tokenize(text, add_special=False)
        overhead = len(full) - len(bare)
        if 0 <= overhead <= 16:
            for offset in range(overhead + 1):
                if full[offset:offset + len(bare)] != bare:
                    continue
                suffix = full[offset + len(bare):]
                available = limit - offset - len(suffix)
                if available > 0:
                    # Token-array input is passed through verbatim by b12202.
                    # Retain both BOS and terminal separator/EOS, including E5.
                    return full[:offset] + bare[:available] + suffix
        # Unusual tokenizers may not add special tokens as a simple wrapper.
        # Find a Unicode string prefix and verify its actual token count instead.
        left, right, candidate = minimum, len(text), None
        while left <= right:
            middle = (left + right) // 2
            part = text[:middle]
            if len(self._tokenize(part, add_special=True)) <= limit:
                candidate, left = part, middle + 1
            else:
                right = middle - 1
        if candidate is None or len(self._tokenize(candidate, add_special=True)) > limit:
            raise EmbeddingError("Encoder token budget cannot fit its required prefix")
        return candidate

    def encode(self, texts, *, query=False):
        if not isinstance(texts, (list, tuple)) or not texts or len(texts) > 16 or any(not isinstance(text, str) for text in texts):
            raise EmbeddingError("Embedding batch must contain 1–16 texts")
        if not self._verify_model():
            raise EmbeddingError("Embedding model hash mismatch or file unavailable")
        policy=self._policy()
        prefix = policy['query_prefix'] if query else policy['passage_prefix']
        with self.model.lock:
            self.model._start()
            self.model.last_used = time.monotonic()
            limit = self._token_limit()
            inputs = [self._bounded_input(prefix + text[:policy['max_input_chars']], limit, len(prefix)) for text in texts]
            result = self.model._request("/v1/embeddings", {"model": self.model.config.get("name", "e5"), "input": inputs, "encoding_format": "float", "truncate": False}, timeout=60)
            self.model.last_used = time.monotonic()
            if not self._verify_model():
                raise EmbeddingError("Embedding model changed during encoding")
        try:
            data = sorted(result["data"], key=lambda item: item["index"])
            if len(data) != len(texts) or [item["index"] for item in data] != list(range(len(texts))):
                raise EmbeddingError("Embedding count/order mismatch")
            vectors = [item["embedding"] for item in data]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("Encoder did not return embeddings") from exc
        normalized = []
        for vector in vectors:
            if len(vector) != self.dimension or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise EmbeddingError("Invalid embedding dimensions or values")
            norm = math.sqrt(sum(value * value for value in vector))
            if norm < 1e-12:
                raise EmbeddingError("Zero embedding")
            normalized.append([value / norm for value in vector])
        return normalized

    def query(self, text):
        if not self._verify_model():
            raise EmbeddingError("Embedding model hash mismatch or file unavailable")
        # Exact URLs stay in the kernel. Tracking parameters and long encoded
        # paths carry no useful intent and must not dominate the encoder/cache.
        key = PromptLiterals(text).mask(text)[:1200]
        with self.cache_lock:
            item = self.cache.get(key)
            if item and time.monotonic() - item[0] < 300:
                self.cache.move_to_end(key)
                return item[1]
        vector = self.encode([key], query=True)[0]
        with self.cache_lock:
            self.cache[key] = (time.monotonic(), vector)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        return vector
