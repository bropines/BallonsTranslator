from functools import lru_cache
import re
from typing import Optional

from .logger import logger as LOGGER

try:
    import pyphen
    HAS_PYPHEN = True
except ImportError:
    pyphen = None
    HAS_PYPHEN = False


# Map short/common language codes to Pyphen/Hunspell dictionary tags
LANG_MAP = {
    'ru': 'ru_RU',
    'rus': 'ru_RU',
    'ru_RU': 'ru_RU',
    'en': 'en_US',
    'eng': 'en_US',
    'en_US': 'en_US',
    'en_GB': 'en_GB',
    'es': 'es_ES',
    'spa': 'es_ES',
    'fr': 'fr_FR',
    'fre': 'fr_FR',
    'fra': 'fr_FR',
    'de': 'de_DE',
    'ger': 'de_DE',
    'deu': 'de_DE',
    'it': 'it_IT',
    'ita': 'it_IT',
    'pt': 'pt_PT',
    'por': 'pt_PT',
    'pt_BR': 'pt_BR',
    'uk': 'uk_UA',
    'ukr': 'uk_UA',
    'pl': 'pl_PL',
    'pol': 'pl_PL',
    'nl': 'nl_NL',
    'nld': 'nl_NL',
    'dut': 'nl_NL',
}

# Regex to separate words from tags, entities, punctuation, and whitespace
TOKEN_PATTERN = re.compile(r'(<[^>]+>|&[a-zA-Z0-9#]+;|[\w]+|[^\w\s<>&]+|\s+)', re.UNICODE)
WORD_PATTERN = re.compile(r'^\w+$', re.UNICODE)


@lru_cache(maxsize=16)
def get_hyphen_dict(lang: str) -> Optional[object]:
    """Return a cached Pyphen dictionary instance for a language.

    >>> get_hyphen_dict('invalid_lang_xyz') is None
    True
    """
    if not HAS_PYPHEN or pyphen is None:
        return None

    normalized_lang = LANG_MAP.get(lang.lower(), lang)
    try:
        return pyphen.Pyphen(lang=normalized_lang)
    except Exception as exc:
        LOGGER.debug('Could not load pyphen dictionary for %r: %s', lang, exc)
        # Try base language code if full code failed
        base = normalized_lang.split('_')[0]
        if base != normalized_lang:
            try:
                return pyphen.Pyphen(lang=base)
            except Exception:
                pass
        return None


CYRILLIC_PATTERN = re.compile(r'[\u0400-\u04FF]', re.UNICODE)


def detect_word_lang(word: str, default_lang: str = 'en') -> str:
    """Detect language for a word based on character script."""
    if CYRILLIC_PATTERN.search(word):
        return 'ru'
    return default_lang or 'en'


def hyphenate_word(word: str, lang: str = 'auto', hyphen: str = '\u00ad') -> str:
    """Insert soft hyphens into a single word using pyphen.

    >>> hyphenate_word('test', 'en')
    'test'
    """
    if not word or len(word) <= 3 or '\u00ad' in word:
        return word

    if lang == 'auto' or lang in (None, 'unknown', 'ja', 'zh', 'cjk'):
        lang = detect_word_lang(word)
    elif lang != 'ru' and CYRILLIC_PATTERN.search(word):
        lang = 'ru'

    pyphen_dict = get_hyphen_dict(lang)
    if pyphen_dict is None:
        return word

    try:
        return pyphen_dict.inserted(word, hyphen=hyphen)
    except Exception:
        return word


def hyphenate_text(text: str, lang: str = 'auto', hyphen: str = '\u00ad') -> str:
    """Insert soft hyphens into all words of a text, preserving HTML and symbols.

    >>> hyphenate_text('Hello world', 'en')
    'Hel\\xadlo world'
    >>> hyphenate_text('<b>Hello</b> world', 'en')
    '<b>Hel\\xadlo</b> world'
    """
    if not text or not HAS_PYPHEN:
        return text

    if lang == 'auto' or lang in (None, 'unknown', 'ja', 'zh', 'cjk'):
        lang = 'ru' if CYRILLIC_PATTERN.search(text) else 'en'

    parts = TOKEN_PATTERN.findall(text)
    result = []
    for part in parts:
        if part.startswith('<') or part.startswith('&') or not WORD_PATTERN.match(part):
            result.append(part)
        else:
            result.append(hyphenate_word(part, lang=lang, hyphen=hyphen))
    return ''.join(result)


def strip_soft_hyphens(text: str) -> str:
    """Remove soft hyphen characters from a string.

    >>> strip_soft_hyphens('hel\\u00adlo')
    'hello'
    """
    if not text:
        return text
    return text.replace('\u00ad', '').replace('&shy;', '')
