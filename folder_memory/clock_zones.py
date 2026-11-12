"""Resolve the location of a clock from local timezone data."""

import os
import re
import unicodedata
import zoneinfo
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files as package_files
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .catalog import resource
from .literals import extract_urls
from .request_mode import action_prefix_end, compound_parts, local_source_supplied, request_mode


ZONE_FIELD = r'(?:часов\w*\s+пояс\w*|time\s?zone|tz)'
_PREFIX = re.compile(
    rf'^(?:(?:(?:давай|тогда|лучше|пусть|пожалуйста|время|времени|город(?:е|у)?|city|time|'
    rf'по|в|на|для|of|for|in|on)\s+|(?:с\s+)?{ZONE_FIELD}\b\s*[:=—–-]?\s*|[:,]\s*))+', re.I,
)
_LIST = re.compile(r'\s+(?:и|или|and|or)\s+|\s*,\s*', re.I)
_IANA = re.compile(r'(?:[a-z0-9_+-]+/)+[a-z0-9_+-]+|utc|gmt', re.I | re.ASCII)
_AREA = {'Africa', 'America', 'Antarctica', 'Arctic', 'Asia', 'Atlantic', 'Australia', 'Europe', 'Indian', 'Pacific'}
_WRAPPERS = " \t.,?!…`\"'«»“”"
_UNKNOWN = 'Укажи город или часовой пояс для часов, например Лондон или Europe/Moscow.'
_OBSERVATION_SCOPE = 'Где посмотреть время? Например, на этом компьютере, в Токио или по UTC.'
_OBSERVATION = resource('clock-zones.json')['observation']
_READ_VERBS = '|'.join(re.escape(word) for word in _OBSERVATION['verbs'])
_READ_INFINITIVES = '|'.join(re.escape(word) for word in _OBSERVATION['infinitives'])
_DEVICE = '|'.join(re.escape(word) for word in _OBSERVATION['devices'])
_LOCAL_HOST = re.compile(rf'(?:(?:мо[её]м|тво[её]м|нашем|этом|данном|локальном|сво[её]м|my|this|the|local)\s+){{0,2}}(?:{_DEVICE})', re.I)
_TIME_NOUN = r'(?:(?:текущее|точное)\s+)?(?P<local>(?:местное|локальное|системное)\s+)?время'
_TIME_QUESTION = (r'(?:(?:который|какой)\s+(?:сейчас\s+)?час|сколько\s+(?:сейчас\s+)?времени|'
                  r'какое\s+(?:сейчас\s+)?время)')
_TIME_HEAD = re.compile(
    rf'(?:(?:{_READ_VERBS})(?:те)?[\s,]+(?:мне\s+)?(?:{_TIME_NOUN}|{_TIME_QUESTION})|{_TIME_QUESTION}|'
    r'(?:(?:show|display|tell|check)(?:\s+me)?\s+|what(?:\s+is|\x27s)\s+(?=(?:the\s+)?(?:current|local)\s+))'
    r'(?:the\s+)?(?:current\s+)?(?P<local_en>local\s+)?time|'
    r'what\s+time\s+is\s+it)\b', re.I,
)
_MODAL_HEAD = re.compile(
    rf'(?:(?:ты|вы)\s+)?(?P<negative>не\s+)?(?:мож(?:ешь|ете)(?:\s+ли)?|можно)'
    rf'(?:\s+(?:ты|вы))?[\s,]+(?:(?:мне|пожалуйста|случайно)[\s,]+)*(?:{_READ_INFINITIVES})\b[\s,:]*', re.I,
)
_MODAL_TIME = re.compile(_TIME_NOUN + r'\b', re.I)
_SELECTED_TIME = re.compile(
    r'(?:(?:текущее|точное)\s+время|current\s+time|'
    r'(?P<local>(?:местное|локальное|системное)\s+время|(?:current\s+)?local\s+time))\b', re.I,
)
_PLACE_FIRST = re.compile(r'(?:в|на|по|in|on|for)\b', re.I)
_NOW = r'(?:(?:прямо\s+)?сейчас|(?:right\s+)?now)'
_TIME_NOW = re.compile(rf'^{_NOW}(?:\s+|$)|\s+{_NOW}$', re.I)
_TIME_ZONE_SLOT = re.compile(rf'^(?:(?:с\s+)?{ZONE_FIELD}|(?:в|для)\s+городе?)\b', re.I)
_ZONE_COMPARISON = re.compile(
    r'^(?:а\s+)?почему\s+(?:между\s+(?P<first>[^\n?!;]+?)\s+и\s+(?P<second>[^\n?!;]+?)\s+разниц\w*'
    r'|(?:разниц\w*\s+во\s+времени\s+)?между\s+(?P<left>[^\n?!;]+?)\s+и\s+(?P<right>[^\n?!;]+?))'
    r'(?:\s+(?:во\s+времени\s+)?в\s+(?:один\s+)?час\w*)?[.?!…]*$', re.I,
)


@dataclass(frozen=True)
class ClockZone:
    time_zone: str | None = None
    label: str | None = None
    error: str | None = None
    choices: tuple[tuple[str, str], ...] = ()


def _name(text):
    return re.sub(r'[\s.,_–—-]+', ' ', text.casefold().replace('ё', 'е').strip(_WRAPPERS)).strip()


def _tzdb_rows(filename):
    for directory in zoneinfo.TZPATH:
        try:
            text = (Path(directory) / filename).read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        if text.strip():
            break
    else:
        try:
            text = package_files('tzdata.zoneinfo').joinpath(filename).read_text(encoding='utf-8')
        except (ImportError, OSError, UnicodeError):
            return []
    return [line.split('\t') for line in text.splitlines() if line.strip() and not line.startswith('#')]


def _country_zones(available):
    # zone.tab keeps geographic names even when another country shares the
    # same rules. zone1970.tab may list several codes against one zone.
    for filename in ('zone.tab', 'zone1970.tab'):
        countries = {}
        for row in _tzdb_rows(filename):
            if len(row) < 3 or row[2] not in available:
                continue
            for code in row[0].split(','):
                if re.fullmatch(r'[A-Z]{2}', code):
                    countries.setdefault(code, []).append(row[2])
        if countries:
            return countries
    return {}


def _city_name(zone):
    return zone.rsplit('/', 1)[-1].replace('_', ' ')


def _location(choices, error, *, label=None):
    zones = {}
    for caption, zone in choices:
        zones.setdefault(zone, caption)
    if len(zones) == 1:
        zone, caption = next(iter(zones.items()))
        return ClockZone(zone, label or caption)
    return ClockZone(error=error, choices=tuple((caption, zone) for zone, caption in zones.items()))


def _country_names(countries, available):
    system_names = {row[0]: row[1] for row in _tzdb_rows('iso3166.tab') if len(row) >= 2}
    data = resource('clock-zones.json')
    localized = {record['country']: (label, record) for label, record in data['regions'].items()}
    names = {}
    for code in dict.fromkeys([*countries, *localized]):
        system_name = system_names.get(code)
        label, record = localized.get(code, (system_name or code, {}))
        location = record.get('location') or f'регионе «{label}»'
        examples = [(place, data['places'][place]['zone']) for place in record.get('places', ())]
        choices = [(place, zone) for place, zone in examples if zone in available]
        choices.extend((_city_name(zone), zone) for zone in countries.get(code, ()))
        if choices:
            result = _location(choices, f'В {location} несколько часовых поясов. Выбери город из списка или напиши свой.',
                               label=label)
            # Case-sensitive codes avoid treating ordinary words like "in",
            # "no" or "me" as country selections.
            names[code] = result
            for alias in (system_name, label if label != code else None, *record.get('names', ())):
                if alias:
                    names[_name(alias)] = result
    return names


@lru_cache(maxsize=1)
def _iana_names():
    zones = {zone.casefold(): zone for zone in sorted(available_timezones()) if _IANA.fullmatch(zone)}
    available = set(zones.values())
    countries = _country_zones(available)
    canonical = {zone for options in countries.values() for zone in options}
    cities = {}
    for zone in zones.values():
        if zone.partition('/')[0] in _AREA:
            label = _city_name(zone)
            cities.setdefault(_name(label), []).append((label, zone))
    for name, choices in cities.items():
        cities[name] = [choice for choice in choices if choice[1] in canonical] or choices
    return zones, cities, _country_names(countries, available)


@lru_cache(maxsize=1)
def _places():
    data = resource('clock-zones.json')
    places = {label: ClockZone(record['zone'], label) for label, record in data['places'].items()}
    names = {_name(alias): places[label] for label, record in data['places'].items()
             for alias in (label, *record['names'])}
    names.update({_name(alias): ClockZone(label='Местное время') for alias in data['local']})
    return names


def _one(text):
    value = _PREFIX.sub('', text.strip(_WRAPPERS)).strip(_WRAPPERS)
    name = _name(value)
    if place := _places().get(name):
        return place
    if _LOCAL_HOST.fullmatch(value):
        return ClockZone(label='Местное время')
    zones, cities, countries = _iana_names()
    if zone := zones.get(value.casefold()):
        return ClockZone(zone)
    if country := countries.get(value) or countries.get(name):
        return country
    matches = cities.get(name, ())
    if matches:
        choices = matches if len(matches) == 1 else [(zone, zone) for _, zone in matches]
        return _location(choices, 'Уточни часовой пояс для этого города.')
    return None


def resolve_clock_zone(text):
    """Accept a complete place or IANA value, plus a small set of location phrases."""
    if not isinstance(text, str) or len(text) > 180 or re.search(r'[\r\n]', text):
        return ClockZone(error=_UNKNOWN)
    value = unicodedata.normalize('NFKC', text).strip(_WRAPPERS)
    if result := _one(value):
        return result
    parts = _LIST.split(value, maxsplit=6)
    if 2 <= len(parts) <= 6:
        matches = [_one(part) for part in parts]
        if all(match and match.time_zone for match in matches):
            choices = [(match.label or match.time_zone, match.time_zone) for match in matches]
            return _location(choices, 'Для этих часов выбери один город или часовой пояс.')
    return ClockZone(error=_UNKNOWN)


def local_clock_zone():
    """Return the system's named zone where available, for a matching preview."""
    name = os.environ.get('TZ')
    if not name:
        try:
            name = str(Path('/etc/localtime').resolve(strict=True))
        except (OSError, RuntimeError):
            return None
    name = name.removeprefix(':').split('/zoneinfo/', 1)[-1]
    try:
        return ZoneInfo(name).key
    except (ValueError, ZoneInfoNotFoundError):
        return None


def current_time_request(text: str, *, recent=None, selected=False) -> ClockZone | None:
    """Bind a complete read. Resolve recent scope lazily for a modal follow-up."""
    if not isinstance(text, str) or len(text) > 260 or re.search(r'[;\r\n]', text):
        return None
    text = text.strip()
    start = action_prefix_end(text)
    modal = _MODAL_HEAD.match(text) or _MODAL_HEAD.match(text, start)
    if modal:
        head = _MODAL_TIME.match(text, modal.end()) or _TIME_HEAD.match(text, modal.end())
    else:
        head = _TIME_HEAD.match(text, start)
    prior_location = ''
    if head is None and selected:
        head = _SELECTED_TIME.match(text, start)
        if head is None and _PLACE_FIRST.match(text, start):
            head = _TIME_HEAD.search(text, start) or _SELECTED_TIME.search(text, start)
            if head:
                prior_location = text[start:head.start()].strip(' \t.,!?…—–')
    if head is None:
        # Once a clock has been observed, an entire named zone can supply
        # the one missing slot of a new read. The old timestamp is never reused.
        if recent and start and not re.search(r'[!?]\s+\S', text) and recent():
            zone = resolve_clock_zone(text[start:])
            if zone.error is None or zone.choices:
                return zone
        return None
    if (request_mode(text) == 'prohibition' or extract_urls(text) or local_source_supplied(text)
            or compound_parts(text)):
        return None
    location = _TIME_NOW.sub('', text[head.end():].strip(' \t.,!?…')).strip()
    if prior_location and location:
        return None
    location = prior_location or location
    if not location:
        if head.groupdict().get('local') or head.groupdict().get('local_en'):
            return ClockZone(label='Местное время')
        if modal and recent and (zone := recent()):
            return zone
        if modal and modal['negative']:
            return ClockZone(error=_OBSERVATION_SCOPE)
        return ClockZone('UTC')
    zone = resolve_clock_zone(location)
    if (zone.error is None or zone.choices or _TIME_ZONE_SLOT.match(location)
            or _IANA.fullmatch(_PREFIX.sub('', location).strip(_WRAPPERS))):
        return zone
    return None


def compared_time_zones(text):
    """Ground an explanation of a time difference in two named places."""
    if len(text) > 260 or not (match := _ZONE_COMPARISON.fullmatch(text)):
        return None
    if not re.search(r'\b(?:разниц\w*|в\s+час\w*|во\s+времени)\b', text, re.I):
        return None
    left, right = (resolve_clock_zone(place) for place in
                   (match['first'] or match['left'], match['second'] or match['right']))
    if left.time_zone and right.time_zone:
        return left, right
    return None
