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
            # no amendment bracket: the rule text ends at the first section heading; a blank line is only the fallback
            # (page breaks and column breaks put blank lines inside the rule text)
            cut = [seg.find(x) for x in ("Information Note", "Defined Terms", "Related Provisions") if seg.find(x) > 0]
            if not cut:
                cut = [seg.find("\n\n")] if seg.find("\n\n") > 0 else []
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
            # the extraction sometimes prints a rule's text twice (heading repeated across a page/file break): keep one copy, log the skip
            o.setdefault("_rule_paras", [])
            if len(t) > 40 and t in o["_rule_paras"]:
                o.setdefault("duplicate_rule_text_paragraphs_skipped", []).append(p["paragraph_id"])
                continue
            o["_rule_paras"].append(t)
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
        o.pop("_rule_paras", None)
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
        "BOOK_A": re.findall(r"\[Alta\.\s?Reg\.[^\]]*\]", (a or {}).get("operative_text_raw") or ""),
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
    "3.5": {"BOOK_A": "same wording as official 3.5 (read by eye) except missing spaces, '*' line marks, the title and number run together ('3.5The Court') "
                       "and the footnote marker '2' after clause (b). No amendment bracket; the official text lists no amendment for 3.5. No substantive "
                       "difference. This rule's history footnote (p.3-21 fn 2, 'Quite similar to previous 1968 R.12 ...') is printed inside the built text "
                       "of 3.4 and is kept verbatim in the 3.4 text note."},
    "3.4": {"BOOK_A": "same wording as official 3.4(1)-(6) (read by eye) except the bracket label '[Determining the appropriate judicial centre]' (after "
                       "'rule 3.3'), missing spaces, '*' line marks and the footnote marker '1' after (6)(c). No amendment bracket; the official text lists no "
                       "amendment for 3.4. No substantive difference. The raw text also held the p.3-21 footnote block, kept here verbatim: "
                       "'1Quite similar to previous 1981 R.237(c), first enacted by Alta.Reg.313/81.It was probably designed to move foreclosure actions to "
                       "the smaller judicial centres when the land is located there.' (footnote 1, history of 3.4) and '2Quite similar to previous 1968 R.12.It "
                       "came from 1944 C.R.14.Cf.Ont.1897 R.132.See further on the history,Pac.Inv.& Dev.v.Wood Buffalo (R.M.)2017 ABQB 469, JC FtMcM 1713 0016 "
                       "(Jul 26).' Footnote 2 is the history note of rule 3.5, not of 3.4: its marker '2' is printed after 3.5's text (line 464) and Book B's "
                       "3.3 commentary calls 3.5 'similar to previous Rule 12'."},
    "3.3": {"BOOK_A": "same wording as official 3.3(1)-(3) (read by eye) except missing spaces, '*' line marks and the footnote marker '1' printed after "
                       "subrule (3). No amending regulation on either side (the official text carries none for 3.3). No substantive difference."},
    "3.2": {"BOOK_A": "same wording as official 3.2(1)-(6) (read by eye) except: editorial bracket labels '[Determining the appropriate "
                       "judicial centre]' (after 'rule 3.3') and '[Resolving Issues and Preserving Rights]' (after 'Part 6'), the title in "
                       "capitals ('How to Start an Action'), many spaces missing, and the amendment bracket '[Alta.Reg.124/10;143/11]' (official "
                       "'AR 124/2010 s3.2;143/2011'). No substantive difference. The dropped raw text also held the page 3-4 footnote block, "
                       "printed between clauses (2)(e) and (2)(f); kept here verbatim because Book A's footnotes are not split out: "
                       "'1M & D Farm v.Man. Agric.Cr.Corp.[1999] 2 SCR 961, 245 NR 165. 2C.I.B.C.v.Green2015 SCC 60, [2015] 3 SCR 802. "
                       "3Bara Academy of Bus.Sci. v.R.2001 ABCA 4 (28 Nov '00, filed 5 Jan '01),leave den(SCC 2001) 276 NR 396. "
                       "41384034 Alta.v.1180263 Alta.2011 ABQB 599, [2011] AR Uned 663 (Sep 29). 5Homestead Housing Co-op.v.Barth(M) 2016 ABQB 538, "
                       "JCE 1503 12328 (Sep 27). 6Keaton v.Keaton2017 ABQB 429, JCE 4803 180087 (Jul 10).' These six footnotes follow the "
                       "commentary text that opens page 3-4 before the 3.2 title; which rule that text belongs to is not shown in the file."},
}
MANUAL_SEE_ALSO = {
    "3.1": ["Official text searched for 'rule 3.1', 'Rules 3.1' (including line-wrapped forms) and form headings '[Rule 3.1]': the only hit is the running page header 'Rule 3.1 AR 124/2010' (pdf p.49). No official rule cites 3.1 by number. BOOK_B (parts 01-10) has no 'Commentary § 3.1' section; BOOK_C's only text mentioning '3.1' is a displaced running head (see BOOK_C flag).",
            "FLAG on BOOK_A R.4.3 (p.4-6, line 207, Related Provisions): '3.1 (statement of defence)'. Official 3.1 is 'Rules govern Court actions'; 'Statement of defence' is the official title of rule 3.31, and official 4.3(3) speaks of a statement of defence being filed. The sources do not say which number was meant.",
            "BOOK_A related provisions that list 3.1 (searched all Book A files, line-wrapped forms included): R.1.1 (p.1-3, lines 45-46, wrapped) and R.1.7 (p.1-33, line 1624), both '3.1 (rules govern all proceedings)' - the label paraphrases the official title 'Rules govern Court actions'. Official 1.1(1): 'These rules govern the practice and procedure in (a) the Court of King's Bench of Alberta, and (b) the Court of Appeal of Alberta'.",
            
        "BOOK_A has no rule text or notes for 3.1: its Part 3 file starts at book page 3-4 (pages 3-1 to 3-3 are not in it). Book A pointers into its own missing 3.1 note: p.3-7 (line 85, footnote text) 'See Sabir v. Gill and commentary on it, in R.3.1n.' and p.13-70 (line 3653, under R.13.13) 'See Sabir v. Gill, R.3.1 n.'; the case is cited in full at p.3-43 fn 1 and p.3-45 fn 8 (Sabir v. Gill 2023 ABKB 679) and at p.13-68 fn 3 and p.13-102 fn 4 (Part 13). The 3.1 note itself cannot be checked.",
    ],
    "3.2": ["Official text (searched for 'rule 3.2', 'Rules 3.2', line-wrapped forms and form headings): rule 12.16(1) 'Despite rule 3.2(1), a proceeding under the Family Law Act must be started by filing a claim in Form FL-10'; Form 5 is headed '[Rule 3.2]' (3.2(4) and (5) name Form 5). Read by subject: 1.4 (procedural orders), 1.5 (rule contravention, non-compliance and irregularities), 3.8, 3.12 and 3.15 (named in the information note), 3.24(1) (set aside instead of declaring; cited in BOOK_A p.3-16, statement matches), 14.5(1)(j) and (4) (appeals by declared vexatious litigants; the subject of BOOK_B sections 1-2).",
            "BOOK_A other Parts that cite 3.2 (all combined*.txt searched, line-wrapped forms included; page markers checked): R.1.3 p.1-12 'On the jurisdiction to grant declarations, see R.3.2n.' (3.2 Part C is about declarations); R.1.5 related provisions p.1-25 '3.2(6) (wrong form of action)'; R.6.3 related p.6-14 '3.2(3) (applications under statute)' and text p.6-22 'Rule 3.2(3) governs special statutory applications'; R.6.5 related p.6-57 '3.2 (starting an action)'; R.6.55 related p.6-166 '3.2(3) (commencing originating applications)'; R.9.21 related p.9-65 '3.2(3) (application in an action)'; R.12.7 (p.12-8) and R.12.8 (p.12-11) related '3.2 (commencing an action)' plus 'See a long note about the dangers, in R.3.2n. B.4.' (B.4 Procedure and Parties is about booking, filing and email-filing difficulties: pointer lands); R.12.9(1) related p.12-12 '3.2 (commencing an action); 3.69 (joining claims)'; R.12.10 p.12-13 'rule 3.2 [How to start an action], rule 3.25 [Contents of statement of claim]'; R.12.16 text p.12-18 and related p.12-19 '3.2 (commencing an action)' with commentary 'Rule 12.16(1) (claim form) expressly overrides R.3.2(1)' (mirrored in 3.2 A.3, p.3-8; official 12.16(1) confirms); R.14.75 p.14-221 'decision under R.3.2(2) is partly discretionary and is owed some deference ... whether the preconditions in R.3.2(2) are met is a question of law'. Inside Part 3: p.3-26 (line 586, R.3.8 note) 'see Sabir v. Gill and comments, in R.3.2n., supra' while 3.2's own p.3-7 fn 2 sends the reader on to R.3.1n. (the only mention of Sabir in 3.2's note).",
            "FLAG on BOOK_A wrong-form pointers to 3.2(4): R.13.16 related provision '3.2(4) (wrong form of action)' (p.13-72, lines 3767-3769), R.1.5 fn 6 'Warnke v. Skrobek ... (striking out vs. summary judgment) and R.3.2(4)' (p.1-27, line 1347) and R.6.25 fn 8 'Grey v. Edmonton (City) 2005 ABQB 231 ... and R.3.2(4)' (p.6-124, line 6557). Official 3.2(4) fixes the form of an appeal or reference; the power to correct and continue an action started in the wrong form is 3.2(6) (which R.1.5's related provisions, p.1-25, list as '3.2(6) (wrong form of action)'). The sources do not say which subrule was meant. Grey v. Edmonton (City) is also cited in BOOK_A 3.2 p.3-6 fn 7.",
            "BOOK_B: the single commentary section (§ 3.2:1) has five numbered parts printed 1, 2, 3, 4, 4 (two parts are numbered 4: 'Notice of the Proposed Order Must be Given to the Party To Be Affected' and 'Family Law Matters'); no number is missing. Rules quoted inside it match the official text: 6.4(b) (notice not required if serving notice might cause undue prejudice to the applicant) and 14.5(4) (no appeal under (1)(j) from an order denying a vexatious litigant permission to institute or continue proceedings). The Karas v. Mongeon quotation (para 22) says rule 3.2 provides that actions are commenced 'only by Statement of Claim, Originating Notice, and Notice of Appeal'; official 3.2(1)(b) says 'originating application' ('originating notice' appears only in (2)(d)). BOOK_B rule text = official; its amendment note 'Alta. Reg. 143/2011, s. 3' agrees with official 'AR 124/2010 s3.2;143/2011'.",
            "Cross-book links (same decisions in more than one book): Karas v. Mongeon 2018 ABQB 149 (BOOK_B section 4; BOOK_A p.3-8 fn 2; BOOK_C rule 12.16 commentary); Kwadrans v. Kwadrans 2023 ABCA 203 (BOOK_B section 4 at para 25 and § 1.5:1 at para 35; BOOK_A p.3-7 fn 6 at paras 12-20; BOOK_A p.3-7 also says a Notice to Attend Family Docket Court is not a commencement document, as in BOOK_B); Blackburn v. Boucher 2018 ABQB 509 (BOOK_A p.3-8 fn 4; BOOK_C rule 12.16); Shell Can. Prods. v. Sunterra Beef (BOOK_C 3.2 commentary: 2013 ABQB 193 and 2014 ABCA 243; BOOK_A p.3-6 fn 6, p.3-8 fn 10, p.3-11 fn 4); Genstar Development v. Plains Midstream 2012 ABQB 457 is in BOOK_C only (no Book A file and no Book B file names Genstar). The README note that Shell and Genstar were seen as displaced 'Part 3 venue cases' in Part 2 (2.28, 2.29) is consistent: they recur here, in BOOK_C's 3.2 commentary about originating application versus statement of claim (not about venue).",
            
        "BOOK_B other Parts and BOOK_C (all rule*_part*.json and all Book C page files searched for 'rule 3.2', 'R.3.2', '[Rule 3.2' and 'label' forms): BOOK_B rule 12.16 text ('Despite rule 3.2(1)...') and BOOK_B rule 1.5 commentary (§ 1.5:1, quoting Kwadrans v. Kwadrans, 2023 ABCA 203, para 35: 'The general rule 1.5 should not be invoked if there is a specific rule that addresses the issue in question, here being rule 3.2(6)'). BOOK_C rule 12.16 (file 541-560): text 'Despite rule 3.2(1)' and commentary 'rule 12.16 overrides the requirement in rule 3.2(1) that an action be commenced only by statement of claim, originating notice, or notice of appeal' (citing Blackburn v. Boucher 2018 ABQB 509 para 25 and Karas v. Mongeon 2018 ABQB 149 paras 22-23) - the wording 'originating notice' is the same as in the Karas quotation; official 3.2(1)(b) says 'originating application'.",
    ],
    "3.3": ["Official text (searched for 'rule 3.3', 'rules 3.3', lists such as 'and 3.3', line-wrapped forms and form headings '[Rule 3.3]'): 3.2(1) ('determined under rule 3.3'), 3.4(1) ('Despite rule 3.3, if possession of land is claimed ...') and 15.13 ('The coming into force of rules 3.3 and 3.4 does not operate to require an existing proceeding to be carried on in a different judicial centre ...'); no form heading names 3.3. By subject: 3.5 (transfer of action), 3.6 (where an action is carried on; 3.6(2) place of hearing or trial), 3.7 (post-judgment transfer), 14.8(5) and 14.84 (place of filing appeals) and the Appendix definition of 'judicial centre' (the office of the Court in Calgary, ...).",
            "BOOK_A other Parts and Part 3 (all combined*.txt searched, line-wrapped forms included; pages from the page markers): R.15.13 (p.15-9, text 'rules 3.3 and 3.4' and related provision '3.3 (venue)'; official 15.13 confirms the text); R.10.49 note (p.10-140, line 7485) 'Rule 3.3 requires commencement in the judicial centre specified, without waiting for the defendant to complain. The court can transfer the suit ... with costs under R.10.49' (the same sentence as in 3.3 p.3-20 with R.10.47; see the 3.3 flag); R.13.15 information note (p.13-72, line 3750) 'The appropriate judicial centre is determined under rule 3.3 [Determining the appropriate judicial centre] and rule 3.4 [Claim for possession of land]' (both titles match the official); R.13.12 checklist (see FLAG). Inside Part 3: 3.2(1) text (p.3-4); 3.4 text (p.3-20, 'Despite rule 3.3 [Determining the appropriate judicial centre]') and related provision '3.3 (determining the appropriate judicial centre)' (p.3-21, line 463); notes at p.3-22 (lines 475 and 489: 'Rule 3.3 creates a presumption that the location R.3.3 dictates is the suitable one'; 'Rule 3.3 does not allow a chambers judge to pick the place of trial on wide or ...') and p.3-23 (line 521: 'Rule 3.3 prevents abusive choice of a plainly unsuitable court-house') - the first two are in 3.5's note (under the running head R.3.5), the third in 3.6's note (it opens 'Different Rules and tests apply to three different questions'); corrected/settled in the 3.4 pass.",
            "FLAG on BOOK_A R.13.12 (p.13-66, lines 3475-3477, checklist for a statement of claim): '2. Is the Judicial Centre selected by the statement of claim suitable? Should one move to change it? (Rr.3.3, 3.36)'. Official 3.36 is 'Judgment in default of defence and noting in default'; the transfer rules are 3.4 and 3.5. The sources do not say which number was meant. In the same checklist '1. Is the place of trial suggested in the Statement of Claim acceptable? (R.3.3)' is consistent with 3.3.",
            "BOOK_B: one commentary section (§ 3.3:1) with one numbered part ('1. Old Cases Apply to R. 3.3(2)'), ending at Odland para 13. Its list of what rule 3.3 requires (Odland para 7, i-iv: r 3.3(1)(a), (1)(b), (2), (3)) and the quoted paragraphs match the official 3.3 subrules; the rule 3.5 it mentions ('similar to previous Rule 12') is 'Transfer of action' in the official text.",
            "Cross-book links: 325303 Alberta Ltd. v. Prime Property Management, 2011 ABQB 817, 531 AR 204 (BOOK_B 3.3 commentary; BOOK_A p.3-19 fn 2 and p.3-20 fn 5 'supra'); Apache Canada Ltd. v. Johnson, 2005 ABCA 71, 363 AR 100 (BOOK_B; BOOK_A p.3-19 fn 4, p.3-20 fn 4 'supra'); Odland v. Odland, 2017 ABCA 397 (BOOK_B 3.3 and 3.5 commentary; BOOK_A p.3-19 fn 6 and p.3-22 fn 2; BOOK_C 3.5); Lim v. Young 2004 ABQB 489, 360 AR 277 (BOOK_B inside the Apache quotation; BOOK_A p.3-20 fn 1); Nat. Hldg. v. Blair, 2009 ABQB 351 (BOOK_A only: p.3-19 fn 5, p.3-20 fn 1 and 3). BOOK_A 'Calgary cannot be the proper judicial centre if both parties reside in Edmonton' matches Odland paras 12-13 in BOOK_B; BOOK_A 'the place in R.3.3 is presumed correct' matches Odland (onus on the defendant if the plaintiff complied with 3.3, on the plaintiff if not). BOOK_B rule text = official and BOOK_C rule text = official (both read by eye); the amendment note in BOOK_C ('Alta. Reg. 124/2010, r. 3.3 effective November 1, 2010') and the absence of one in BOOK_A and BOOK_B agree with the official text, which lists no amendment.",
            
        "BOOK_B and BOOK_C other Parts (all Book B files and all Book C page files searched): BOOK_B rule 15.13 text and BOOK_C rule 15.13 (file 721-740) 'The coming into force of rules 3.3 and 3.4 does not operate to require an existing proceeding to be carried on in a different judicial centre ...'; BOOK_B 3.5 commentary (§ 3.5:1) quotes Odland v. Odland para 19 ('rule 3.3 operates much like a presumption'); BOOK_C's 3.5 entry cites Regular v. Regular 2016 ABQB 570 para 5 and Odland for the same point ('Rule 3.3 operates \"much like a presumption\"') [corrected in the 3.4 pass: an earlier version of this note put both under 3.4]; BOOK_C's 3.4 entry also has the running head 'R. 3.3 50' inside 3.4(1)(b) (to note in the 3.4 pass). BOOK_C 3.2 commentary says the originating application may be used only where 'the requirements of rule 3.3(2) are met' - flagged under 3.2 (the exceptions are in 3.2(2)).",
    ],
    "3.4": ["Official text (searched for 'rule 3.4', 'rules 3.3 and 3.4', lists, line-wrapped forms and form headings): 3.6(1)(b) ('if the action is transferred in accordance with rule 3.4 or rule 3.5'), 15.13 ('rules 3.3 and 3.4') and Form 6, headed '[Rule 3.4]': its notice to the plaintiff says the court clerk transfers the action 'unless these facts as stated by the defendant(s) are incorrect, the pleadings in this action have closed, or one of the exceptions ... applies', warns that the clerk will transfer if the plaintiff does not respond 'within 10 days of service', and its notice to the clerk lists exceptions (a)-(c) that mirror 3.4(6)(a)-(c) plus '(d) an objection has been filed under rule 3.4(4)'. Also fee item 3.4 of Schedule B (rule 13.x fee waiver) is not this rule. By subject: 3.5 (transfer of action), 3.6, 3.67 ('Close of pleadings', named in the information note; 3.4(3)(a) requires the request before close of pleadings), Appendix definitions of the Defined Terms.",
            "BOOK_A other Parts and Part 3 (all combined*.txt searched, line-wrapped forms included; page markers checked): R.13.15 information note p.13-72 'rule 3.4 [Claim for possession of land]' (title matches official); R.9.30 (p.9-74, line 3841) and R.9.37 (p.9-89, line 4612) related provisions '3.4 (venue)' - a short paraphrase of 'Claim for possession of land' (the rule moves a possession-of-land action to the centre closest to the land); R.15.13 (p.15-9); 3.3 related provision '3.4 (claimingland)' (p.3-19, line 413); 3.5 note p.3-22 line 500 '(subject to claims for possession of land to R.3.4)'; 3.6(1)(b) text p.3-23 'rule 3.4 [Claim for possession of land]'. FLAG on BOOK_A 3.5 related provision '3.4(2) (transfer of action where land claimed)' (p.3-21, line 466): official 3.4(2) says what the request must contain (the centre to which the action is to be transferred, and the reason); the label describes rule 3.4 as a whole. Book A R.13.x fee text 'item 1 or 3.4 respectively of Schedule B' (p.13-99, line 5141) is fee item 3.4, not this rule.",
            "BOOK_B and BOOK_C other Parts (all Book B files and all Book C page files searched, JSON text only): BOOK_B 3.6(1)(b) ('transferred in accordance with rule 3.4 or rule 3.5') and 15.13; BOOK_C 15.13. The other hits for '3.4' in B and C (rule 13.x fee waiver 'under item 1 or 3.4 respectively of Schedule B') are fee item 3.4, not this rule. Neither book has a commentary that cites 3.4 by number.",
            
        "Cross-book: Pac. Inv. & Dev. v. Wood Buffalo (R.M.), 2017 ABQB 469 appears in BOOK_A p.3-19 fn 1 (3.3) and p.3-21 fn 2 (3.5's history note); Regular v. Regular 2016 ABQB 570 and Behiels v. Tibu belong to 3.5 (BOOK_C's 3.5 entry and BOOK_A p.3-22 fn 3, fn 8), not to 3.4. Book B's source prints the 3.4 rule text twice (paragraphs part3_part_3_court_actions_012 and _014, identical, 1,601 characters, headings repeated); the builder now keeps one copy and logs the skipped paragraph; the text equals the official 3.4.",
    ],
    "3.5": ["Official text (searched for 'rule 3.5', 'rules 3.4 and 3.5', lists, line-wrapped forms and form headings): only 3.6(1)(b) ('if the action is transferred in accordance with rule 3.4 or rule 3.5, continued in the judicial centre to which the action is transferred') and the table of contents; no form heading names 3.5. By subject: 3.3 (appropriate judicial centre), 3.4 (transfer by request, land), 3.6, 3.7 (temporary transfer after judgment), 1.4(2) (procedural orders) and the Appendix definition of 'judicial centre'.",
            "Other books (all Book A files, all Book B files, all Book C page files searched; line-wrapped forms included): no Book A file outside Part 3 cites 3.5 (no 'R.3.5', 'rule 3.5' or '3.5 (label)' in Parts 1, 2, 4-15); in Part 3 it is cited in 3.3 (p.3-19 lines 413, 415, 419; p.3-20 fn 7 'See R.3.5'), in 3.4's related provisions (p.3-21, line 463), in 3.6's text (p.3-23, line 518) and note (line 523: 'R.3.5 lets the court change the judicial centre at any time'). Notes at p.3-22 lines 475 and 489 ('Rule 3.3 creates a presumption ...'; 'Rule 3.3 does not allow a chambers judge to pick the place of trial on wide or vague grounds') are in this rule's note; the sentence at p.3-23 line 521 ('Rule 3.3 prevents abusive choice ...') is in 3.6's. BOOK_C: 3.6(1)(b) text only. BOOK_B: 3.6(1)(b) and the 3.26 list above.",
            "BOOK_B: one commentary section (§ 3.5:1) with five numbered parts printed 1 Venue for Application, 2 Onus of Proof and Balance of Convenience, 3 Procedure, 4 Location of Counsel's Office, 5 'Unreasonable' - no gap. BOOK_B rule text = official; no amendment note on either side. BOOK_B rule 3.26 commentary (rule3_part03_document.json, after the 3.26 heading) lists '3.5 (transfer an action)' among rules that do not mention affidavits but whose applications would need affidavit evidence.",
            
        "Cross-book links (same decisions in more than one book): Odland v. Odland 2017 ABCA 397 (BOOK_B paras. 19-22, 24; BOOK_C; BOOK_A fn 2, fn 5, fn 8 and p.3-19 fn 6); Regular v. Regular 2016 ABQB 570 (BOOK_B, quoted at paras. 5-9; BOOK_C; BOOK_A fn 3 and 8); Behiels v. Tibu 2024 ABKB 12 (BOOK_A fn 3, 4, 7, 9-13; BOOK_B 'See also: Behiels v. Tibu, 2024 ABKB 12 for a review of the law'; BOOK_C prints only the name); Sobeys Capital Inc. v. Gulf & Pacific Equities Corp. 2018 ABQB 151 (BOOK_A fn 3; BOOK_C, name in pieces); Pac. Inv. & Dev. v. Wood Buffalo (R.M.) 2017 ABQB 469 (BOOK_A; BOOK_B 'Pacific Investments' inside Odland); Christensen v. Proprietary Industries 2002 ABQB 97, Wickstrom v. Wetter 2007 ABQB 402, Schafer v. Lenhardt 1998 ABCA 47, Silver Springs Oil Recovery v. UMA Eng. 2004 ABQB 942, C.S. v. A.J. 2004 ABQB 73 (BOOK_B in Regular para 7; BOOK_A; C.S. v. A.J. is at BOOK_A p.3-24 fn 2); Siver v. Siver 2010 ABQB 755 (BOOK_B in Regular paras. 6-9; BOOK_A p.3-23 line 525, in the note on 3.6: 'There is a curious decision, using instead a lax almost subjective test'); Keaton v. Keaton 2017 ABQB 429 (BOOK_B in Odland para 22; BOOK_A p.3-24 fn 1 and p.3-4). BOOK_B's Regular para 5 cites Abou-Morad v. Aboumourad 2015 ABQB 584 and 325303 Alberta v. Prime Property Management 2011 ABQB 817 (both also in BOOK_A p.3-19/3-20, note 3.3). BOOK_A p.3-22 'An application under this Rule is brought in the judicial centre in which the proceedings were commenced' matches BOOK_B section 1 (Royal Trust Corp. of Canada v. Fillo).",
    ],
}
MANUAL_BOOK_A_COMMENTARY_FLAGS = {
    "3.5": "Read in full, pp.3-22 to 3-23 (lines 473-515). (1) Extraction: columns are out of order and words are split across paragraphs (e.g. "
           "'the respondent's financial resources (inter' ... 'alia) are'; 'The test in R.3.5(2) is' ... '\"unreasonable\", and case law differs on how strict a "
           "test that should be'); footnotes are not split out. Page 3-22 has footnotes 1-13: 1 Lund (2017) 56 Alta LRev 429 (#4) ('See an article' is the "
           "first line of the commentary); 2 Odland v. Odland 2017 ABCA 397; 3 Regular v. Regular infra, Behiels v. Tibu 2024 ABKB 12 (para. 8), Sobeys "
           "Cap. Inc. v. Gulf & Pac. Eq. Corp. 2018 ABQB 151; 4 Behiels (para. 64); 5 Odland supra, Pac. Inv. v. Wood Buffalo supra, ibid; 6 'See n.7 "
           "below'; 7 Behiels (para. 15(7)); 8 Regular 2016 ABQB 570, 46 Alta LR(6th) 385, Odland supra, Pac. Inv. & Dev. v. Wood Buffalo supra; "
           "9 Behiels (para. 16); 10 Behiels (paras. 30-31, 38); 11 Behiels (para. 51; printed early, line 481); 12 Behiels (para. 61); 13 Schafer v. "
           "Lenhardt 1998 ABCA 47, Silver Springs v. UMA Eng. (#2), J.T.A. v. D.G.K. 2001 ABQB 612, Behiels (para. 12). Page 3-23 has footnotes 1-6 for the "
           "last two paragraphs of 3.5 and the note on 3.6: footnote 4 ('Quite similar to previous 1968 R.715, 716 ...') is 3.6's history note (its marker "
           "is printed after 3.6's text, line 519); which rule owns footnotes 2, 3, 5 and 6 (Christensen supra; Oleynik v. Univ. of Calg. 2011 ABCA 281; "
           "Ferguson v. Rubik 2002 ABQB 779; Min. of Justice v. Mohamed 2018 ABQB 897) is not shown, and they are the last lines of the built 3.5 commentary, "
           "followed by 3.6's title. "
           "(2) p.3-22 line 487: 'The test in R.3.5(2)' - official 3.5 has no subrule (2); the test is in clause (a) ('unreasonable'). "
           "(3) p.3-23 line 513: 'If one litigant lives outside the province, then the other litigant probably has a right to have the proceedings "
           "transferred to the city where it and its counsel are' - official 3.5 says the Court 'may order' a transfer, and the same note says the location "
           "of counsel is 'not an important factor' (line 477) and 'not a significant factor' (line 493); Book B (Odland para 22, Regular para 8) says "
           "the location of counsel is not decisive. The book does not reconcile them. "
           "(4) Related Provisions has one entry, '3.4(2) (transfer of action where land claimed)': official 3.4(2) is what a request must contain; the label "
           "describes rule 3.4 as a whole. 3.3 is not listed although the note relies on it (see 3.3's list, which names 3.5). "
           "(5) supra/infra: Regular infra (fn 3) -> fn 8; Behiels (fn 4, 7, 9-12 supra) -> fn 3 (2024 ABKB 12); Odland supra (fn 5, 8) -> fn 2; Pac. Inv. supra "
           "(fn 5, 8) -> p.3-19 fn 1 and p.3-21 fn 2 (2017 ABQB 469); Wickstrom v. Wetter supra (fn 13 text) -> line 485 (2007 ABQB 402, 419 AR 393); "
           "Christensen v. Proprietary Ind. supra p.3-23 fn 2 -> p.3-23 fn 1 (2002 ABQB 97, 309 AR 201, line 525). The Silver Springs Oil Recovery v. UMA "
           "Eng. (#2) citation is split: '2004' at line 500 and 'ABQB 942' at line 485 (Book B, Regular para 7, gives 2004 ABQB 942). "
           "(6) Statements without a source in the repository (not checked): a suit can be transferred after most discoveries but before trial; the court may "
           "decline to transfer until after a summary judgment is dealt with; the summary of an unnamed case (plaintiff's officer in Texas, defendants in "
           "Calgary, Red Deer majority). The history footnote (p.3-21 fn 2: previous 1968 R.12) agrees with Book B's quotation 'Rule 3.5 (which is similar to "
           "previous Rule 12)'. Defined Terms 'Court, judicial centre' match the Appendix.",
    "3.4": "Book A has no commentary for 3.4: after the related provisions the text of 3.5 follows at once (p.3-21, line 463; the title '*Transfer of "
           "Action*' printed there is 3.5's and ends the built related-provisions field); the notes on pp.3-22 to 3-23 are under the running head R.3.5 "
           "and, from line 521, R.3.6. Read in full: rule text, footnotes 1-2 (p.3-21; two footnotes on that page; see the text note for whose they are), "
           "information note 'Close of pleadings is a time determined under rule 3.67 [Close of pleadings]' (official 3.67 is 'Close of pleadings'), "
           "Defined Terms 'Court, court clerk, defendant, file, judicial centre, order, pleading' (the Appendix defines each), and Related Provisions "
           "'3.3 (determining the appropriate judicial centre); 3.5 (transfer of an action)' (both match the official titles). Footnote 1 says the rule "
           "'was probably designed to move foreclosure actions to the smaller judicial centres'; a conjecture, no source. The 3.5 related provision "
           "'3.4(2) (transfer of action where land claimed)' is flagged under see_also.",
    "3.3": "Read in full, pp.3-19 to 3-20. (1) Title and footnotes: the Book A title 'Determining the Appropriate Judicial Centre' (p.3-19 line 406) and "
           "footnotes 2-6 of this commentary (lines 404-407) are printed BEFORE the rule text and so sit at the end of the built 3.2 commentary (see the 3.2 "
           "flag); footnote 1 (history: 'Quite similar to previous 1996 R.6.1, first enacted by Alta.Reg.243/96 ...', line 421) follows the text; p.3-19 has "
           "footnotes 1-6 and p.3-20 has footnotes 1-9 (1 Lim v. Young; 2 Ferguson v. Rubik; 3 Nat. Hldg. v. Blair; 4 Apache Can. v. Johnson supra; 5 'See n.4, "
           "supra' and 325303 Alta. v. Prime supra; 6 Tuckanow v. Bowden Penitentiary; 7 'See R.3.5' and Kristal v. Nicholl & Akers; 8 Abou-Morad; 9 Acciona). "
           "Their supra/infra references resolve: Blair, Apache and 325303 Alta. supra (p.3-20) go back to p.3-19 fn 5, 4 and 2. The paragraphs are out of "
           "order (a sentence resumes after a footnote block, e.g. 'Rule 3.2(1) requires commencement ... without waiting for the' / 'defendant to complain'). "
           "(2) p.3-20: 'Rule 3.3(3)(c) on suing in the \"wrong\" Judicial Centre by consent' - official 3.3(3) has no clauses; the summary of rule 3.3 quoted "
           "in Book B (Odland, para 7) cites only 3.3(1)(a), (1)(b), (2) and (3). "
           "(3) p.3-20: 'with costs under R.10.47' - official 10.47 is 'Liability of litigation representative for costs'; Book A's R.10.49 note (p.10-140, "
           "official 10.49 'Penalty for contravening rules') carries the same sentence ('Rule 3.3 requires commencement in the judicial centre specified, "
           "without waiting for the defendant to complain ... with costs under R.10.49'), so the number here looks wrong; not shown which was meant. "
           "(4) Related Provisions read against the official titles and text: '6.11 (electronic hearings)' (also in the commentary, p.3-20) - official 6.11 is "
           "'Evidence at application hearings'; the electronic hearing rule is 6.10 ('Electronic hearing'; official 8.18 says 'On application under rule "
           "6.10, the Court may permit an electronic hearing'); '13.41 (fax filing)' - official 13.41 is 'Authority of court clerk' and (2)(a) says 'if sent "
           "by electronic means, including by electronic mail'; the word 'fax' is not in 13.41; '3.4 (claimingland)' as printed lacks a space. In subject "
           "they agree with the official: 3.2, 3.4, 3.5 ('Transfer of action'), 3.6(2) (an application or originating application may be heard, or a trial "
           "held, in a place specified by the Court other than the judicial centre), 8.18 ('Trial conducted by electronic hearing'), 11.21, 14.8(5) (appeals "
           "from Calgary, Drumheller, Lethbridge, Medicine Hat and Red Deer are filed in Calgary, others in Edmonton), 14.84 ('Place of filing') and 15.13. "
           "(5) p.3-19: 'The defendants have a right to have it moved there if it was not started there' - official 3.5 says the Court 'may order' a transfer "
           "(a) if it would be unreasonable for the action to be carried on where it is, or (b) at the request of the parties; a defendant's right to have "
           "an action moved by request (Form 6) is in 3.4 and only for claims for possession of land. The same page says (p.3-20 fn 7) that the rule 'is not "
           "mandatory, and the judge has a discretion'. "
           "(6) File number of Pac. Inv. & Dev. v. Wood Buffalo (R.M.) 2017 ABQB 469: p.3-19 fn 1 prints 'JC FtMcM 1713 00116', footnote 2 on p.3-21 (3.5's history note: its marker '2' is printed after 3.5's text) prints "
           "'JC FtMcM 1713 0016'; which is right is not shown. "
           "(7) Statements of law without a source in the repository (not checked): residence is fixed when the action starts; poverty is no ground to move "
           "a suit; exclusive King's Bench jurisdiction over consolidating arbitrations (last paragraph). The Defined Terms line (Court, judicial centre, "
           "party, rules) matches Appendix definitions of 'Court', 'judicial centre' and 'party'.",
    "3.2": "Read in full, pp.3-4 to 3-19. (1) Extraction order: paragraphs and footnote blocks are out of order (sentences resume after other "
           "paragraphs, e.g. p.3-7 to 3-8) and footnotes are not split out; footnote numbers restart on each page (p.3-6 has 12, the twelfth "
           "printed at the top of the page; p.3-7 has 11; p.3-8 has 10). Headings as printed: A.General (1 Effect of Errors in Commencement, "
           "2 Examples, 3 Miscellaneous); B.Originating Applications (1 General, 2 No Disputed Facts, 3 Opposing an Originating Application, "
           "4 Procedure and Parties); C.Declarations (1 General with (a) Introduction and (b) Tests, 2 Declarations Against the Crown, "
           "3 Miscellaneous); D.Controverted Elections. "
           "(2) p.3-6: 'it should be continued as a statement of claim under R.3.2(4)' - official 3.2(4) fixes the form of an appeal or reference; "
           "the power to correct and continue an action started in the wrong form is 3.2(6) (see see_also for the same pattern in other rules). "
           "(3) p.3-6: 'R.3.12 lets the court turn an Originating Application into a full-fledged suit', and Related Provisions '3.12 (converting "
           "application to trial)': official 3.12 lets the Court, on application, direct that the rules for an action started by statement of claim "
           "apply to the action started by originating application; it does not say the application becomes a suit. "
           "(4) Related Provisions as printed: '12.16 (commencing an action under theFamily Law Act;13.15 (when a document is filed)' - closing "
           "parenthesis missing after 'Act'. The labels for 1.5, 3.15, 3.68, 12.7-10, 12.16, 13.15 and 13.28 agree in subject with the official "
           "titles; 3.12: see (3). "
           "(5) p.3-13 fn 7 'TsuuT'ina Nation v. Min. of Env., supra': no full citation comes earlier in Book A (the name occurs first at p.3-13 fn 7, "
           "then in full at p.3-14 fn 5: 2010 ABCA 137, 482 AR 198), so 'supra' points forward. Other supra/infra/Ibid references in these pages "
           "resolve: O'Malley (#2) supra p.3-6 fn 4 -> fn 3 (2007 ABQB 574; a second decision under the same name, 2006 ABQB 364, is cited in "
           "full on pp.3-7 and 3-9); Shell Can. Prods. v. Sunterra Beef infra p.3-6 fn 6 -> p.3-8 fn 10; Kingsway infra p.3-9 fn 3 -> fn 4; "
           "TransAlta supra p.3-10 fn 5 -> p.3-9 fn 2; Re Hearing Office supra p.3-13 fn 8-9 -> fn 5; Nassichuk-Dean supra p.3-14 fn 2 -> p.3-13 fn 6; "
           "Sheila Holmes supra p.3-16 fn 9 -> p.3-8 fn 10; Leung v. Smith supra p.3-17 fn 9 -> fn 4; Sideleau supra p.3-18 fn 6 -> fn 5. "
           "(6) p.3-17 fn 7: 'the Act does not apply to time limits in the Rules: R.1.8' - official 1.8 applies the Interpretation Act except "
           "sections 10, 12, 22(3) to (8), 23 (service of documents) and 26(1); it does not say the Act does not apply to time limits generally. "
           "(7) p.3-19 lines 404-407 belong to rule 3.3, not 3.2 (found in the 3.3 pass; this CORRECTS the first version of this flag, which "
           "put footnotes 4-6 with D.Controverted Elections): the footnotes '4 The tests are reviewed in Apache Can. v. Johnson 2005 ABCA 71, "
           "363 AR 100. 5 Nat. Hldg. v. Blair (M) 2009 ABQB 351. 6 Odland v. Odland 2017 ABCA 397, Calg 1701 0127 AC (Nov 27)' at the top of "
           "the page, the title 'Determining the Appropriate Judicial Centre' (the title of 3.3) and the footnotes '2 325303 Alta. v. Prime Prop. "
           "Mgmt. 2011 ABQB 817, 531 AR 204' and '3 Many of the old boundaries seemed influenced by railway lines' are printed before the text of "
           "3.3, and the built 3.2 commentary ends with them. Evidence: footnotes 1, 4 and 5 on p.3-20 (3.3's commentary) cite Blair, Apache and "
           "325303 Alta. as 'supra', and Book B's 3.3 commentary discusses Apache, Prime Property and Odland. "
           "(8) Pointers: p.3-13 '(a) Introduction See also R.3.24n.B.' points into Book A's note at R.3.24 (to be checked in the 3.24 pass); "
           "p.3-8 'Part A.1 above' (A.1 says an Originating Application is never compulsory) and p.3-7 'See further Part B below' resolve; "
           "p.3-10 fn 7 'R.3.2n.A.2.' resolves (Elite, Shell and Sheila Holmes are in A.2, p.3-8 fn 1 and 10). p.3-7 fn 2 'See Sabir v. Gill and "
           "commentary on it, in R.3.1n.' points to the 3.1 note, which is not in the file. "
           "(9) The sentence after the Related Provisions on p.3-5 ('The ceiling for civil suits in the Alberta Court of Justice went up to $50,000 in "
           "August 2014 ... In 2022 the ceiling went up to $100,000') is about another court's jurisdiction, not in the Rules; not checked against "
           "any source in the repository. Rule numbers cited in the commentary: official text read for 3.2(3), 3.2(4), 3.2(6), 3.12, 3.24(1) "
           "(set aside instead of declaring - matches), 12.16(1) (matches) and 1.8; official title only for 1.4, 1.5, 3.8, 3.15, 3.68, 7.3, 9.24, "
           "13.13, 13.16 (not read for the proposition each is cited for).",
}
MANUAL_BOOK_A_FOOTNOTE_FLAGS = {
}
MANUAL_BOOK_C = {
    "3.5": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): it stops after clause (a) ('... in which it is "
                          "located, or') and clause (b) 'at the request of the parties.' is missing; the rest equals the official text. Amendment note agrees "
                          "(no amending regulation).",
        "citation_names": {"2018 ABQB 151": "Sobeys Capital Inc. v. Gulf & Pacific Equities Corp."},
        "citation_notes": {"2018 ABQB 151": "printed in pieces: 'Odland v. Odland Sobeys , [2017] A.J. No. 1265, 2017 ABCA 397 paras. 20-23 (Alta. C.A.); and Inc. v. Gulf "
                                            "& Pacific Equities Corp., [2018] A.J. No. 231, 2018 ABQB 151 at paras. 3-19' and 'Odland v. Odland Sobeys , ... ; Capital Inc. v. "
                                            "Gulf & Pacific Equities Corp., [2018] A.J. No. 231, 2018 ABQB 151 at paras. 13-25'; the name is assembled from those pieces. "
                                            "Book A prints 'Sobeys Cap.Inc.v.Gulf & Pac.Eq.Corp. 2018 ABQB 151, JCC 1601 00082 (Feb 28)' (p.3-22 fn 3).",
                           "2016 ABQB 570": "Regular v. Regular; the 'Behiels v. Tibu' name printed after 'sought a change of venue:' has no citation here (Book A: 2024 ABKB 12; "
                                            "Book B names it in Odland para 22's follow-on sentence).",
                           "2017 ABCA 397": "Odland v. Odland; also in Book A p.3-19 fn 6 and p.3-22 fn 2, and quoted in Book B (paras. 19-24)"},
        "commentary_flag": "(1) 'Test for unreasonableness': 'the current judicial centre is unreasonable in the sense of being arbitrary or irrational ... "
                           "Regular ... para. 6, endorsed in Odland ... paras. 20-23' - Book B's quotations show that Regular para 6 describes the Siver v. Siver "
                           "plain-meaning line, that Regular para 7 says the weight of authority is a balance-of-convenience test, and that Odland para 20 says "
                           "'We endorse the approach that reasonableness is determined on the balance of convenience'; the endorsement claimed here does not match "
                           "those quotations (not adjudicated). (2) The passage 'the factors were considered and applied at length in a case where ... sought a "
                           "change of venue: , Behiels v. Tibu' is also printed, displaced, in Book C's 3.4 text (dropped there). (3) The commentary ends "
                           "'The location of counsel is not a decisive factor' with no full stop or citation (Book B, Odland para 22: 'Generally, the location of "
                           "counsel is not a decisive factor: Pacific Investments at para 38; Regular at para 8'). The factor list (a)-(e) plus the two lesser "
                           "factors (pre-trial motions, assets) agrees with Regular para 9 as quoted in Book B (a)-(g).",
    },
    "3.4": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): subrule (1) stops after 'to the Alberta residence "
                          "of' - the words 'a defendant, a defendant may, by making a request in Form 6, require the court clerk in the judicial centre in "
                          "which the action is located to transfer the action to the judicial centre that is closest, by road, to the land or the Alberta "
                          "residence of that defendant.' are missing and the running head 'R. 3.3 50' stands in their place; in (4) the number '10' is missing "
                          "('must file an objection within [ ] days') and this text stands there: 'Regular v. Regular, [2016] A.J. No. 1042, 2016 ABQB 570 at "
                          "para. 9 (Alta. Q.B.); the factors were considered and applied at length in a case where both plaintiff and plaintiff by "
                          "counterclaim had proposed Edmonton as place of trial; but many years later, after plaintiff had discontinued claim and plaintiff by "
                          "counterclaim had moved to Calgary, plaintiff by counterclaim sought a change of venue: , Behiels v. Tibu'. Subrules (2), (3), (5) and "
                          "(6) equal the official text; the amendment note agrees (no amending regulation).",
        "drop_citations": "Regular v. Regular, 2016 ABQB 570 (para. 9) is not a 3.4 authority: it stands where the number '10' is lost in 3.4(4), the text "
                          "beside it is about a change of venue ('sought a change of venue: , Behiels v. Tibu'), Book C's own 3.5 entry cites Regular (paras. 5 "
                          "and 6) for onus and the test, and Book A cites Regular and Behiels v. Tibu (2024 ABKB 12) in its 3.5 notes (p.3-22 fn 3 and fn 8). "
                          "Displaced from 3.5: Book C's 3.5 entry prints the same sentence (Regular at para. 9 ... 'sought a change of venue: , Behiels v. Tibu'), "
                          "which confirms the home. The text is kept in the rule-text note.",
        "drop_note_variant": "same words as Book A's information note; only the bracket label '[Close of pleadings]' is moved after the full stop ('rule 3.67 "
                             ".\n[Close of pleadings]'); not retained.",
        "commentary_flag": "the sentence agrees with Form 6 ('without the need to file an application': the request is made in Form 6), but it says the "
                           "defendant can 'force a plaintiff to litigate ... in the judicial centre closest to the land'; official 3.4(1) lets the defendant "
                           "name the centre closest to the land or the one closest to the defendant's Alberta residence, the transfer follows only if no "
                           "objection is filed within 10 days (3.4(4)-(5)), and 3.4(6) lists exceptions. Books A and B have no 3.4 commentary to compare.",
    },
    "3.3": {"commentary_flag": "the sentences that the rule 'has nothing to do with determining the appropriate forum in the face of competing "
                               "jurisdictions' and that the justices of the Court of King's Bench 'have jurisdiction over the entirety of the province' "
                               "are not in the official 3.3 text and are not repeated in Books A or B (both read for 3.3); not checked against any source "
                               "in the repository. Rule text, amendment note and (empty) information notes agree with the official 3.3 and Book A."},
    "3.2": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): it reads '3.3 [Determining the "
                          "appropriate judicial centre]:' for 'rule 3.3', has line-break hyphens ('specifi- cally', 'proce- dure'), and subrule (3) "
                          "is cut off after 'application to be made': the words 'to the Court, (a) if the application is made in an action in respect of "
                          "which a commencement document has been filed, the application must be made under Part 6 unless' are missing, and in their "
                          "place stand a footnote text ('2 George E. Woodbine, ed., , , ca. 1250, Bracton on the Laws and Customs of England Volume 2 "
                          "translated by Samuel E. Thorne (Cambridge: The Belknap Press of Harvard University, 1968-1977) at 282.'), the running head "
                          "'R. 3.1 48' and the bracket label '[Resolving Issues and Preserving Rights]'. The 3.1 commentary ends with footnote marker 2, "
                          "so the Bracton text is that commentary's footnote. Amendment note agrees with official (143/2011).",
        "drop_c_note": "garbled copy of the Book A information note (same words, out of order): the citation 'Thompson v. Procrane Inc (c.o.b. Sterling "
                       "Crane) . , [2016] A.J. No. 237, 2016 ABCA 71 at para. 9 (Alta. C.A.).' stands where the number '13' of 'Part 13' should be "
                       "(Book A: 'Part 13 [Technical Rules]'), the word 'pleadings' is printed after it (Book A: '(called pleadings)'), and "
                       "'[Originating and rule 3.15 application for judicial review]' is scrambled (Book A: 'rule 3.15 [Originating application for "
                       "judicial review]'); not retained. The same Procrane citation (para. 12) stands where a number is lost in Book C rules 10.47 "
                       "and 10.53; the sources do not show its home rule.",
        "commentary_flag": "'may only be used ... where the requirements of rule 3.3(2) are met': official 3.3(2) is about a party carrying on business "
                           "in more than one Alberta location; the enumerated exceptions are in 3.2(2), so the citation looks like 3.2(2) (the sources "
                           "do not say which was meant). Shell Canada Products v. Sunterra Beef (2013 ABQB 193; 2014 ABCA 243) is also cited in Book A "
                           "3.2 (p.3-8 fn 10; p.3-11 fn 4); Genstar 2012 ABQB 457 is in no other book.",
        "citation_names": {"2012 ABQB 457": "Genstar Development Co. v. Plains Midstream Canada ULC"},
        "citation_notes": {"2012 ABQB 457": "printed as 'See also , [2012] A.J. No. Genstar Development Co. v. Plains Midstream Canada ULC 755, "
                                            "2012 ABQB 457 (Alta. Q.B. (Master))': the name stands inside the A.J. number ([2012] A.J. No. 755); "
                                            "name and number re-ordered from the book's own text.",
                           "2013 ABQB 193": "also cited in Book A 3.2 p.3-8 fn 10 (554 AR 283)",
                           "2014 ABCA 243": "also cited in Book A 3.2 p.3-11 fn 4 (577 AR 280, leave den)"},
    },
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
