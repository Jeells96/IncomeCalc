#!/usr/bin/env python3
"""Tax-data autopilot for the Take-Home paycheck app.

Fetches the three official sources, extracts the new year's numbers,
runs them through guardrails against the current tax-data.json, and
writes the new file. No LLMs, no fuzzy matching: strict regexes that
fail LOUDLY (exit 2 -> GitHub issue) the moment a source changes shape
or a number moves in a way inflation can't explain. The app's paystub
learning layer is the final backstop either way.

Sources:
  - SSA contribution & benefit base (HTML table)  -> ssWageBase, pfmlWageCap
  - IRS inflation-adjustments news release (HTML) -> federal brackets, std deductions, CTC
  - Oregon Pub 150-206-436 formulas (PDF)         -> OR brackets, std deductions, credit, fed-tax cap

Statutory carry-forwards (change only when Congress/Salem changes law,
which trips the rate guardrail and escalates): ssRate, medicareRate,
addMedicareRate, addMedicareThreshold, transitRate, pfmlRate,
creditPhaseout, odc, otExemptAnnualCap.

Exit codes: 0 = new file written (or would be, in dry-run)
            2 = escalate: parse/guardrail failure, open an issue
            3 = sources not published yet, check again later
            4 = already up to date
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (tax-data-autopilot; github.com personal payroll app)"}

FED_RATES = [0.10, 0.12, 0.22, 0.24, 0.32, 0.35, 0.37]
OR_RATES = [0.0475, 0.0675, 0.0875, 0.099]
MAX_YOY_GROWTH = 1.12  # inflation indexing runs 1-7%; anything past 12% is not indexing


def log(msg):
    print(msg, flush=True)


def fail(msg):
    print(f"ESCALATE: {msg}", file=sys.stderr, flush=True)
    sys.exit(2)


def money(s):
    return int(s.replace(",", ""))


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", errors="replace")


def exists(url):
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def strip_tags(html):
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&#036;", "$").replace("&dollar;", "$").replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", text)


# ---------------------------------------------------------------- SSA
def parse_ssa(text, target):
    """cbb.html lists a Year/Amount table of the contribution & benefit base."""
    m = re.search(rf"\b{target}\b[^\d]{{0,12}}\$?([\d,]{{6,}})", text)
    if not m:
        return None  # row for target year not posted yet
    base = money(m.group(1))
    if not (100_000 <= base <= 500_000):
        fail(f"SSA wage base for {target} parsed as {base} — implausible")
    return base


# ---------------------------------------------------------------- IRS
def irs_urls(target):
    stems = [
        f"https://www.irs.gov/newsroom/irs-provides-tax-inflation-adjustments-for-tax-year-{target}",
        f"https://www.irs.gov/newsroom/irs-releases-tax-inflation-adjustments-for-tax-year-{target}",
    ]
    return stems


def dollar_for_phrase(text, phrase_rx, require="standard deduction"):
    """Find the amount tied to a filing-status phrase inside a sentence that
    also mentions `require`. Tries 'phrase ... $X' first (current IRS house
    style: 'for married couples filing jointly, the standard deduction rises
    to $32,200'), then '$X ... phrase'. The [^$.] windows stop at other
    dollar amounts so a neighboring figure can't be picked up."""
    for s in re.split(r"(?<=[.\n])\s+", text):
        low = s.lower()
        if require in low and re.search(phrase_rx, s, re.I):
            m = re.search(phrase_rx + r"[^$.]{0,140}?\$([\d,]+)", s, re.I)
            if m:
                return money(m.group(1))
            m = re.search(r"\$([\d,]+)[^$.]{0,80}?" + phrase_rx, s, re.I)
            if m:
                return money(m.group(1))
    return None


def parse_irs(text, target):
    """The annual inflation-adjustments release lists single thresholds with
    MFJ in parentheses per rate. 'over $X' amounts are bracket FLOORS; the
    schema wants ceilings, so rate i's ceiling = rate i+1's floor."""
    out = {}

    out["std_single"] = dollar_for_phrase(text, r"single\s+(?:taxpayers|filers|individuals)")
    out["std_married"] = dollar_for_phrase(text, r"married\s+couples\s+filing\s+jointly")
    if not (out["std_single"] and out["std_married"]):
        fail(f"IRS {target}: standard deduction sentence not found")
    if not (out["std_married"] > out["std_single"]):
        fail(f"IRS {target}: MFJ std {out['std_married']} not > single std {out['std_single']} — wrong amounts matched")

    floors = {}  # rate -> (single_floor, married_floor)
    for pm in re.finditer(
        r"(\d{2})\s*(?:%|percent)[^.$]*?(?:over|above|greater than)\s+\$([\d,]+)\s*\(\$([\d,]+)[^)]*married[^)]*\)",
        text, re.I,
    ):
        floors[int(pm.group(1)) / 100] = (money(pm.group(2)), money(pm.group(3)))
    ten = re.search(
        r"10\s*(?:%|percent)[^.$]*?\$([\d,]+)\s+or\s+less\s*\(\$([\d,]+)[^)]*\)", text, re.I
    )
    expect = set(FED_RATES[1:])  # 12..37 appear as "over"; 10% appears as "or less"
    if set(floors) != expect or not ten:
        fail(
            f"IRS {target}: bracket lines incomplete — found rates {sorted(floors)}, "
            f"10%-line={'yes' if ten else 'no'}. Release format likely changed."
        )
    ten_single, ten_married = money(ten.group(1)), money(ten.group(2))
    if (ten_single, ten_married) != floors[0.12]:
        fail(f"IRS {target}: 10% ceiling {ten_single}/{ten_married} ≠ 12% floor {floors[0.12]}")

    def build(idx):
        rows = []
        for i, rate in enumerate(FED_RATES):
            ceil = floors[FED_RATES[i + 1]][idx] if i + 1 < len(FED_RATES) else None
            rows.append({"rate": rate, "upTo": ceil})
        return rows

    out["brackets_single"], out["brackets_married"] = build(0), build(1)

    ctc = re.search(r"child\s+tax\s+credit[^.$]{0,160}?\$([\d,]+)", text, re.I) \
        or re.search(r"\$([\d,]+)[^.$]{0,160}?child\s+tax\s+credit", text, re.I)
    out["ctc"] = money(ctc.group(1)) if ctc else None  # carry-forward if absent
    return out


# ---------------------------------------------------------------- Oregon
def oregon_url(target):
    return (
        "https://www.oregon.gov/dor/forms/FormsPubs/"
        f"withholding-tax-formulas_206-436_{target}.pdf"
    )


DASH = r"[-\u2013\u2014]"
X = r"[x\u00d7]"


def parse_oregon(text, target):
    out = {}
    m = re.search(r"standard deduction \(\$([\d,]+)\[S\]\)", text)
    m2 = re.search(r"standard deduction \(\$([\d,]+)\[M\]\)", text)
    if not (m and m2):
        fail(f"Oregon {target}: [S]/[M] standard deduction markers not found")
    out["stdLow"], out["stdHigh"] = money(m.group(1)), money(m2.group(1))

    m = re.search(r"not to exceed \$([\d,]+)\)", text)
    if not m:
        fail(f"Oregon {target}: federal-tax 'not to exceed' cap not found")
    out["fedSubtractionCap"] = money(m.group(1))

    m = re.search(rf"\(\$?([\d,]+)\s*{X}\s*allowances\)", text)
    if not m:
        fail(f"Oregon {target}: exemption credit '(N x allowances)' not found")
    out["exemptionCredit"] = money(m.group(1))

    # Married sections follow each "Single with 3 or more allowances, or married"
    # header: first occurrence = under-$50k table, second = $50k-and-over table.
    heads = [h.start() for h in re.finditer(r"Single with 3 or more allowances,\s*or married", text)]
    if len(heads) < 2:
        fail(f"Oregon {target}: expected two married formula sections, found {len(heads)}")
    lo_block = text[heads[0]: heads[0] + 1500]
    hi_block = text[heads[1]: heads[1] + 1800]

    b1 = re.search(rf"0\s*{DASH}\s*([\d,]+)\s+WH\s*=\s*([\d,]+)\s*\+\s*\[BASE\s*{X}\s*0\.0475\]", lo_block)
    b2 = re.search(
        rf"([\d,]+)\s*{DASH}\s*([\d,]+)\s+WH\s*=\s*([\d,]+)\s*\+\s*\[\(BASE\s*{DASH}\s*[\d,]+\)\s*{X}\s*0\.0675\]",
        lo_block,
    )
    top = re.search(
        rf"([\d,]+)\s+WH\s*=\s*[\d,]+\s*\+\s*\[\(BASE\s*{DASH}\s*([\d,]+)\)\s*{X}\s*0\.099", hi_block
    )
    if not (b1 and b2 and top) or top.group(1) != top.group(2):
        fail(f"Oregon {target}: married bracket formulas not matched (format change?)")
    bp1, bp2, bp_top = money(b1.group(1)), money(b2.group(2)), money(top.group(1))

    # Independent cross-check: the 6.75%-tier constant must equal
    # credit + bp1 * 4.75% (that's how the DOR builds the table).
    implied = out["exemptionCredit"] + bp1 * 0.0475
    stated = money(b2.group(3))
    if abs(implied - stated) > 2:
        fail(
            f"Oregon {target}: internal consistency check failed — "
            f"tier-2 constant {stated} vs implied {implied:.2f}. Wrong table matched?"
        )

    out["brackets"] = [
        {"rate": 0.0475, "upTo": bp1},
        {"rate": 0.0675, "upTo": bp2},
        {"rate": 0.0875, "upTo": bp_top},
        {"rate": 0.099, "upTo": None},
    ]
    return out


# ---------------------------------------------------------------- assembly
def grew_sanely(name, old, new):
    if new < old or new > old * MAX_YOY_GROWTH:
        fail(
            f"guardrail: {name} moved {old} -> {new} "
            f"({(new / old - 1) * 100:+.1f}%) — outside 0..+{(MAX_YOY_GROWTH - 1) * 100:.0f}% "
            f"inflation range. If a law changed, update by hand once."
        )


def bracket_guardrail(name, old_b, new_b):
    if [r["rate"] for r in new_b] != [r["rate"] for r in old_b]:
        fail(f"guardrail: {name} RATES changed — that's legislation, not indexing")
    for o, n in zip(old_b, new_b):
        if (o["upTo"] is None) != (n["upTo"] is None):
            fail(f"guardrail: {name} bracket count/shape changed")
        if o["upTo"] is not None:
            grew_sanely(f"{name} upTo@{o['rate']}", o["upTo"], n["upTo"])


def validate_schema(d):
    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def br(b):
        return (
            isinstance(b, list) and b
            and all(isinstance(r, dict) and num(r.get("rate")) and 0 <= r["rate"] < 1
                    and (r.get("upTo") is None or (num(r["upTo"]) and r["upTo"] > 0)) for r in b)
            and any(r.get("upTo") is None for r in b)
        )

    ok = (
        num(d.get("year")) and 2026 <= d["year"] <= 2100
        and num(d.get("revision")) and d["revision"] >= 1
        and br(d["fed"]["brackets"]) and br(d["fedCheckbox"]["brackets"]) and br(d["oregon"]["brackets"])
        and all(num(d["fed"][k]) for k in ("standardDeduction", "ctc", "odc", "otExemptAnnualCap"))
        and num(d["fedCheckbox"]["standardDeduction"])
        and all(num(d["oregon"][k]) for k in (
            "stdLow", "stdHigh", "exemptionCredit", "creditPhaseout",
            "fedSubtractionCap", "transitRate", "pfmlRate", "pfmlWageCap"))
        and all(num(d["fica"][k]) for k in (
            "ssRate", "ssWageBase", "medicareRate", "addMedicareRate", "addMedicareThreshold"))
        and d["oregon"]["transitRate"] < 0.02 and d["oregon"]["pfmlRate"] < 0.02
        and d["fica"]["ssRate"] < 0.2 and d["fica"]["medicareRate"] < 0.05
        and all(b["rate"] < 0.5 for b in d["oregon"]["brackets"])
    )
    return ok


def assemble(cur, target, ssa_base, irs, orx):
    new = json.loads(json.dumps(cur))
    new["year"], new["revision"] = target, 1
    new["_notes"] = cur.get("_notes", "")

    new["fed"]["standardDeduction"] = irs["std_married"]
    new["fed"]["brackets"] = irs["brackets_married"]
    carried = []
    if irs["ctc"] is not None:
        new["fed"]["ctc"] = irs["ctc"]
    else:
        carried.append("ctc")
    carried += ["odc", "otExemptAnnualCap", "creditPhaseout", "transitRate", "pfmlRate",
                "ssRate", "medicareRate", "addMedicareRate", "addMedicareThreshold"]
    new["fedCheckbox"]["standardDeduction"] = irs["std_single"]
    new["fedCheckbox"]["brackets"] = irs["brackets_single"]
    for k in ("stdLow", "stdHigh", "exemptionCredit", "fedSubtractionCap", "brackets"):
        new["oregon"][k] = orx[k]
    new["fica"]["ssWageBase"] = ssa_base
    new["oregon"]["pfmlWageCap"] = ssa_base  # statutory: Paid Leave cap = SS wage base

    # guardrails vs current file
    bracket_guardrail("fed.brackets", cur["fed"]["brackets"], new["fed"]["brackets"])
    bracket_guardrail("fedCheckbox.brackets", cur["fedCheckbox"]["brackets"], new["fedCheckbox"]["brackets"])
    bracket_guardrail("oregon.brackets", cur["oregon"]["brackets"], new["oregon"]["brackets"])
    for name, o, n in (
        ("fed.standardDeduction", cur["fed"]["standardDeduction"], new["fed"]["standardDeduction"]),
        ("fedCheckbox.standardDeduction", cur["fedCheckbox"]["standardDeduction"], new["fedCheckbox"]["standardDeduction"]),
        ("oregon.stdLow", cur["oregon"]["stdLow"], new["oregon"]["stdLow"]),
        ("oregon.stdHigh", cur["oregon"]["stdHigh"], new["oregon"]["stdHigh"]),
        ("oregon.exemptionCredit", cur["oregon"]["exemptionCredit"], new["oregon"]["exemptionCredit"]),
        ("oregon.fedSubtractionCap", cur["oregon"]["fedSubtractionCap"], new["oregon"]["fedSubtractionCap"]),
        ("fica.ssWageBase", cur["fica"]["ssWageBase"], new["fica"]["ssWageBase"]),
    ):
        grew_sanely(name, o, n)
    if irs["ctc"] is not None:
        grew_sanely("fed.ctc", cur["fed"]["ctc"], new["fed"]["ctc"])
    if not validate_schema(new):
        fail("assembled file failed schema validation")
    return new, carried


def summarize(cur, new):
    lines = []

    def d(path, o, n):
        if o != n:
            lines.append(f"  {path}: {o} -> {n}")

    d("fed.standardDeduction", cur["fed"]["standardDeduction"], new["fed"]["standardDeduction"])
    d("fedCheckbox.standardDeduction", cur["fedCheckbox"]["standardDeduction"], new["fedCheckbox"]["standardDeduction"])
    for i, (o, n) in enumerate(zip(cur["fed"]["brackets"], new["fed"]["brackets"])):
        d(f"fed.brackets[{i}].upTo", o["upTo"], n["upTo"])
    for i, (o, n) in enumerate(zip(cur["oregon"]["brackets"], new["oregon"]["brackets"])):
        d(f"oregon.brackets[{i}].upTo", o["upTo"], n["upTo"])
    for k in ("stdLow", "stdHigh", "exemptionCredit", "fedSubtractionCap"):
        d(f"oregon.{k}", cur["oregon"][k], new["oregon"][k])
    d("fica.ssWageBase", cur["fica"]["ssWageBase"], new["fica"]["ssWageBase"])
    d("fed.ctc", cur["fed"]["ctc"], new["fed"]["ctc"])
    return "\n".join(lines) if lines else "  (no numeric changes?)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int)
    ap.add_argument("--file", default="tax-data.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures-dir", help="read {ssa,irs,oregon}.txt locally instead of fetching")
    ap.add_argument("--check-file", action="store_true", help="schema-validate --file and exit")
    args = ap.parse_args()

    cur = json.loads(Path(args.file).read_text())

    if args.check_file:
        if validate_schema(cur):
            log(f"{args.file} OK — year {cur['year']}, revision {cur['revision']}")
            sys.exit(0)
        fail(f"{args.file} failed schema validation")

    target = args.target
    if cur["year"] >= target:
        log(f"tax-data.json is already {cur['year']} (target {target}) — up to date.")
        sys.exit(4)

    fx = Path(args.fixtures_dir) if args.fixtures_dir else None

    # --- SSA ---
    ssa_text = (fx / "ssa.txt").read_text() if fx else strip_tags(
        fetch("https://www.ssa.gov/oact/cola/cbb.html"))
    ssa_base = parse_ssa(ssa_text, target)
    if ssa_base is None:
        log(f"SSA hasn't posted the {target} wage base yet — waiting.")
        sys.exit(3)
    log(f"SSA {target} wage base: {ssa_base:,}")

    # --- IRS ---
    if fx:
        irs_text = (fx / "irs.txt").read_text()
    else:
        irs_text = None
        for u in irs_urls(target):
            try:
                irs_text = strip_tags(fetch(u))
                log(f"IRS release found: {u}")
                break
            except Exception:
                continue
        if irs_text is None:
            log(f"IRS {target} inflation-adjustments release not found yet — waiting.")
            sys.exit(3)
    irs = parse_irs(irs_text, target)
    log(f"IRS {target}: MFJ std {irs['std_married']:,}, single std {irs['std_single']:,}, "
        f"ctc {irs['ctc'] if irs['ctc'] is not None else 'carry-forward'}")

    # --- Oregon ---
    if fx:
        or_text = (fx / "oregon.txt").read_text()
    else:
        url = oregon_url(target)
        if not exists(url):
            log(f"Oregon {target} formulas PDF not published yet — waiting.")
            sys.exit(3)
        pdf = fetch(url, binary=True)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf)
        r = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"pdftotext failed on Oregon PDF: {r.stderr[:400]}")
        or_text = r.stdout
    orx = parse_oregon(or_text, target)
    log(f"Oregon {target}: married brackets "
        f"{orx['brackets'][0]['upTo']:,}/{orx['brackets'][1]['upTo']:,}/{orx['brackets'][2]['upTo']:,}, "
        f"std {orx['stdLow']:,}/{orx['stdHigh']:,}, credit {orx['exemptionCredit']}, "
        f"fed cap {orx['fedSubtractionCap']:,}")

    new, carried = assemble(cur, target, ssa_base, irs, orx)
    log(f"\nChanges {cur['year']} -> {target}:\n" + summarize(cur, new))
    log(f"Carried forward (statutory, verify only if a law changed): {', '.join(carried)}")

    if args.dry_run:
        log("\nDRY RUN — nothing written.")
        sys.exit(0)
    Path(args.file).write_text(json.dumps(new, indent=2) + "\n")
    log(f"\nWrote {args.file} for {target}.")
    sys.exit(0)


if __name__ == "__main__":
    main()
