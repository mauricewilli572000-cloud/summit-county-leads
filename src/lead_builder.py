"""Feed row -> LeadRecord (pure, offline-testable)."""
import re
from typing import Optional

import config
import parsers
import summit_feed
from enrichment import ParcelIndex, is_absentee
from lead_schema import LeadRecord
from lead_scoring import score_lead

COUNTY = "Summit"

_ACRONYMS = {"Llc": "LLC", "Lp": "LP", "Llp": "LLP", "Pnc": "PNC", "Usaa": "USAA", "Hsbc": "HSBC", "Fha": "FHA",
             "Hud": "HUD", "Mers": "MERS", "Bmo": "BMO", "Td": "TD", "Us": "US", "Na": "N.A."}
_AKA_RE = re.compile(r"\s+(?:A/K/A|AKA|F/K/A|FKA|N/K/A|NKA)\s+", re.I)


def clean_company(name: str) -> str:
    """Plaintiffs are companies/trustees, never 'LAST, FIRST' - title-case them without reordering."""
    n = parsers.clean(name)
    if n.isupper():
        n = n.title()
    n = re.sub(r"\b[A-Za-z]{2,5}\b", lambda m: _ACRONYMS.get(m.group(0), m.group(0)), n)
    return re.sub(r"\b(a/k/a|s/b/m|f/k/a|n/k/a)\b", lambda m: m.group(1).lower(), n, flags=re.I)


def strip_alias(defendant: str) -> str:
    """'MATTHEW KRISTOFF A/K/A MATTHEW PAUL KRISTOFF' -> 'MATTHEW KRISTOFF' (the alias is the same person)."""
    return _AKA_RE.split(parsers.clean(defendant))[0]


def _mdY(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else ""


def match_address(lead: LeadRecord, parcels: ParcelIndex) -> None:
    """Fill property/mailing from the parcel file by owner name (unique matches only)."""
    if lead.property_address or not parcels:
        return
    _, defendants = summit_feed.split_case_name(lead.case_name)
    owners = parsers.dedupe_names([d for d in defendants if not parsers.is_non_owner(d)])
    parcel = parcels.by_owner(owners) if owners else None
    if parcel:
        lead.property_address = parcel["situs"]
        lead.mailing_address = parcel["mailing"]
        lead.parcel_number = parcel["parcel"]
        if "Address from name match" not in lead.flags:
            lead.flags.append("Address from name match")


def apply_docs(lead: LeadRecord, info: dict) -> None:
    """Copy facts read from the complaint / title report onto the lead (never overwrite with blanks)."""
    for k in ("property_address", "property_city", "property_zip", "parcel_number", "amount_owed",
              "interest_rate", "loan_type", "mailing_address"):
        if info.get(k) not in (None, ""):
            setattr(lead, k, info[k])
    if info.get("property_address"):
        lead.flags = [f for f in lead.flags if f != "Address from name match"]
        src = {"title report": "Address from title report", "complaint": "Address from complaint"}.get(
            info.get("address_source"), "Address from service list")
        if src not in lead.flags:
            lead.flags.append(src)
    if not lead.owner_name and info.get("record_owner"):
        lead.owner_name = info["record_owner"]
        lead.flags = [f for f in lead.flags if f != "No individual owner named"]
    if info.get("tax") and "Tax lien (county)" not in lead.flags:
        lead.flags.append("Tax lien (county)")
    if info.get("modified") and "Loan modified" not in lead.flags:
        lead.flags.append("Loan modified")
    if info.get("modified_principal") and lead.amount_owed is None:
        lead.amount_owed = info["modified_principal"]


def build_lead(row: dict, parcels: Optional[ParcelIndex] = None, categories=None) -> Optional[LeadRecord]:
    cats = [c.lower() for c in (categories or config.FEED_CATEGORIES)]
    if not any(c in row["category"].lower() for c in cats):
        return None
    plaintiff, defendants = summit_feed.split_case_name(row["case_name"])
    defendants = [strip_alias(d) for d in defendants]
    owners = parsers.dedupe_names([d for d in defendants if not parsers.is_non_owner(d)])
    key = summit_feed.lead_key(row["case_name"], row["date_added_raw"])
    lead = LeadRecord(
        county=COUNTY, case_number="SUM-" + key[:8].upper(), doc_number="",
        owner_name=" & ".join(owners[:3]), plaintiff=("Summit County Fiscal Officer" if "tax" in row["category"].lower() else clean_company(plaintiff)),
        defendant=", ".join(parsers.pretty_name(d) for d in defendants),
        lead_type="Tax Foreclosure" if "tax" in row["category"].lower() else "Foreclosure", filing_date=_mdY(row["date_added"]),
        source_url=config.FEED_URL, case_name=row["case_name"], review_status=row["review_status"],
    )
    if "pending" in row["review_status"].lower():
        lead.flags.append("Pending clerk review")
    if not owners:
        lead.flags.append("No individual owner named")
    match_address(lead, parcels)
    lead.lead_score = score_lead(lead)
    return lead
