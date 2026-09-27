"""Find portrait candidates for a person via Wikidata (P18 image on Wikimedia Commons).

Prints each matching Wikidata item with its description and a direct image URL,
so the right person can be confirmed before the URL goes into data/leaders/*.csv.

Usage:
    python tools/find_portrait.py "Abdelaziz Bouteflika" ["Lamine Zéroual" ...]
    python tools/find_portrait.py --lang fr "Mohamed Lamari"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request

UA = "TND-HOI4-mod/1.0 (portrait finder)"


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001 - retried
            if attempt == 3:
                raise
            time.sleep(3 * 2 ** attempt)
    return {}


def commons_url(filename: str) -> str:
    """Direct upload.wikimedia.org URL for a Commons file name."""
    name = filename.replace(" ", "_")
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    return f"https://upload.wikimedia.org/wikipedia/commons/{h[0]}/{h[:2]}/{urllib.parse.quote(name)}"


def search(name: str, lang: str, limit: int) -> list[str]:
    q = urllib.parse.urlencode({"action": "wbsearchentities", "search": name, "language": lang,
                                "uselang": "en", "type": "item", "limit": limit, "format": "json"})
    data = get(f"https://www.wikidata.org/w/api.php?{q}")
    out = []
    ids = [s["id"] for s in data.get("search", [])]
    if not ids:
        return [f"{name}: no Wikidata match"]
    ents = get("https://www.wikidata.org/w/api.php?action=wbgetentities&props=claims|descriptions|labels"
               f"&languages=en&format=json&ids={'|'.join(ids)}").get("entities", {})
    for qid in ids:
        e = ents.get(qid, {})
        claims = e.get("claims", {})
        if "Q5" not in {c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                        for c in claims.get("P31", [])}:
            continue  # not a human
        label = e.get("labels", {}).get("en", {}).get("value", name)
        desc = e.get("descriptions", {}).get("en", {}).get("value", "")
        imgs = [c["mainsnak"]["datavalue"]["value"] for c in claims.get("P18", []) if "datavalue" in c["mainsnak"]]
        url = commons_url(imgs[0]) if imgs else "(no image on Wikidata)"
        out.append(f"{name}: {qid} {label} - {desc}\n    {url}")
    return out or [f"{name}: no human among the matches"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="+")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    for n in args.names:
        print("\n".join(search(n, args.lang, args.limit)))
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
