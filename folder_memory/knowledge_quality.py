"""Deduplicate corpus records only when normalized text and meaning fields agree.
Preserve provenance; fuzzy matches and conflicting labels remain diagnostic.
"""

from collections import Counter, defaultdict
import hashlib
import json
import re
import unicodedata


DEDUP_POLICY = 'funny-semantic-dedup-v1'
_PROVENANCE = {'original_id', 'original_line', 'english_text', 'english_locale',
               'english_annot_utt', 'slot_method', 'role', 'text', 'provenance',
               'sources', 'aliases'}


def canonical_text(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def meaning_metadata(metadata):
    """Keep labels, frames, commands, slot annotation, locale and split exact."""
    result = {key: value for key, value in metadata.items() if key not in _PROVENANCE}
    if isinstance(result.get('entry'), dict):
        result['entry'] = {key: value for key, value in result['entry'].items()
                           if key not in {'id', 'sources'}}
    return result


def document_key(document):
    value = [document['kind'], canonical_text(document['text']),
             meaning_metadata(document.get('metadata', {}))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def deduplicate_documents(documents):
    groups = defaultdict(list)
    identities = {}
    for document in documents:
        if not isinstance(document, dict) or not all(isinstance(document.get(key), str) and document[key] for key in ('id', 'kind', 'text')):
            raise ValueError('Knowledge document needs a stable id, kind and text')
        if not isinstance(document.get('metadata', {}), dict):
            raise ValueError('Knowledge metadata must be an object')
        key = document_key(document)
        if document['id'] in identities and identities[document['id']] != key:
            raise ValueError('One stable knowledge id has conflicting meanings: '+document['id'])
        identities[document['id']] = key
        groups[key].append(document)
    result, merged_by_kind = [], Counter()
    for group in groups.values():
        group = sorted(group, key=lambda item: item['id'])
        # JSON-copy: no caller-owned frames, entries or source lists mutate.
        chosen = json.loads(json.dumps(group[0], ensure_ascii=False, allow_nan=False))
        if len(group) > 1:
            metadata = chosen.setdefault('metadata', {})
            members = []
            for item in group:
                members.extend(item.get('metadata', {}).get('provenance') or [
                    {'id': item['id'], 'source': item.get('source', ''),
                     'metadata': item.get('metadata', {})}])
            members = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in members}
            metadata['provenance'] = [members[key] for key in sorted(members)]
            metadata['sources'] = sorted({item.get('source', '') for item in members.values() if item.get('source')})
            metadata['aliases'] = sorted({item['id'] for item in members.values()})
            if isinstance(metadata.get('entry'), dict):
                metadata['entry']['sources'] = sorted({source for item in group for source in item.get('metadata', {}).get('entry', {}).get('sources', [])})
            merged_by_kind[chosen['kind']] += len(group) - 1
        result.append(chosen)
    result.sort(key=lambda item: item['id'])
    return result, {'policy': DEDUP_POLICY, 'input_documents': sum(map(len, groups.values())),
                    'output_documents': len(result), 'merged_documents': sum(merged_by_kind.values()),
                    'merged_by_kind': dict(sorted(merged_by_kind.items()))}


def audit_documents(documents):
    documents = list(documents)
    exact, normalized, word_forms = defaultdict(list), defaultdict(list), defaultdict(list)
    for item in documents:
        exact[(item['kind'], item['text'])].append(item)
        normal = canonical_text(item['text'])
        normalized[(item['kind'], normal)].append(item)
        word_forms[(item['kind'], ' '.join(re.findall(r'\w+', normal)))].append(item)
    def counts(groups):
        selected = [group for group in groups.values() if len(group) > 1]
        return {'groups': len(selected), 'extra_rows': sum(len(group) - 1 for group in selected),
                'conflicting_meaning_groups': sum(len({document_key(item) for item in group}) > 1 for group in selected)}
    _, dedup = deduplicate_documents(documents)
    fingerprint = hashlib.sha256()
    for item in sorted(documents, key=lambda item: item['id']):
        fingerprint.update(json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
    return {'documents': len(documents), 'kinds': dict(sorted(Counter(item['kind'] for item in documents).items())),
            'source_fingerprint': fingerprint.hexdigest(), 'exact_text': counts(exact),
            'normalized_text': counts(normalized), 'punctuation_variant_candidates': counts(word_forms),
            'deduplication': dedup, 'fuzzy_merge': False,
            'splits': dict(sorted(Counter(item.get('metadata', {}).get('split', 'builtin') for item in documents).items()))}
