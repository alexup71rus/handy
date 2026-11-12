"""Keep exact URLs in the kernel and pass short references to language models."""

import re
from urllib.parse import urlsplit


_BARE_URL=re.compile(r'https?://[^\s<>"`]+',re.I)
_MARKDOWN=re.compile(r'\[([^\]\n]*)\]\((?=https?://|<https?://)',re.I)
_ESCAPE=re.compile(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]^_`{|}~\\])')
_URL_DELIMITERS = {"<": ">", '"': '"', "'": "'", "`": "`", "«": "»", "“": "”", '(': ')'}
_AFTER_HEAD_COURTESY = re.compile(r'^([a-zа-яё]+),[ \t]*(?:пожалуйста|please)[ \t]*,[ \t]*', re.I)
_FINAL_COURTESY = re.compile(r',[ \t]*(?:пожалуйста|please)[ \t.!?…]*$', re.I)
MARKDOWN_CODE_FENCE = re.compile(
    r"^[ \t]*(?P<fence>(?P<marker>[`~])(?P=marker){2,})[ \t]*(?P<tag>[\w+-]*)[ \t]*\r?\n"
    r"(?P<source>.*?)^[ \t]*(?P=fence)(?P=marker)*[ \t]*(?=\r?$)", re.M | re.S)


def strip_command_courtesy(text, *, trailing=True):
    """Normalize an instruction header; callers keep literal bodies separate."""
    text = _AFTER_HEAD_COURTESY.sub(r'\1 ', text, count=1)
    return _FINAL_COURTESY.sub('', text, count=1) if trailing else text


def url_spans(text):
    if not isinstance(text,str) or len(text)>16000:
        raise ValueError('Запрос превышает лимит 16 000 символов.')
    links=[]
    for count,match in enumerate(_MARKDOWN.finditer(text)):
        if count>=32:
            raise ValueError('Слишком много ссылок в одном запросе.')
        if links and match.start()<links[-1][1]:
            continue
        depth=1;index=match.end();start=index
        while index<len(text) and depth:
            if text[index]=='\\' and index+1<len(text):index+=2;continue
            if text[index]=='(':depth+=1
            elif text[index]==')':depth-=1
            index+=1
        if depth:
            continue
        destination=text[start:index-1].strip()
        if destination.startswith('<'):
            destination=destination[1:destination.find('>')]
        else:
            destination=destination.split(maxsplit=1)[0]
        value=_ESCAPE.sub(r'\1',destination)
        if value.lower().startswith(('http://','https://')):
            links.append((match.start(),index,value))
    masked=list(text)
    for start,end,_ in links:masked[start:end]=' '*(end-start)
    for match in _BARE_URL.finditer(''.join(masked)):
        value=match.group()
        quote = _URL_DELIMITERS.get(text[match.start() - 1]) if match.start() else None
        if (quote and (closing := value.rfind(quote)) >= 0
                and re.fullmatch(r"[,.!?;:]*", value[closing+1:])
                and (quote != ')' or value[:closing].count('(') == value[:closing].count(')'))):
            value = value[:closing]
        for closing,opening in ((')','('),(']','['),('}','{')):
            while value.endswith(closing) and value.count(closing)>value.count(opening):value=value[:-1]
        # Strip sentence punctuation only from plain URL paths. Explicit
        # links and query/fragment bytes may legitimately end with punctuation.
        protected=quote is not None
        punctuation = value.endswith('.') or value.endswith(',') and bool(text[match.end():].strip())
        if (punctuation and not value.endswith('..') and text[:match.start()].strip()
                and not protected and not text.lstrip().startswith('{') and '?' not in value and '#' not in value):
            value=value[:-1]
        if value:
            links.append((match.start(),match.start()+len(value),value))
            if len(links)>32:
                raise ValueError('Слишком много ссылок в одном запросе.')
    return sorted(links)


def extract_urls(text):
    return list(dict.fromkeys(value for _,_,value in url_spans(text)))


def policy_text(text):
    """Remove URL data so its words cannot become policy verbs or live evidence."""
    characters=list(text)
    for start,end,_ in url_spans(text):characters[start:end]=' '*(end-start)
    return ''.join(characters)


def url_wrapper_span(text, start, end):
    """Include paired delimiters when replacing a complete URL source."""
    if start and end < len(text) and _URL_DELIMITERS.get(text[start-1]) == text[end]:
        return start-1, end+1
    return start, end


def bind_url_source(text,selected,*,spans=None):
    """Replace adjacent URL alternatives after a source choice, preserving the task.
    An OR in a row predicate remains untouched.
    """
    if spans is None:
        spans=url_spans(text)
    source='<'+selected+'>'
    if not spans:return text+'\n'+source
    ranges=[url_wrapper_span(text,start,end) for start,end,_ in spans]
    edits=[(start,end,source if i==0 else '') for i,(start,end) in enumerate(ranges)]
    for (_,end),(start,_) in zip(ranges,ranges[1:]):
        if re.fullmatch(r'\s*(?:и|или|and|or|,)\s*',text[end:start],re.I):edits.append((end,start,' '))
    for start,end,replacement in sorted(edits,reverse=True):text=text[:start]+replacement+text[end:]
    return text


class PromptLiterals:
    def __init__(self,*texts):
        self.by_url={}
        for text in texts:
            if isinstance(text,str):
                for url in extract_urls(text):self.alias(url)

    def alias(self,url):
        if not url:
            return url
        if url not in self.by_url:self.by_url[url]='URL_'+str(len(self.by_url)+1)
        return self.by_url[url]

    def mask(self,text):
        output=[];position=0
        for start,end,url in url_spans(text):
            output.append(text[position:start])
            try:host=urlsplit(url).hostname or ''
            except ValueError:host=''
            output.append(self.alias(url)+(' ('+host[:255]+')' if host else ''))
            position=end
        output.append(text[position:])
        return ''.join(output)

    def mask_history(self, text):
        try:
            return self.mask(text)
        except ValueError:
            # Historic messages need not fit the URL parser's input limits.
            return _BARE_URL.sub('[ссылка]', text)

    def value(self,value):
        if isinstance(value,str):
            urls=extract_urls(value)
            return self.alias(value) if urls==[value] else self.mask(value)
        if isinstance(value,list):return [self.value(item) for item in value]
        if isinstance(value,dict):return {key:self.value(item) for key,item in value.items()}
        return value

    def restore(self,value):
        if not isinstance(value,str):return value
        reverse={alias:url for url,alias in self.by_url.items()}
        return reverse.get(value,value)

    def unmask(self,text):
        reverse={alias:url for url,alias in self.by_url.items()}
        return re.sub(r'\bURL_\d+\b',lambda match:reverse.get(match.group(),match.group()),text)
