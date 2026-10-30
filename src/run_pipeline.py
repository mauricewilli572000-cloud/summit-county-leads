"""
Summit County foreclosure lead pipeline (its own repo).

Clerk "new civil filings" feed -> keep Foreclosure rows -> owner/plaintiff from the case name
-> parcel-file address match (data/summit_parcels.csv, optional) -> score -> accumulate (merge by key)
-> GHL CSV + dashboard/summit.json for the combined dashboard.

  python run_pipeline.py            (from summit-foreclosure-scraper/src)
  python run_pipeline.py --headed
"""
import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List

import doc_extract
import summit_feed
from enrichment import ParcelIndex
from ghl_export import export_to_ghl_csv
from lead_builder import apply_docs, build_lead, match_address
from lead_schema import LeadRecord
from lead_scoring import score_lead

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = PROJECT_DIR      # this project is its own repo
DATA_DIR = os.path.join(PROJECT_DIR, "data")
LEADS_JSON_PATH = os.path.join(DATA_DIR, "leads.json")
GHL_CSV_PATH = os.path.join(DATA_DIR, "ghl_export.csv")
PARCEL_CSV_PATH = os.path.join(DATA_DIR, "summit_parcels.csv")
SHARED_DASHBOARD_DIR = os.path.join(REPO_ROOT, "dashboard")
DASHBOARD_DATA_PATH = os.path.join(SHARED_DASHBOARD_DIR, "summit.json")
DASHBOARD_CSV_PATH = os.path.join(SHARED_DASHBOARD_DIR, "summit_ghl_export.csv")

# Same rule as Franklin: only leads with a property address go to the GHL CSV.
# The dashboard shows EVERY Summit foreclosure (the feed has no address, so an address-only
# dashboard would be empty until a parcel file exists). Set False to show all in the CSV too.
GHL_REQUIRES_ADDRESS = True


def _load() -> Dict[str, dict]:
    try:
        with open(LEADS_JSON_PATH, encoding="utf-8") as f:
            return {item["key"]: item for item in json.load(f)}
    except Exception:
        return {}


def _lead_from(d: dict) -> LeadRecord:
    known = {k: v for k, v in d.items() if k in LeadRecord.__dataclass_fields__}
    return LeadRecord(**known)


def merge(new: List[LeadRecord], parcels) -> List[LeadRecord]:
    existing = _load()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for lead in new:
        old = existing.get(lead.key())
        d = lead.to_dict()
        if old:
            for k, v in d.items():
                if v in ("", None, []) and old.get(k) not in ("", None, []):
                    d[k] = old[k]
            d["added_date"] = old.get("added_date") or today
        else:
            d["added_date"] = today
        d["key"] = lead.key()
        existing[lead.key()] = d
    leads = [_lead_from(d) for d in existing.values()]
    for l in leads:                                   # retry addresses once a parcel file appears
        match_address(l, parcels)
        l.lead_score = score_lead(l)
    return sorted(leads, key=lambda l: (-l.lead_score, l.filing_date))


def _write_all(leads: List[LeadRecord]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SHARED_DASHBOARD_DIR, exist_ok=True)
    with open(LEADS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump([{**l.to_dict(), "key": l.key()} for l in leads], f, indent=2)

    ghl = [l for l in leads if l.property_address] if GHL_REQUIRES_ADDRESS else leads
    export_to_ghl_csv(ghl, GHL_CSV_PATH)
    export_to_ghl_csv(ghl, DASHBOARD_CSV_PATH)

    with_addr = sum(1 for l in leads if l.property_address)
    payload = {
        "county": "Summit",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_leads": len(leads),
        "leads_with_property_address": with_addr,
        "leads_with_mailing_address": sum(1 for l in leads if l.mailing_address),
        "average_lead_score": round(sum(l.lead_score for l in leads) / len(leads), 1) if leads else 0,
        "leads": [l.to_dict() for l in leads],
    }
    with open(DASHBOARD_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


MAX_DOC_PULLS = 15       # "Get Docs" downloads per run; only leads that still lack an address are pulled


def _save_doc_debug(case_number: str, case_name: str, data) -> None:
    """When no address can be read, keep a text dump (file names + start of each document) for diagnosis."""
    import io
    import zipfile
    out = [f"{case_number}  {case_name}", f"download: {'none (server error / row not found)' if not data else f'{len(data)} bytes'}"]
    if data:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out += [f"  file: {i.filename} ({i.file_size} bytes)" for i in z.infolist()]
            for kind, txt in doc_extract.read_zip(data).items():
                out += ["", f"--- {kind} ({len(txt)} chars) ---", txt[:2500]]
        except Exception as e:
            out.append(f"zip unreadable: {e}")
    d = os.path.join(DATA_DIR, "doc_debug")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, case_number + ".txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def run(headed: bool = False) -> List[LeadRecord]:
    parcels = ParcelIndex(PARCEL_CSV_PATH) if os.path.exists(PARCEL_CSV_PATH) else None
    print("parcel file:", "loaded" if parcels else "none (addresses stay blank)")
    existing = _load()
    pulled = failed = 0
    new: List[LeadRecord] = []
    with summit_feed.SummitFeed(headless=not headed) as feed:
        rows = feed.fetch()
        print(f"feed rows parsed: {len(rows)}")
        if not rows:
            print("No rows parsed - keeping existing data. (data/last_feed.html holds the page for debugging.)")
        for r in rows:
            lead = build_lead(r, parcels)
            if not lead:
                continue
            old = existing.get(lead.key())
            if not (old and old.get("property_address")) and pulled < MAX_DOC_PULLS:
                pulled += 1
                try:
                    data = feed.fetch_docs(r)
                    info = doc_extract.extract(doc_extract.read_zip(data)) if data else {}
                    if info.get("property_address"):
                        apply_docs(lead, info)
                    else:
                        failed += 1
                        print(f"  no address found in docs for {lead.case_number}")
                        _save_doc_debug(lead.case_number, r["case_name"], data)
                except Exception as e:           # one bad document must not stop the run
                    failed += 1
                    print(f"  ! docs failed for {lead.case_number}: {e}")
            new.append(lead)
    for l in new:
        l.lead_score = score_lead(l)
    leads = merge(new, parcels)
    _write_all(leads)
    print(f"foreclosures this run: {len(new)}  document pulls: {pulled} (no address: {failed})  "
          f"total on file: {len(leads)}  with address: {sum(1 for l in leads if l.property_address)}")
    return leads


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    run(ap.parse_args().headed)
    sys.exit(0)
