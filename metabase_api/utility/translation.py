import logging
import os
import time
from enum import Enum, auto
from pathlib import Path
from typing import Callable
from copy import copy
import yaml
from googletrans import Translator as GoogleTranslator

from metabase_api.utility.util import case_expand

THIS_FILE_PATH = Path(os.path.dirname(os.path.realpath(__file__)))
TRANSLATION_CONFIG_LOC = (THIS_FILE_PATH / ".." / "resources").resolve()

_logger = logging.getLogger(__name__)


class TranslationPolicyOnMiss(Enum):
    FAIL = auto()
    MIRROR = auto()
    FALLBACK_ON_GOOGLE_TRANSLATE = auto()


class Language(Enum):
    EN = auto()
    FR = auto()


def _start_google_translator() -> GoogleTranslator:
    _google_translator: GoogleTranslator = GoogleTranslator()
    _google_translator.raise_Exception = True
    return _google_translator


class Translator:
    """Translator from English."""

    def __init__(
        self,
        to: Language,
        on_miss: TranslationPolicyOnMiss,
        case_expand_user_terms: bool,
    ):
        """

        Args:
            to: to what language will be translate
            on_miss: what is the action taken when there is a miss on term-matching
            case_expand_user_terms:
        """
        self.to_lang = to
        self.on_miss = on_miss
        _logger.info("Firing up Google Translator")
        self._google_translator: GoogleTranslator = _start_google_translator()
        # Reads translation terms from a YAML file
        with open(
            TRANSLATION_CONFIG_LOC / "translation_user_defined_from_en.yml", "r"
        ) as stream:
            _user_labels = yaml.safe_load(stream)
        # since we know the specific language, let's get rid of a level of this dict:
        _logger.info(f"Flattening translation dict for {self.to_lang.name}")
        self._user_terms: dict[str, str] = {
            k: v[self.to_lang.name]
            for (k, v) in _user_labels.items()
            if self.to_lang.name in v
        }
        if case_expand_user_terms:
            _d: dict[str, str] = dict()
            for s, t in self._user_terms.items():
                combs = case_expand(s)
                for c in combs:
                    _d[c] = t
            self._user_terms = self._user_terms | _d
        # we also keep a copy of these initial values in case we get asked
        _logger.info(
            f"Seeding translation structure for {self.to_lang.name} with {len(self._user_terms)} terms"
        )
        self._translation_dict = copy(self._user_terms)

    def _use_google_translate(self, s: str) -> str:
        def _with_retry(f: Callable[[str], str], s: str) -> str:
            try:
                r = f(s)
            except Exception as e:
                # let's wait and re-try
                _logger.warning("Google Translate needs to re-try... sleeping...")
                # self._google_translator = _start_google_translator()
                time.sleep(5)  # in seconds
                _logger.warning("...back!")
                r = f(s)
            return r

        _detected_lang = _with_retry(
            f=lambda p: self._google_translator.detect(p).lang, s=s
        )
        if _detected_lang != "en":
            # weird, what I've been asked to translate is not in English. Is it already translated?
            if _detected_lang.lower() == self.to_lang.name.lower():
                _logger.warning(f"'{s}' is already on language '{self.to_lang.name}'")
                t = s
            else:
                msg = f"Sentence '{s}' does not seem to be in English nor the target language ({self.to_lang.name})"
                msg += f", so I can't translate it"
                raise RuntimeError(msg)
        else:
            t = _with_retry(
                f=lambda p: self._google_translator.translate(
                    p, dest=self.to_lang.name
                ).text,
                s=s,
            )
            # specific rules for capitalization in different languages
            if self.to_lang == Language.FR:
                # in a sentence: only first word is capitalized
                as_list = t.split()
                first_word = as_list[0].capitalize()
                rest_of_words = [s.casefold() for s in as_list[1:]]
                t = " ".join([first_word] + rest_of_words)
            _logger.debug(f"[Google Translate] '{s}' -> '{t}'; updating dictionary...")
            self._translation_dict[s] = t
        return t

    @property
    def user_defined_terms(self) -> dict[str, str]:
        return self._user_terms

    def translate(self, sentence: str) -> str:
        """Translates a full expression. Handles pre/post white spaces."""
        if self.to_lang == Language.EN:
            # nothing to do - we assume the dashboard is originally in English
            return sentence
        # handle whitespaces
        # how many at the left? And at the right?
        lblanks = len(sentence) - len(sentence.lstrip())
        rblanks = len(sentence) - len(sentence.rstrip())
        sentence = sentence.strip()
        t: str
        if len(sentence) == 0:
            t = sentence
        else:
            # by any chance - am I trying to translate something I already translated...?
            if sentence in self._translation_dict.values():
                _logger.debug(
                    f"Asked to translate '{sentence}', which is in fact a translated term. Ignoring it :shrug:"
                )
                t = sentence
            else:
                if sentence not in self._translation_dict:
                    _err_msg = f"No translation found for '{sentence}'"
                    if self.on_miss == TranslationPolicyOnMiss.FAIL:
                        _logger.error(_err_msg)
                        raise RuntimeError(_err_msg)
                    elif self.on_miss == TranslationPolicyOnMiss.MIRROR:
                        _logger.debug(
                            f"{_err_msg}; returning it as policy is '{self.on_miss}'"
                        )
                        t = sentence
                    elif (
                        self.on_miss
                        == TranslationPolicyOnMiss.FALLBACK_ON_GOOGLE_TRANSLATE
                    ):
                        t = self._use_google_translate(sentence)
                    else:
                        raise RuntimeError(
                            f"Internal: I don't know what to do with translation policy {self.on_miss}"
                        )
                else:
                    t = self._translation_dict[sentence]
        # let's re-add the white spaces
        t = " " * lblanks + t + " " * rblanks
        if self.to_lang == Language.FR:
            # only first word of a sentence is capitalized (if any!)
            # do we have more than 1 word?
            as_array = t.split()
            if len(as_array) > 1:
                # I don't do _anything_ with first word but I un-capitalize the rest
                t = " ".join([as_array[0]] + [s.lower() for s in as_array[1:]])
        return t


"""Structure with a translator for each language we handle."""
_logger.info("Seeding translator for all languages...")
Translators: dict[Language, Translator] = {
    lang: Translator(
        to=lang,
        on_miss=TranslationPolicyOnMiss.FALLBACK_ON_GOOGLE_TRANSLATE,
        case_expand_user_terms=True,
    )
    for lang in Language
}
