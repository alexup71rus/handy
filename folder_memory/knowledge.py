"""Small, source-backed reference packs. Examples are data, never tool plans."""

import json
import re
from functools import lru_cache
from pathlib import Path

from .catalog import resource
from .literals import policy_text
from .request_mode import compound_parts, conceptual_question, needs_observation, specific_observation, system_request_scope


_TERMINAL = re.compile(r'\b(?:команд\w*|утилит\w*|консол\w*|терминал\w*|cli|shell|bash|zsh|unix|linux|macos|terminal|utility|command)\b', re.I)
_TECHNICAL_TASK = re.compile(r'\b(?:искать|поиск\w*|найти|вывести|посмотр\w*|посчит\w*|сосчит\w*|подсч[иеё]\w*|счита\w*|проверить|читать|прочитать|определить|фильтр\w*|извлечь|сортиров\w*|сравнить|count\w*|filter\w*|list\w*|print\w*|search\w*|find|show|read)\b', re.I)
_TECHNICAL_OBJECT = re.compile(r'\b(?:файл\w*|каталог\w*|пап(?:к|ок)\w*|директор\w*|строк\w*|байт\w*|процесс\w*|сокет\w*|порт\w*|json|yaml|csv|tsv|files?|directories|folders?|lines?|bytes?|process(?:es)?|sockets?|ports?)\b', re.I)
_GUIDANCE = re.compile(
    r'^(?:(?:а|ну|please|пожалуйста)[,\s]+)?'
    r'(?:как|ч[её]м|в\s+ч[её]м|како\w*\s+(?:инструмент\w*|команд\w*|утилит\w*|способ\w*)|'
    r'где|куда|почему|зачем|можно\s+ли|для\s+чего|'
    r'что\s+(?:такое|означает|делает|выводит)|объясни\w*|подскажи\w*|справк\w*|пример\w*|'
    r'explain|why|how|which|what|is|are|does|do|when|where|can\s+(?!you\b)\w+)\b', re.I)
_GUIDE_CONTEXT = re.compile(
    r'^(?:(?:in|on)\s+[^,?!\n]{1,80},\s+|(?:на|в)\s+(?:linux|debian|ubuntu|fedora|macos|маке|линуксе)[,\s]+|'
    r'[A-Za-z][\w. +]{0,32}:\s+)', re.I)
_GUIDE_DIRECTIVE = re.compile(r'^(?:покажи\w*|выведи\w*|show|give)\s+(?:мне\s+|me\s+)?(?:как|чем|пример\w*|справк\w*|команду|how|an?\s+example|the\s+command)\b', re.I)
_COMPARISON_HEAD = re.compile(r'^(?:(?:а|пожалуйста|please)[,\s]+)?(?:сравни(?:те)?|сопоставь(?:те)?|compare)\b', re.I)
_COMPOSITION = re.compile(
    r'\b(?:сочета\w*|совмещ\w*|совмест\w*|комбинир\w*|сравн\w*|сопостав\w*|отлич\w*|разниц\w*|'
    r'объясни\w*|вместе|combin\w*|compar\w*|differ\w*|explain\w*|together|interact\w*)\b', re.I,
)
_EXECUTE = re.compile(r'^(?:(?:пожалуйста|можешь|давай|please|can\s+you|could\s+you)[,\s]+)*(?:выполни\w*|запусти\w*|перезапусти\w*|останови\w*|удали\w*|установи\w*|открой\w*|(?:по|со|под)считай\w*|run|execute|restart|start|stop|kill|delete|install|open|count)\b', re.I)
_NAMED = re.compile(r'\b(?:команд(?:а|у|ы|е|ой)|утилит(?:а|у|ы|е|ой)|the\s+(?:command|utility)|(?:command|utility)(?=\s+[`\"\']))\s+[`\"\']?([A-Za-z][A-Za-z0-9_.+-]*?)(?=$|[^\w.-]|\.(?=$|\s))', re.I)
_BARE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_.+-]{0,63}')
_BINARY_PREFIX = re.compile(r'(?<![\w./])/(?:usr/)?s?bin/(?=[\w.-]+(?:[`\"\'\s!?;,]|$))')
_PATH = re.compile(r'''(?<![\w./])(?:~?/|\.\.?/)[^\s`"'«»<>]+''')
_AMBIGUOUS_NAMES = frozenset('cat cd cut echo file find free head ip log node ps sort stat tail'.split())
_COMMAND_DEFINITION = re.compile(
    r'^(?:(?:а|ну|please|пожалуйста)[,\s]+)?(?:what\s+is|how\s+(?:do\s+i|can\s+i|to)\s+use|'
    r'что\s+такое|как\s+(?:работает|использовать|пользоваться))\s+$', re.I)
_COMMAND_SUBJECT = re.compile(r'^(?:(?:а|ну|please|пожалуйста)[,\s]+)?(?:как|how\s+does)\s+$', re.I)
_DEFINITION_QUESTION = re.compile(
    r'(?:(?:а|так|please|скажи(?:те)?|поясни(?:те)?|объясни(?:те)?|tell\s+me)[,\s]+)*'
    r'(?:что\s+(?:такое|значит|означает)|what\s+is)\s+(?P<term>[^\n.!?…]{1,96})[.!?…]*', re.I)
_DEFINITION_COMPLETION = re.compile(
    r'(?P<term>[\w -]{1,96}?)\s*(?:[—–-]\s*)?(?:это|is)\s+(?:\.{2,}|…+)\s*\??', re.I)
_PLATFORMS = {
    'linux': re.compile(r'\b(?:linux|линукс\w*|debian|ubuntu|fedora|systemd|gnu\s+sed|gsed)\b', re.I),
    'osx': re.compile(r'\b(?:macos|os\s+x|mac(?:book)?|мак(?:е|а|ом|бук\w*)?|unified\s+log(?:ging)?|bsd\s+sed)\b', re.I),
}
_EXCLUDED_COMMANDS = {'rm', 'cp', 'mv', 'chmod', 'chown'}
_COMPOSE_OPTIONS = r"""(?:\s+(?:-f|-p|--file|--project-name|--profile|--env-file|--project-directory)(?:=|\s+)(?:"[^"]+"|'[^']+'|[^\s\x60"';&|]+))*"""
_REFERENCE_THRESHOLD = .77
_TOPIC_THRESHOLD = .70
_TOPIC_MARGIN = .06
_TLDR_PATH = Path(__file__).resolve().parents[1]/'data/tldr-reference.json'
_CLI_PATH = Path(__file__).with_name('resources')/'cli-knowledge.json'


def _surface(question):
    text = re.sub(r'```[\s\S]*?(?:```|$)', ' ', policy_text(question)).strip()
    return _PATH.sub(' __path__ ', _BINARY_PREFIX.sub('', text))


def _admit(question, surface):
    if not surface.strip() or _EXECUTE.match(surface) or specific_observation(question):
        return False
    scope = system_request_scope(question)
    return scope != 'prohibition' and (scope != 'live' or bool(_GUIDE_DIRECTIVE.match(surface)))


def _guidance_head(surface):
    return bool(_GUIDANCE.match(_GUIDE_CONTEXT.sub('', surface)) or _GUIDE_DIRECTIVE.match(surface))


def _definition_subject(surface):
    if match := _DEFINITION_QUESTION.fullmatch(surface) or _DEFINITION_COMPLETION.fullmatch(surface):
        return ' '.join(match['term'].strip(' `«»"\'').casefold().split())
    return ''


def reference_question(question):
    """Distinguish a request for guidance from a request to observe or act."""
    surface = _surface(question)
    return _admit(question, surface) and bool(_guidance_head(surface) or _definition_subject(surface)
                                             or _TERMINAL.search(surface) and conceptual_question(question))


def _reference_domain(surface):
    return bool(_TERMINAL.search(surface) or _TECHNICAL_OBJECT.search(surface) and _TECHNICAL_TASK.search(surface))


def _rule_score(item, surface):
    return max((len(rule) for rule in item.get('match_any', ())
                if all(re.search(pattern, surface, re.I) for pattern in rule)), default=0)


@lru_cache(maxsize=256)
def _command_pattern(name):
    # Flags such as ssh -G and ssh -g have different meanings.
    parts = [re.escape(part) if part.startswith('-') else '(?i:' + re.escape(part) + ')' for part in name.split()]
    for prefix in ('docker compose ', 'docker-compose '):
        if name.startswith(prefix):
            parts[len(prefix.split()) - 1] += _COMPOSE_OPTIONS
            break
    return re.compile(r'(?<![\w./-])' + r'\s+'.join(parts) + r'(?=$|[^\w./-]|\.(?=$|\s))')


def _command_definition(before, after):
    if not _COMMAND_DEFINITION.match(before):
        return False
    suffix = after.strip(' .!?\t')
    return not suffix or bool(re.match(r'^(?:in|on|в|на)\s+', suffix, re.I) and (
        _TERMINAL.search(suffix) or any(pattern.search(suffix) for pattern in _PLATFORMS.values())
    ))


def _exact_entries(entries, surface):
    named = _NAMED.search(surface)
    spans = []
    for item in entries:
        for spelling in (item['name'], *item.get('command_aliases', ())):
            for hit in _command_pattern(spelling).finditer(surface):
                before, after = surface[:hit.start()], surface[hit.end():]
                if spelling in _AMBIGUOUS_NAMES and not (
                    surface.strip('`\"\' .?!') == hit[0]
                    or before.endswith(('`', '"', "'")) or re.match(r'\s+-[A-Za-z]', after)
                    or named and named.start(1) == hit.start()
                    or re.search(r'\b(?:назначение|означает|делает|explain|what\s+does)\s+$', before, re.I)
                    or _command_definition(before, after)
                    or _COMMAND_SUBJECT.match(before) and _reference_domain(after)
                ):
                    continue
                spans.append((hit.start(), hit.end(), item))
    if named and not any(start == named.start(1) for start, _, _ in spans):
        return []
    # A qualified command owns its words. Its parent and any command used as
    # a subcommand (for instance ps in docker compose ps) cannot compete.
    selected, end = [], -1
    for start, stop, item in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start >= end:
            selected.append((start, stop, item))
            end = stop
        elif selected and (start, stop) == selected[-1][:2]:
            selected.append((start, stop, item))
    return list({item['id']: item for _, _, item in selected}.values())


def _platform_entries(entries, surface):
    platforms = {name for name, pattern in _PLATFORMS.items() if pattern.search(surface)}
    if not platforms:
        return list(entries)
    specific = {item['name'] for item in entries if item.get('platform') in platforms}
    return [item for item in entries if item.get('platform') in platforms
            or item.get('platform', 'common') == 'common' and item['name'] not in specific]


def _card(item, score, *, match, identity=None):
    label = item.get('title', item['name'])
    text = '\n'.join([label + ': ' + item['description'], *item['signatures'], *item['caveats']])
    return {'id': identity or item['id'], 'name': item['name'], 'platform': item.get('platform', 'common'),
            'text': text[:4000], 'source': item['sources'][0], 'sources': item['sources'],
            'score': score, 'match': match, **({'semantic': True} if match == 'semantic' else {})}


def _topic(item, score, match):
    return {'id': 'topic:'+item['id'], 'title': item['title'], 'text': item['text'],
            'source': item['sources'][0], 'sources': item['sources'], 'score': score,
            'match': match, 'answer_origin': 'reference'}


def _technology(item):
    return item['id'].partition('.')[0]


@lru_cache(maxsize=1024)
def _literal_pattern(term):
    term = re.escape(term.removesuffix('()'))
    return re.compile(r'(?<![\w.$@-])' + term + r'(?![\w.$@-])', re.I)


def _technical_literal(term):
    return bool(re.fullmatch(r'[A-Za-z_$@][\w.:@$/-]*(?:\(\))?', term)
                and (re.search(r'[a-z][A-Z]|[._:$@/()]', term) or len(term) >= 3 and term.isupper()))


def _api_terms(item):
    if identifiers := item.get('identifiers'):
        return identifiers
    terms = item.get('terms')
    if not terms:
        return ()
    term = terms[0].split(maxsplit=1)[0]
    return (term,) if _technical_literal(term) else ()


def _has_api_term(item, surface):
    return any(_literal_pattern(term).search(surface) for term in _api_terms(item))


def _mentioned_technologies(surface):
    return {name for name, info in resource('developer-knowledge.json')['technologies'].items()
            if any(_literal_pattern(alias).search(surface) for alias in info['aliases'])}


@lru_cache(maxsize=1)
def _reference_entries(path, tldr_mtime, builtin_mtime):
    def key(item):
        return item['name'], item.get('platform', 'common')

    entries={key(item):dict(item) for item in resource('cli-knowledge.json')['entries']}
    for item in json.loads(path.read_text()).get('entries',[]) if path.exists() else []:
        if key(item) in entries:
            base=entries[key(item)]
            for field in ['signatures','aliases','sources']:
                base[field]=list(dict.fromkeys([*base[field],*item[field]]))
            base['description']+=' '+item['description']
        else:entries[key(item)]=item
    return tuple(item for item in entries.values() if item['name'] not in _EXCLUDED_COMMANDS)


class ReferenceLibrary:
    def __init__(self, retrieval=None):
        self.retrieval=retrieval

    @staticmethod
    def entries():
        return _reference_entries(_TLDR_PATH, _TLDR_PATH.stat().st_mtime_ns if _TLDR_PATH.exists() else None,
                                  _CLI_PATH.stat().st_mtime_ns)

    @classmethod
    def mentions_command(cls, text):
        return bool(_exact_entries(cls.entries(), _surface(text)))

    @staticmethod
    def developer_topics():
        return resource('developer-knowledge.json')['entries']

    @classmethod
    def topics(cls):
        return (*resource('practical-knowledge.json')['entries'], *cls.developer_topics())

    @classmethod
    def developer_catalog(cls):
        groups = resource('developer-knowledge.json')['technologies']
        result = {key: {'technology': info['name'], 'topics': []} for key, info in groups.items()}
        for item in cls.developer_topics():
            result[_technology(item)]['topics'].append({key: item[key] for key in ('id', 'title', 'sources')})
        return result

    @classmethod
    def topic_search(cls, question, limit=2):
        """Match explicit topics after the caller selects general knowledge.
        Live/private queries require observation or clarification instead.
        """
        if not isinstance(question,str) or not 1<=limit<=3:return []
        surface=_surface(question)
        if not _admit(question, surface):
            return []
        if subject := _definition_subject(surface):
            definitions = [item for item in cls.topics()
                           if any(term.casefold() == subject for term in item.get('definition_terms', ()))]
            if len(definitions) == 1:
                return [_topic(definitions[0], 1, 'definition')]
        if technologies := _mentioned_technologies(surface):
            direct = [item for item in cls.developer_topics()
                      if _technology(item) in technologies and _has_api_term(item, surface)]
            if len(direct) == 1:
                return [_topic(direct[0], 1, 'api_term')]
        matches=[]
        for item in cls.topics():
            score=_rule_score(item, surface)
            if not score:continue
            matches.append(_topic(item, score, 'explicit_topic'))
        matches.sort(key=lambda item:(-item['score'],item['id']))
        if len(matches)>1 and matches[0]['score']==matches[1]['score']:
            # Do not silently decide between two equally explicit topics.
            return []
        return matches[:1]

    def semantic_topics(self, question):
        if not self.retrieval or not reference_question(question):
            return []
        surface = _surface(question)
        technologies = _mentioned_technologies(surface)
        eligible = {'topic:'+item['id']: item for item in self.topics()
                    if any(re.search(pattern, surface, re.I) for pattern in item.get('anchors', ()))
                    or item.get('terms') and (_technology(item) in technologies
                                              or not technologies and _has_api_term(item, surface))}
        if not eligible:
            return []
        hits = self.retrieval.search(surface, kinds=['topic'], limit=2)
        if not hits or hits[0]['id'] not in eligible or hits[0]['score'] < _TOPIC_THRESHOLD:
            return []
        if len(hits) > 1 and hits[0]['score'] - hits[1]['score'] < _TOPIC_MARGIN:
            return []
        return [_topic(eligible[hits[0]['id']], hits[0]['score'], 'semantic_topic')]

    def guidance(self, question):
        if topics := self.topic_search(question) or self.semantic_topics(question):
            return topics
        surface = _surface(question)
        if (subject := _definition_subject(surface)) and not _NAMED.search(surface):
            # An ecosystem named in a concept question is context for that
            # concept. A card about its interpreter alone does not answer it.
            name = re.split(r'\s+(?:в|на|in|on)\s+', subject, maxsplit=1)[0] if _TERMINAL.search(surface) else subject
            if not any(name == item['name'].casefold() for item in self.entries()):
                return []
        return self.search(question)

    def answer_sources(self, question, *, guidance=False):
        """Select bounded evidence for a conversational answer, never a tool plan."""
        if (topics := self._composed_topics(question)) is not None:
            return topics
        return self.guidance(question) if guidance else self.topic_search(question)

    @classmethod
    def _composed_topics(cls, question):
        if not isinstance(question, str) or len(question) > 1000 or not _COMPOSITION.search(question):
            return None
        surface = _surface(question)
        if (not (_guidance_head(surface) or _COMPARISON_HEAD.match(surface)) or not _COMPOSITION.search(surface)
                or not _admit(question, surface) or needs_observation(question) or compound_parts(question)):
            return None
        technologies = _mentioned_technologies(surface)
        if len(technologies) != 1:
            return None
        spans, entries = {}, {}
        for item in cls.developer_topics():
            if _technology(item) not in technologies:
                continue
            for term in _api_terms(item):
                for hit in _literal_pattern(term).finditer(surface):
                    span = hit.span()
                    if span in spans and spans[span] != item['id']:
                        return []
                    spans[span] = item['id']
                    entries[item['id']] = item
        if len(entries) < 2:
            return None
        if len(entries) > 3:
            return []
        # Two catalog names can overlap without identifying two user mentions.
        ends = {}
        for (start, end), key in sorted(spans.items()):
            if any(other != key and last_end > start for other, last_end in ends.items()):
                return []
            ends[key] = max(end, ends.get(key, 0))
        return [_topic(entries[key], 1, 'api_term') for key in ends]

    def search(self, question, limit=3):
        if not isinstance(question, str) or not 1 <= limit <= 20:
            return []
        entries = self.entries()
        if bound := next((item for item in entries if item['id'] == question), None):
            return [_card(bound, 10, match='command')]
        surface = _surface(question)
        if not _admit(question, surface):
            return []
        commands = _exact_entries(entries, surface)
        eligible = _platform_entries(entries, surface)
        eligible_by_id = {item['id']: item for item in eligible}
        explicit = [item for item in commands if item['id'] in eligible_by_id]
        # An explicitly named unsupported command must never be replaced by a
        # nearby utility, including when the name exists on a different OS.
        if (commands and not explicit) or (not commands and (_NAMED.search(surface) or _BARE_NAME.fullmatch(surface))):
            return []
        generic = {item['name'] for item in explicit if item.get('platform', 'common') == 'common'}
        explicit = [item for item in explicit if item.get('platform', 'common') == 'common' or item['name'] not in generic]
        names = {item['name'] for item in explicit}
        rules = [(_rule_score(item, surface), item) for item in eligible
                 if not names or any(item['name'].startswith(name + ' ') for name in names)]
        rules = [(score, item) for score, item in rules if score]
        if rules:
            best = max(score for score, _ in rules)
            selected = [item for score, item in rules if score == best]
            if len(selected) == 1:
                return [_card(selected[0], best, match='task')]
        if explicit:
            return [_card(item, 10, match='command') for item in explicit[:limit]]
        if not self.retrieval or not _reference_domain(surface):
            return []

        from .command_policy import validate_read_recipe
        matches = []
        for hit in self.retrieval.search(surface, kinds=['reference','recipe'], limit=max(limit, 8)):
            if hit['score'] < _REFERENCE_THRESHOLD:
                continue
            if hit['kind'] == 'reference':
                if current := eligible_by_id.get(hit['id'].removeprefix('reference:')):
                    matches.append(_card(current, hit['score'], match='semantic', identity=hit['id']))
            else:
                command = hit['metadata']['command']
                try:
                    validate_read_recipe(command)
                except ValueError:
                    continue
                matches.append({'id':hit['id'],'text':hit['text']+'\n'+command,'source':hit['source'],
                                'score':hit['score'],'match':'semantic','semantic':True})
        return matches[:limit]
