"""
Shared helper for scrapers that infer a municipality's website from a
contact email's domain (Kentucky, Mississippi) rather than from a
directory that states the website directly.

A domain resolving to a live page isn't enough on its own — a first
pass on both states turned up domains that are perfectly live but
belong to something other than the city: a clerk's local ISP or phone
co-op (windstream.net, scrtc.com), a webmail login page the clerk's
email address itself resolves to (mail.yahoo.com), even a college
whose town shares its name coincidentally. A pure "does the domain
contain the city's name" check isn't enough either, since plenty of
real municipal domains are abbreviations (Bowling Green, KY's real site
is bgky.org; Fort Thomas, KY's is ftthomas.org) that a plain substring
check would wrongly reject.

So a domain is accepted if the city's name appears in the domain OR in
the page's <title> — title only, not full body text, after an initial
version that checked body text let through several false positives
whose homepage happens to mention the town in passing (a furniture
store's "...in Woodland, Tupelo..." location blurb; a phone directory
site with enough scraped text to coincidentally contain the town's
name as a substring once punctuation is stripped). Even title-only
matching isn't foolproof on its own, since a few small-town ISPs and
utilities are themselves *named after* the town they serve (Georgetown
Telephone Co.'s title literally says "Georgetown") — so on top of the
name check, a domain or title containing an obvious non-government
business/utility keyword is vetoed outright regardless of whether the
name also matches.

Sites that clear neither bar are skipped — a real gap in the source
data is preferable to a wrong entry.

One more failure mode a domain-name match alone doesn't catch: a
domain that used to be the city's real site but has since expired and
been squatted (one Mississippi town's old contact domain now 302s to
an unrelated gambling site). A name match on the *requested* domain
string doesn't mean the page actually landed there — so the title is
always checked, even when the domain matched, specifically to veto
obvious squatter content (the domain-match path only skips needing the
title to *confirm* the match, it never skips being allowed to *reject*
it).
"""

import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

NON_GOVERNMENT_KEYWORDS = (
    "tel", "phone", "wireless", "cable", "furniture", "electric", "coop",
    "bank", "insurance", "realty", "hardware", "pharmacy", "clinic",
    "hospital", "motors", "chevrolet", "dealership", "casino", "hotel",
)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def verify_domain_belongs_to(session: requests.Session, name: str, domain: str) -> str | None:
    """Returns a working URL for domain if it both resolves live AND is
    plausibly this municipality's own site, else None."""
    normalized_name = normalize(name)
    normalized_domain = normalize(domain)
    if any(keyword in normalized_domain for keyword in NON_GOVERNMENT_KEYWORDS):
        return None  # a phone co-op, utility, or business, not the city itself
    domain_name_matches = normalized_name in normalized_domain

    for scheme in ("https://", "http://"):
        url = f"{scheme}{domain}/"
        try:
            resp = session.get(url, timeout=12, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code >= 400:
            continue

        final_netloc = normalize(urlparse(resp.url).netloc)
        stayed_on_requested_domain = normalized_domain.rstrip("/") in final_netloc or final_netloc in normalized_domain

        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.get_text() if soup.title else ""
        normalized_title = normalize(title)
        if any(keyword in normalized_title for keyword in NON_GOVERNMENT_KEYWORDS):
            return None

        if domain_name_matches and stayed_on_requested_domain:
            return resp.url  # e.g. bgky.org — the requested domain itself names the city
        if normalized_name in normalized_title:
            return resp.url  # abbreviated domain, but the page confirms the city
        return None  # resolves, but nothing here actually ties it to the city
    return None
