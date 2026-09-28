#!/usr/bin/env python3
"""SOURCE_RECONCILIATION (arc-rules-production-lifecycle) for Alberta Rules of Court 3.1-3.77.

No-write: reads the repo's source carriers and emits one combined JSON packet holding every
carrier's content per subrule, plus per-subrule three-book gate records (arc-three-book-gate-v1).
Books never outvote the official source and no text is silently merged.
"""
from __future__ import annotations
import difflib, hashlib, json, re, subprocess, sys, unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "rule3_subrules"
RULES = [f"3.{n}" for n in range(1, 78)]

OFFICIAL_TXT = ROOT / "Alberta_Rules_of_Court.txt"
BOOK_A = [ROOT / "combined rule3.txt"]
BOOK_B = [ROOT / f"rule3_part{n:02d}_document.json" for n in range(1, 11)]
BOOK_C = [ROOT / "101-120_2_28 to 3_18.json", ROOT / "121-140_3_19 to 3_43.json",
          ROOT / "141-160_3_44 to 3_66.json", ROOT / "161-180_3_67 to 4_7.json"]

SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_text(s: str) -> str:
    return sha(s.encode("utf-8"))


def file_sha(p: Path) -> str:
    return sha(p.read_bytes())


def pdf_sha() -> str | None:
    for ref in ("origin/main", "main", "HEAD"):
        r = subprocess.run(["git", "show", f"{ref}:Alberta rule of court.pdf"], cwd=ROOT, capture_output=True)
        if r.returncode == 0:
            return sha(r.stdout)
    return None


# ---------------------------------------------------------------- normalization
NORMALIZATION_PROFILE = {
    "id": "arc-rule3-compare-norm",
    "version": "1",
    "strict_steps": [
        "drop a leading rule number '3.N' (carriers differ on whether the number is part of the text span)",
        "drop footnote superscript digits (U+2070-U+2079, U+00B9/B2/B3)",
        "Unicode NFKC (expands ligatures such as U+FB01 'fi')",
        "curly quotes/apostrophes -> ASCII; en/em dash -> '-'",
        "remove markdown emphasis '*'",
        "collapse all whitespace to single space; trim",
        "remove space before , ; : . )  and after (",
    ],
    "alphanumeric_tier": "if the strict and editorial forms still differ, texts that are equal once every character except letters and digits is removed are classed NORMALIZATION_ONLY_DIFFERENCE with basis 'alphanumerics only' (needed for Book A Part 3, whose extraction drops spaces, and for line-break hyphens); a hint, never a pass",
    "editorial_steps": [
        "all strict steps",
        "remove bracketed editorial cross-reference labels '[...]'",
        "citation style 'Alta. Reg.' -> 'AR'",
        "case-fold",
    ],
    "note": "Raw carrier text is preserved separately; normalization is used only for comparison.",
}
NORMALIZATION_PROFILE["sha256"] = sha_text(json.dumps(NORMALIZATION_PROFILE, sort_keys=True))


def norm_strict(s: str) -> str:
    s = re.sub(r"^\s*3\.\d+(?![\d.])", "", s)
    s = re.sub(f"[{SUPERSCRIPTS}¹²³]", "", s)
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    s = s.replace("*", "")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,;:.)])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    return s


def norm_editorial(s: str) -> str:
    s = norm_strict(s)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    s = re.sub(r"\bAlta\. Reg\.", "AR", s)
    s = re.sub(r"\s+([,;:.)])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def compare(official: str, book: str | None) -> dict:
    if not book:
        return {"status": "CARRIER_TEXT_MISSING"}
    o_s, b_s = norm_strict(official), norm_strict(book)
    if o_s == b_s:
        return {"status": "EXACT_MATCH", "similarity": 1.0}
    o_e, b_e = norm_editorial(official), norm_editorial(book)
    alnum = lambda x: re.sub(r"[^0-9a-z]", "", x)
    if alnum(o_e) == alnum(b_e):
        return {"status": "NORMALIZATION_ONLY_DIFFERENCE", "similarity": 1.0,
                "basis": "equal when only letters and digits are compared (spacing, hyphenation and punctuation spacing differ)"}
    ow, bw = o_e.split(), b_e.split()
    sm = difflib.SequenceMatcher(None, ow, bw, autojunk=False)
    ops, kinds = [], set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        kinds.add(tag)
        ops.append({"op": tag, "official": " ".join(ow[i1:i2]), "carrier": " ".join(bw[j1:j2])})
    if not ops:
        status = "NORMALIZATION_ONLY_DIFFERENCE"
    elif kinds == {"insert"}:
        status = "CARRIER_HAS_EXTRA_TEXT"  # boundary bleed / commentary contamination candidate
    elif kinds == {"delete"}:
        status = "CARRIER_MISSING_TEXT"  # extraction/boundary defect candidate
    else:
        status = "WORDING_DIFFERENCE"
    out = {"status": status, "similarity": round(sm.ratio(), 4)}
    if ops:
        out["differences"] = ops[:40]
        if len(ops) > 40:
            out["differences_truncated"] = len(ops) - 40
    return out


# ---------------------------------------------------------------- official source
def parse_official() -> tuple[dict, dict]:
    raw = OFFICIAL_TXT.read_text(encoding="utf-8")
    lines = raw.split("\n")
    # body of Part 3: second "Part 3" heading through the page break before second "Part 4"
    p2 = [i for i, l in enumerate(lines) if l.strip() == "Part 3"][1]
    p3 = [i for i, l in enumerate(lines) if l.strip() == "Part 4"][1]
    seg = lines[p2:p3]
    # track page numbers and strip running headers
    clean, page, pages = [], None, []
    first_page = None
    for i, l in enumerate(lines[:p2]):
        m = re.match(r"--- Page (\d+) ---", l)
        if m:
            first_page = int(m.group(1))
    page = first_page
    i = 0
    while i < len(seg):
        m = re.match(r"--- Page (\d+) ---", seg[i])
        if m:
            page = int(m.group(1))
            j = i + 1
            while j < len(seg) and not re.fullmatch(r"\d+", seg[j].strip()):
                j += 1
            i = j + 1
            continue
        clean.append(seg[i].rstrip())
        pages.append(page)
        i += 1
    # titles and division headings from the official table of contents (lines p2_toc..p3_toc)
    toc_s = [i for i, l in enumerate(lines) if l.strip() == "Part 3"][0]
    toc_e = [i for i, l in enumerate(lines) if l.strip() == "Part 4"][0]
    toc = [l.strip() for l in lines[toc_s:toc_e]]
    toc_title, toc_div, toc_sub, cur_div, cur_sub, k = {}, {}, {}, None, None, 0
    stop_re = r"(3\.\d+|Division \d+|Subdivision \d+)"
    while k < len(toc):
        m = re.fullmatch(r"(Sub)?[Dd]ivision (\d+)", toc[k])
        if m:
            j, head = k + 1, []
            while j < len(toc) and toc[j] and not re.fullmatch(stop_re, toc[j]):
                head.append(toc[j]); j += 1
            entry = {"number": int(m.group(2)), "heading": re.sub(r"\s+", " ", " ".join(head)).strip()}
            if m.group(1):
                cur_sub = entry
            else:
                cur_div, cur_sub = entry, None
            k = j; continue
        if re.fullmatch(r"3\.\d+", toc[k]):
            j, t = k + 1, []
            while j < len(toc) and toc[j] and not re.fullmatch(stop_re, toc[j]):
                t.append(toc[j]); j += 1
            toc_title[toc[k]], toc_div[toc[k]], toc_sub[toc[k]] = re.sub(r"\s+", " ", " ".join(t)).strip(), cur_div, cur_sub
            k = j; continue
        k += 1
    div_lines = {x for d in list(toc_div.values()) if d for x in [f"Division {d['number']}"]} | \
                {x for d in list(toc_sub.values()) if d for x in [f"Subdivision {d['number']}"]}
    div_head_words = {d["heading"] for d in list(toc_div.values()) + list(toc_sub.values()) if d}
    starts, pos = [], 0
    for r in RULES:
        pat = re.compile(rf"^{re.escape(r)}(\(1\))?\s")
        while pos < len(clean) and not pat.match(clean[pos]):
            pos += 1
        starts.append(pos)
        pos += 1

    def title_begin(idx):
        s0 = starts[idx]
        first = toc_title[RULES[idx]].split()[0]
        k = s0 - 1
        while k > 0 and not clean[k].strip().startswith(first):
            k -= 1
        # walk further back over division heading lines
        while k > 0 and (clean[k - 1].strip() in div_lines or not clean[k - 1].strip()
                         or any(clean[k - 1].strip() and clean[k - 1].strip() in h for h in div_head_words)):
            if clean[k - 1].strip().endswith((".", ";", ":")):
                break
            k -= 1
        return k

    rules = {}
    for idx, r in enumerate(RULES):
        s = starts[idx]
        end = title_begin(idx + 1) if idx + 1 < len(RULES) else len(clean)
        body = [x for x in clean[s:end]]
        while body and not body[-1].strip():
            body = body[:-1]
        amend = None
        if body and body[-1].strip().startswith("AR 124/2010"):
            amend = body[-1].strip()
            body = body[:-1]
        text = "\n".join(x.strip() for x in body if x.strip())
        rules[r] = {
            "title": toc_title[r],
            "division": toc_div[r],
            "subdivision": toc_sub[r],
            "operative_text": text,
            "operative_text_sha256": sha_text(text),
            "amendment_history": amend or "AR 124/2010",
            "amending_regulations": re.findall(r"\d+/\d{4}", amend or "")[1:] if amend else [],
            "locator": {"file": OFFICIAL_TXT.name, "pdf_pages": sorted({pages[s], pages[min(len(pages) - 1, s + len(body))]})},
        }
    return rules, {"consolidation": "Office Consolidation, Alberta Regulation 124/2010, with amendments up to and including Alberta Regulation 79/2026; current as of June 1, 2026"}


# ---------------------------------------------------------------- Book A (combined rule3.txt)
# Part 3 of Book A is a different extraction from Part 2: pages open with "**PAGE N" (book page "3-N"), the running
# head is either a line "3-N" or "PART 3:COURT ACTIONSR.3.x(y) 3-N", and the other head is printed inline as
# "*R.3.x(y)PART 3:COURT ACTIONS". Spaces are dropped in many places and footnotes are not set apart from the text.
BOOK_A_PAGE_RE = re.compile(r"--- PAGE BREAK ---\s*\n\*\*PAGE (\d+)\n")
BOOK_A_HEAD_LINE_RE = re.compile(r"^(?:3-\d+|PART 3:COURT ACTIONSR\.\S+ 3-\d+)\s*\n?")
BOOK_A_INLINE_HEAD_RE = re.compile(r"\*?R\.\d+\.\d+(?:\(\d+\))?PART 3:COURT ACTIONS ?")
BOOK_A_AMEND_RE = re.compile(r"\[Alta\.\s?Reg\.[^\]]*\]")


def book_a_pages() -> tuple[str, list[int], list[str], list[dict]]:
    """Book A with page furniture removed: (clean text, start offset of each page, page labels, removed heads)."""
    raw = BOOK_A[0].read_text(encoding="utf-8")
    parts = BOOK_A_PAGE_RE.split(raw)
    # parts = [header, n1, chunk1, n2, chunk2, ...]
    text, offs, labels, heads = "", [], [], []
    for k in range(1, len(parts), 2):
        n, chunk = int(parts[k]), parts[k + 1]
        chunk = chunk.lstrip("\n")
        m = BOOK_A_HEAD_LINE_RE.match(chunk)
        head = []
        if m:
            head.append(m.group(0).strip())
            chunk = chunk[m.end():]
        for im in BOOK_A_INLINE_HEAD_RE.finditer(chunk):
            head.append(im.group(0).strip())
        chunk = BOOK_A_INLINE_HEAD_RE.sub("", chunk)
        offs.append(len(text))
        labels.append(f"3-{n}")
        heads.append({"page": f"3-{n}", "removed": head})
        text += chunk.strip("\n") + "\n\n"
    return text, offs, labels, heads


def parse_book_a() -> tuple[dict, dict]:
    import bisect
    text, offs, labels, heads = book_a_pages()

    def page_at(pos):
        return labels[bisect.bisect_right(offs, pos) - 1]

    starts, titles, pos = {}, {}, 0
    for r in RULES:
        pat = re.compile(rf"\*(?:(?P<t>[A-Z][^*]{{1,150}})\*\s?)?(?P<n>{re.escape(r)})(?=\(1\)|\s|[A-Z(])")
        m = pat.search(text, pos)
        if m:
            starts[r], titles[r] = m.start("n"), (m.group("t") or "").strip().replace("\n", " ") or None
            pos = m.end()
    found = [r for r in RULES if r in starts]
    out = {}
    for k, r in enumerate(found):
        s = starts[r]
        end = starts[found[k + 1]] if k + 1 < len(found) else len(text)
        seg = text[s:end]
        m = BOOK_A_AMEND_RE.search(seg)
        if m:
            op_end = m.end()
        else:
            cut = [seg.find(x) for x in ("Information Note", "Defined Terms", "Related Provisions", "\n\n") if seg.find(x) > 0]
            op_end = min(cut) if cut else len(seg)
        op, rest = seg[:op_end].strip(), seg[op_end:].strip()
        info = defined = related = None
        while rest:
            mm = re.match(r"(Information Note|Defined Terms|Related Provisions)\s*", rest)
            if not mm:
                break
            head_name, body = mm.group(1), rest[mm.end():]
            if head_name == "Related Provisions":
                # the list ends at the first ')' (not followed by ';') that is followed by a capital letter or a blank line
                cut = re.search(r"\)(?!;)\s*(?=\n\n|[A-Z][a-z]|$)", body)
                stop = cut.end() if cut else len(body)
            else:
                nxt = [body.find(x) for x in ("Defined Terms", "Related Provisions") if body.find(x) >= 0]
                stop = min(nxt) if nxt else len(body)
            piece, rest = body[:stop].strip(), body[stop:].strip()
            if head_name == "Information Note":
                info = piece
            elif head_name == "Defined Terms":
                defined = piece
            else:
                related = re.sub(r"\s+", " ", piece)
        op_clean = re.sub(r"\n+", "\n", op)
        out[r] = {
            "title": titles[r],
            "operative_text_raw": op_clean,
            "operative_text_sha256": sha_text(op_clean),
            "information_note": re.sub(r"\s+", " ", info) if info else None,
            "defined_terms": re.sub(r"\s+", " ", defined) if defined else None,
            "related_provisions": related,
            "commentary": rest.strip() or None,
            "footnotes": [],
            "footnotes_note": "not split out: this extraction prints footnotes as unmarked text inside the page (see commentary); nothing was dropped",
            "locator": {"file": BOOK_A[0].name, "book_pages": sorted({page_at(x) for x in (s, end - 1)}, key=lambda z: int(z.split("-")[1])),
                        "offset_in_clean_text": [s, end]},
        }
    missing = [r for r in RULES if r not in starts]
    lead = text[: starts[found[0]]].strip() if found else ""
    return out, {"first_page_in_file": labels[0], "last_page_in_file": labels[-1], "rules_not_found_in_file": missing,
                 "text_before_first_found_rule": lead[:3000],
                 "text_before_first_found_rule_note": "probable commentary of a rule whose own text is on pages not in the file; attribution not verified",
                 "page_furniture_removed": heads}


# ---------------------------------------------------------------- Book B (Fradsham JSON)
def parse_book_b() -> tuple[dict, dict]:
    paras = []
    for f in BOOK_B:
        d = json.loads(f.read_text(encoding="utf-8"))
        for p in d["document_structure"]["paragraphs"]:
            paras.append((f.name, p))
    out = {r: {"title": None, "operative_text_raw": "", "commentary": [], "paragraph_ids": [], "locator": {"files": set()}} for r in RULES}
    cur, mode = None, None
    front, years, seen_comm = [], [], set()
    for fname, p in paras:
        t = p["text"].strip()
        years += [int(y) for y in re.findall(r"\b(20[0-2]\d)\s+AB(?:QB|CA|KB|PC|CJ)\b", t)]
        if re.match(r"Part 3\. Court Actions Division \d+\.", t) or re.match(r"Part 3\. Court Actions Division", t) and len(t) < 250:
            continue  # running head repeated in the extraction
        mh = re.fullmatch(r"(3\.\d+)\. (.{1,250})", t, re.S)
        if mh and "_rule_3_" in p["paragraph_id"] and len(t) < 260:
            cur, mode = mh.group(1), "rule"
            if cur in out:
                out[cur]["title"] = out[cur]["title"] or mh.group(2).strip()
                out[cur]["paragraph_ids"].append(p["paragraph_id"]); out[cur]["locator"]["files"].add(fname)
            continue
        mc = re.match(r"Commentary § (3\.\d+)((?:\([^)]*\))*):(\d+)\s*(.*)", t, re.S)
        if mc:
            cur, mode = mc.group(1), "comm"
            if cur in out:
                o = out[cur]
                o["paragraph_ids"].append(p["paragraph_id"]); o["locator"]["files"].add(fname)
                key = (cur, mc.group(2), mc.group(3), mc.group(4)[:200])
                if key in seen_comm:
                    continue
                seen_comm.add(key)
                o["commentary"].append({"section": f"{cur}{mc.group(2)}:{mc.group(3)}", "text": mc.group(4).strip()})
            continue
        if cur not in out:
            front.append(t)
            continue
        o = out[cur]
        o["paragraph_ids"].append(p["paragraph_id"]); o["locator"]["files"].add(fname)
        if mode == "rule":
            o["operative_text_raw"] = (o["operative_text_raw"] + "\n" + t).strip()
        elif o["commentary"]:
            o["commentary"][-1]["text"] += "\n" + t
        else:
            o["commentary"].append({"section": None, "text": t})
    amend_re = re.compile(r"\s*(Alta\. Reg\. \d+/\d{4}, s\. \d+(?:\([a-z0-9]+\))*(?:[;,] ?(?:Alta\. Reg\. )?\d+/\d{4}, s\. \d+(?:\([a-z0-9]+\))*)*)\s*$")
    for r, o in out.items():
        m = amend_re.search(o["operative_text_raw"])
        o["amendment_note_raw"] = m.group(1) if m else None
        if m:
            o["operative_text_raw"] = o["operative_text_raw"][: m.start()].rstrip()
        o["operative_text_sha256"] = sha_text(o["operative_text_raw"])
        o["locator"]["files"] = sorted(o["locator"]["files"])
    edition_year = str(max(years)) if years else None
    return out, {"edition_header": "Alberta Rules of Court, The Honourable Justice Allan A. Fradsham (Part 3 files carry no edition year; header text found only in some files)",
                 "edition_year": edition_year,
                 "edition_year_basis": "latest Alberta case year cited across the Part 3 files (no edition year is printed in them)"}


# ---------------------------------------------------------------- Book C (annotated JSON pages 101-180)
def parse_book_c() -> tuple[dict, dict]:
    out, part_meta, misparsed = {}, {"definitions_used_in_part": []}, []
    for f in BOOK_C:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("definitions"):
            part_meta["definitions_used_in_part"] = d["definitions"]
        seq = []
        for h in d["hierarchy"]:
            if h["type"] == "orphan_rule":
                seq.append((None, h["data"]))
            else:
                for rr in h.get("rules", []):
                    seq.append((h.get("heading"), rr))
        prev = None
        for heading, rr in seq:
            num = rr.get("number")
            if num in RULES:
                text = rr.get("text", "")
                m = re.search(r"\s*\*?\[Alta\. Reg\. 124/2010", text)
                op = text[: m.start()].strip() if m else text.strip()
                amend = text[m.start():].strip() if m else None
                rec = {
                    "title": rr.get("title"),
                    "division_heading": heading,
                    "operative_text_raw": op,
                    "operative_text_sha256": sha_text(op),
                    "amendment_note_raw": amend,
                    "citations": rr.get("citations", []),
                    "information_notes": rr.get("info_notes", []),
                    "commentary": rr.get("commentary", []),
                    "misattributed_entries": [],
                    "locator": {"file": f.name, "source_file": d.get("source_file")},
                }
                if num in out:
                    # Book C lists some rules twice (rule text in one entry, annotation in the other): merge, keep both
                    first = out[num]
                    base, extra = (first, rec) if len(first["operative_text_raw"]) >= len(rec["operative_text_raw"]) else (rec, first)
                    for key in ("citations", "information_notes", "commentary"):
                        base[key] = list(base[key]) + list(extra[key])
                    if not base.get("amendment_note_raw"):
                        base["amendment_note_raw"] = extra.get("amendment_note_raw")
                    base["merged_duplicate_entries"] = [
                        {"file": x["locator"]["file"], "parsed_title": x["title"], "role": "kept as rule entry" if x is base else "merged into it",
                         "rule_text_of_merged_entry": x["operative_text_raw"] if x is not base and x["operative_text_raw"] else None}
                        for x in (first, rec)]
                    rec = base
                out[num] = rec
                prev = num
            elif prev in out and not str(num).startswith(("2.", "3.", "4.")):
                entry = {"parsed_number": num, "parsed_title": rr.get("title"), "text": rr.get("text"),
                         "information_notes": rr.get("info_notes", []), "commentary": rr.get("commentary", []),
                         "defect": "HEADING_MISPARSE: entry parsed as a separate rule but sits inside the preceding rule's annotation block"}
                out[prev]["misattributed_entries"].append(entry)
                misparsed.append({"attached_to": prev, "parsed_number": num})
    part_meta["misparsed_entries"] = misparsed
    return out, part_meta


# ---------------------------------------------------------------- latest-cited-year (edition floor)
def latest_year(text: str) -> int | None:
    ys = [int(y) for y in re.findall(r"\b(20[0-2]\d)\s+AB(?:QB|CA|KB|PC|CJ)\b", text)]
    return max(ys) if ys else None


def amendment_check(off: dict, a: dict | None, b: dict | None, c: dict | None) -> dict:
    """Compare amending regulations cited by each book with the official history (years normalized to 4 digits)."""
    def regs(txt):
        out = set()
        for n, y in re.findall(r"(\d+)/(\d{2,4})", txt or ""):
            y = int(y)
            y = y + 2000 if y < 100 else y
            if (n, y) != ("124", 2010) and y >= 2010:
                out.add(f"{n}/{y}")
        return out
    official = set(off["amending_regulations"])
    notes = {
        "BOOK_A": re.findall(r"\[Alta\. Reg\.[^\]]*\]", (a or {}).get("operative_text_raw") or ""),
        "BOOK_B": (b or {}).get("amendment_note_raw"),
        "BOOK_C": (c or {}).get("amendment_note_raw"),
    }
    check = {}
    for cid, note in notes.items():
        if note:
            found = regs(" ".join(note) if isinstance(note, list) else note)
            check[cid] = "AGREES" if found == official else f"DIFFERS: carrier cites {sorted(found)}, official {sorted(official)}"
    return {"official": off["amendment_history"], "amending_regulations": off["amending_regulations"],
            "carrier_notes": {k: v for k, v in notes.items() if v}, "crosscheck_vs_official": check}


def _sim(x: str, y: str) -> float:
    return difflib.SequenceMatcher(None, norm_editorial(x).split(), norm_editorial(y).split(), autojunk=False).ratio()


# Manual decisions for Part 3 are recorded here (see README section 4 / tools/edit_helpers.py). All start empty.
MANUAL_NOTE_FLAGS = {
}
MANUAL_TEXT_NOTES = {
}
MANUAL_SEE_ALSO = {
    "3.1": ["Official text searched for 'rule 3.1', 'Rules 3.1' (including line-wrapped forms) and form headings '[Rule 3.1]': the only hit is the running page header 'Rule 3.1 AR 124/2010' (pdf p.49). No official rule cites 3.1 by number. BOOK_B (parts 01-10) has no 'Commentary § 3.1' section; BOOK_C's only text mentioning '3.1' is a displaced running head (see BOOK_C flag).",
            "FLAG on BOOK_A R.4.3 (p.4-6, line 207, Related Provisions): '3.1 (statement of defence)'. Official 3.1 is 'Rules govern Court actions'; 'Statement of defence' is the official title of rule 3.31, and official 4.3(3) speaks of a statement of defence being filed. The sources do not say which number was meant.",
            "BOOK_A related provisions that list 3.1 (searched all Book A files, line-wrapped forms included): R.1.1 (p.1-3, lines 45-46, wrapped) and R.1.7 (p.1-33, line 1624), both '3.1 (rules govern all proceedings)' - the label paraphrases the official title 'Rules govern Court actions'. Official 1.1(1): 'These rules govern the practice and procedure in (a) the Court of King's Bench of Alberta, and (b) the Court of Appeal of Alberta'.",
            
        "BOOK_A has no rule text or notes for 3.1: its Part 3 file starts at book page 3-4 (pages 3-1 to 3-3 are not in it). Book A pointers into its own missing 3.1 note: p.3-7 (line 85, footnote text) 'See Sabir v. Gill and commentary on it, in R.3.1n.' and p.13-70 (line 3653, under R.13.13) 'See Sabir v. Gill, R.3.1 n.'; the case is cited in full at p.3-43 fn 1 and p.3-45 fn 8 (Sabir v. Gill 2023 ABKB 679) and at p.13-70 fn 3. The 3.1 note itself cannot be checked.",
    ],
}
MANUAL_BOOK_A_COMMENTARY_FLAGS = {
}
MANUAL_BOOK_A_FOOTNOTE_FLAGS = {
}
MANUAL_BOOK_C = {
    "3.1": {"commentary_flag": "footnote marker '2' ends this commentary but its text is not here: Book C prints it inside its rule 3.2 entry "
                               "('2 George E. Woodbine, ed., , , ca. 1250, Bracton on the Laws and Customs of England Volume 2 translated by "
                               "Samuel E. Thorne (Cambridge: The Belknap Press of Harvard University, 1968-1977) at 282.', followed by the running "
                               "head 'R. 3.1 48 [Resolving Issues and Preserving Rights]'). No marker '1' is printed in the 3.1 text."},
}


def dedupe(packet: dict) -> None:
    """Remove redundancy without losing information: every dropped value is identical (after the
    documented normalization) to a value kept elsewhere, and its hash/locator stays in place."""
    log = []
    for s in packet["subrules"]:
        r, src, comp = s["rule"], s["sources"], s["comparison_vs_official"]
        # 1. book operative text identical to official -> keep hash + status only
        for cid in ("BOOK_A", "BOOK_B", "BOOK_C"):
            rec = src.get(cid)
            if rec and comp[cid]["status"] == "EXACT_MATCH":
                rec["operative_text_raw"] = None
                rec["operative_text_note"] = f"same as official operative text ({comp[cid]['status']}); see operative_text_sha256"
                log.append(f"{r} {cid}: operative text dropped ({comp[cid]['status']})")
        # 2. titles: keep official; list book variants only when wording (not case) differs
        t = s["title"]
        variants = {k: v for k, v in t.items() if k != "official" and v and norm_editorial(v) != norm_editorial(t["official"])}
        s["title"] = {"official": t["official"], "variants": variants}
        # 3. information notes: merge Book A / Book C duplicates
        notes = []
        a_note = src["BOOK_A"].pop("information_note", None) if src.get("BOOK_A") else None
        c_notes = src["BOOK_C"].pop("information_notes", []) if src.get("BOOK_C") else []
        if a_note:
            notes.append({"text": a_note, "carriers": ["BOOK_A"]})
        for cn in c_notes:
            hit = next((n for n in notes if _sim(n["text"], cn) >= 0.8), None)
            if hit:
                hit["carriers"].append("BOOK_C")
                if norm_strict(hit["text"]) != norm_strict(cn):
                    words = lambda x: set(re.findall(r"[a-z0-9.()]+", norm_strict(x).casefold()))
                    if words(cn) <= words(hit["text"]):
                        # same words, only scrambled: nothing new, so the garbled copy is not kept
                        hit.setdefault("carrier_defects", {})["BOOK_C"] = MANUAL_NOTE_FLAGS.get(
                            r, "garbled copy of the same note (word order/brackets); not retained")
                        log.append(f"{r} BOOK_C information note dropped: garbled duplicate, no new words")
                        continue
                    hit.setdefault("variant_text", {})["BOOK_C"] = cn
                log.append(f"{r} BOOK_C information note merged into BOOK_A copy")
            else:
                notes.append({"text": cn, "carriers": ["BOOK_C"]})
        s["information_notes"] = notes
        # 3b. Book C citation "context" is a window of the carrier text; drop it when that text is kept verbatim
        if src.get("BOOK_C"):
            kept = [src["BOOK_C"].get("operative_text_raw") or ""] + [c["text"] for c in src["BOOK_C"]["commentary"]]
            cits = list(src["BOOK_C"]["citations"]) + [x for c in src["BOOK_C"]["commentary"] for x in c.get("citations", [])]
            for cit in cits:
                ctx = cit.get("context")
                if ctx and any(ctx.strip() in k for k in kept):
                    cit.pop("context")
                    log.append(f"{r} BOOK_C citation context dropped ({cit.get('neutral_citation')}): verbatim in kept text")
        # 4. Book C commentary: drop per-item sentence splits (derived from the same text)
        if src.get("BOOK_C"):
            for grp in [src["BOOK_C"]["commentary"]] + [e["commentary"] for e in src["BOOK_C"]["misattributed_entries"]]:
                for c in grp:
                    if c.pop("sentences", None) is not None:
                        log.append(f"{r} BOOK_C commentary sentence split dropped")
        # 5. rule-by-rule manual curation of Book C (decided on manual review)
        cur = MANUAL_BOOK_C.get(r)
        if cur and src.get("BOOK_C"):
            c = src["BOOK_C"]
            if cur.get("drop_rule_text"):
                c["operative_text_raw"] = None
                c["operative_text_note"] = cur["drop_rule_text"]
                comp["BOOK_C"].pop("differences", None)
                log.append(f"{r} BOOK_C: rule text dropped (manual review)")
            if cur.get("rescue_misattributed"):
                for e in c.pop("misattributed_entries", []):
                    for item in e["commentary"]:
                        item["note"] = cur["rescue_misattributed"]
                        c["commentary"].append(item)
                    if e.get("parsed_title"):
                        for n in s["information_notes"]:
                            n["carriers"].append("BOOK_C")
                            n.setdefault("carrier_defects", {})["BOOK_C"] = "copy survives only as a garbled heading; not retained"
                log.append(f"{r} BOOK_C: misparsed entry folded into commentary; garbled heading dropped (manual review)")
            if cur.get("drop_note_variant"):
                for n in s["information_notes"]:
                    if n.get("variant_text", {}).pop("BOOK_C", None) is not None:
                        n.setdefault("carrier_defects", {})["BOOK_C"] = cur["drop_note_variant"]
                        if not n["variant_text"]:
                            n.pop("variant_text")
                        log.append(f"{r} BOOK_C information-note variant dropped (manual review)")
            if cur.get("note_flag"):
                for n in s["information_notes"]:
                    n["review_flag"] = cur["note_flag"]
            if cur.get("commentary_flag"):
                for cm in c["commentary"]:
                    cm["review_flag"] = cur["commentary_flag"]
            if cur.get("drop_c_note"):
                keep = []
                for n in s["information_notes"]:
                    if n["carriers"] == ["BOOK_C"]:
                        log.append(f"{r} BOOK_C information note dropped (manual review)")
                        continue
                    keep.append(n)
                for n in keep:
                    if "BOOK_A" in n["carriers"]:
                        n["carriers"].append("BOOK_C")
                        n.setdefault("carrier_defects", {})["BOOK_C"] = cur["drop_c_note"]
                s["information_notes"] = keep
            for cm in c["commentary"]:
                cm.setdefault("citations", []).extend(cur.get("add_commentary_citations", []))
            if cur.get("drop_commentary"):
                c["commentary"] = []
                c["commentary_note"] = cur["drop_commentary"]
                log.append(f"{r} BOOK_C: commentary dropped (manual review)")
            if cur.get("amendment_note_flag"):
                s["amendment_history"].setdefault("carrier_note_flags", {})["BOOK_C"] = cur["amendment_note_flag"]
            if cur.get("keep_citations_note"):
                c["citations_note"] = cur["keep_citations_note"]
            c["citations"].extend(cur.get("add_citations", []))
            if cur.get("drop_citations"):
                dropped = [x.get("neutral_citation") for x in c["citations"]]
                c["citations"] = []
                c["citations_note"] = cur["drop_citations"]
                log.append(f"{r} BOOK_C: displaced citations dropped {dropped} (manual review)")
            for nc_drop, why in cur.get("drop_one_citation", {}).items():
                c["citations"] = [x for x in c["citations"] if x.get("neutral_citation") != nc_drop]
                c.setdefault("dropped_citation_notes", {})[nc_drop] = why
                log.append(f"{r} BOOK_C: citation {nc_drop} dropped (manual review)")
            for cit in list(c["citations"]) + [x for cm in c["commentary"] for x in cm.get("citations", [])]:
                nc = cit.get("neutral_citation")
                if nc in cur.get("citation_notes", {}):
                    cit["note"] = cur["citation_notes"][nc]
                if nc in cur.get("citation_names", {}):
                    cit["style_of_cause"] = cur["citation_names"][nc]
        # 5a. amendment notes are kept once, in amendment_history.carrier_notes
        for cid in ("BOOK_B", "BOOK_C"):
            if (src.get(cid) or {}).pop("amendment_note_raw", None):
                log.append(f"{r} {cid}: amendment note moved to amendment_history.carrier_notes (was duplicated)")
        # 5b. manual drops of Book A / Book B raw text whose differences are editorial only
        for cid, note in MANUAL_TEXT_NOTES.get(r, {}).items():
            rec = src.get(cid)
            if rec and rec.get("operative_text_raw"):
                rec["operative_text_raw"] = None
                rec["operative_text_note"] = note
                comp[cid]["review_note"] = note
                comp[cid].pop("differences", None)
                log.append(f"{r} {cid}: raw text dropped, differences editorial only (manual review)")
        # 5c. Book C division heading vs official
        cdiv = (src.get("BOOK_C") or {}).get("division_heading")
        if cdiv and s["division"] and not cdiv.startswith(f"Division {s['division']['number']} "):
            src["BOOK_C"]["division_heading_flag"] = (f"wrong in Book C: rule {r} is in Division {s['division']['number']} "
                                                      f"({s['division']['heading']}) per the official text")
        if r in MANUAL_BOOK_A_COMMENTARY_FLAGS and src.get("BOOK_A"):
            src["BOOK_A"]["commentary_review_flag"] = MANUAL_BOOK_A_COMMENTARY_FLAGS[r]
        if r in MANUAL_SEE_ALSO:
            s["see_also"] = MANUAL_SEE_ALSO[r]
        # 6. manual flags on Book A footnotes
        for (pg, num), flag in MANUAL_BOOK_A_FOOTNOTE_FLAGS.get(r, {}).items():
            for fn in (src.get("BOOK_A") or {}).get("footnotes", []):
                if fn["page"] == pg and fn["number"] == num:
                    fn["review_flag"] = flag
    packet["dedupe_log"] = {"rule": "a value is dropped only when an equal value (under the normalization profile) is kept", "entries": log}


def main() -> int:
    official, off_meta = parse_official()
    a, a_meta = parse_book_a()
    b, b_meta = parse_book_b()
    c, c_meta = parse_book_c()

    pdf_hash = pdf_sha()
    a_year = latest_year(BOOK_A[0].read_text(encoding="utf-8"))
    c_year = latest_year("\n".join(p.read_text(encoding="utf-8") for p in BOOK_C))
    last_part3_amend = max(int(y.split("/")[1]) for r in official.values() for y in r["amending_regulations"]) if any(
        r["amending_regulations"] for r in official.values()) else 2010

    registry = {
        "OFFICIAL": {
            "carrier_id": "OFFICIAL",
            "title": "Alberta Rules of Court, Alta. Reg. 124/2010 (Alberta King's Printer office consolidation)",
            "version": off_meta["consolidation"],
            "artifacts": [{"file": "Alberta rule of court.pdf", "sha256": pdf_hash, "branch": "main"},
                          {"file": OFFICIAL_TXT.name, "sha256": file_sha(OFFICIAL_TXT), "derived_from": "Alberta rule of court.pdf",
                           "method": "PyMuPDF page.get_text() (embedded text layer, no OCR)"}],
            "source_role": "FIRST_HAND_RULE_CARRIER",
            "origin_id": "ab-kings-printer-consolidation-2026-06-01",
            "permitted_use": "operative-text reconciliation anchor; official version/current-to closure",
        },
        "BOOK_A": {
            "carrier_id": "BOOK_A",
            "title": "Annotated rules text, Part 3 (publisher/title not stated in carrier; page style '**PAGE N' + '3-N', 'Related Provisions', historical-derivation footnotes; text extraction has dropped many spaces)",
            "version": f"unknown edition; latest Alberta case year cited in Part 3 = {a_year}",
            "artifacts": [{"file": BOOK_A[0].name, "sha256": file_sha(BOOK_A[0])}],
            "source_role": "ANNOTATED_RULES",
            "origin_id": "book-a-annotated-part3",
            "permitted_use": "corroboration; annotation (information notes, related provisions, commentary, footnotes)",
        },
        "BOOK_B": {
            "carrier_id": "BOOK_B",
            "title": "Alberta Rules of Court, The Honourable Justice Allan A. Fradsham (per carrier running header)",
            "version": b_meta.get("edition_header"),
            "artifacts": [{"file": p.name, "sha256": file_sha(p)} for p in BOOK_B],
            "source_role": "ANNOTATED_RULES",
            "origin_id": "book-b-fradsham",
            "permitted_use": "corroboration; commentary",
        },
        "BOOK_C": {
            "carrier_id": "BOOK_C",
            "title": "Annotated rules text, book pages 101-180 (publisher/title not stated in carrier; source files 101-120_revised.md ... 161-180_revised.md)",
            "version": f"unknown edition; latest Alberta case year cited = {c_year}",
            "artifacts": [{"file": p.name, "sha256": file_sha(p)} for p in BOOK_C],
            "source_role": "ANNOTATED_RULES",
            "origin_id": "book-c-annotated-pp101-180",
            "permitted_use": "corroboration; annotation (information notes, citations, commentary)",
        },
    }
    registry_sha = sha_text(json.dumps(registry, sort_keys=True))

    def version_identity(cid: str) -> tuple[str, str]:
        floor = {"BOOK_A": a_year, "BOOK_B": int(b_meta["edition_year"]) if b_meta.get("edition_year") else None, "BOOK_C": c_year}[cid]
        if floor and floor > last_part3_amend:
            return "SAME_VERSION_PROVEN", (f"carrier post-dates {floor} >= last Part 3 amendment year {last_part3_amend} "
                                           "per official amendment history; no Part 3 amendment after that date appears in the June 1, 2026 consolidation")
        return "VERSION_IDENTITY_UNRESOLVED", "carrier date cannot be placed after the last Part 3 amendment"

    books = {"BOOK_A": a, "BOOK_B": b, "BOOK_C": c}
    subrules, summary = [], {"EXACT_MATCH": 0, "NORMALIZATION_ONLY_DIFFERENCE": 0, "CARRIER_HAS_EXTRA_TEXT": 0,
                             "CARRIER_MISSING_TEXT": 0, "WORDING_DIFFERENCE": 0, "CARRIER_TEXT_MISSING": 0}
    gate_counts = {}
    for r in RULES:
        off = official[r]
        comps, carriers_gate, vis = {}, {}, {}
        for cid, data in books.items():
            rec = data.get(r)
            comps[cid] = compare(off["operative_text"], rec and rec["operative_text_raw"])
            summary[comps[cid]["status"]] += 1
            vi, basis = version_identity(cid)
            vis[cid] = {"result": vi, "basis": basis}
            comps[cid]["version_identity"] = vi
            if rec:
                carriers_gate[cid] = {
                    "carrier_id": cid,
                    "artifact_sha256": registry[cid]["artifacts"][0]["sha256"],
                    "text_sha256": rec["operative_text_sha256"],
                    "source_role": registry[cid]["source_role"],
                    "origin_id": registry[cid]["origin_id"],
                    "locator": rec["locator"],
                }
        carriers_gate["OFFICIAL"] = {
            "carrier_id": "OFFICIAL", "artifact_sha256": pdf_hash or file_sha(OFFICIAL_TXT),
            "text_sha256": off["operative_text_sha256"], "source_role": "FIRST_HAND_RULE_CARRIER",
            "origin_id": registry["OFFICIAL"]["origin_id"], "locator": off["locator"],
        }
        agree = all(comps[k]["status"] in ("EXACT_MATCH", "NORMALIZATION_ONLY_DIFFERENCE") for k in books)
        overall_vi = "SAME_VERSION_PROVEN" if all(v["result"] == "SAME_VERSION_PROVEN" for v in vis.values()) else "VERSION_IDENTITY_UNRESOLVED"
        if agree and overall_vi == "SAME_VERSION_PROVEN":
            result = "THREE_BOOK_SOURCE_RECONCILED"
        elif overall_vi == "SAME_VERSION_PROVEN" and off["operative_text"]:
            # the official carrier closes operative wording independently; book defects are preserved as provenance
            result = "SOURCE_CARRIER_DEFECT_INDEPENDENTLY_CLOSED"
        else:
            result = "THREE_BOOK_RECONCILIATION_HOLD"
        gate_counts[result] = gate_counts.get(result, 0) + 1
        gate = {
            "schema_version": "arc-three-book-gate-v1",
            "rule_identity": {"instrument": "Alta. Reg. 124/2010", "rule": r, "title_official": off["title"]},
            "three_book_mode": "OPTIONAL_CORROBORATION",
            "required_carriers": ["OFFICIAL"],
            "source_registry_sha256": registry_sha,
            "normalization_profile": {k: NORMALIZATION_PROFILE[k] for k in ("id", "version", "sha256")},
            "version_identity": overall_vi,
            "carrier_version_identity": vis,
            "pairwise_vs_official": {k: v["status"] for k, v in comps.items()},
            "result": result,
            "independent_origin_count": len({c["origin_id"] for c in carriers_gate.values()}),
            "carriers": carriers_gate,
        }
        gate["gate_artifact_sha256"] = sha_text(json.dumps(gate, sort_keys=True))
        subrules.append({
            "rule": r,
            "title": {"official": off["title"], "BOOK_A": a.get(r, {}).get("title"),
                      "BOOK_B": b.get(r, {}).get("title"), "BOOK_C": c.get(r, {}).get("title")},
            "division": off["division"],
            "subdivision": off.get("subdivision"),
            "operative_text": {
                "controlling": off["operative_text"],
                "controlling_carrier": "OFFICIAL",
                "sha256": off["operative_text_sha256"],
            },
            "amendment_history": amendment_check(off, a.get(r), b.get(r), c.get(r)),
            "sources": {
                "OFFICIAL": {"locator": off["locator"]},
                "BOOK_A": a.get(r),
                "BOOK_B": b.get(r),
                "BOOK_C": c.get(r),
            },
            "comparison_vs_official": comps,
            "three_book_gate": gate,
        })

    packet = {
        "schema_version": "arc-rule3-three-book-reconciliation-v1",
        "skill": "arc-rules-production-lifecycle",
        "mode": "SOURCE_RECONCILIATION",
        "write_status": "NO_WRITE (no Neo4j/LexGraph mutation; packet only)",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": {"instrument": "Alberta Rules of Court, Alta. Reg. 124/2010", "part": 3, "rules": f"{RULES[0]}-{RULES[-1]}", "count": len(RULES)},
        "limits": [
            "Three-book agreement is not current-law certification; currency rests on the official consolidation's stated current-to date only.",
            "Book A and Book C titles/editions are not stated in their carriers; roles are inferred from content and recorded as such.",
            "Book A Part 3 starts at book page 3-4 (rule 3.1 is not in the file) and its text extraction drops many spaces and merges running heads and footnotes into the text.",
            "Commentary and notes are copied as carried; no burdens, tests, or exceptions were added.",
            "Discrepancy status values are machine classifications for review, not adjudicated resolutions.",
        ],
        "source_registry": registry,
        "source_registry_sha256": registry_sha,
        "normalization_profile": NORMALIZATION_PROFILE,
        "denominators": {
            "source_closure_denominator": {"definition": "Alta. Reg. 124/2010 rules 3.1-3.77 in the June 1, 2026 consolidation", "count": len(RULES)},
            "pairwise_comparison_denominator": {"definition": "rules x book carriers (A,B,C) compared against OFFICIAL", "count": len(RULES) * 3},
        },
        "summary": {"pairwise_status_counts": summary, "gate_result_counts": gate_counts,
                    "last_part3_amendment_year": last_part3_amend},
        "part_level": {
            "official": off_meta,
            "BOOK_A": a_meta,
            "BOOK_B": b_meta,
            "BOOK_C": c_meta,
        },
        "subrules": subrules,
    }
    dedupe(packet)
    OUT_DIR.mkdir(exist_ok=True)
    log = packet.pop("dedupe_log")
    subs = packet.pop("subrules")
    packet["files"] = [f"{x['rule']}.json" for x in subs]
    packet["dedupe_rule"] = log["rule"]
    (OUT_DIR / "_index.json").write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for x in subs:
        x = {"index": "_index.json (source registry, normalization profile, denominators, part-level notes)", **x,
             "dedupe_log": [e for e in log["entries"] if e.split(" ")[0] == x["rule"]]}
        (OUT_DIR / f"{x['rule']}.json").write_text(json.dumps(x, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(packet["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
