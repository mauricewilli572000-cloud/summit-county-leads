import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import lead_builder
import run_pipeline as rp
import summit_feed as feed
from enrichment import ParcelIndex

D = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
HTML = f"""<table><tr><th>Action</th><th>Case Name</th><th>Date Added</th><th>Case Category</th><th>Status</th></tr>
<tr><td><button>Get Docs</button></td><td>WELLS FARGO BANK, N.A. -vs- JOHN Q PUBLIC, ET AL</td><td>{D} 9:14 AM</td><td>Foreclosure</td><td>Pending Review</td></tr>
<tr><td><button>Get Docs</button></td><td>ACME LLC -vs- BOB BUILDER</td><td>{D} 8:01 AM</td><td>Breach of Contract</td><td>Filing Complete</td></tr>
<tr><td><button>Get Docs</button></td><td>MIDFIRST BANK -vs- MARY ROE, UNKNOWN SPOUSE OF MARY ROE</td><td>{D} 7:30 AM</td><td>Foreclosure</td><td>Filing Complete</td></tr></table>"""


def test_rows_to_leads():
    leads = [lead_builder.build_lead(r) for r in feed.parse_rows(HTML)]
    assert [bool(l) for l in leads] == [True, False, True]
    a = leads[0]
    assert a.county == "Summit" and a.lead_type == "Foreclosure" and a.case_number.startswith("SUM-")
    assert a.owner_name == "John Q Public" and a.plaintiff == "Wells Fargo Bank, N.A."
    assert "Pending clerk review" in a.flags and a.lead_score >= 60
    assert len(a.filing_date) == 10 and a.filing_date[2] == "/"        # MM/DD/YYYY like Cuyahoga
    assert leads[2].owner_name == "Mary Roe"


def test_pipeline_writes_dashboard_files(tmp_path, monkeypatch):
    dash, data = tmp_path / "dashboard", tmp_path / "data"
    monkeypatch.setattr(rp, "SHARED_DASHBOARD_DIR", str(dash))
    monkeypatch.setattr(rp, "DATA_DIR", str(data))
    monkeypatch.setattr(rp, "LEADS_JSON_PATH", str(data / "leads.json"))
    monkeypatch.setattr(rp, "GHL_CSV_PATH", str(data / "ghl.csv"))
    monkeypatch.setattr(rp, "DASHBOARD_DATA_PATH", str(dash / "summit.json"))
    monkeypatch.setattr(rp, "DASHBOARD_CSV_PATH", str(dash / "summit_ghl_export.csv"))
    new = [l for l in (lead_builder.build_lead(r) for r in feed.parse_rows(HTML)) if l]
    leads = rp.merge(new, None)
    rp._write_all(leads)
    out = json.loads((dash / "summit.json").read_text())
    assert out["county"] == "Summit" and out["total_leads"] == 2 and out["leads_with_property_address"] == 0
    assert out["leads"][0]["flags"] is not None and "added_date" in out["leads"][0]
    assert (dash / "summit_ghl_export.csv").read_text(encoding="utf-8-sig").startswith("First Name,Last Name")
    # second run: same feed -> no duplicates, added_date kept
    again = rp.merge(new, None)
    assert len(again) == 2


def test_address_added_later_from_parcel_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "LEADS_JSON_PATH", str(tmp_path / "leads.json"))
    new = [l for l in (lead_builder.build_lead(r) for r in feed.parse_rows(HTML)) if l]
    (tmp_path / "leads.json").write_text(json.dumps([{**l.to_dict(), "key": l.key()} for l in new]))
    p = tmp_path / "p.csv"
    p.write_text("PARCEL,OWNER,SITUS,MAILAD1,MAILAD2\n5,PUBLIC JOHN Q,10 MAPLE AVE,10 MAPLE AVE,AKRON OH 44301\n")
    leads = rp.merge([], ParcelIndex(str(p)))
    hit = [l for l in leads if l.property_address]
    assert len(hit) == 1 and hit[0].property_address == "10 MAPLE AVE" and "Address from name match" in hit[0].flags


def test_real_world_name_cleanup():
    rows = feed.parse_rows(f"""<table><tr><th>Action</th><th>Case Name</th><th>Date Added</th><th>Case Category</th><th>Status</th></tr>
<tr><td>x</td><td>ROCKET MORTGAGE, LLC S/B/M NATIONSTAR MO -VS- MATTHEW KRISTOFF A/K/A MATTHEW PAUL KRISTOFF</td><td>{D} 9:14 AM</td><td>Foreclosure</td><td>Pending Review</td></tr>
<tr><td>x</td><td>VELVET A. GRABLE, TRUSTEE OF THE VELVET A. GRABLE FAMILY TRUST -VS- RYAN STERRITT</td><td>{D} 9:15 AM</td><td>Foreclosure</td><td>Pending Review</td></tr>
<tr><td>x</td><td>PNC BANK NATIONAL ASSOCIATION -VS- DELORES E HOLLAND</td><td>{D} 9:16 AM</td><td>Foreclosure</td><td>Filing Complete</td></tr></table>""")
    a, b, c = [lead_builder.build_lead(r) for r in rows]
    assert a.owner_name == "Matthew Kristoff" and a.plaintiff == "Rocket Mortgage, LLC s/b/m Nationstar Mo"
    assert b.owner_name == "Ryan Sterritt" and b.plaintiff.startswith("Velvet A. Grable, Trustee")
    assert c.plaintiff == "PNC Bank National Association"


def test_docs_zip_to_lead():
    """Real-shaped documents: title report gives the address, complaint gives amount/rate/loan type."""
    import io, zipfile
    import doc_extract
    report = ("Effective Date: 09/30/2026\nFOR INFORMATIONAL PURPOSES ONLY:\nPROPERTY ADDRESS: 1708 ROBINWOOD DRIVE UNIONTOWN, OHIO 44685\n"
              "PARCEL NO .: 2810195\nvested in MATTHEW PAUL KRISTOFF by instrument recorded")
    complaint = ("SECRETARY OF HOUSING AND URBAN DEVELOPMENT there is due the sum of $114,083.89 plus interest at a current rate "
                 "of 3% per annum from May 1, 2026. Loan Modification Agreement effective January 1, 2022")
    docs = {"report": report, "complaint": complaint, "service": ""}
    info = doc_extract.extract(docs)
    assert info["property_address"] == "1708 Robinwood Drive" and info["property_city"] == "Uniontown"
    assert info["property_zip"] == "44685" and info["parcel_number"] == "2810195"
    assert info["amount_owed"] == 114083.89 and info["interest_rate"] == 3.0 and info["loan_type"] == "FHA"
    lead = lead_builder.build_lead(feed.parse_rows(HTML)[0])
    before = lead.lead_score
    lead_builder.apply_docs(lead, info)
    from lead_scoring import score_lead
    assert lead.property_address == "1708 Robinwood Drive" and "Address from title report" in lead.flags
    assert score_lead(lead) > before


def test_read_zip_ignores_bad_pdf():
    import io, zipfile
    import doc_extract
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1_2_FORECLOSURE_COMPLAINT.pdf", b"not a pdf")
    assert doc_extract.read_zip(buf.getvalue()) == {}


TAX_COMPLAINT = """IN THE BOARD OF REVISION
KRISTEN M. SCALISE CASE NO:
As Fiscal Officer of Summit County, Ohio
NORTH TEXAS ENTERPRISES INC
PO BOX 2861
AMARILLO, TX 79105 PERMANENT PARCEL NUMBER(S):
& 67-53387
2701 MACKENZIE TRL
AMARILLO, TX 79124 YEAR(S) CERTIFIED:
2007
CITY OF AKRON
172 S BROADWAY ST, SUITE 200 PROPERTY ADDRESS(ES):
AKRON, OH 44308 908 JOHNSTON ST, AKRON, OH 44306
Defendant(s)
3. there appears THIRTY-FOUR THOUSAND FOUR HUNDRED SIXTY-EIGHT AND 33/100 DOLLARS ($34,468.33) appearing to be due"""
TAX_SERVICE = """INSTRUCTIONS FOR SERVICE
You are hereby requested to make CERTIFIED MAIL service of the Complaint to:

NORTH TEXAS ENTERPRISES INC
PO BOX 2861
AMARILLO, TX 79105
&
2701 MACKENZIE TRL
AMARILLO, TX 79124
"""
SCANNED_REPORT = "Schedule A\nDESCRIPTION OF THE LAND\nProperty Address: 908 Johnston St\nAkron, Ohio 44306\nSituated in"


def test_tax_foreclosure_docs():
    import doc_extract
    info = doc_extract.extract({"complaint": TAX_COMPLAINT, "service": TAX_SERVICE,
                                "report": "is at the date hereof vested in North Texas Enterprises, inc. by instrument recorded"})
    assert info["tax"] and info["amount_owed"] == 34468.33 and info["loan_type"] == "Tax lien"
    assert (info["property_address"], info["property_city"], info["property_zip"]) == ("908 Johnston St", "Akron", "44306")
    assert info["parcel_number"] == "67-53387" and info["mailing_address"] == "PO Box 2861, Amarillo, TX 79105"
    row = {"case_name": "KRISTEN M SCALISE -VS- NORTH TEXAS ENTERPRISES INC", "date_added_raw": f"{D} 5:26 PM",
           "date_added": "2026-10-30", "category": "Land Bank Tax Foreclosure", "review_status": "Pending Review"}
    lead = lead_builder.build_lead(row)
    lead_builder.apply_docs(lead, info)
    assert lead.lead_type == "Tax Foreclosure" and lead.plaintiff == "Summit County Fiscal Officer"
    assert lead.mailing_address.startswith("PO Box 2861") and "No individual owner named" not in lead.flags


def test_two_line_report_address_and_doc_kinds():
    import doc_extract
    rep = doc_extract.parse_report(SCANNED_REPORT)
    assert (rep["street"], rep["city"], rep["zip"]) == ("908 Johnston St", "Akron", "44306")
    assert doc_extract.doc_kind("1_1006_FORECLOSURE_COMPLAINT.pdf") == "complaint"
    assert doc_extract.doc_kind("1_2964_NOTICE_OF_SUMMONS_AND_COMPLAINT.pdf") == "other"
    assert doc_extract.doc_kind("1_738_TITLE_INVOICE.pdf") == "other"
    assert doc_extract.doc_kind("1_663_PRELIMINARY_JUDICIAL_REPORT.pdf") == "report"


def test_mortgage_complaint_naming_fiscal_officer_is_not_tax():
    import doc_extract
    c = ("COURT OF COMMON PLEAS SUMMIT COUNTY, OHIO Plaintiff -vs- RYAN STERRITT SUMMIT COUNTY FISCAL OFFICER, defendant "
         "there is due the sum of $99,900.00 plus interest at the rate of 4.5% per annum")
    out = doc_extract.parse_complaint(c)
    assert not out.get("tax") and out["amount_owed"] == 99900.0 and out["interest_rate"] == 4.5
    r = doc_extract.parse_report("Property Address: 1071 Sawyer Ave Parcel #: 67-14253")
    assert r["street"] == "1071 Sawyer Ave" and not r.get("city") and not r.get("zip")
