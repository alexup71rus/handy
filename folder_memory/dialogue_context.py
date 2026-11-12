"""Identify explicit references to the current chat."""

import re

_CHAT_SCOPE = re.compile(
    r'\b(?:(?P<answer_channel>ответь(?:те)?|напиши(?:те)?|reply|respond|write)'
    r'(?:[,\s]+(?:мне|нам|пожалуйста|коротко|кратко|briefly|directly|please))*[,\s]+)?'
    r'(?:в\s+(?:этом|нашем|текущем)\s+(?:чате|разговоре|диалоге)|'
    r'(?:в|из)\s+(?:этой|нашей|текущей)\s+переписк[еи]|'
    r'(?:in|from)\s+this\s+(?:chat|conversation))\b', re.I,
)


def refers_to_dialogue(request):
    return any(not match['answer_channel'] for match in _CHAT_SCOPE.finditer(request))
