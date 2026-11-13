"""Каталог надёжных источников.

Нейросеть не пишет ссылки сама (она может их выдумать). Она выбирает id источника
и поисковый запрос, а ссылку на поиск по этому источнику собирает код.
"""
from urllib.parse import quote_plus


def _site(domain: str):
    return lambda q: "https://www.google.com/search?q=" + quote_plus(f"site:{domain} {q}")


SOURCES = {
    "who": ("ВОЗ", _site("who.int")),
    "minzdrav": ("Клин. рекомендации Минздрава", _site("cr.minzdrav.gov.ru")),
    "rospotreb": ("Роспотребнадзор", _site("rospotrebnadzor.ru")),
    "pubmed": ("PubMed", lambda q: "https://pubmed.ncbi.nlm.nih.gov/?term=" + quote_plus(q)),
    "cochrane": ("Cochrane", _site("cochranelibrary.com")),
    "cdc": ("CDC", _site("cdc.gov")),
    "nice": ("NICE", _site("nice.org.uk")),
    "cancer": ("Нац. институт рака США", _site("cancer.gov")),
}


def link(source_id: str, query: str) -> tuple[str, str] | None:
    entry = SOURCES.get(str(source_id))
    if not entry or not query:
        return None
    name, build = entry
    return name, build(str(query))
