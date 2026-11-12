"""Shared request cues for reference evidence and clock helpers."""

import re

from .catalog import resource, tokens
from .dialogue_context import refers_to_dialogue
from .literals import policy_text, extract_urls, url_spans


_POLICY=resource('request-policy.json')
_MEMORY_SIGNALS = resource('understanding.json')['signals']
_RECALL = re.compile('|'.join(_MEMORY_SIGNALS['recall']), re.I)
_SAVED_FACT = re.compile(
    r'\b(?:из\s+памяти|(?:личн\w*|подтвержд[её]нн\w*)\s+памят\w*|'
    r'сохран[её]нн\w*\s+(?:номер|телефон|контакт|адрес|факт|значени|запис)\w*|'
    r'(?:personal|confirmed)\s+memory|saved\s+(?:number|phone|contact|address|fact|value|record))\b', re.I,
)
_SYSTEM_STATE = re.compile('|'.join(_MEMORY_SIGNALS['system_state']), re.I)


def _words(values):return '(?:'+'|'.join(re.escape(value) for value in values)+')'


_NEGATIVE=re.compile(r"\b(?:не\s+(?:(?:надо|нужно|следует|стоит)\s+)?"+_words([*_POLICY['directive_forms'],*_POLICY['infinitives']])+r"|(?:do\s+not|don't|never)\s+(?:run|send|show|execute|read|save|delete|check|open|install|inspect|stop|restart|connect|create))\b",re.I)
_OBSERVE=re.compile(r"^\s*(?:(?:пожалуйста[, ]*|можешь\s+|можно\s+|давай\s+|please\s+|а\s+|ну\s+|теперь\s+|(?:тогда|then)[,\s]+))*"+_words(_POLICY['observe_forms'])+r"\b",re.I)
_GUIDE = re.compile(r"^\s*(?:а\s+)?(?:как\s+(?:мне\s+)?[а-яё]{3,}(?:ться|ть|ти)\b|how\s+(?:to|do I|can I)\s+|"
                    r"(?:покажи|приведи)\s+пример\b)", re.I)
_ACTION_LEAD = r"^\s*(?:(?:пожалуйста|можешь|давай|теперь|тогда|а|please|then|can you|could you)[,\s]+)*"
_ACTION_PREFIX = re.compile(_ACTION_LEAD, re.I)
_COMMAND_QUESTION = r'(?:какой\s+командой|какую\s+команду|which\s+command|what\s+command)'
_COMMAND_GUIDE = re.compile(
    _ACTION_LEAD + r'(?:(?:просто|только|just|only)\s+)*'
    r'(?:(?:напиши|подскажи|покажи|дай|укажи|приведи|write|show|give|suggest)\b[\s,]+'
    r'(?:(?:мне|нам|просто|только|me|us|the|an?|just|only)\s+)*'
    r'(?:(?:(?:bash|shell|zsh)[ -])?команду|(?:текст|пример)\s+команды|'
    rf'(?:(?:bash|shell|zsh)\s+)?command|{_COMMAND_QUESTION})|{_COMMAND_QUESTION})\b', re.I,
)
_ACTION = re.compile(_ACTION_LEAD +
                     r"(?:(?P<verb>прочт\w*|прочитай|проверь|выясни|найди|посмотри|покажи|выведи|открой|запусти|останови|"
                     r"выполни|скачай|отправь|установи|удали|перемести|перейди|поменяй|прокинь|"
                     r"запомни|сохрани|запиши|закоммить|run|execute|read|open|start|stop|send|install)|"
                     r"curl|wget|ssh|GET|HEAD|POST|PUT|PATCH|DELETE)\b", re.I)
_PERSONAL = re.compile(r"\b(?:мой\s+(?:номер|адрес|логин|пароль|телефон)|как\s+меня\s+зовут|"
                       r"код\s+доступа|пароль|"
                       r"установлен\w*\s+ли|на\s+(?:мо[её]м|этом)\s+(?:маке|компьютере|сервере))\b", re.I)
_MEMORY_QUESTION = re.compile(r"\bпомнишь\b", re.I)
_LIVE = re.compile(r"\b(?:сейчас|сегодня|текущ\w*|current|currently)\b", re.I)
_LIVE_VALUE = re.compile(r"\b(?:погод\w*|цен\w*|курс\w*|новост\w*|стоит|доступн\w*|сколько|загруж\w*)\b", re.I)
_CONTENT = re.compile(r"\b(?:файл\w*|документ\w*|(?:под)?пап(?:к|ок)\w*|(?:под)?каталог\w*|директори\w*|страниц\w*|ссылк\w*|вкладк\w*|"
                      r"процесс(?:ы|а|ов|ам|ах|ами|е|ом)?|pid|контейнер\w*|"
                      r"(?:тут|там|здесь)\s+(?:написано|лежит|показано)|о\s+ч[её]м\s+(?:тут|там|здесь))\b", re.I)
_REFERENT = re.compile(r"\b(?:там|тут|здесь|эт(?:от|ом|у|ой)|эт[иа]\s+данн\w*)\b", re.I)
_BOUND_SOURCE = re.compile(
    r'\b(?:эт(?:ого|ом|ой)\s+(?:проект\w*|компьютер\w*|файл\w*|сервер\w*)|'
    r'this\s+(?:[\w.-]+\s+){0,2}(?:project|app|file|machine|computer|server)|'
    r'(?:computer|machine)\s+\w+\s+is\s+running\s+on|(?:my|our)\s+alias)\b', re.I)
_FOLLOWUP_ACTION = re.compile(
    r'(?:[.!?;]\s+|\b(?:и|затем|and|then)\s+)(?:(?:пожалуйста|please)[,\s]+)?'
    r'(?:выполни\w*|проверь\w*|прочитай\w*|запусти\w*|'
    r'(?:run|execute|check|read|inspect|open)\s+(?:it|them|this|the|my|[\w.-]+\.[A-Za-z0-9]{1,9})\b)', re.I)
_HTTP_METHOD = r'GET|HEAD|POST|PUT|PATCH|DELETE'
_HTTP_METHOD_HINT = _HTTP_METHOD + r'|OPTIONS|TRACE|CONNECT'
_HTTP_METHOD_CODE = re.compile(r'\x60(' + _HTTP_METHOD_HINT + r')\x60', re.I)
_HTTP_COMMAND = re.compile(
    _ACTION_LEAD + r'(?:(?P<verb>сделай|выполни|отправь|запроси|получи|используй|use|send|fetch|make|perform)\s+)?'
    r'(?:(?P<representation>json|данные|ответ|data|the\s+response)\s+(?:методом|через|via|using)\s+)?'
    r'(?:(?:a|an)\s+)?(?P<protocol>http\s+)?(?P<method>' + _HTTP_METHOD +
    r')\b(?:[\s-]+(?:запрос|request)\b)?', re.I)
_HTTP_ALTERNATE = re.compile(r'\s+(?:или|or)\s+(' + _HTTP_METHOD + r')\b', re.I)
_CURL_COMMAND = re.compile(_ACTION_LEAD + r'(?:(?:выполни|запусти|run|execute)\s+)?curl\b', re.I)
_HTTP_PREVIOUS_URL = re.compile(
    r'\s*(?:(?:по|к|на)\s+(?:этой\s+ссылке|этому\s+адресу)|(?:to|from|at)\s+this\s+(?:link|url|address))\b',
    re.I)
_HTTP_METHOD_LINK = re.compile(
    rf'{_HTTP_PREVIOUS_URL.pattern}[.!?]?\s*[<("`]*|'
    r'\s*(?:(?:to|on|from|на|к|по)\s+)?'
    r'(?:(?:одной из ссылок|one of the links|(?:the\s+)?(?:эндпоинт|адресу?|url|endpoint))\s+)?[\s<("\x60]*', re.I)
_HTTP_BODY = re.compile(
    r'\b(?:с\s+(?:json(?:\s+телом)?|данными|телом)|with\s+(?:a\s+)?(?:json(?:\s+body)?|data|body)|'
    r'(?:json\s+)?(?:body|тело|телом))\b', re.I)
_HTTP_OPTION = re.compile(r'(?<!\S)--?[A-Za-z]|\b(?:с\s+заголовк\w*|with\s+(?:the\s+)?headers?)\b', re.I)
_SAVE_VERB = r'(?:сохрани|запиши|save|write)'
_SAVE_RESPONSE = re.compile(r'[,;\s]+(?:(?:и|затем|потом|and|then)\s+)*' + _SAVE_VERB + r'\b', re.I)
_HTTP_FILE_WRITE = re.compile(r'\b(?:сохран(?:и|ить)|запиш(?:и|ите)|записать|выгрузи|save|write)\b|'
                              r'\b(?:положи|put)\b.*\b(?:в\s+файл|in\s+(?:a\s+)?file)\b', re.I | re.S)


def surface_text(text):
    original=policy_text(text)
    surface=re.sub(r'''`[^`]*`|"(?:\\[^\n]|[^"\\\n])*"|'(?:\\[^\n]|[^'\\\n])*'|«[^»\n]*»|“[^”\n]*”|„[^“\n]*“''',lambda match:' '*len(match.group()),original)
    return surface if surface.strip() else original


def action_prefix_end(text):
    """Return the end of leading courtesy and turn-transition words."""
    return _ACTION_PREFIX.match(text).end()


def conceptual_question(text):
    surface=surface_text(text)
    if _COMMAND_GUIDE.match(surface) and not (local_source_supplied(text) or specific_observation(text)):
        return True
    observe=_OBSERVE.search(surface)
    if not observe:
        if (action := _ACTION.match(surface)) and action['verb']:
            return False
        return any(re.search(pattern,surface,re.I) for pattern in _POLICY['conceptual_patterns'])
    # The verb's complement can request knowledge: "show how it works".
    # Later explanations preserve a primary request to observe current state.
    complement=surface[observe.end():].lstrip(' \t,:—–-')
    complement=re.sub(r'^(?:(?:мне|нам|пожалуйста|пример(?:ы)?|please|me|us|an?\s+example|examples?)\b[\s,:—–-]*)+', '',complement,flags=re.I)
    if _GUIDE.match(complement):
        return True
    patterns=_POLICY['conceptual_patterns']
    if any(re.match(pattern,complement,re.I) for pattern in patterns):return True
    return bool(re.match(r'(?:как|како\w*|как(?:ая|ую|ие|им|их)|сколько|how|what|which)\b',complement,re.I)) and any(re.search(pattern,complement,re.I) for pattern in patterns)


def request_mode(text):
    surface=surface_text(text)
    # A why/definition question can quote or discuss a negative instruction.
    # Explicit prohibitions themselves still cannot enter execution.
    if re.match(r'^\s*(?:почему|зачем|что\s+будет|why\b|what\s+happens)',surface,re.I):return 'explanation'
    if _NEGATIVE.search(surface):
        # A constraint and an explanatory goal can coexist. Answering the
        # latter must not grant permission to the prohibited operation.
        if conceptual_question(text) and re.search(r'\b(?:объясни\w*|расскажи\w*|почему|зачем|explain|why)\b',surface,re.I):return 'explanation'
        return 'prohibition'
    if conceptual_question(text):return 'explanation'
    return 'request'


def system_request_scope(text):
    """Require positive evidence of a local/current observation, else abstain."""
    surface=surface_text(text)
    if request_mode(text)=='prohibition':return 'prohibition'
    if conceptual_question(text):return 'knowledge'
    if _OBSERVE.search(surface) or specific_observation(text):return 'live'
    if any(re.search(pattern,surface,re.I) for pattern in _POLICY['current_patterns']):return 'live'
    if re.search(r'^\s*(?:сколько|хватает\s+ли)\b.*\b(?:свободн\w*|доступн\w*)\b',surface,re.I) and not re.search(r'\b(?:нужно|требуется|надо)\b',surface,re.I):return 'live'
    if re.search(r'\b(?:что|почему|как\w*|зачем|ли|это)\b|[?]',surface,re.I):return 'knowledge'
    return 'unclear'


def hypothetical_question(text):
    surface = surface_text(text)
    premise = re.search(
        r'\b(?:если|if)\b[^.!?\r\n]{0,120}\b(?:не\s+указан\w*|не\s+привед[её]н\w*|'
        r'не\s+извест\w*|not\s+(?:stated|given|specified)|unknown)\b', surface, re.I)
    return conceptual_question(text) and bool(
        re.search(r'\b(?:если\s+бы|допустим|предположим|hypothetically|что\s+будет)\b', surface, re.I)
        or premise and not action_turn_head(text))


def local_source_supplied(text):
    extensions='|'.join(re.escape(value.lstrip('.')) for value in resource('file-types.json')['extensions'])
    surface=policy_text(text)
    return bool(re.search(r'(?:^|[\s`"\'«])(?:~?/[^\s]+|[^\s`"\'«»]+\.(?:'+extensions+r'))(?=$|[\s`"\'»?!,.])',surface,re.I))


def specific_observation(text):
    """Detect a bound source or an explicit follow-up action in a question."""
    surface = surface_text(text)
    return bool(_BOUND_SOURCE.search(surface) or _FOLLOWUP_ACTION.search(surface))


_SEQUENCE = r'(?:(?:и|а|and)\s+)?(?:затем|потом|после\s+этого|then|next)[,:]?'
_SEQUENCE_INTRO = re.compile(rf'^{_SEQUENCE}\s+', re.I)
_FIRST = re.compile(r'^(?:сначала|first)[,:]?\s+', re.I)
_CLAUSE_END = r'(?<!\s\.)(?<=[.!?;])\s+|\n+'
_COMPOUND_BOUNDARY = re.compile(
    rf'(?P<clause_end>{_CLAUSE_END})|(?<![\s,])[\s,]+'
    rf'(?:(?P<sequence>{_SEQUENCE})|и\s+ещ[её]|а\s+ещ[её]|заодно|отдельно|and\s+also|also|separately|(?P<join>и|and))\s+'
    r'|(?P<comma>,[ \t]+)', re.I,
)
_COMPOUND_INTRO = re.compile(r'^(?:(?:и|а)(?:\s+ещ[её])?|заодно|отдельно|and(?:\s+also)?|also|separately)[,:]?\s+', re.I)
_TASK_LIST_LEAD = re.compile(
    _ACTION_LEAD + r'(?:сделай\s+(?:(?:дв[ае]|три|несколько|\d+)\s+)?(?:дела?|задач[уи]?|вещ[ьи])|'
    r'(?:do|handle)\s+(?:two|three|these|several|\d+)\s+(?:things|tasks))\s*:\s*', re.I)
_QUESTION_HEAD = re.compile(
    r'^(?:а\s+)?(?:что|почему|зачем|для\s+чего|как|кто|где|когда|сколько|чем|'
    r'како(?:й|е|го|му|м)|какая|какую|какие|каких|what|why|how|who|where|when|which)\b', re.I,
)
_LINK_OUTPUT = re.compile(
    _ACTION_LEAD + r'(?:(?:просто|только|just|only|можно)[,\s]+)*'
    r'(?:(?:дай|дать|скинь|пришли|покажи|показать|напиши|укажи|подскажи|give|share|show|send|paste|write|provide)'
    r'(?:[,\s]+(?:мне|нам|пожалуйста|me|us|the|an?|official|официальную))*[,\s]+(?:ссылку|link|url)'
    r'|(?:ссылку|link|url)\s+(?:дай|скинь|напиши|paste)'
    r'|(?:какая\s+)?ссылка|what(?:[’\x27]s|\s+is)\s+the\s+(?:link|url))'
    r'(?:\s+(?:сюда|в\s+(?:этот\s+)?чат|here|in\s+(?:(?:this|the)\s+)?chat))?'
    r'(?:\s+(?:на|у|to|for|of)\s+(?P<target>[^\r\n!?;]{1,160}?))?[.!?…]*\s*', re.I,
)
_LINK_ON_PAGE = re.compile(
    r'^(?:(?:открытой|текущей)\s+)?(?:странице|вкладке|сайте)\b|'
    r'^(?:прошлую|предыдущую|прочитанную)\s+(?:страницу|статью|вкладку)\b', re.I,
)
_YES_NO_QUESTION = re.compile(r'^(?:(?!\.\s)[^\n?!;]){1,160}\?(?=\s|$)')
_READ_HEAD = re.compile(_ACTION_LEAD + r'(?:найди|выясни|разбери|проанализируй|перескажи|inspect|summarize|find|'
                       r'`?git\s+(?:status|log|blame|diff))\b', re.I)
_GIT_READ_CLAUSE = re.compile(_ACTION_LEAD + r'(?:(?:покажи|посмотри|проверь|show(?:\s+me)?|check)\s+)?'
                             r'`?git\s+(?:status|log|blame|diff|branch)\b', re.I)
_TASK_HEAD = re.compile(_ACTION_LEAD + r'(?:сделай|разверни|подготовь|переведи|инициализируй|создай|поставь|'
                        r'собери|добавь|подними|примени|накати|сгенерируй|закомми(?:ть|тить)|исправь|отформатируй|'
                        r'выполни|вызови|execute|run|deploy|bootstrap|translate|initialize|create|git\s+commit|ruff\s+(?:check|format)|'
                        r'npm\s+(?:init|install|i|add|run\s+(?:build|lint|format))|composer\s+install|php\s+artisan)\b', re.I)
_GIT_COMMIT_HEAD = re.compile(_ACTION_LEAD + r'(?:закомми(?:ть|тить)|git\s+commit|'
                             r'(?:давай|хочу|можно|сделаем)[,\s]+(?:(?:быстрый|обычный)\s+)?коммит)\b', re.I)
_NUMBERED_STEP = re.compile(r"^\s*(?:[0-9]{1,4}[.)]|[-*])\s+(\S.*)$")
_COMPOUND_DEPENDENCY = re.compile(
    r'(?<![\w./-])(?:сначала|затем|потом|после\s+(?:этого|того\s+как)|при\s+условии|если|'
    r'first|then|if|unless|afterwards|after\s+that)\b(?![./-])', re.I,
)
_DEPENDENT_QUESTION = re.compile(
    r'\b(?:эт(?:от|ой|ом|им|ого|у)|эти|тут|там|здесь|(?:в|из|о)\s+н[её]м|(?:из|для)\s+него|'
    r'(?:что|почему|зачем|где|как|what|why|where|how)\s+(?:это|он[аио]?|его|е[её]|их|it|they|them)|'
    r'this|that|the\s+(?:result|output))\b', re.I,
)
_OUTPUT_ITEM = (r'(?:строк\w*|номера\s+строк|пути|путь|метод\w*|реализаци\w*|'
                r'мест[ао]\s+в\s+исходник\w*|ссылк\w*\s+на\s+исходник\w*|'
                r'line\s+numbers|lines|methods?|paths?|source\s+locations?)')
_OUTPUT_REQUIREMENT = re.compile(
    r'^(?:покажи|укажи|приведи|добавь|show|include|cite)\s+'
    rf'(?:(?:конкретн\w*|точн\w*|the|exact)\s+)?{_OUTPUT_ITEM}'
    rf'(?:\s*(?:,\s*|\s+(?:и|and)\s+){_OUTPUT_ITEM}){{0,3}}[.!?…]*$', re.I)
_QUESTION_DIRECTIVE = re.compile(
    _ACTION_LEAD + r'(?:скажи(?:те)?|поясни(?:те)?|объясни(?:те)?|(?:хочу|хотелось\s+бы)\s+(?:понять|узнать)|'
    r'explain|tell me|(?:I want|I would like) to (?:know|understand))[,\s]+', re.I)
_EXPLANATION_JUNCTION = re.compile(r'[,.:—–]\s*|\b(?:и|а|но|или|затем|потом|and|but|or|then)\s+', re.I)
_PERSONAL_SCOPE = re.compile(r'\b(?:у\s+(?:меня|нас)|мо(?:й|я|[её]|ю|и|его|[её]му|[её]м|ей|их|им|ими)|наш\w*|my|our)\b', re.I)


def _compound_head(surface, *, sentence=True):
    if _QUESTION_HEAD.match(surface):
        return 'question'
    if (directive := _QUESTION_DIRECTIVE.match(surface)) and _QUESTION_HEAD.match(surface[directive.end():]):
        return 'question'
    if _OBSERVE.match(surface) or _READ_HEAD.match(surface):
        return 'read'
    if _ACTION.match(surface) or _TASK_HEAD.match(surface) or _GIT_COMMIT_HEAD.match(surface):
        return 'task'
    if sentence and _YES_NO_QUESTION.match(surface):
        return 'question'
    return None


def action_turn_head(text):
    return _compound_head(surface_text(text), sentence=False) in {'read', 'task'}


def link_output_requested(text):
    """Recognize a request to write a link in chat, distinct from reading its source."""
    match = _LINK_OUTPUT.fullmatch(text.strip())
    if not match and len(extract_urls(text)) == 1:
        surface = policy_text(text).strip(' \t\r\n<>«»[]()"\'`—:')
        surface = re.sub(r'\s+(?:на|to|for)$', '', surface, flags=re.I)
        match = _LINK_OUTPUT.fullmatch(surface)
    if not match or local_source_supplied(text) or specific_observation(text):
        return False
    target = surface_text(match['target'] or '')
    return not (_NEGATIVE.search(target) or _REFERENT.search(target) or _PERSONAL_SCOPE.search(target)
                or _DEPENDENT_QUESTION.search(target) or _LINK_ON_PAGE.match(target)
                or any(_compound_head(target[join.end():], sentence=False) in {'read', 'task', 'question'}
                       for join in _EXPLANATION_JUNCTION.finditer(target)))


def compound_parts(text, *, literal_tail=lambda _: False, typed_read=lambda _: False, complete_read=None):
    """Return explicit clauses and whether their order was requested."""
    def head(part, *, masked=None, sentence=True):
        surface = surface_text(part) if masked is None else masked
        if kind := _compound_head(surface, sentence=sentence):
            return kind
        boundary = re.search(_CLAUSE_END, surface)
        end = boundary.start() if boundary else len(part)
        if (masked is None or surface[:end].strip(' \t.!?;')) and typed_read(part[:end]):
            return 'read'
        return None

    if lead := _TASK_LIST_LEAD.match(surface_text(text)):
        text = text[lead.end():]
    lines = [line for line in text.splitlines() if line.strip()]
    numbered = [_NUMBERED_STEP.fullmatch(line) for line in lines] if len(lines) > 1 else []
    if numbered and all(numbered):
        parts = [match[1] for match in numbered]
        if any(not literal_tail(part) and (not head(part) or _NEGATIVE.search(surface_text(part)))
               for part in parts):
            return None
        return _compound_steps(parts, literal_tail, typed_read), True
    surface = surface_text(text)
    first = _FIRST.match(surface)
    start = first.end() if first else 0
    sequenced = bool(first)
    if (not head(text[start:]) or _CURL_COMMAND.match(surface[start:])
            or literal_tail(text[start:])):
        return None
    parts = []
    first_has_clause_end = False
    end_of_commands = len(surface)
    for boundary in _COMPOUND_BOUNDARY.finditer(text):
        if boundary.start() <= start or boundary[0] != surface[boundary.start():boundary.end()]:
            continue
        tail = surface[boundary.end():]
        sequence = _SEQUENCE_INTRO.match(tail)
        intro = sequence or _COMPOUND_INTRO.match(tail)
        end = boundary.end() + (intro.end() if intro else 0)
        joined = boundary['join'] and not intro
        left = surface[start:boundary.start()]
        if boundary['comma']:
            # A comma normally introduces the question the read should answer.
            # Only split it after a complete typed read with its own goal.
            question = _YES_NO_QUESTION.match(surface[end:])
            if (not complete_read or not question or not _QUESTION_HEAD.match(question[0])
                    or not conceptual_question(question[0]) or _DEPENDENT_QUESTION.search(question[0])
                    or not complete_read(text[start:boundary.start()])):
                continue
        owns_tail = literal_tail(text[end:])
        kind = 'task' if owns_tail else head(text[end:], masked=surface[end:], sentence=not joined)
        if kind is None or not (sequence or boundary['sequence']) and _OUTPUT_REQUIREMENT.fullmatch(tail.strip()):
            continue
        if joined and kind == 'question':
            if _DEPENDENT_QUESTION.search(surface[end:]):
                continue
            if _compound_head(surface[start:]) == 'question':
                # A shared interrogative ("where and when") does not supply
                # two separate questions. Require a self-contained first one.
                question = _QUESTION_HEAD.match(left)
                if not (question and conceptual_question(left) and len(tokens(left[question.end():])) >= 2):
                    continue
        if (joined and kind == 'question' and _QUESTION_DIRECTIVE.match(surface[end:])
                and _compound_head(surface[start:]) == 'task'):
            continue
        if joined:
            lead = _ACTION.match(left) or _OBSERVE.match(left) or _READ_HEAD.match(left) or _TASK_HEAD.match(left)
            if (lead and not text[start + lead.end():boundary.start()].strip(' \t,.!?;')
                    and not (_GIT_READ_CLAUSE.match(left) or _GIT_COMMIT_HEAD.match(left))):
                continue
        if _SAVE_RESPONSE.match(surface, boundary.start()) and _http_command(text[start:boundary.start()]):
            continue
        # A bare conjunction can introduce an output instruction belonging
        # to the current operation. Require another source for a second read.
        if joined and kind == 'read' and not (
                local_source_supplied(text[end:]) or extract_urls(text[end:]) or _PERSONAL.search(surface, end)
                or _MEMORY_QUESTION.search(surface, end)
                or _GIT_READ_CLAUSE.match(text[end:])):
            continue
        part = text[start:boundary.start()].rstrip(' \t\r\n,;')
        if len(part) > 1 and part.endswith('.') and not part[-2].isspace():
            part = part[:-1]
        if not parts:
            first_has_clause_end = boundary['clause_end'] is not None
        parts.append(part)
        sequenced |= bool(sequence or boundary['sequence'])
        start = end
        # A typed request with a literal body owns the remaining text.
        # Commands and prohibitions there are part of the supplied data.
        if owns_tail:
            end_of_commands = start
            break
    if not parts or _NEGATIVE.search(surface[:end_of_commands]):
        return None
    parts.append(text[start:])
    steps = _compound_steps(parts, literal_tail, typed_read)
    # A complete read followed by a question about its result has a natural
    # order. An effect or condition still needs the existing explicit syntax.
    if (not sequenced and first_has_clause_end and complete_read
            and steps[0][1] == 'read' and complete_read(parts[0])):
        following = {kind for _, kind in steps[1:]}
        sequenced = 'question_followup' in following and following <= {'question', 'question_followup'}
    return steps, sequenced


def _compound_steps(parts, literal_tail, typed_read):
    result = []
    for part in parts:
        if literal_tail(part):
            result.append((part, 'task'))
            continue
        masked = surface_text(part)
        kind = 'read' if typed_read(part) else _compound_head(masked)
        if kind == 'question' and needs_observation(part, has_source=True):
            kind = 'read'
        if (http := _http_command(part)) and http[0] <= {'GET', 'HEAD'} and not _HTTP_FILE_WRITE.search(masked):
            kind = 'read'
        if kind == 'question' and _DEPENDENT_QUESTION.search(masked):
            kind = 'question_followup'
        elif (_CURL_COMMAND.match(masked) or http and (_HTTP_BODY.search(masked) or re.search(r'[{\[]', policy_text(part)))
                or kind != 'question' and _COMPOUND_DEPENDENCY.search(masked)):
            kind = 'dependent'
        result.append((part, kind))
    return tuple(result)


def _http_command(text):
    command_text = _HTTP_METHOD_CODE.sub(r' \1 ', text)
    command = _HTTP_COMMAND.match(surface_text(command_text))
    if not command:
        return None
    methods = {command['method'].upper()}
    end = command.end()
    while alternate := _HTTP_ALTERNATE.match(command_text, end):
        methods.add(alternate[1].upper())
        end = alternate.end()
    tail = command_text[end:]
    links = url_spans(command_text)
    if (not tail.strip(' \t\r\n.!?') or _HTTP_PREVIOUS_URL.match(tail)
            or _HTTP_BODY.match(tail.lstrip()) or _HTTP_OPTION.match(tail.lstrip())
            or any(start >= end and _HTTP_METHOD_LINK.fullmatch(command_text[end:start])
                   for start, _, _ in links)):
        return methods, end, True
    if links and (command['verb'] or command['protocol'] or command['method'].isupper()):
        return methods, end, False


def personal_recall_requested(text):
    surface = surface_text(text)
    if refers_to_dialogue(surface) and not _SAVED_FACT.search(surface):
        return False
    return bool(_RECALL.search(surface) and not re.search(
        r'\b(?:напомни|вспомни)[,\s]+(?:как|что\s+такое|зачем|почему)\b', surface, re.I))


def needs_observation(text, *, has_source=False):
    """Keep free-form answers separate from requests needing real data or an effect."""
    surface = surface_text(text)
    if personal_recall_requested(text):
        return True
    if hypothetical_question(text) or link_output_requested(text):
        return False
    if specific_observation(text):
        return True
    if extract_urls(text) or local_source_supplied(text) or _LIVE.search(surface) and _LIVE_VALUE.search(surface):
        return True
    if has_source and _REFERENT.search(surface) and _CONTENT.search(surface):
        return True
    if (_GUIDE.search(surface) or conceptual_question(text)
            or not action_turn_head(text) and re.search(r'\b(?:почему|зачем|why)\b', surface, re.I)):
        return False
    # First-person statements and time references also appear in ordinary chat.
    asks_for_state = (_SYSTEM_STATE.search(surface) or _OBSERVE.search(surface)
                      or _QUESTION_HEAD.match(surface) or surface.rstrip().endswith('?'))
    live_or_recalled = (not refers_to_dialogue(surface) and (_MEMORY_QUESTION.search(surface)
                        or asks_for_state and system_request_scope(text) == 'live'))
    return bool(live_or_recalled or _PERSONAL.search(surface) or _ACTION.search(surface) or _CONTENT.search(surface))
