# translators/laratranslate.py
from .base import *
import requests
import time
from typing import Optional

@register_translator('Lara Translate')
class LaraTranslator(BaseTranslator):
    concate_text = False
    MAX_CHARS_PER_REQUEST = 5000

    params: Dict = {
        'endpoint': 'https://webapi.laratranslate.com/translate',
        'delay': 0.1,
        'multiple_proxies': {
            'type': 'editor',
            'value': '',
            'description': 'Proxy list (e.g., http://host:port, socks5://host:port), one per line. Requests will rotate through this list. If empty, system proxy will be used.',
        },
    }

    def _setup_translator(self):
        self.lang_map['Auto'] = ''
        self.lang_map['简体中文'] = 'zh'
        self.lang_map['繁體中文'] = 'zh'
        self.lang_map['日本語'] = 'ja'
        self.lang_map['English'] = 'en'
        self.lang_map['한국어'] = 'ko'
        self.lang_map['Tiếng Việt'] = 'vi'
        self.lang_map['čeština'] = 'cs'
        self.lang_map['Nederlands'] = 'nl'
        self.lang_map['Français'] = 'fr'
        self.lang_map['Deutsch'] = 'de'
        self.lang_map['magyar nyelv'] = 'hu'
        self.lang_map['Italiano'] = 'it'
        self.lang_map['Polski'] = 'pl'
        self.lang_map['Português'] = 'pt'
        self.lang_map['limba română'] = 'ro'
        self.lang_map['русский язык'] = 'ru'
        self.lang_map['Español'] = 'es'
        self.lang_map['Türk dili'] = 'tr'
        self.lang_map['Arabic'] = 'ar'
        self.lang_map['Hindi'] = 'hi'
        self.lang_map['Malayalam'] = 'ml'
        self.lang_map['Tamil'] = 'ta'

        self.translate_url = self.params['endpoint']
        self.current_proxy_index = 0

    @property
    def multiple_proxies_list(self) -> List[str]:
        proxies_str = self.get_param_value("multiple_proxies")
        if not isinstance(proxies_str, str):
            proxies_str = ""
        return [proxy.strip() for proxy in proxies_str.splitlines() if proxy.strip()]

    def _get_next_proxy(self) -> Optional[Dict[str, str]]:
        proxies_list = self.multiple_proxies_list

        if not proxies_list:
            if PROXY:
                 LOGGER.debug(f"No user proxies, using system default: {PROXY}")
                 return PROXY
            LOGGER.debug("No user proxies and no system proxies.")
            return None

        proxy_string = proxies_list[self.current_proxy_index % len(proxies_list)]
        self.current_proxy_index = (self.current_proxy_index + 1) % len(proxies_list)

        formatted_proxies = {
            'http': proxy_string,
            'https': proxy_string
        }
        LOGGER.debug(f"Using user proxy (index {self.current_proxy_index-1}/{len(proxies_list)}): {proxy_string}")
        return formatted_proxies

    def updateParam(self, param_key: str, param_content):
        super().updateParam(param_key, param_content)
        LOGGER.debug(f"Parameter '{param_key}' updated to: {param_content}")

        if param_key == 'endpoint':
            self.translate_url = self.params['endpoint']
            LOGGER.info(f"Lara Translate endpoint updated to: {self.translate_url}")


    def _translate(self, src_list: List[str]) -> List[str]:
        translated_list = [''] * len(src_list)
        api_source = self.lang_map.get(self.lang_source, '') if self.lang_source == 'Auto' else self.lang_map.get(self.lang_source, '')
        api_target = self.lang_map.get(self.lang_target, 'en')

        total_chars_processed_in_batch = 0

        for i, text in enumerate(src_list):
            if text is None or text.strip() == "":
                translated_list[i] = text if text is not None else ''
                continue

            text_length = len(text)
            total_chars_processed_in_batch += text_length

            LOGGER.debug(f"Lara Translate: Processing block {i+1}/{len(src_list)} ({text_length} chars). Batch total: {total_chars_processed_in_batch}")

            if text_length > self.MAX_CHARS_PER_REQUEST:
                LOGGER.warning(f"Lara Translate: Skipping block {i+1} ({text_length} > {self.MAX_CHARS_PER_REQUEST} chars).")
                translated_list[i] = f"[Text too long, {text_length} chars]"
                continue

            selected_proxies = self._get_next_proxy()

            try:
                body = {
                    'q': text,
                    'source': api_source,
                    'target': api_target,
                    'instructions': []
                }

                headers = {
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'origin': 'https://laratranslate.com',
                    'referer': 'https://laratranslate.com/',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0'
                }

                response = requests.post(
                    self.translate_url,
                    json=body,
                    headers=headers,
                    proxies=selected_proxies
                )
                response.raise_for_status()

                data = response.json()
                translated_text = data.get('content', {}).get('translation', '')

                quota_info = data.get('content', {}).get('quota', {})
                if quota_info:
                    LOGGER.debug(f"Lara Translate Quota: Used {quota_info.get('current_value', '?')} / {quota_info.get('threshold', '?')} chars ({quota_info.get('interval', 'interval unspecified')})")

                translated_list[i] = translated_text

                time.sleep(self.delay())

            except requests.exceptions.RequestException as e:
                LOGGER.error(f"Lara Translate: Request failed for block {i+1}: {e}")
                translated_list[i] = f"[Translation Error: {e}]"
            except Exception as e:
                LOGGER.error(f"Lara Translate: Unexpected error for block {i+1}: {e}")
                translated_list[i] = f"[Translation Error: {e}]"

        LOGGER.info(f"Lara Translate: Batch finished. Total chars processed: {total_chars_processed_in_batch}")
        return translated_list