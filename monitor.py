#!/usr/bin/env python3
"""FAA public MIT watch; supplemental only, not an operational ATC source."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

SOURCE = "https://www.fly.faa.gov/restrictions/restrictions?provFac=ALL&reqFac=ALL"
STATE = Path("mit_state.json")
REQUESTERS = {"ZNY", "ZAU"}
PROVIDER = "ZOB"


def fetch_restrictions():
    req = Request(SOURCE, headers={"User-Agent": "FAA-MIT-public-monitor/1.0"})
    with urlopen(req, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).upper() for c in rows[0].find_all(["th", "td"])]
        columns = ["REQUESTING", "PROVIDING", "RESTRICTION", "START TIME", "STOP TIME"]
        if all(any(h == label for h in headers) for label in columns):
            indices = {name: headers.index(name) for name in columns}
            out = []
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) > max(indices.values()):
                    out.append({name: cells[i] for name, i in indices.items()})
            return out
    raise RuntimeError("FAA table/header layout not recognized; no alerts issued.")


def matching(rows):
    results = {}
    for row in rows:
        # Facilities may be grouped, e.g. ZOB/ZID.
        requested = set(re.findall(r"\b[A-Z][A-Z0-9]{2,3}\b", row["REQUESTING"].upper()))
        provided = set(re.findall(r"\b[A-Z][A-Z0-9]{2,3}\b", row["PROVIDING"].upper()))
        if not (requested & REQUESTERS) or PROVIDER not in provided:
            continue

        description = row["RESTRICTION"].upper()
        # "20MIT", "20 MIT", and "20 MILES IN TRAIL"; don't count MINIT.
        amounts = [int(v) for v in re.findall(
            r"\b(\d{1,3})\s*(?:MIT\b|MILES?\s+IN\s+TRAIL\b)", description
        )]
        if not any(v > 10 for v in amounts):
            continue

        signature = "|".join(row[name].strip() for name in
                             ("REQUESTING", "PROVIDING", "RESTRICTION", "START TIME", "STOP TIME"))
        fingerprint = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        results[fingerprint] = row
    return results


def notify(topic, title, message):
    if not topic:
        raise RuntimeError("Missing NTFY_TOPIC repository secret.")
    url = "https://ntfy.sh/" + quote(topic, safe="")
    req = Request(url, data=message.encode("utf-8"), method="POST", headers={
        "Title": title,
        "Priority": "high",
        "Click": SOURCE,
        "Content-Type": "text/plain; charset=utf-8",
    })
    with urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy returned HTTP {response.status}")


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if "--test" in sys.argv:
        notify(topic, "FAA MIT monitor test", "GitHub to ntfy test succeeded. This is not an actual FAA restriction.")
        print("Test notification sent.")
        return

    rows = fetch_restrictions()
    current = matching(rows)
    prior = json.loads(STATE.read_text()) if STATE.exists() else {"active": []}
    previous_ids = set(prior.get("active", []))
    unseen = [(key, row) for key, row in current.items() if key not in previous_ids]

    for _, row in unseen:
        message = (f"REQUESTING: {row['REQUESTING']}\n"
                   f"PROVIDING: {row['PROVIDING']}\n"
                   f"RESTRICTION: {row['RESTRICTION']}\n"
                   f"START: {row['START TIME']} UTC\n"
                   f"STOP: {row['STOP TIME']} UTC\n"
                   f"Checked: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n"
                   f"FAA: {SOURCE}")
        notify(topic, "FAA MIT alert: ZOB providing", message)
        print(f"Sent qualifying restriction: {row['RESTRICTION']}")

    # Current active set, not lifetime history: alerts again on a reissued restriction
    # after it disappeared from the public FAA table.
    new_state = {"active": sorted(current)}
    if prior != new_state:
        STATE.write_text(json.dumps(new_state, indent=2) + "\n")
    print(f"FAA rows: {len(rows)}; matching: {len(current)}; newly alerted: {len(unseen)}")


if __name__ == "__main__":
    main()
