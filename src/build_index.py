"""
Build the municipality index from Wikidata: every US city/town/village with a
known official website (property P856).

Coverage note: only ~130-150 US municipalities nationwide have P856 set on
Wikidata at all (mostly major cities) — this is a small seed, not a
comprehensive index. Full coverage comes from the state-league scrapers in
src/scrapers/. PLACE_TYPES covers city/town/village (Q515/Q3957/Q532); New
England town governments and Midwest civil townships may need additional
QIDs — verify any new QID on wikidata.org before adding it.

State resolution: a single SPARQL join from place to state (via P131+ or
bounded P131 alternation) reliably times out (502/504) on the public
endpoint at this row count, so instead each place's immediate P131 parent is
fetched in the same query, and the walk up to the state is done afterward
with small per-entity REST lookups (cheap, cached, no query timeout risk).
"""

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
ENTITY_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "muni-packet-crawler/0.1 (personal research tool; contact: local use only)"

PLACE_TYPES = ["wd:Q515", "wd:Q3957", "wd:Q532"]  # city, town, village
US_STATE_QID = "Q35657"
MAX_HOPS = 5

QUERY_TEMPLATE = """
SELECT ?place ?placeLabel ?website ?placeType ?parent WHERE {{
  VALUES ?placeType {{ {place_types} }}
  ?place wdt:P31 ?placeType .
  ?place wdt:P17 wd:Q30 .
  ?place wdt:P856 ?website .
  OPTIONAL {{ ?place wdt:P131 ?parent . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?place
LIMIT {limit}
OFFSET {offset}
"""

PAGE_SIZE = 500

PLACE_TYPE_NAMES = {
    "http://www.wikidata.org/entity/Q515": "city",
    "http://www.wikidata.org/entity/Q3957": "town",
    "http://www.wikidata.org/entity/Q532": "village",
}

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_entity_cache: dict[str, dict] = {}


def run_query(offset: int) -> list[dict]:
    query = QUERY_TEMPLATE.format(
        place_types=" ".join(PLACE_TYPES), limit=PAGE_SIZE, offset=offset
    )
    resp = _session.get(
        SPARQL_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def fetch_entity(qid: str) -> dict:
    if qid in _entity_cache:
        return _entity_cache[qid]
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = _session.get(
                ENTITY_API,
                params={
                    "action": "wbgetentities",
                    "ids": qid,
                    "props": "labels|claims",
                    "languages": "en",
                    "format": "json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            entity = resp.json()["entities"][qid]
            _entity_cache[qid] = entity
            return entity
        except (requests.RequestException, ValueError) as e:
            last_error = e
            time.sleep(2 * (attempt + 1))
    raise last_error  # type: ignore[misc]


def claim_value_ids(entity: dict, prop: str) -> list[str]:
    ids = []
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and "id" in value:
            ids.append(value["id"])
    return ids


def resolve_state(parent_qid: str | None) -> str:
    """Walk P131 parents up from the immediate parent until an entity that
    is itself a US state (Q35657) is found, or MAX_HOPS is exhausted."""
    current = parent_qid
    for _ in range(MAX_HOPS):
        if not current:
            return ""
        try:
            entity = fetch_entity(current)
        except requests.RequestException:
            return ""
        if US_STATE_QID in claim_value_ids(entity, "P31"):
            return entity.get("labels", {}).get("en", {}).get("value", "")
        parents = claim_value_ids(entity, "P131")
        current = parents[0] if parents else None
    return ""


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    offset = 0
    total_inserted = 0

    while True:
        for attempt in range(3):
            try:
                rows = run_query(offset)
                break
            except requests.RequestException as e:
                wait = 5 * (attempt + 1)
                print(f"query failed ({e}), retrying in {wait}s...")
                time.sleep(wait)
        else:
            print(f"giving up at offset {offset} after 3 attempts")
            break

        if not rows:
            break

        for row in rows:
            wikidata_id = row["place"]["value"].rsplit("/", 1)[-1]
            name = row["placeLabel"]["value"]
            website = row["website"]["value"]
            place_type = PLACE_TYPE_NAMES.get(row["placeType"]["value"], "unknown")
            parent_qid = row.get("parent", {}).get("value", "").rsplit("/", 1)[-1] or None

            state = resolve_state(parent_qid)
            if not state:
                print(f"  skipping {name} ({wikidata_id}) — could not resolve state")
                continue

            cur.execute(
                """
                INSERT INTO municipalities (wikidata_id, name, state, place_type, official_url, source)
                VALUES (?, ?, ?, ?, ?, 'wikidata')
                ON CONFLICT(wikidata_id) DO UPDATE SET
                    name=excluded.name, state=excluded.state,
                    place_type=excluded.place_type, official_url=excluded.official_url,
                    updated_at=CURRENT_TIMESTAMP
                ON CONFLICT(state, name) DO UPDATE SET
                    wikidata_id=excluded.wikidata_id,
                    official_url=excluded.official_url,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (wikidata_id, name, state, place_type, website),
            )
            total_inserted += 1
        conn.commit()
        print(f"offset {offset}: processed {len(rows)} rows (inserted so far {total_inserted})")

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(1)  # be polite to the shared public endpoint

    conn.close()
    print(f"done — {total_inserted} municipalities indexed")


if __name__ == "__main__":
    main()
