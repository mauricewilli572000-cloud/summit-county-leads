"""Read the documents behind a Summit "Get Docs" download (a .zip of PDFs) and pull out lead facts.

The zip holds text-based PDFs, named like  <id>_<n>_FORECLOSURE_COMPLAINT.pdf :
  FORECLOSURE_COMPLAINT            amount due, interest rate, modified principal, HUD/VA/USDA parties
  PRELIMINARY_JUDICIAL_REPORT      PROPERTY ADDRESS, PARCEL NO, record owner, mortgage amount/date
  INSTRUCTIONS_TO_CLERK_FOR_SERVICE  defendants with the address they will be served at
Everything is best-effort: any field we cannot find stays empty rather than guessed.
"""
import io
import re
import zipfile
from typing import Dict, List, Optional

SUFFIX = (r"(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|CT|COURT|BLVD|BOULEVARD|WAY|PL|PLACE|CIR|CIRCLE|"
          r"PKWY|PARKWAY|TRL|TRAIL|TER|TERRACE|HWY|HIGHWAY|PIKE|LOOP|ROW|RUN|PATH)")
DIRS = r"(?:\s+(?:N|S|E|W|NE|NW|SE|SW))?"
UNIT = r"(?:\s+(?:APT|UNIT|STE|SUITE|#)\.?\s*[\w-]+)?"
SUMMIT_CITIES = ["CUYAHOGA FALLS", "MUNROE FALLS", "NORTHFIELD", "TWINSBURG", "MACEDONIA", "BARBERTON", "TALLMADGE",
                 "UNIONTOWN", "FAIRLAWN", "COVENTRY", "RICHFIELD", "BATH", "COPLEY", "AKRON", "STOW", "HUDSON",
                 "GREEN", "NORTON", "SILVER LAKE", "LAKEMORE", "MOGADORE", "PENINSULA", "SAGAMORE HILLS",
                 "BOSTON HEIGHTS", "CLINTON", "CANTON", "KENT", "RAVENNA", "MEDINA", "WADSWORTH", "STREETSBORO",
                 "ROOTSTOWN", "NORTH CANTON", "NEW FRANKLIN", "SPRINGFIELD", "NORTHAMPTON", "BRECKSVILLE"]


def pdf_text(data: bytes, max_pages: Optional[int] = None) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            if max_pages and i >= max_pages:
                break
            out.append(page.extract_text(layout=False) or "")
    return "\n".join(out)


def read_zip(data: bytes) -> Dict[str, str]:
    """{'complaint': text, 'report': text, 'service': text, ...} keyed by document type."""
    docs: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if not n.lower().endswith(".pdf"):
                continue
            up = n.upper()
            kind = ("complaint" if "COMPLAINT" in up else "report" if "JUDICIAL_REPORT" in up or "TITLE" in up
                    else "service" if "INSTRUCTIONS" in up or "SERVICE" in up else "other")
            try:
                txt = pdf_text(z.read(n), max_pages=10 if kind == "complaint" else 12)
            except Exception:
                continue
            docs[kind] = (docs.get(kind, "") + "\n" + txt).strip()
    return docs


def _money(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def split_street_city(blob: str) -> (str, str):
    """'1708 ROBINWOOD DRIVE UNIONTOWN' -> ('1708 ROBINWOOD DRIVE', 'UNIONTOWN')."""
    blob = re.sub(r"\s+", " ", blob.strip(" ,"))
    if "," in blob:
        a, b = blob.rsplit(",", 1)
        return a.strip(), b.strip()
    up = blob.upper()
    for c in sorted(SUMMIT_CITIES, key=len, reverse=True):
        if up.endswith(" " + c):
            return blob[: -len(c)].strip(), blob[-len(c):]
    m = re.match(rf"^(.*\b{SUFFIX}\b\.?{DIRS}{UNIT})\s+(.+)$", blob, re.I)
    return (m.group(1), m.group(2)) if m else (blob, "")


def _title(s: str) -> str:
    return re.sub(r"\b(Po|Pob)\b", "PO", s.title())


def parse_report(text: str) -> dict:
    """Preliminary Judicial Report -> property address, parcel, record owner, mortgage."""
    out = {}
    m = re.search(r"PROPERTY ADDRESS:\s*(.+?)\s*(?:\n|PARCEL)", text, re.I)
    if m:
        raw = m.group(1).strip()
        z = re.search(r"(?:,|\s)\s*(?:OH|OHIO)?\s*(\d{5})(?:-\d{4})?\s*$", raw, re.I)
        zip5 = z.group(1) if z else ""
        body = re.sub(r"[,\s]*(?:OH|OHIO)?\s*\d{5}(?:-\d{4})?\s*$", "", raw, flags=re.I) if z else raw
        body = re.sub(r"[,\s]*\b(?:OH|OHIO)\b\s*$", "", body, flags=re.I)
        street, city = split_street_city(body)
        out.update(street=_title(street), city=_title(city), zip=zip5)
    m = re.search(r"PARCEL\s*(?:NO|NUMBER|#)\s*\.?\s*:?\s*([\dA-Z\-]+)", text, re.I)
    if m:
        out["parcel"] = m.group(1)
    m = re.search(r"vested in\s+(.+?)\s+by instrument", text, re.I | re.S)
    if m:
        out["record_owner"] = _title(re.sub(r"\s+", " ", m.group(1)))
    m = re.search(r"SUBJECT TO MORTGAGE FROM.*?IN THE AMOUNT OF\s*\$([\d,\.]+).*?DATED:\s*([\d\-/]+)", text, re.I | re.S)
    if m:
        out["mortgage_amount"] = _money(m.group(1).rstrip("."))
        out["mortgage_date"] = m.group(2)
    m = re.search(r"in an amount not to exceed\s*\$([\d,\.]+)", text, re.I)
    if m:
        out["guarantee_amount"] = _money(m.group(1).rstrip("."))
    return out


def parse_complaint(text: str) -> dict:
    out = {}
    flat = re.sub(r"\s+", " ", text)
    m = (re.search(r"there is due the sum of\s*\$\s*([\d,]+(?:\.\d{2})?)", flat, re.I)
         or re.search(r"(?:principal balance|unpaid principal balance|balance) of\s*\$\s*([\d,]+(?:\.\d{2})?)", flat, re.I))
    if m:
        out["amount_owed"] = _money(m.group(1))
    m = re.search(r"interest at (?:a|the)?\s*(?:current\s*)?rate of\s*([\d\.]+)\s*%", flat, re.I) \
        or re.search(r"([\d\.]+)\s*%\s*(?:per annum|per year)", flat, re.I)
    if m:
        out["interest_rate"] = float(m.group(1).rstrip("."))
    m = re.search(r"unpaid principal (?:balance )?was modified to\s*\$\s*([\d,]+(?:\.\d{2})?)", flat, re.I)
    if m:
        out["modified_principal"] = _money(m.group(1))
    if re.search(r"HOUSING AND URBAN DEVELOPMENT|\bFHA\b", flat, re.I):
        out["loan_type"] = "FHA"
    elif re.search(r"VETERANS AFFAIRS|\bVA\b loan", flat, re.I):
        out["loan_type"] = "VA"
    elif re.search(r"RURAL HOUSING|\bUSDA\b|RURAL DEVELOPMENT", flat, re.I):
        out["loan_type"] = "USDA"
    elif re.search(r"\bnote\b", flat, re.I):
        out["loan_type"] = "Conventional"
    if re.search(r"loan modification", flat, re.I):
        out["modified"] = True
    return out


def parse_service(text: str) -> dict:
    """First numbered defendant with a street address -> where the borrower is served (mailing address)."""
    for m in re.finditer(r"^\s*\d+\.\s+(.+?),\s*(\d+[^,\n]*?),\s*([A-Za-z .]+?),\s*([A-Z]{2})\s+(\d{5})", text, re.M):
        who = m.group(1)
        if re.search(r"unknown spouse|united states|treasurer|city of|state of|county|bank|llc|inc\b", who, re.I):
            continue
        return {"service_name": _title(who), "street": _title(m.group(2)), "city": _title(m.group(3)),
                "state": m.group(4), "zip": m.group(5)}
    return {}


def extract(docs: Dict[str, str]) -> dict:
    """Combine everything; keys are ready for LeadRecord."""
    rep, comp, svc = parse_report(docs.get("report", "")), parse_complaint(docs.get("complaint", "")), parse_service(docs.get("service", ""))
    out = {k: comp[k] for k in ("amount_owed", "interest_rate", "loan_type") if k in comp}
    out["modified"] = comp.get("modified", False)
    for k in ("modified_principal",):
        if k in comp:
            out[k] = comp[k]
    for k in ("mortgage_amount", "mortgage_date", "record_owner"):
        if k in rep:
            out[k] = rep[k]
    street, city, zip5 = rep.get("street"), rep.get("city"), rep.get("zip")
    if not street and svc:                         # report missing: borrower's service address is the best guess
        street, city, zip5 = svc["street"], svc["city"], svc["zip"]
        out["address_source"] = "service address"
    elif street:
        out["address_source"] = "title report"
    if street:
        out.update(property_address=street, property_city=city or "", property_zip=zip5 or "")
    if rep.get("parcel"):
        out["parcel_number"] = rep["parcel"]
    if svc:
        out["served_at"] = f"{svc['street']}, {svc['city']}, {svc['state']} {svc['zip']}"
    return out
