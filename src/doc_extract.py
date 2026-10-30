"""Read the documents behind a Summit "Get Docs" download (a .zip of PDFs) and pull out lead facts.

The zip holds text-based PDFs, named like  <id>_<n>_FORECLOSURE_COMPLAINT.pdf :
  FORECLOSURE_COMPLAINT            amount due, interest rate, modified principal, HUD/VA/USDA parties
  PRELIMINARY_JUDICIAL_REPORT      PROPERTY ADDRESS, PARCEL NO, record owner, mortgage amount/date
  INSTRUCTIONS_TO_CLERK_FOR_SERVICE  defendants with the address they will be served at
Everything is best-effort: any field we cannot find stays empty rather than guessed.
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
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


def ocr_pdf(data: bytes, max_pages: int = 5) -> str:
    """Scanned PDFs (many title reports) have no text layer: rasterize the first pages and OCR them.
    Needs the poppler `pdftoppm` and `tesseract` programs; returns "" if they are not installed."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return ""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as fh:
            fh.write(data)
        subprocess.run(["pdftoppm", "-r", "150", "-f", "1", "-l", str(max_pages), "-png", src, os.path.join(tmp, "p")],
                       check=False, capture_output=True, timeout=120)
        for png in sorted(f for f in os.listdir(tmp) if f.endswith(".png")):
            r = subprocess.run(["tesseract", os.path.join(tmp, png), "-"], capture_output=True, text=True, timeout=120)
            out.append(r.stdout)
    return "\n".join(out)


def pdf_text(data: bytes, max_pages: Optional[int] = None) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            if max_pages and i >= max_pages:
                break
            out.append(page.extract_text(layout=False) or "")
    text = "\n".join(out)
    if len(text.strip()) < 150:                      # no text layer -> scanned image
        text = ocr_pdf(data, max_pages=min(max_pages or 5, 5)) or text
    return text


def doc_kind(filename: str) -> str:
    up = os.path.basename(filename).upper()
    if "NOTICE" in up or "INVOICE" in up or "AFFIDAVIT" in up or "READINESS" in up:
        return "other"
    if "COMPLAINT" in up:
        return "complaint"
    if "JUDICIAL_REPORT" in up:
        return "report"
    if "INSTRUCTIONS" in up or "REQUEST_FOR_SERVICE" in up:
        return "service"
    return "other"


def read_zip(data: bytes) -> Dict[str, str]:
    """{'complaint': text, 'report': text, 'service': text} - only the documents we read (others are skipped)."""
    docs: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            kind = doc_kind(n) if n.lower().endswith(".pdf") else "other"
            if kind == "other":
                continue
            try:
                txt = pdf_text(z.read(n), max_pages=10 if kind == "complaint" else 6)
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
    m = (re.search(r"PROPERTY ADDRESS:\s*(\d[^\n]*?\n?[^\n]{0,60}?\b\d{5}\b)", text, re.I)
         or re.search(r"PROPERTY ADDRESS:\s*(.+?)\s*(?:\n|PARCEL)", text, re.I))
    if m:
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
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
    if re.search(r"BOARD OF REVISION|FISCAL OFFICER|Delinquent Tax", flat, re.I):      # land-bank / tax foreclosure
        out["tax"] = True
        m = re.search(r"\(\$\s*([\d,]+\.\d{2})\)", flat)
        if m:
            out["amount_owed"] = _money(m.group(1))
        m = re.search(r"PERMANENT PARCEL NUMBER\(S\):\s*(?:&\s*)?(\d[\d\-]{4,})", flat, re.I)
        if m:
            out["parcel"] = m.group(1)
        i = re.search(r"PROPERTY ADDRESS(?:\(ES\))?:", flat, re.I)
        if i:     # PDF columns interleave, so take the first "<no.> <street>, <city>, OH <zip>" after the label
            m = re.search(r"(?<!OH )(?<!\d)\b(\d{1,6})\s+([A-Z][A-Z0-9 .'\-]+?),\s*([A-Z][A-Z .'\-]+?),\s*OH\s+(\d{5})",
                          flat[i.end(): i.end() + 300], re.I)
            if m:
                out.update(street=_title(f"{m.group(1)} {m.group(2)}"), city=_title(m.group(3)), zip=m.group(4))
        out["loan_type"] = "Tax lien"
        return out
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


def parse_owner_mailing(text: str) -> str:
    """Tax-foreclosure service list: the first addressee's mailing address (often a PO Box / out of state)."""
    m = re.search(r"\bto:\s*\n+\s*[^\n]+\n\s*([^\n]+)\n\s*([A-Za-z][A-Za-z .]+?),\s*([A-Z]{2})\s+(\d{5})", text)
    if not m or re.match(r"\s*(&|AND\b)", m.group(1)):
        return ""
    return f"{_title(m.group(1).strip())}, {_title(m.group(2).strip())}, {m.group(3)} {m.group(4)}"


def extract(docs: Dict[str, str]) -> dict:
    """Combine everything; keys are ready for LeadRecord."""
    rep, comp, svc = parse_report(docs.get("report", "")), parse_complaint(docs.get("complaint", "")), parse_service(docs.get("service", ""))
    out = {k: comp[k] for k in ("amount_owed", "interest_rate", "loan_type") if k in comp}
    out["modified"] = comp.get("modified", False)
    out["tax"] = bool(comp.get("tax"))
    if "modified_principal" in comp:
        out["modified_principal"] = comp["modified_principal"]
    for k in ("mortgage_amount", "mortgage_date", "record_owner"):
        if k in rep:
            out[k] = rep[k]
    street, city, zip5 = comp.get("street"), comp.get("city"), comp.get("zip")      # tax complaints state it outright
    if street:
        out["address_source"] = "complaint"
    else:
        street, city, zip5 = rep.get("street"), rep.get("city"), rep.get("zip")
        if street:
            out["address_source"] = "title report"
        elif svc and not comp.get("tax"):              # report missing: borrower's service address is the best guess
            street, city, zip5 = svc["street"], svc["city"], svc["zip"]
            out["address_source"] = "service address"
    if street:
        out.update(property_address=street, property_city=city or "", property_zip=zip5 or "")
    parcel = comp.get("parcel") or rep.get("parcel")
    if parcel:
        out["parcel_number"] = parcel
    if comp.get("tax"):
        mail = parse_owner_mailing(docs.get("service", ""))
        if mail:
            out["mailing_address"] = mail
    elif svc:
        out["served_at"] = f"{svc['street']}, {svc['city']}, {svc['state']} {svc['zip']}"
    return out
