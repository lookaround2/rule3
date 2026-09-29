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
        marker = MANUAL_BOOK_B_SPLIT.get(r)
        if marker and marker in o["operative_text_raw"]:
            # manual decision: the source prints commentary in the same paragraph as the rule text, without a 'Commentary §' header
            k = o["operative_text_raw"].index(marker)
            o["commentary"].insert(0, {"section": None, "text": o["operative_text_raw"][k:].strip(),
                                       "note": f"printed in the same source paragraph as the rule text, with no 'Commentary §' header; split at '{marker}' (manual review)"})
            o["operative_text_raw"] = o["operative_text_raw"][:k].rstrip()
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
MANUAL_BOOK_B_SPLIT = {
    "3.8": "1. Test for amending originating application",
}
MANUAL_NOTE_FLAGS = {
}
MANUAL_TEXT_KEEP = {
    "3.15": {"BOOK_A": "raw text KEPT, not dropped (71,074 characters): Book A's extraction prints the whole 3.15 note (Parts A-G, footnotes 1-12 per page, unsplit and out of order) interleaved with the rule text, pages 3-32 to 3-52, combined rule3.txt lines 740-1374; that note text is in this packet only here, so it stays as extracted. Rule wording read by eye against the official text: (1)(a)-(b) printed at p.3-32 (lines 740-741), (2) at p.3-44 (line 1092), (3)(a)-(c) at p.3-49 (lines 1255-1264), (4) at p.3-49 (lines 1264-1265), (5) at p.3-52 (lines 1371-1372); each subrule is printed just before its own note. Same words as the official 3.15(1)-(5) except editorial: bracket labels '[Originating application for judicial review: habeas corpus]' after 'rule 3.16' (printed '[Originating application for judicial review:habeas**corpus]'; matches the official title of 3.16) and '[Variation of time periods]' after 'rule 13.5' (matches the official title of 13.5); missing spaces; '*' line marks; footnote markers ('5' after (1), '7' after (2), '1' after (4)). The amendment bracket '[Alta.Reg.124/10;170/12;216/22]' agrees with the official 'AR 124/2010 s3.15;170/2012;216/2022'. No substantive difference in the rule text."},
}
MANUAL_TEXT_NOTES = {
    "3.14": {"BOOK_A": "same wording as official 3.14(1)(a)-(g) and (2) (read by eye) except: in (1)(c) Book A prints 'the transcript evidence or answers to questions, or both' where the "
                        "official text says 'answers to written questions' ('written' missing); bracket labels '[Questioning on an affidavit and questioning witnesses]' (3.13; the official title "
                        "is 'Questioning on affidavit and questioning witnesses'), '[Disclosure of Information]' (Part 5) and '[Use of transcript and answers to written questions]' (5.31, matches); the "
                        "title in capitals; missing spaces and '*' line marks. No amendment bracket; the official text lists no amendment for 3.14. The raw text also held the p.3-32 footnote block "
                        "(five footnotes), kept here verbatim: '3Campbell v. Chief Electoral Officer2018 ABQB 248, JCE 1703 07561/3 (Mar 29) (¶'s 39-45). 4Jacobson v. Newell (Cty.)2021 ABQB 505, "
                        "JCE 2103 00316 (Jun 30) (¶'s 32 ff.). 5Quite similar to previous 1987 Rr.753.03, 753.04.They were first enacted in 1987. 1Baralot Int.Corp. v.Rundle Dev. Co-op.2008 "
                        "ABCA 103, 429 AR 64. 2Amack v.AW Hldg. Corp.2014 ABQB 92, [2014] AR Uned 168.Is there then deliberative privilege?'"},
    "3.13": {"BOOK_A": "same wording as official 3.13(1)-(5) (read by eye) except bracket labels '[Limit on questioning]' (3.21), '[Contents of appointment notice]' (6.16; the official "
                        "title is 'Contents of notice of appointment'), '[Form of questioning and transcript]' (6.20) and '[Requiring attendance for questioning]' (6.38), missing spaces, "
                        "'*' line marks and the footnote markers '1' (after (1)) and '2' (after (2)). No amendment bracket; the official text lists no amendment for 3.13. No substantive "
                        "difference. The raw text also held the p.3-31 footnote block, kept here verbatim: '1Quite similar to previous 1968 R.314.It came from theChancery Procedure "
                        "Act1852, s.38.OR s.40? Cf. English Chancery Order of 5 February 1861, R.XIX.Cf.1883 O.37 r.20; O.38 r.1;and cf.Judic.Ord.rr.282, 293.1968 R.314(1), (2) were virtually "
                        "identical to 1914 R.382, and 1944 C.R.360. 2Quite similar to previous 1968 R.266.It came fromChancery Procedure Act1852 (15 & 16Vict) s.40; cf. Common Law Procedure "
                        "Act1854 ss.47, 48.Rearranged somewhat from 1914 R.384, but substantially the same.Cf.Judic.Ord.rr.284, 285;1883 O.37 rr.20, 22.Came from 1944 C.R.317.Cf.C.O.O.XIX r.3. "
                        "3Holden (Village) v. Sen2019 ABQB 472, JCE 1503 12322 (Jan 21) (¶'s 54-56). 4Re Metanczuk2024 ABKB 270, JCC B201 716395 (May 13) (¶'s 50-61).'",
              "BOOK_C": "same wording as official 3.13(1)-(5) (read by eye) except the bracket labels after 'rule 3.21' ('[Limit on questioning]') and after 'Rules 6.16 to 6.20' ('[Contents of "
                        "appointment notice] [Form of questioning and transcript] [Requiring attendance for questioning]'); amendment note agrees (no amending regulation)."},
    "3.12": {"BOOK_A": "same wording as official 3.12 (read by eye) except missing spaces and '*' line marks. No amendment bracket; the official text lists no amendment for 3.12. "
                        "No substantive difference."},
    "3.11": {"BOOK_A": "same wording as official 3.11(1)-(3) (read by eye) except missing spaces, '*' line marks, the title in capitals and the footnote marker '3' after "
                        "(3)(b). No amendment bracket; the official text lists no amendment for 3.11. No substantive difference."},
    "3.10": {"BOOK_A": "same wording as official 3.10(1)-(2) (read by eye) except bracket labels '[Managing Litigation]' (Part 4), '[Disclosure of Information]' (Part 5), "
                       "'[Responsibilities of parties to manage litigation]' (after 4.1; the official title is 'Responsibility of parties to manage litigation'), '[What the "
                       "responsibility includes]' (4.2) and '[Discontinuance of claim]' (4.36), missing spaces and '*' line marks; the amendment bracket "
                       "'[Alta.Reg.124/10;122/12;23/21]' agrees with the official 'AR 124/2010 s3.10;122/2012;23/2021'. No substantive difference."},
    "3.9": {"BOOK_A": "same wording as official 3.9 (read by eye) except the bracket label '[Originating application for judicial review]' (after 'rule 3.15(5)'), "
                       "missing spaces, '*' line marks and the footnote marker '6' after the text. No amendment bracket; the official text lists no amendment for 3.9. "
                       "No substantive difference. This rule's history footnote 6 ('Quite similar to previous 1968 R.310 ...') is printed at the top of p.3-26 inside "
                       "the built 3.8 commentary and is kept verbatim there."},
    "3.8": {"BOOK_A": "same wording as official 3.8(1)-(2) (read by eye) except missing spaces, '*' line marks and the footnote markers '3' (after (1)(d)) and '1' (after "
                       "(2)(b)). No amendment bracket; the official text lists no amendment for 3.8. No substantive difference. The raw text also held the p.3-25 "
                       "footnote block (footnotes 1-7 of that page), kept here verbatim: '1Quite similar to previous 1968 R.305(1).It came from 1914 R.416 and 1944 "
                       "C.R.348.Cf.Judic.Ordinance R.295, 1883 O.38 r.3, and 1897 Ont.R.519.Cf.C.O.O.XVIII r.3.These older Rules were more general. 2The July 8, 2020 "
                       "Notice to the Profession about digital filing is now permanent as \"Guidelines for Documents Filed by Email or Digital Upload\", though "
                       "sometimes later updated by announcements.See R.3.25n. 3Veniniv. Venini2023 ABKB 524, JCE 1503 06113 (AJ Sep 20) (¶'s 55, 57, 61-63). "
                       "4Canmore Apts.v.Cormode & Dickson Constr.2023 ABKB 659, JCC 2101 03737 (Nov 22) (¶ 30(2)). 52024 ABKB 209, JCC FL01 41775 (Apr 10) "
                       "(¶'s 52-54). 6Tole v.Lucki2017 ABCA 79, [20017] AJ #184 (one JA). 7ANCTimber v. Min.of Agric.2019 ABQB 653, 5 Alta LR(7th) 102 "
                       "(¶'s 81-83).' Footnote 5 (2024 ABKB 209) is the citation of L.Y. v. R.Y., whose name is printed in the commentary ('On double or treble "
                       "hearsay, see L.Y. v. R.Y.'); footnote 6 prints the year as '[20017]'."},
    "3.7": {"BOOK_A": "same wording as official 3.7(1)-(2) (read by eye) except missing spaces and '*' line marks; the title is printed 'Post-judgmentTransfer of Action'. "
                       "No amendment bracket; the official text lists no amendment for 3.7. No substantive difference. This rule's history footnote is "
                       "presumably p.3-24 fn 3 ('Quite similar to previous 1968 R.405. It was new in 1968.'), which is printed in the built 3.6 commentary and "
                       "kept verbatim there; the book does not show which of the two rules it belongs to."},
    "3.6": {"BOOK_A": "same wording as official 3.6(1)-(2) (read by eye) except: bracket labels '[Claim for possession of land]' (after 'rule 3.4') and "
                       "'[Transfer of an action]' (after 'rule 3.5'), missing spaces, '*' line marks, the footnote marker '4' after (2), and one word in (1)(b): "
                       "'continued in that judicial centre to which the action is transferred' where the official text says 'continued in the judicial centre to "
                       "which the action is transferred' ('that' for 'the'; no change of meaning). No amendment bracket; the official text lists no amendment "
                       "for 3.6. This rule's history footnote 4 ('Quite similar to previous 1968 R.715, 716 ...') is printed in the built 3.5 commentary "
                       "(p.3-23) and is kept verbatim there."},
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
    "3.6": ["Official text (searched for 'rule 3.6', 'rules 3.5 and 3.6', lists, line-wrapped forms and form headings): no other official rule cites 3.6 by number and no form heading names it. By subject: 3.3, 3.4 and 3.5 (where the action starts and moves), 3.7 (temporary transfer after judgment), 6.2 and 6.9 (applications and how the Court considers them; 6.9(2) judge or applications judge), 6.10 (electronic hearing) and 8.18 (trial conducted by electronic hearing), 14.8(5) and 14.84 (place of filing appeals).",
            "BOOK_A other Parts and Part 3 (all combined*.txt searched, line-wrapped forms included; page markers checked): R.14.84 related provisions p.14-252 (line 13355) '3.6 (place of proceedings); 14.8(5) (venue of appeals)'; R.6.2 related provisions p.6-3 (line 80) '3.6 (venue for applications)' and R.6.2 footnote p.6-8 '4 See R.3.6.'; R.6.3 related p.6-14 (line 633) '3.6 (venue)'; R.6.16 related p.6-104 (line 5519) '3.6 (venue)'; R.6.18 note p.6-105 (line 5583) 'no need for the cross-examination to take place in the city where the trial will be: Baltimore v. Baltimore ...; but see R.3.6'. Inside Part 3: 3.3 related provision and commentary '3.6(2) (movement of motions and trials)' (p.3-19 line 413, p.3-20 line 444; official 3.6(2) confirms: hearing or trial in a place specified by the Court other than the judicial centre); 3.5 (p.3-23 line 518 text and 3.6's own note).",
            "BOOK_B: rule text only (paragraph part3_part_3_court_actions_020), equal to the official 3.6; no commentary section for 3.6; no amendment note on either side. BOOK_C: one 'General Principles' paragraph (kept, flagged). Neither book (all Book B files and all Book C page files searched) cites 3.6 by number except 3.6's own text.",
            
        "Cross-book: the venue-change authorities that BOOK_B gives under 3.5 are used by BOOK_A under 3.6 as well: Siver v. Siver 2010 ABQB 755 (BOOK_A p.3-23 line 525, 'a curious decision, using instead a lax almost subjective test'; BOOK_B 3.5 commentary, Regular paras. 6-9), Keaton v. Keaton 2017 ABQB 429 (BOOK_A p.3-24 fn 1 and p.3-4 fn 6; BOOK_B 3.5, Odland para 22), C.S. v. A.J. 2004 ABQB 73 (BOOK_A p.3-24 fn 2; BOOK_B 3.5, Regular para 7), Silver Springs v. UMA Eng. and Christensen v. Proprietary Industries (BOOK_A; BOOK_B 3.5, Regular). Hansraj v. Ao: BOOK_A cites 2002 ABQB 772 (#2), affd on this point 2004 ABCA 223; BOOK_B cites 2004 ABCA 223 in its rule 1.5 commentary ('Time Requirements of the Rule'), so it is the same appeal cited for a different point.",
    ],
    "3.7": ["Official text (searched for 'rule 3.7', 'rules 3.6 and 3.7', lists, line-wrapped forms and form headings): no other official rule cites 3.7 by number and no form heading names it. By subject: 3.4-3.6 (transfer and place of action), Part 9 (9.5 entry of judgments and orders; enforcement rules 9.17-9.29 that BOOK_A links to 3.7), 3.37 (named in BOOK_A R.9.17's related provisions with 3.7), Appendix definitions of judgment, judgment creditor, judicial centre and order.",
            "BOOK_A statements about 3.7 (all combined*.txt searched, line-wrapped forms included; pages from the page markers): R.9.5 note p.9-16 (line 710) and R.9.17 note p.9-50 (line 2618): 'Rule 3.7 [Post-judgment transfer of action] permits a judgment creditor to apply to the Court, on notice to each of the other parties, for a temporary transfer of the action to a different judicial centre for purposes of an application to enforce the judgment or order' (title matches the official; official 3.7(1) says the creditor 'may request' a temporary transfer and 3.7(2) speaks of 'an order granting' it, so 'apply to the Court' is a paraphrase); R.9.20 note p.9-63 (line 3314): 'Rule 3.7 allows enforcement other than by writ to take place at a different judicial centre' - 'other than by writ' is not in the official 3.7, which speaks of 'an application to enforce the judgment or order' (FLAG: gloss). Related provisions: R.9.17 p.9-50 (line 2623) and R.9.20 p.9-62 (line 3282) '3.7 (venue of enforcement proceedings)', R.9.29 p.9-73 (line 3780) '3.7 (venue of enforcement)' - short paraphrases; R.13.41 note p.13-103 (line 5292) lists 3.7 among rules that 'seem to require filing' (official 3.7(2): the order 'must be filed in the judicial centre from which the action has temporarily been transferred'; consistent). FLAG: R.13.44 (p.13-104) pointer 'Rr.3.7 n. J' (see the Book A flag).",
            
        "BOOK_B: rule text only (paragraph part3_part_3_court_actions_022), equal to the official 3.7, no commentary, no amendment note on either side; BOOK_B's next paragraph is the running head 'Part 3. Court Actions Division 2. Actions Started by Originating Application Subdivision 1. General Rules'. BOOK_C: rule text and amendment note ('Alta. Reg. 124/2010, r. 3.7 effective November 1, 2010') agree with the official text; one 'General Principles' paragraph (flagged); its information note is garbled (see BOOK_C note). No Book B or Book C file cites 3.7 by number.",
    ],
    "3.8": ["Official text (searched for 'rule 3.8', 'rules 3.8 ...', lists, line-wrapped forms and form headings): rules 12.26(1) (application under section 22.1 of the Divorce Act: '(i) an originating application in accordance with rule 3.8(1); (ii) a supporting affidavit in accordance with rule 3.8(2) to which are attached as exhibits ...'), 12.27(1)-(2) (originating application 'in accordance with rule 3.8(1)', affidavit 'in accordance with rule 3.8(2)'), 12.30 and 12.33(1) (originating application in accordance with rule 3.8(1)); Form 7 is headed '[Rule 3.8]' (3.8(1)(a) names Form 7). By subject: 3.9 (service), 3.12, 3.13 (questioning on affidavit), 3.15, 13.18 and 13.19 (affidavits), 6.11 (evidence at application hearings).",
            "BOOK_A other Parts and Part 3 (all combined*.txt searched, line-wrapped forms included; pages from the page markers; rule numbers read from the text): R.12.26 (p.12-23), R.12.27 (p.12-27; related p.12-28), R.12.30 (p.12-30), R.12.33 (p.12-31; related p.12-32) quote 'in accordance with rule 3.8(1)/(2) [Originating applications and associated evidence]' (title matches official); R.12.27 and R.12.33 related provisions '3.8 (originating applications)'; R.6.1 related p.6-3 (line 61) and R.6.3 related p.6-14 (line 633) '3.8 (originating applications)'; R.9.42 (p.9-91), R.9.50 (p.9-95) and R.9.52 (p.9-97) related '3.8 (originating applications)'; R.13.18 related p.13-74 (line 3837) '3.8(2) (affidavits used for originating applications)'. Inside Part 3: 3.2 information note p.3-5 ('rule 3.8 [Originating applications and associated evidence]'); 3.22 note p.3-65 lines 1712 and 1719 'Under Rr.3.8(2), 13.18, judicial review is final, not interlocutory' and 'Violation of Rr.3.8(2), 13.18 bars the offending parts, not the whole affidavit'; 3.25 note p.3-70 line 1879 'See R.3.8n.'; 3.31 note p.3-81 line 2199 'See notes to Rr.3.8 and 3.25' (rule attribution from the nearest earlier rule marker in the file).",
            "BOOK_B: rule text = official; one commentary part, '1. Test for amending originating application' (Thomson v. Thomson 2024 ABCA 293 para 28 quoting the four exceptions to amending pleadings; then Terrigno v Butzner 2021 ABCA 18, Attila Dogan 2014 ABCA 74, Aramark 2023 ABKB 42, Ingram 2021 ABQB 343 affd 2022 ABCA 97, Alberta March for Life 2020 ABQB 220, McCargar 2017 ABQB 692 rev'd in part 2018 ABCA 144). It is printed inside the rule-text paragraph with no 'Commentary §' header; split off by manual decision (MANUAL_BOOK_B_SPLIT). Same authorities elsewhere: Thomson v. Thomson 2024 ABCA 293 also in BOOK_B rule3_part06_document.json; Attila Dogan, McCargar and Terrigno v. Butzner also in BOOK_A Part 3 (and other Parts) and other BOOK_B Part 3 files - to be linked in the 3.65 (amendment) pass; Aramark and Alberta March for Life appear only here.",
            
        "BOOK_B other Parts and BOOK_C (all Book B files and all Book C page files searched): BOOK_B rule 12.x text (rule12_part01_document.json) and BOOK_C (file 561-580) repeat the official wording that cites 3.8(1) and 3.8(2) (rules 12.26, 12.27, 12.30, 12.33); BOOK_B rule15_part02_document.json (paragraph part15_address_038, a Hague Convention practice notice, item 15) says the party seeking the return of a child 'must file an Originating Application (Form 7) pursuant to Rule 3.8'. The 3.2 information notes in Book A and Book C name 3.8 with its title (see the 3.2 review note).",
    ],
    "3.9": ["Official text (searched for 'rule 3.9', 'rules 3.9 and ...', lists, line-wrapped forms and form headings): 12.26(3) ('Despite rules 3.9 and 12.44(1)(b), the filed documents referred to in subrule (1) must be served on the respondent ...') and 12.27(3) ('Despite rule 3.9, the originating application to vary a custody order ...'); the other hit is the running page header 'Rule 3.9'. No form heading names 3.9. By subject: 3.8, 3.11, 3.15(2) and 3.15(5), 6.3(3) (file and serve at least 5 days before), 11.4 (methods of service), 13.3 (counting days), 1.5(5).",
            "Cross-book: Thompson v. Procrane 2016 ABCA 71 paras. 9-10 is cited in BOOK_A's 3.9 note (p.3-27 fn 7); BOOK_C's 3.2 information note has a Procrane para. 9 citation where '13' is lost - 3.9 is a candidate home for that citation (same case, paras. 9-10), but no source says so. Regular v. Regular (BOOK_C, para. 9 passage) is displaced in BOOK_C's 3.9 rule text and information note (home 3.5). Patrus v. Alberta (Workers' Compensation Board, Appeals Commission) [2011] A.J. No. 1346 stands displaced in BOOK_C's 3.9 commentary (3.10 material) and in its 3.12 information note; BOOK_A cites Patrus v. W.C.B. 2014 ABCA 117, 572 AR 250 at p.3-5 fn 3 (3.2), a different decision.",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers; rule numbers from the text): R.11.3 related p.11-6 '3.9 (time to serve originating application)'; R.13.3 note p.13-18 'Rule 3.9 [Service of originating application and evidence] requires certain documents to be filed and served on parties 10 days or more before the date scheduled for hearing the application' (agrees with official 3.9); R.9.44 related p.9-93 '3.9 (service of originating application)'; R.6.2 time table p.6-12 (lines 525, 550) 'Originating Applications 10 days (R.3.9) See various limitations statutes and R.3.15(2)' and 'Judicial review to set aside a decision or act 10 days (Rr.3.9 and 3.15)' (the second row cites 3.15, whose official 3.15(2) period is 6 months for filing and serving; the table's layout is not reliable); R.6.3 fn p.6-15 'See R.3.9.'; R.12.26 p.12-24 and R.12.27 p.12-27 quote 'Despite rule 3.9 [...]'; the 3.11 note p.3-29 (line 662) 'On order of filing and service, see R.3.9n.' (lands: 3.9's note says filing and service is sequential, file then serve). FLAG on BOOK_A R.4.30 related provision '3.9 (defence of tender)' (p.4-71, line 3664): the same note says 'Rule 13.9 [Defence of tender] sets out the conditions for a defence of tender before action', and official 13.9 is 'Defence of tender'; official 3.9 is service of an originating application, so '3.9' there looks like 13.9 (evidence: the note's own text).",
            
        "BOOK_B and BOOK_C other Parts (all files searched): BOOK_B rule12_part01_document.json (12.26(3) 'Despite rules 3.9 and 12.44(1)(b)' and 12.27(3) 'Despite rule 3.9') and rule14_part04_document.json (13.3 note: 'Counting backwards ... Rule 3.9 requires certain documents ... 10 days or more before the hearing'); BOOK_C files 561-580 (12.27(3)) and 601-620 (13.3, same counting-backwards sentence). BOOK_B has no 3.9 commentary (rule text only).",
    ],
    "3.10": ["Official text (searched for 'rule 3.10', lists, line-wrapped forms and form headings): 12.34(1) ('Despite Rule 3.10, Part 4 applies to (a) a proceeding under the Family Law Act, and ...') and 12.37(1) ('Despite rule 3.10, Part 5 applies to (a) a proceeding under the Family Law Act, and (b) an application to v...'); no form heading names 3.10. By subject: 3.12 (rules for statement-of-claim actions may be applied by direction), Part 4 (4.1 responsibility of parties to manage litigation, 4.2, 4.36 discontinuance of claim), Part 5, 12.34-12.37 (family rules that override 3.10).",
            "BOOK_C material for 3.10 sits inside BOOK_C's 3.9 entry (Book C has no 3.10 entry; see the 3.9 flag). Verbatim, as printed there: rule text '[Managing Litigation] [Disclosure of Information] do not apply to an and [Managing Litigation] rules 4.1 , 4.2(a) [Responsibilities of parties to manage litigation] [What the responsibility includes] [Discontinuance of claim] and (d) and 4.36 apply, with all necessary modifications, to actions started by originating application unless the Court otherwise orders.' (the words '(1) Subject to subrule (2), Part 4 and Part 5 ... the Court otherwise orders. (2) The rules in Divisions 2, 4, 5 and 6 of Part 4 and' are not in the text); amendment note '[Alta. Reg. 124/2010, r. 3.10 effective November 1, 2010 (Alta. Gaz. August 14, 2010); Alta. Reg. 122/2012, s. 3; Alta. Reg. 23/2021, s. 2 effective March 1, 2021]' (agrees with the official 122/2012 and 23/2021); information note 'See also rule 3.12 [Application of statement of claim rules to originating applications]. Typically, an action started by originating application will not require the same kind of management, either by the parties or the Court, nor require the same kind of disclosure of records or questioning, as actions started by statement of claim. However, if management, document disclosure and questioning are required, the parties may agree or the Court may order them.' (same words as BOOK_A's note, without the 'qpplications' typo); commentary 'The court can, by order, or the parties may agree that the discovery Absent agreement, the parts of Part [Patrus v. Alberta (Workers' Compensation Board, Appeals Commission), [2011] A.J. No. 1346, stands where the Part number is lost] that apply to such actions are: do * Division 2 - ACTIONS STARTED BY ORIGINATING APPLICATION' (cut off; no other book has this content). BOOK_C's label '[Responsibilities of parties to manage litigation]' has the same plural wording as BOOK_A's.",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers): 3.12 information note p.3-30 (line 686) 'See also rule 3.10 [Application of Part 4 and Part 5]'; R.4.2 note p.4-4 (line 137) 'See rule 3.10 [Application of Part 4 and Part 5] and rule 3.12 [...]'; R.5.1 note p.5-3 (line 61) 'See rule 3.10 [Application of Part 4 and Part 5]' and related provision p.5-3 (line 67) '3.10 (application to originating applications)'; R.7.1 note p.7-4 (line 86) 'On deciding issues raised by an originating application, see also Rr.3.10n. and 3.14n.' (3.10's note deals with limited discovery on an originating application; 3.14 to be checked in its pass); R.12.27 p.12-28 (line 1231) 'rule 3.10 [Application of Part 4 and Part 5] does not apply to an application ...'; R.12.34(1) p.12-33 and R.12.37(1) p.12-40 'Despite rule 3.10 [Application of Part 4 and Part 5] ...' (title matches official).",
            
        "BOOK_B and BOOK_C other Parts (all files searched): BOOK_B rule12_part01_document.json (12.34(1) 'Despite Rule 3.10, Part 4 applies to ...'; 12.37(1) 'Despite rule 3.10, Part 5 applies to ...'); BOOK_C file 161-180 (rule 4.2 information note: 'See rule 3.10 [Application of statement of ...] and rule 3.12 ...', garbled) and file 561-580 (12.34, 12.37 texts and, in 12.37's commentary, 'Rule 3.10 says that the disclosure rules in Part 5 do not apply to actions started ...'). BOOK_B has no commentary on 3.10 (rule text and amendment note 'Alta. Reg. 122/2012, s. 3; 23/2021, s. 2' only, agreeing with the official).",
    ],
    "3.11": ["Official text (searched for 'rule 3.11', lists, line-wrapped forms and form headings): 12.26(4) ('Despite rule 3.11(1) and 12.44(2), if the respondent to the application under this rule intends to rely on an ...') and 12.27(4) ('Despite rule 3.11(1), if the respondent to an application to vary a custody order ...'); no form heading names 3.11. By subject: 6.6 (response and reply to application: 6.6(1)-(3) parallel 3.11(1)-(3)), 3.9 (service time for the originating application), 3.13 (questioning on affidavits), 13.18-13.19 (affidavits), 12.44.",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers; rule numbers from the text): R.6.6 related provisions p.6-59 (line 3096) '3.11 (reply materials for originating applications)' (matches: 6.6 is the ordinary-application counterpart); R.13.18 note p.13-74 (line 3867) 'On deadlines for affidavits, see Rr.3.11, 6.6, and 12.44' and R.13.18 checklist p.13-85 (line 4447) 'Filed and served reasonable time before motion (if to oppose motion or to reply) Rr.3.11, 6.6'; R.12.26(4) p.12-24 and R.12.27(4) p.12-28 quote 'Despite rule 3.11(1) [Service and filing of affidavits and other evidence in reply and response] ...' (title matches the official). Inside Part 3: none besides 3.11's own note and the 3.9 pointer above.",
            
        "BOOK_B and BOOK_C other Parts (all files searched): BOOK_B rule12_part01_document.json (12.26(4) 'Despite rule 3.11(1) and 12.44(2)'; 12.27(4) 'Despite rule 3.11(1)'); BOOK_C: no other file cites 3.11 (only its own 3.11 commentary 'Rule 3.11(3) makes it clear ...'). BOOK_B has no 3.11 commentary (rule text only, equal to the official 3.11).",
    ],
    "3.12": ["Official text (searched for 'rule 3.12', lists, line-wrapped forms and form headings): no other official rule cites 3.12 by number (the only hit is the running page header 'Rule 3.12'); no form heading names it. By subject: 3.10 (Parts 4 and 5 do not apply to originating-application actions unless another rule, agreement or order provides), 3.13-3.14 (questioning and cross-examination on originating applications), 1.7(2) (rules applied by analogy), 3.2(6) (procedural order to correct and continue in another form), 7.3 (summary judgment, applied via 1.7(2) and 3.12 according to BOOK_A R.7.3), the Appendix definition of 'claim'.",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers; rule numbers from the text): R.1.7 related p.1-33 (line 1624) '3.12 (action wrongly started by originating application)' - FLAG: official 3.12 lets the Court direct that statement-of-claim rules apply to an originating-application action; it says nothing about an action 'wrongly started'; 3.2 p.3-5 information note and related provision '3.12 (converting application to trial)' (flagged under 3.2) and p.3-6 (line 50) 'R.3.12 lets the court turn an Originating Application into a full-fledged suit'; R.4.2 note p.4-4 (line 138) 'rule 3.12 [Application of statement of claim rules to originating applications]' (title matches); R.7.1 note p.7-8 (line 314) 'See this book's annotation of R.3.12 and the description of a statement of the applicant's claim' (lands: this note); R.7.3 note p.7-41 (line 2160) 'Rule 7.3 applies (via Rr.1.7(2) and 3.12)' to a respondent to an Originating Application for judicial review; R.13.6 note p.13-37 (line 1896) 'an application under R.3.12 to permit filing a defence to the originating application'.",
            
        "BOOK_B: rule text printed twice in the source (headings 3.12_001, _002 and paragraphs _032, _034 identical; the builder keeps one copy); one commentary section (§ 3.12:1) headed 'Not a New Action' (unnumbered): Singh v. Kaler 2017 ABCA 275 paras. 66-69 - para 66 quotes rule 3.12 with capitals ('Originating Application'; official 'originating application'), para 67 quotes the Alberta Civil Procedure Handbook 2017 (Stevenson & Cote) at 3-21 (the passage in BOOK_A's 3.12 note), para 68 cites Canadian Gulf Oil Co v Crown Trust Co (Alta CA 1954). BOOK_A cites Singh v. Kaler at 3.12 fn 6 (paras. 65-70), 3.2 p.3-9 (line 116) and 3.2 p.3-17 fn 1 (paras. 62-64). No other Book B file cites 3.12; BOOK_C: 3.12's own entry, the 3.2 information note and the 4.2 information note (garbled 'See rule 3.10 [Application of statement of and rule 3.12 claim rules ...]', file 161-180).",
    ],
    "3.13": ["Official text (searched for 'rule 3.13', lists, line-wrapped forms and form headings): 3.14(1)(b) ('a transcript referred to in rule 3.13') and 12.25 ('Exception to rule 3.13(5)': the questioning party need not file the transcript if the parties agree). No form heading names 3.13. By subject: 3.21 (limit on questioning), 6.7 and 6.8 (questioning on affidavit; witness before hearing), 6.16-6.20 and 6.38 (appointment, allowance, lawyer's responsibilities, interpreter, form of questioning and transcript, requiring attendance), 5.11(2)(b) (cross-examination on affidavit of records), 3.8(2) and 13.18-13.19 (affidavits).",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers; rule numbers from the text): 3.14 text p.3-31 (line 719, '[b] a transcript referred to in rule 3.13') and related provision p.3-32 (line 734) '3.13 (questioning on an affidavit)'; R.5.11 related p.5-71 (lines 3776-3777) '3.13 (cross-examination on affidavits)' and its note (line 3783) 'Rules 3.13 and 5.11(2)(b) also allow one to cross-examine on an affidavit of records' (official 5.11(2)(b): the Court may permit cross-examination on the original and any subsequent affidavit of records; agrees); R.5.17 note p.5-92 (line 4940) 'examination in chief for a pending application (now R.3.13(2))'; R.6.7 related p.6-62 (line 3263) and R.6.8 related p.6-70 (line 3740) '3.13 (questioning re originating applications)'; R.6.11 checklist p.6-92 (line 4895) 'motion: Rr.3.13, 6.7, 6.8, 6.11(1)(b), 6.20 (either side may use)'; R.12.25 p.12-23 (title 'Exception to Rule 3.13(5)' and text); R.13.41 note p.13-103 (line 5292) lists 3.13 among rules that 'seem to require filing' (official 3.13(5): the questioning party must file the transcript).",
            "BOOK_B and BOOK_C other Parts (all files searched): BOOK_B rule12_part01_document.json (12.25 'Exception to rule 3.13(5)' and its text); BOOK_B rule5_part08_document.json (a commentary under 5.25, paragraph part5_part_5_disclosure_of_information_022: 'Rule 3.13 ... governs who counsel may question on an affidavit for an Originating Application. In his commentary on this rule, Judge Fradsham notes that the examination under this rule may be \"as searching and thorough\" as examination on discovery; however, it must not \"extend to matters wholly immaterial and irrelevant to the affidavit\": Judge Allan A Fradsham, Alberta Rules of Court Annotated 2019 (Toronto: Thomson Reuters, 2018) at 119' - the wording matches this rule's BOOK_B commentary, which confirms that BOOK_B is Fradsham's annotation); BOOK_C file 561-580 (12.25). BOOK_C 3.14 text 'a transcript referred to in rule 3.13' is in 3.14.",
            
        "BOOK_B: read in full (one section, § 3.13:1, 35,149 characters). Six numbered parts, no gap: 1 Scope of Examination (with (i) Generally and (ii) When affiant is a non-party), 2 Cross-examination on Statutory Declaration Exhibited to Affidavit, 3 Duty to Inform, 4 Withdrawal of an Affidavit (under which the text prints (i) Right to cross-examine, (ii) Effect of inability to cross-examine, (iii) Filing one's own affidavit not required), 5 Loss of Right to Cross-examine Due to Delay, 6 Failure to File Undertakings. It quotes the former rules (R.314(1)-(2), 311, 312 and 305(3) as numbered in the 1968 Rules) and decisions under them (College Brand Clothes v. Brown, Ed Miller Sales v. Caterpillar, Dy-Reyes v. Carina Holdings 2000 ABQB 386, Colortech v. Toh 2000 ABQB 814, Alberta Treasury Branches v. Leahy, International Securities Group v. Alberta (Securities Commission) 2011 ABQB 737, CRC-Evans Pipeline v. O.J. Pipelines, R.O.M. Construction v. Heeley, Becker v. Alberta 2000 ABCA 329, Point on the Bow Development, Fech v. Lewington 2022 ABCA 154, Hoda v. Hoda 2021 ABCA 122, Reference re Firearms Act 1998 ABCA 306); none of these is compared with the current 3.13 text (the current rule names the persons who may be questioned, the transcript and rules 6.16-6.20). Fech v. Lewington quotes 'Stevenson & Cote, Alberta Civil Procedure Handbook (Edmonton: Juriliber, 1999) at 226'. BOOK_B's rule text = official; no amendment note on either side.",
    ],
    "3.14": ["Official text (searched for 'rule 3.14', lists, line-wrapped forms and form headings): no other official rule cites 3.14 by number (the only hit is the running page header 'Rule 3.14'); no form heading names it. By subject: 6.11(1)-(2) (evidence at application hearings; the same list of evidence), 3.13 (transcript), 3.21 and 3.16-3.24 (judicial review, for which 3.14 does not apply), 5.31 (use of transcript and answers to written questions), 8.17(3) and 8.19 (evidence in other actions and proceedings).",
            "BOOK_A other Parts and Part 3 (all combined*.txt; pages from the markers; rule numbers from the text): 3.18 note p.3-59 (line 1573) 'In a statutory appeal, with issues of redaction of confidential information, Rr.3.18 to 3.20 can be applied to R.3.14 by analogy under R.1.7'; R.6.11 checklists p.6-91 (line 4871) 'Live evidence (with leave of the judge): R.6.11(1)(g); cf. R.3.14(g)' [3.14(1)(g) as printed], p.6-92 (line 4900) 'Evidence from another suit with the same parties (with leave of the court): Rr.3.14(1), 6.11(1)(f), 8.17(3)' and (line 4923) 'cf. R.3.14(1)(a)' (official 3.14(1)(a), (f), (g) mirror 6.11(1)(a), (f), (g)); R.6.57 footnote p.6-167 (line 8749) 'the commentary on R.3.14 and 7.1'; R.7.1 related p.7-4 (line 79) '3.14 (deciding originating applications)' and its note (line 86) 'see also Rr.3.10n. and 3.14n.' (lands in this rule's note only loosely: the note is about evidence and a respondent's duty to say it wants an adjournment); R.8.17 related p.8-44 (line 2207) '3.14(1) (evidence on applications)' - official 3.14(1) is about originating applications other than judicial review (ordinary applications are 6.11(1)); R.8.19 related p.8-51 (line 2546) '3.14(1)(f) (evidence on originating applications)' (agrees).",
            
        "BOOK_B: rule text only (paragraph part3_part_3_court_actions_042), equal to the official 3.14; no commentary; no amendment note on either side; the next paragraph is the running head for Subdivision 2 (judicial review). BOOK_C: no other file cites 3.14 by number; BOOK_B: no other file cites it.",
    ],
    "3.15": ["BOOK_B and BOOK_C other Parts (all files searched): BOOK_B rule13_part01_anno.json (commentary on time periods: 'except those embedded in rr. 3.15(2), 3.27(2) and 4.33(4)' and a quotation of 3.15(2)); BOOK_B rule7_part02_document.json (a commentary that says a judicial review application 'has not been commenced within the 6 month time frame mandated by Rule 3.15', citing Athabasca Chipewyan First Nation v Alberta (Minister of Energy)); BOOK_B rule3_part02_document.json 3.23 commentary (paragraph part3_part_3_court_actions_024) points back to '3.15: Service of notice of the application suspends proceedings in the Alberta Court of Justice' (lands: section 4 of the 3.15 commentary has exactly that heading). BOOK_C 601-620_12_55 to 13_5.json (13.5 information note and commentary: 'the time for bringing most originating applications for judicial review under rule 3.15(2)') and 101-120 file (3.9, 3.12 information notes naming 3.15). BOOK_B 3.15 commentary READ IN FULL (rule3_part02_document.json paragraph part3_alberta_rules_of_court_rule_3_part_2_001, one section 'Commentary § 3.15:1', 72,129 characters): 23 numbered parts, no gap: 1 Rule applies to municipal bylaws, 2 Domestic tribunals, 3 Service of application, 4 Service ... suspends proceedings in the Alberta Court of Justice, 5 Service of the Originating Application, 6 Rule 3.15(3)(b) 'as the circumstances require', 7 Rule 3.15(3)(c) 'Directly affected', 8 Certiorari and Prohibition Distinguished, 9 Style of Cause for an Order in the Nature of Certiorari, 10 Standards of Review, 11 The Participation of the Tribunal Whose Decision is Impugned, 12 The Provincial Court of Alberta is not a tribunal, 13 Appeal by the Statutory Tribunal, 14 Nature of Awards Available, 15 Judicial Review Is Not an Appeal, 16 Applicability of Rule R. 3.15(2), 17 Reason for Limitation, 18 Time Limits, 19 Extensions in cases of disability?, 20 Time Limits and Procedural Fairness, 21 Computing the Limitation Date, 22 Time Cannot Be Extended, 23 Rule 3.15 applies to criminal cases. Official statements quoted in it checked: 13.4(1) (quoted exactly), 13.14 and 13.15 (quoted by Tartal, para 47: agree), 7.3(1)(b) (no merit), 1.5(5) (agrees), 1.8(b) (Tartal says s 22(7) does not apply: official 1.8(b) disapplies 'section 22(3) to (8)', which includes (7)), 3.23 (stay; the text at part 4 distinguishes it correctly), 3.24(1) (quoted by Lee v. Yeung: agrees). Cautions: parts 9, 11, 14, 16, 17, 20 and 21 are decisions under the former rules (Rr.753.03-753.13, Pt. 56.1, 'Rule 753.11') and are not compared with the current rule text; part 21 (Becker) counts months by s. 22(7)-(8) of the Interpretation Act, which official 1.8(b) now disapplies, and part 5 (Tartal para 44) says 13.4(1) governs instead; part 3 and 6 quote 'Minister of Justice and Solicitor General' (the official 3.15(3)(b) says 'Minister of Justice or the Attorney General for Canada'); the heading of part 16 reads 'Applicability of Rule R. 3.15(2)' as printed; part 11 quotes the Court of Appeal's 'Court of Queen's Bench' wording. Book B's authorities were not checked for uniqueness to 3.15 (the same cases Tartal, ENMAX, Julien, Boll, Baker v. Drouin, Athabasca Chipewyan, Okotoks and Brewer are cited in Book A's 3.15 note: same neutral citations, except that Book A prints Boll as 'Bull' in two places). BOOK_C: entry read in full; one paragraph of rule text only (no commentary, no information note, no amendment note); see the drop note.",
            "BOOK_A other Parts and Part 3 (all combined*.txt searched, line-wrapped forms included; pages from the markers; the citing rule from the running heads): combined rule1.txt l.1426 p.1-29 note 'On inability to extend statutory time limits, and R.1.5 not changing that, see R.3.15(2) n. F.1' (lands: note F.1 'Effect'); 3.2 related provision p.3-5 (l.36) '3.15 (judicial review applications)' and 3.2 note p.3-16 (l.327) 'It is doubtful that an attack on an arbitration award must be made under R.3.15 rather than ...'; p.3-17 (l.348) 'See now Rr.3.2(2)(f) and 3.15(1)'; 3.9 text p.3-26 (l.586) 'Except as otherwise provided in rule 3.15(5) [Originating application for judicial review]' and its note (l.589) 'The requirement to serve in R.3.15(2) is substantive, not procedural ... Rr.1.5(5) and 3.15(2)'; 3.16 note p.3-53 (l.1391) 'See also R.3.15 n.B' (lands: B Discretion); 3.17 note p.3-57 (l.1524) 'See also note G to R.3.15, supra' (lands: G Service and Parties); 3.23 note p.3-65 (l.1727) 'See also R.3.15n.E.5' (lands: E.5 Stay and Transfer of File); p.3-149 (l.4304; the built segment that holds it starts at the 3.65 marker, line 3874, so the citing rule is not confirmed) '... applies to the 6-month limitation period for quashing by judicial review in R.3.15(2)'; combined_rule13.txt R.13.5 information note p.13-20 (l.952) 'the time for bringing most originating applications for judicial review under rule 3.15(2) [Originating application for judicial review]', p.13-21 (l.1000, l.1016) 'R.3.15(2)' and 'Now see R.3.15(2) n. F' (lands); combined_rule6.txt p.6-12 time table (l.525, l.550, l.553): 'Originating Applications 10 days (R.3.9) See various limitations statutes and R. 3.15(2)' and 'Judicial review to set aside a decision or act 10 days (Rr.3.9 and 3.15)' / '(R.3.15(2))' - the 10 days is the 3.9 service period, the 6 months is 3.15(2) (see the 3.9 flag).",
            
        "OFFICIAL text (searched for 'rule 3.15', 'rules 3.15', lists, line-wrapped forms and form headings): rule 3.9 ('Except as otherwise provided in rule 3.15(5), an originating application and any affidavit and other evidence filed with the originating application must be filed and served ...') and rule 3.16(1) ('must be served under rule 3.15(3) as soon as practicable after filing'); no form heading names 3.15. By subject (read): 3.16 (habeas corpus), 3.17 (Minister of Justice or Attorney General for Canada), 3.18-3.19 (notice and record of proceedings), 3.20-3.24 (further steps, evidence 3.22, stay 3.23, additional remedies 3.24), 1.5(5) (no cure that extends a period the Court is prohibited from extending), 13.4 (counting months), 13.5 (variation of time periods), 13.14-13.15 (endorsement and filing), 11.4 and 11.14 (service), 7.3(1)(b) (summary judgment: no merit), 1.8(b) (section 22(3) to (8) of the Interpretation Act does not apply to the Rules).",
    ],
}
MANUAL_BOOK_A_COMMENTARY_FLAGS = {
    "3.15": "Read in full, p.3-32 (line 740) to p.3-52 (line 1375): rule text of (1)-(5), notes A History, B Discretion (1 General, 2 Other Remedy as Good, 3 Technical Flaws), C Miscellaneous, D Tests and Standards (1-7), E Procedure (1-8), F Limitation period (1-4), G Service and Parties (1-3), defined terms and one related provision. The built commentary field holds only the tail (evidence pointer, defined terms, related provision, title of 3.16); the note itself is inside the kept rule-text field (see the text note). Footnotes are not split out and were not counted. (1) Pointers checked against the official text: R.3.16 (habeas corpus, title matches), R.3.18 (official 3.18(1): notice in Form 8 to send the record when the applicant seeks to set aside a decision or act), R.3.22 (evidence on judicial review), R.3.23 (stay of the operation of a decision or act; official 3.23(1)-(2)), R.3.24(3) (note D.2 p.3-34, line 882 says 'That is now enacted by R.3.24(3)': official 3.24(3) is 'If the sole ground for a remedy is a defect in form or a technical irregularity ... refuse a remedy ... validate the decision'; agrees), R.13.4 (counting months to the same-numbered day; agrees with the note at p.3-48 'The last day to sue is that with the same calendar number'), R.1.5 (official 1.5(5) does not let the Court cure a contravention if that would extend a period it is prohibited from extending), Rr.1.3(2) and 13.6(2)(c) (p.3-46, line 1026; official 1.3(2) remedy may be granted whether or not claimed; official 13.6(2)(c) 'the remedy claimed'), R.14.75 (exists), R.2.10 ('Intervenor status'), R.3.68 (exists), R.3.2 and R.3.9 (exist), R.13.14n. (the R.13 note exists in combined_rule13.txt, line 3703 'A hidden trap lurks here'), R.3.2n.B.4 (Book A's 3.2 note B.4 'Procedure and Parties', line 192, on booking an Originating Application). (2) Pointers that need a caution: 'R.11.14 exists and the Crown or the Minister ... is the person or place to be served' (p.3-52, line 1367): official 11.14 is 'Service on statutory and other entities' and its text (lines 13666-13708) does not name the Crown, a Minister or the Attorney General; the basis given is the case Environmental Defence Can. v. Alta. 2024 ABKB 265. 'See also n.9, p.3-32' (p.3-51, line 1334): no footnote 9 is printed on p.3-32; a footnote 9 of this note (Dir. (S. Sask. Reg.) v. Prov. Ct. (Handel Tpt.) 2017 ABQB 3) is printed at p.3-34 (line 797). R.3.68n.C.5 and R.14.75n.C.1(f) and R.2.10n.E were not opened in this pass (Pass 2). Old-rule references R.753.11 and R.753.09 (history) and 'Rr.827 to 838' (criminal procedure) are not in the official Rules of Court; 'C.P.E., Chapter 79, Parts A-P' are pointers to another work. (3) Defined terms 'Court, file, Minister of Justice and Attorney General, order, party, remedy' (p.3-52): the official Appendix defines 'Court' (line 40688), 'file' (40751), 'order', 'party' and 'remedy'; it defines 'Minister' (= the Minister of Justice for Alberta) but has no term 'Minister of Justice and Attorney General' - 3.15(3)(b) reads 'the Minister of Justice or the Attorney General for Canada'. (4) Related provision '11.4 (service of commencement documents in Alberta)': official 11.4 is titled 'Methods of service in Alberta'. (5) The same case is printed 'Boll v. Woodlands (Cty.) 2021 ABQB 406, JCE 1603 22391' (p.3-33 line 749 as 'Boll'; p.3-44 line 1080) and 'Bull v. Woodlands (Cty.) 2021 ABQB 406, JCE 1603 22391' (p.3-45 line 1112; p.3-47 line 1203); Book B prints 'Boll v Woodlands County, 2021 ABQB 406'. 'Cot? v. Safe Roads 2021 ABQB 313, JCE 2103 00058' is dated '(Apr 20) (¶ 12)' at p.3-45 (line 1113, joined to its name at line 1131 by reading order) and '(Apr 21) (¶'s 8-10)' at p.3-47 (line 1205, footnote 5); the accent of the first name is lost ('Cot?'). Note F.2 p.3-46 (line 1158) says 'the application to the Court of Queen's Bench is for judicial review' (the court is now King's Bench elsewhere in the note); fn 5 p.3-33 prints 'Sweat & Maxwell'. (6) Statements of law without a source in the note (for example 'Rule 3.15(2) cannot be waived', 'The equivalent Rule in the criminal judicial review Rules binds the Crown', 'Formerly time used to run until the application was heard') were not checked; the statement that the six months run from the decision or act, the sequential file-then-serve reading and 'a courtesy copy is not service' agree with the cases Book B quotes (Tartal, ENMAX). The page order of the raw text is not reading order (columns and footnotes interleave), so a sentence can be split across lines that are not adjacent (for example note D.2 p.3-34 lines 777-779).",
    "3.14": "Read in full, p.3-31 (line 718) to p.3-32 (line 740): rule text, five footnotes (kept in the text note), defined terms, related provision and a note of three paragraphs; the built "
            "commentary ends with the Subdivision 2 heading and the title of 3.15 (they match the official Subdivision 2 'Additional Rules Specific to Originating Applications for Judicial Review'). "
            "(1) Wording: (1)(c) omits 'written' before 'questions' (see the text note). (2) Defined Terms 'Court, enactment, expert, party, record': the Appendix defines each ('record' includes the "
            "representation of or a record of any information, data or other thing ...). Related Provision '3.13 (questioning on an affidavit)' - official 3.13 is 'Questioning on affidavit and "
            "questioning witnesses'. (3) The note: 'A respondent objecting to the short time for an (originating) application has a duty to tell the judge of its objection, and that it wants an "
            "adjournment to cross-examine on an affidavit'; a report by a court-appointed Inspector of a company and disclosure of back-up documents; the Record for appeal and confidential "
            "personal information; 'Materials in a legal brief, not sworn to by any affidavit, are not evidence. Nor should the book of authorities contain materials not in the record nor verified "
            "by affidavit' - the last statement agrees with the closed list in official 3.14(1) ('may consider the following evidence only'); the others are statements of law without a source in "
            "the repository (not checked). (4) Footnote 5 (history: 'Quite similar to previous 1987 Rr.753.03, 753.04. They were first enacted in 1987') and footnote 2 ('Is there then "
            "deliberative privilege?', a question in the text of the note) are printed as shown.",
    "3.13": "Read in full, p.3-30 (line 698) to p.3-31 (line 718): rule text, footnotes 1-4 (kept in the text note), defined terms, related provisions and a short note; the built commentary "
            "ends with the title of 3.14. (1) Labels read against the official titles: '[Contents of appointment notice]' - official 6.16 is 'Contents of notice of appointment'; the other "
            "brackets (3.21 'Limit on questioning', 6.20 'Form of questioning and transcript', 6.38 'Requiring attendance for questioning') match. Book A elsewhere names this rule "
            "'Questioning on an affidavit and questioning witnesses' (in its 3.14 text and R.12.25's quotation) where the official title is 'Questioning on affidavit and questioning "
            "witnesses'. (2) Related Provisions: '3.21 (questioning on judicial review)' - official 3.21 is 'Limit on questioning' (its scope to be read in the 3.21 pass); '6.7 (questioning on "
            "affidavits)' - official 6.7 'Questioning on affidavit in support, response and reply to application'; '6.8 (questioning a witness before hearing)' - official 6.8 'Questioning "
            "witness before hearing'; '12.25 (transcripts optional in family law)' - official 12.25 'Exception to rule 3.13(5)' (the transcript need not be filed if the parties agree; agrees). "
            "(3) Note: 'A non-affiant can be examined to get a transcript to use at an application, if Rr.6.16-6.20 are observed' agrees with official 3.13(2)-(4). Pointers 'On cross-examination "
            "on affidavits, see R.6.8n.' and 'On examinations to obtain evidence on a pending application, see R.6.20n.' (to be checked in Part 6). The note cites the 'Metanczuk case' "
            "(Re Metanczuk 2024 ABKB 270, fn 4) for a court using inherent or vaguely worded statutory powers to order examination of a witness by a trustee in bankruptcy (statement of law "
            "without a source in the repository; not checked). Holden (Village) v. Sen 2019 ABQB 472 (fn 3) is also cited in 3.8 (p.3-26 fn 1). (4) Book A says little on the scope of "
            "questioning; Book B has a long commentary on it (see_also).",
    "3.12": "Read in full, p.3-29 (line 670) to p.3-30 (line 698). (1) Footnotes on p.3-30 (eight, printed among the paragraphs): 1 Newell (Cty.) v. Dola 2003 ABCA 371, 6 MPLR(4th) "
            "292; 2 'Trial of an issue was ordered where the judicial review matter was complex and would require expert evidence and oral history evidence: Athabasca Tribal Council v. "
            "Min. of Env'l. Protection 1998 ABQB 879, 233 AR 97; cf. Anderson v. R. 2006 ABCA 158, 384 AR 371. See also Cdn. Natural Res. v. Encana Oil & Gas P'ship. 2008 ABCA 267, 440 AR 338'; "
            "3 Mathai v. George (M) 2018 ABQB 51, JCE 1703 09296 (Jan 22) (also 3.10 fn 1); 4 Smith v. R. 2004 ABQB 711 (also 3.2 p.3-11 fn 2); 5 'On a contested will case, see Re Serdahely Est. "
            "2002 ABQB 10, 309 AR 370'; 6 'And so the suit was begun long before this new \"statement\", and limitation periods stopped running long before: Singh v. Kaler 2017 ABCA 275, "
            "[2018] 3 WWR 284 (paras. 65-70)'; 7 'See further the C.P.E., Chapter 33, Part I'; 8 Simonelli v. Rocky View (M.D.) 2004 ABQB 45, 350 AR 286 (paras. 46-57). The built commentary "
            "ends with the title of 3.13. "
            "(2) Related Provisions read against the official text: '1.7(2) (Rules fill gaps by analogy)' agrees with official 1.7(2) ('These rules may be applied by analogy to any matter "
            "arising that is not dealt with in these rules'); '3.2(4) (wrong form of action)' - the same 3.2(4)/3.2(6) mismatch flagged under 3.2 (official 3.2(4) fixes the form of an "
            "appeal or reference; the wrong-form power is 3.2(6)). The information note ('See also rule 3.10 [Application of Part 4 and Part 5]') matches the official title of 3.10; "
            "Defined Terms 'court, rules'. "
            "(3) Identity of Book A: the passage 'If the court directs pleadings, then the party who has the onus of proof should file a document called \"Statement of the Applicant's "
            "Claim\". This is not a statement of claim, does not start a new proceeding, uses the action number of the existing suit (Originating Application), and there is no fee for "
            "filing it' (lines 694-696) is the passage that Book B quotes, in Singh v. Kaler 2017 ABCA 275 para 67, from 'the Alberta Civil Procedure Handbook 2017 (Stevenson & Cote "
            "(Edmonton: Juriliber, 2017)) ... at 3-21'; Book A calls itself 'this Handbook' (p.3-22 fn 5; Part 12 and Part 14 footnotes) and 'this book' (R.7.1 note, p.7-8). Book A is "
            "therefore very probably a later edition of that Handbook (its 3.12 page is 3-30); no file states the title or edition. "
            "(4) Read with 3.2: this note says the 'Statement of the Applicant's Claim' 'does not start a new proceeding' and that a declaration of constitutional rights does not give the "
            "respondent a right to have the Originating Application converted into an orthodox suit; 3.2's note (p.3-6) says 'R.3.12 lets the court turn an Originating Application into a "
            "full-fledged suit' (flagged under 3.2 against official 3.12, which only lets the Court direct that statement-of-claim rules apply). "
            "(5) Statements of law without a source in the repository (not checked): legislation letting a municipality have a summary hearing for an injunction does not mandate that "
            "procedure; trial of an issue at a late stage; how pleadings in an Originating Application are deemed closed. The passage on the Statement of the Applicant's Claim is "
            "supported by the Singh v. Kaler quotation in Book B.",
    "3.11": "Read in full, p.3-28 (line 634) to p.3-29 (line 670). (1) Defined Terms 'costs award, party' (the Appendix defines both) and Related Provisions '6.6 (late "
            "responses to application)': official 6.6 is 'Response and reply to application' - a near copy of 3.11 for ordinary applications; its subrule (3) deals with lack of "
            "reasonable notice (costs; no reliance without permission), so the label covers only 6.6(3). "
            "(2) Footnotes on p.3-29 (seven, printed among the paragraphs): 1 'This is unpredictable. The Court of King's Bench has a Sharepoint document management service, "
            "but counsel cannot access it directly to upload materials ...'; 2 Abel v. Modi 2020 ABQB 530, JCC 1801 14851 (Sep 11) (paras. 14-21); 3 Re Sultan Mgmt. Grp. 2023 ABCA "
            "110, Edm 2203 0085 AC (Mar 31); 4 Karmali v. Donorworx (M) 2015 ABQB 105, 610 AR 258, and TAQA Drilling Solutions v. Yar Hldg. 2021 ABQB 309, JCE 2003 08740 "
            "(Apr 26) (paras. 17-25); 5 dictum in GG & HH v. 2306084 Alta. 2022 ABQB 58, JCC 2101 07555 (para. 23); 6 'id. at para. 23'; 7 Bromley v. Robertson 2019 ABQB 79, JCC FL01 "
            "27788 (para. 1(1)). The built commentary ends with the title of 3.12. "
            "(3) Extraction: paragraphs are interleaved and sentences are split ('But a party wishing to enforce quick cross-' ... 'examination must give a formal notice and "
            "serve conduct money'; 'A supporting affidavit had been presented ... as the Bankruptcy Act' ... 'On order of filing and service, see R.3.9n.'). "
            "(4) Pointers: 'On order of filing and service, see R.3.9n.' resolves (3.9's note: filing and service is sequential, file then serve); 'On late responses to an "
            "application, see R.6.6n.' (to be checked in the 6.6 pass; official 6.6 is the matching rule). The backlog passage here ('a bad backlog in the Clerk's office led to its "
            "being stamped and filed almost two months late', line 662) is the same case as the one in R.13.41's note (p.13-102). "
            "(5) Statements of law and practice without a source in the repository (not checked): the Court of King's Bench appearance defaults in Edmonton and Calgary and the "
            "request form to the Manager, Court Coordination; that a non-party cannot file affidavits; that a party has the right to run its own case; that a late affidavit "
            "was dealt with only in costs and accepted; that cross-examination requires formal notice and conduct money.",
    "3.10": "Read in full, p.3-27 (line 616) to p.3-28 (line 633): rule text, information note, defined terms, commentary; no Related Provisions are printed. "
            "(1) Footnotes (p.3-28, line 626, printed after the running head): 1 Mathai v. George (M) 2018 ABQB 51, JCE 1703 09296 (Jan 22) (the 'Mathai case' of the "
            "commentary); 2 James H. Meek Tr. v. San Juan Res. 2005 ABCA 448, 376 AR 202, affg 2005 ABQB 9, 356 AR 72; 3 history: 'Quite similar to previous 1996 R.314.1. It was "
            "first enacted by Alta.Reg.243/96.' The built commentary ends with the title of 3.11. "
            "(2) The same 'James H. Meek Tr. v. San Juan Res.' is cited in 3.2 (p.3-15, line 299) as '2003 ABQB 1053, 356 AR 72': the AR citation is the same but the "
            "neutral citations differ (2003 ABQB 1053 there, 2005 ABQB 9 here); one is wrong and the sources do not show which. "
            "(3) The information note reads 'application of statement of claim rules to originating qpplications' ('qpplications' as printed); official 3.12 is 'Application of "
            "statement of claim rules to originating applications'. The bracket label after 4.1 says 'Responsibilities' where the official title says 'Responsibility'. "
            "(4) 'The 2021 amendment allows another Rule of Court to make Parts 4 and 5 apply to certain Originating Applications' agrees with the official text ('unless another "
            "rule otherwise provides'), the official amendment list (23/2021) and rules 12.34(1) and 12.37(1), which say 'Despite rule 3.10, Part 4 [Part 5] applies to ...'. "
            "(5) Statements of law without a source in the repository (not checked): that the chambers judge properly exercised a discretion not to adjourn for more discovery and "
            "that certain documents were ordered produced (an unnamed case).",
    "3.9": "Read in full, p.3-26 (line 586) to p.3-27 (line 616). (1) Footnotes: this rule's history footnote 6 and footnotes 7-9 (Tartal v. Human Rts. Comm'n 2023 ABKB 381 "
           "paras. 48-51; Tartal supra paras. 52 ff.; Re Can.N. Grp. infra) are printed at the top of p.3-26, inside the built 3.8 commentary (see the 3.8 flag). Page 3-27 has "
           "footnotes 1-8: 1 Re Can.N. Grp. infra; 2 Re Can. N. Group 2017 ABQB 550, JCE 1703 12327 (Sep 11) (paras. 61-67); 3 and 4 Tartal supra (paras. 52-60; 61-74); "
           "5 L.C. v. R. (Alta.) 2011 ABQB 12, 509 AR 43; 6 Baker v. Baker 2012 ABQB 296, [2012] AR Uned 340 (May 8); 7 Thompson v. Procrane 2016 ABCA 71, [2016] AJ #237 "
           "(paras. 9-10); 8 Morrison v. Galvanic Applied Sci. (M) 2017 ABQB 514, JCC 1301 11717 (Aug 22). supra/infra resolve (Re Can.N. Grp. infra -> p.3-27 fn 2; Tartal "
           "supra -> p.3-26 fn 7, 2023 ABKB 381). "
           "(2) Extraction: columns are interleaved and sentences are split across the page break (e.g. 'Hearing an originating application originally returnable less than 10 "
           "days later, but deliberately' at line 598 ends 'adjourned and actually held outside the 10 days, is not a nullity' at line 614; 'nor' ... 'require such details "
           "for valid service'). The last sentence, 'This Rule shows that Originating Applications are supposed to move swiftly and makes it easier to dismiss them for "
           "non-prosecution' (line 616), is printed just before 3.10's title; whether it belongs to 3.9 or 3.10 is not shown. "
           "(3) Statements read against the official text: 'The requirement to serve in R.3.15(2) is substantive' and the 6-month period agree with official 3.15(2) "
           "('filed and served within 6 months ... and rule 13.5 does not apply to this time period'); 'Rr.1.5(5) and 3.15(2) expressly bar extending the limitation period' "
           "agrees with official 1.5(5) (the Court must not cure ... if that would extend a period it is prohibited from extending); 'Rule 6.3 says that the applicant must "
           "file and serve, but it does not say in what order' agrees with official 6.3(3). "
           "(4) Related Provisions and notes: '11.4 (method of service)' - official 11.4 'Methods of service in Alberta'; '13.3 (counting time)' - official 13.3 'Counting "
           "days'; the information note names Part 11 [Service of Documents] (official Part 11 title); Defined Terms 'file, party' (Appendix defines both). "
           "(5) Statements of law without a source in the repository (not checked): the Canada Revenue Agency bulletin passage, insolvency legislation and reopening a "
           "decision, that late filing or service cannot be 'cured', the 'directly affected' rule, and the hearing held outside the 10 days not being a nullity.",
    "3.8": "Read in full, p.3-24 (line 547) to p.3-26 (line 586). (1) Footnotes: p.3-25 has 1-7 (kept in the text note); p.3-26 has 9. Footnotes 1-5 there "
           "belong to this note (1 Imp. Finishing v. Moderno Homes 2019 ABQB 64 para. 64; 2 Kissel v. Rocky View (Cty.) 2020 ABQB 406 para. 63; 3 Harco Hldg. "
           "2000 v. M.B. (M) 2010 ABQB 442, 500 AR 258; 4 ANC v. Min. of Agric. paras. 84-87; 5 Condo. Corp. No. 0210494 v. Rotzang 2024 ABKB 111 paras. 33-34); "
           "footnotes 6-9 (6 history 'Quite similar to previous 1968 R.310 ...', 7 Tartal v. Human Rts. Comm'n 2023 ABKB 381 paras. 48-51, 8 Tartal supra, "
           "9 Re Can.N. Grp. infra) are 3.9's - the marker '6' is printed after 3.9's text (line 587) and 3.9's page 3-27 footnotes 1-4 continue with "
           "Re Can.N. Grp. and Tartal - but sit inside the built 3.8 commentary. "
           "(2) 'ANC Timber v. Min. of Agric. 2019 ABQB 653' (p.3-25 fn 7) is short-cited 'ANC v. Min. of Agric.' (p.3-26 fn 4); 'Tole v. Lucki 2017 ABCA "
           "79, [20017] AJ #184' has an extra digit in the printed year. "
           "(3) Related Provisions: '13.18 (contents of affidavits)' - official 13.18 is 'Types of affidavit' (an affidavit may be sworn on personal knowledge or "
           "on information and belief, with the source disclosed; 13.18(3) requires personal knowledge if it supports an application that may dispose of a claim); "
           "the requirements are in 13.19 ('Requirements for affidavits'), whose label agrees. The information note calls 13.18 'Types of affidavit' (matches). "
           "3.2 agrees. "
           "(4) Sabir pointer, p.3-26 line 586: 'On power of the court to decide when a document actually received is \"filed\", thus overruling the Clerk, see "
           "Sabir v. Gill and comments, in R.3.2n., supra' - Sabir v. Gill has no full citation before p.3-43 fn 1 (2023 ABKB 679), and 3.2's own note (p.3-7 "
           "fn 2) sends the reader on to R.3.1n., which is not in the file; 'supra' therefore points nowhere earlier. "
           "(5) Other pointers: 'R.3.2n.B' and 'Rule 3.2n.B.4' resolve (3.2 Part B; B.4 Procedure and Parties carries the booking and filing warnings); "
           "'R.13.14n.' (official 13.14 is 'Endorsements on documents') and p.3-25 fn 2 'See R.3.25n.' are not checked here (to be checked in the 13.14 and 3.25 "
           "passes). "
           "(6) The sentence 'Ordinarily a party cannot get relief or go into issues, which are not in his originating pleading, and so an Originating "
           "Application seeking only declaratory relief (and costs) will not permit them' is also in 3.2 (p.3-16, C.3 Miscellaneous); the following sentence "
           "ends with an unmatched ')' ('to refuse declaratory relief)'). "
           "(7) Statements of law without a source in the repository (not checked): hearsay in affidavits cannot be received respecting contempt or the penalty; "
           "Rule 3.8(2) is no bar to reciting an admission against interest; consent or lack of objection lets the court admit hearsay in a civil case; an "
           "affidavit by a legal assistant is unacceptable except for noncontroversial matters. No Defined Terms line is printed for 3.8.",
    "3.7": "Read in full, p.3-24 (lines 542-545): rule text, information note, defined terms, related provision; no commentary. The built related-provisions "
           "field runs on into 'DIVISION 2 ACTIONS STARTED BY ORIGINATING APPLICATION *Subdivision 1 General Rules *Originating Applications and associated "
           "evidence*': these are headings printed after this rule (they match the official Division 2 'Actions Started by Originating Application', "
           "Subdivision 1 'General Rules' and the title of 3.8) and are not 3.7 material. Information note 'For enforcement of judgments and orders see Part 9 "
           "[Judgments and Orders]' - official Part 9 is 'Judgments and Orders'; related provision '9.5 (entry of judgments and orders)' - official 9.5 is "
           "'Entry of judgments and orders'; Defined Terms 'judgment, judgment creditor, judicial centre, order' - the Appendix defines each. "
           "Pointer from elsewhere: R.13.44 (p.13-104, line 5365) says 'On backlogs in accepting documents submitted for filing, see Rr.3.7 n. J, and 13.41 n.'; "
           "Book A has no note at 3.7 and no Part J in its 3.2 note (headings A-D), so the pointer lands on nothing in the file (the backlog passage is in "
           "R.13.41's note, p.13-102, lines 5269-5271).",
    "3.6": "Read in full, p.3-23 (from line 517) to p.3-24 (line 540). (1) Footnotes: p.3-23 has footnotes 1-6 shared with 3.5 (see the 3.5 flag); this "
           "rule's own are fn 1 (line 525: Christensen v. Proprietary Ind. 2002 ABQB 97, 309 AR 201; Silver Springs v. UMA Eng. supra; Hansraj v. Ao (#2) 2002 "
           "ABQB 772, 314 AR 283, affd on this point 2004 ABCA 223, 354 AR 91; C.S. v. A.J. infra; Behiels v. Tibu supra (paras. 12, 15(5), 15(6)); the text "
           "then adds Siver v. Siver 2010 ABQB 755, [2010] AR Uned 964 (Dec 1)) and fn 4 (history, marker printed after 3.6's text; the footnote text is at "
           "the end of the built 3.5 commentary). Page 3-24 has footnotes 1-3: 1 Keaton v. Keaton 2017 ABQB 429 ('See R.3.6(1). See further the C.P.E., Chapter "
           "23, Part E') and Seabolt Watershed Assn. v. Brown 2002 ABQB 795, 333 AR 193; 2 C.S. v. A.J. 2004 ABQB 73, 50 Alta LR(4th) 91; 3 'Quite similar to "
           "previous 1968 R.405. It was new in 1968.' - fn 3 is printed under the running head R.3.7(1) and, since 3.6 already has its history note (fn 4 on "
           "p.3-23), is presumably 3.7's; the book does not show. The built commentary ends with 3.7's title. "
           "(2) p.3-23 line 523: 'the fact that R.3.6 compels certain places to be suggested for trial' - official 3.6 says nothing about suggesting a place of "
           "trial: 3.6(1) fixes where the action is carried on (the centre where the claim or originating application was filed, or the centre it was "
           "transferred to) and 3.6(2) lets the Court specify another place for an application, an originating application or a trial. Line 523 also "
           "paraphrases 3.6(1)(a) as 'requiring that applications be in the judicial centre where the action was started'; the official text says 'An action "
           "must be carried on in the judicial centre ...'. "
           "(3) Related Provisions as read against the official text: '6.2 (venue of applications)' - official 6.2 ('Application to the Court to exercise its "
           "authority') says a person may apply when the Court has authority and says nothing about venue; '6.9 (jurisdiction of[applications judges] "
           "?masters?)' - official 6.9 is 'How the Court considers applications', and 6.9(2) says 'Applications may be decided by a judge or applications "
           "judge'; the label is printed with an editorial bracket and question marks; 3.2, 14.8(5) and 14.84 agree in subject. "
           "(4) supra/infra: Silver Springs supra -> 3.5 fn 13 (2004 ABQB 942); Behiels supra -> 3.5 fn 3 (2024 ABKB 12); C.S. v. A.J. infra -> p.3-24 fn 2; "
           "Christensen supra (p.3-23 fn 2) -> p.3-23 fn 1. "
           "(5) Statements of law without a source in the repository (not checked): a motion may be moved before a justice rather than an applications judge if "
           "the justice is available sooner; pre-trial steps need not be carried on where the trial is to be held if the plaintiff's choice is reasonable and the "
           "balance of convenience on applications is even; motions belong in the centre where the action was commenced if it has not been transferred.",
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
    "3.15": {"drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): in (2) the number '6' is missing ('within [ ] months after the date') and this text stands there: 'Regular v. Regular, [2016] A.J. No. 1042, 2016 ABQB 570 at para. 5 (Alta. Q.B.).'; 'application' is split as 'appli- cation' in (1); 'rule 13.5 [Variation of time periods], does not apply' has a comma after the label; the text ends inside (5) at 'must be filed and served on every other' - 'party one month or more before the date scheduled for hearing the application.' is missing and no amendment note and no commentary are printed for 3.15 in the file. Subrules (1), (3) and (4) equal the official text.",
             "drop_citations": "Regular v. Regular, 2016 ABQB 570 (para. 5) is not a 3.15 authority: it stands where the number '6' is lost in 3.15(2); the same citation is Book C's 3.5 authority (the same citation is also displaced in Book C's 3.4, 3.7 and 3.9 entries)."},
    "3.14": {"drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): it holds the lead-in and (1)(a)-(b) only, ending with a displaced bracket label "
                              "'[Disclosure of Information]' (Part 5, which belongs to (1)(c)); (1)(c)-(g) and subrule (2) are missing; no amendment note and no commentary are printed for 3.14."},
    "3.13": {
        "add_commentary_citations": [{"neutral_citation": "2000 ABQB 814", "style_of_cause": "Colortech Painting and Decorating Ltd. v. Toh",
                                      "note": "printed as 'citing , [2000] A.J. No. 1345, 2000 ABQB Colortech Painting and Decorating Ltd. v. Toh': the name is displaced and the number after '2000 ABQB' is lost; "
                                              "the number 814 is taken from Book B (Colortech Painting and Decorating Ltd. v. Toh, 2000 ABQB 814, 276 A.R. 262, quoted in its 3.13 commentary)"},
                                     {"neutral_citation": None, "style_of_cause": "Trizec Equities Ltd. v. Ellis-Don Management Services Ltd.",
                                      "note": "printed as 'citing , [1996] A.J. No. 699, 37 Alta. L.R. (3d) 442 at para. 15 (Alta. Q.B.)' with the name displaced ('Trizec Equities Ltd. v. Ellis-Don Management Services Ltd. No. 699' splits "
                                              "'[1996] A.J.' and 'No. 699'); no neutral citation is printed"}],
        "commentary_flag": "(1) 'the transcript must be filed (rule 6.20(5)(b))' agrees with official 6.20(5)(b) ('file the transcript unless the Court otherwise orders') and 3.13(5); official 12.25 lets "
                           "the parties agree it need not be filed. (2) The citations 'Colortech Painting and Decorating Ltd. v. Toh' and 'Trizec Equities Ltd. v. Ellis-Don Management Services Ltd.' are "
                           "printed in pieces (see the citation notes). (3) The last paragraph stops at 'this is subject to the overriding discretion of the court to determine whether a question should be'; "
                           "Book B's quotation of International Securities Group v. Alberta (Securities Commission), 2011 ABQB 737, para 19, ends the same thought 'to determine whether a question should be answered'. "
                           "(4) The quoted passage 'issues relevant to the application are not confined to the four corners of the affidavit' agrees with the Colortech / Leahy summary in Book B's commentary.",
    },
    "3.12": {
        "drop_c_note": "garbled copy of Book A's information note ('See also rule 3.10 [Application of Part 4 and Part 5]'): the number '4' of 'Part 4' is lost and this text stands in "
                       "its place: 'Patrus v. Alberta (Workers' Compensation Board, Appeals Commission), [2011] A.J. No. 1346,' (a displaced citation, home not shown; it also stands "
                       "displaced in Book C's 3.9 commentary); the label is printed after 'rule 3.10 .'; not retained.",
        "commentary_flag": "meaning reversed by garbling: 'This is a statement of claim and not does not start a new proceeding' - Book A and Book B (quoting the Handbook in Singh v. Kaler "
                           "para 67) say 'This is not a statement of claim, does not start a new proceeding'. The first sentence ('In some cases, the court has not required that new "
                           "pleadings be filed, but that the originating application stand as the statement of claim and commenced by way of originating application, the party who "
                           "has the onus of proof should file a document called \"Statement of the Applicant's Claim\"') is scrambled and differs from Book A's wording; the text stops "
                           "at 'it uses the action number of the existing suit (originating'.",
    },
    "3.11": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): subrule (2)(a) stops after 'serve the response affidavit or other evidence on "
                          "the respondent a' and the words 'reasonable time before the originating application is to be heard or considered, and (b) limit the response to replying to the "
                          "respondent's affidavit or other evidence.' are missing; subrules (1) and (3) equal the official text. Amendment note agrees (no amending regulation).",
        "commentary_flag": "agrees with official 3.11(1)-(3) (reasonable time; response evidence; without reasonable notice a party may not rely on the evidence unless the Court permits, "
                           "and costs may be awarded). The 'common outcome' sentences (option to proceed with the late affidavit considered and lateness dealt with in costs, or an adjournment "
                           "with thrown-away costs) are practice commentary with no source in Books A or B (Book A's note, p.3-29, describes one late affidavit accepted with the delay dealt "
                           "with in costs). Not repeated elsewhere.",
    },
    "3.9": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): the number '10' before 'days or more before the date scheduled for "
                          "hearing' is lost and this text stands in its place: 'Regular v. Regular, [2016] A.J. No. 1042, 2016 ABQB 570 at para. 9 (Alta. Q.B.); the factors were "
                          "considered and applied at length in a case where both plaintiff and plaintiff by counterclaim had proposed Edmonton as place of trial; but many years "
                          "later, after plaintiff had discontinued claim and plaintiff by counterclaim had moved to Calgary, plaintiff by counterclaim sought a change of venue: , "
                          "Behiels v. Tibu'; the bracket label '[Originating application for judicial review]' is printed after 'rule 3.15(5) , an'. The rest equals the official "
                          "text; the amendment note agrees (no amending regulation).",
        "drop_citations": "Regular v. Regular, 2016 ABQB 570 (para. 9) is not a 3.9 authority: it stands where the number '10' is lost, the passage is the one Book C also prints "
                          "in its 3.5 commentary ('Balance of Convenience Test') and, displaced, in its 3.4 text; the text is kept in the rule-text note.",
        "drop_c_note": "garbled copy of Book A's information note ('Service of documents is dealt with in Part 11 [Service of Documents]'): the number '11' is lost and this "
                       "text stands in its place: 'Regular v. Regular, [2016] A.J. No. 1042, 2016 ABQB 570 at para. 9 (Alta. Q.B.).' (the same displaced 3.5 passage); not retained.",
        "commentary_flag": "The first commentary item opens with 3.9's own sentences ('As a commencement document, a respondent is given more time to respond to an originating "
                           "application than an interlocutory application under Part [number lost; Regular v. Regular ... para. 5 stands there] of the Rules. Note that this timing "
                           "can be abridged by the court pursuant to its powers under rule [number lost]') and then runs on into rule 3.10's material: its rule text ('... [Managing "
                           "Litigation] rules 4.1, 4.2(a) ... and (d) and 4.36 apply, with all necessary modifications, to actions started by originating application unless the Court "
                           "otherwise orders'), its amendment note ('r. 3.10 effective November 1, 2010 ... Alta. Reg. 122/2012, s. 3; Alta. Reg. 23/2021, s. 2 effective March 1, "
                           "2021') and its information note ('See also rule 3.12 ... Typically, an action started by originating application will not require the same kind of "
                           "management ...'). The second item ('The court can, by order, or the parties may agree that the discovery ... the parts of Part [Patrus v. Alberta "
                           "(Workers' Compensation Board, Appeals Commission), [2011] A.J. No. 1346, stands there] that apply to such actions are: do * Division 2 - ACTIONS STARTED "
                           "BY ORIGINATING APPLICATION') is 3.10 commentary. Book C has no separate 3.10 entry, so its 3.10 material is here; nothing was dropped (to be handled in "
                           "the 3.10 pass). The first two sentences agree with official 3.9 (10 days) and 6.3(3) (5 days for an ordinary application).",
        "citation_names": {"2016 ABQB 570": "Regular v. Regular"},
        "citation_notes": {"2016 ABQB 570": "displaced 3.5 case (para. 5): it stands where a Part number is lost ('under Part [ ] of the Rules'); Book C's 3.5 entry cites Regular for onus"},
    },
    "3.8": {"drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): it holds subrule (1)(a)-(d) only; subrule (2) "
                              "('If an affidavit is filed to support an originating application, the affidavit must be confined to ...') is not in the file, "
                              "and no amendment note and no commentary are printed for 3.8. The Division heading printed for it ('Division 2 - ACTIONS STARTED BY "
                              "ORIGINATING APPLICATION') agrees with the official Division 2."},
    "3.7": {
        "drop_c_note": "garbled copy of Book A's information note ('For enforcement of judgments and orders see Part 9 [Judgments and Orders]'): the number '9' is "
                       "lost and this text stands in its place: 'Odland v. Odland Sobeys , [2017] A.J. No. 1265, 2017 ABCA 397 at paras. 19-23 (Alta. C.A.); Capital "
                       "Inc. v. Gulf & Pacific Equities Corp., [2018] A.J. No. 231, 2018 ABQB 151 at paras. 13-25 (Alta. Q.B.); and , [2016] A.J. No. 1042, 2016 ABQB "
                       "570 at para. 7 (Alta. Q.B.). Regular v. Regular'. These are the citations and paragraph numbers that Book C prints in its 3.5 commentary "
                       "('Balance of Convenience Test'), so the text is displaced 3.5 material; not retained here.",
        "commentary_flag": "'It allows the judgment creditor, if required, to move the action to the judicial centre in which the enforcement is to take place': official "
                           "3.7(1) says the judgment creditor 'may request' (on notice to the other parties) a temporary transfer 'to a different judicial centre for "
                           "purposes of an application to enforce the judgment or order'; it does not say the centre is the one where enforcement will take place. "
                           "Books A and B have no 3.7 commentary to compare.",
    },
    "3.6": {
        "drop_rule_text": "Book C's rule text is dropped (official, Book A and Book B carry the whole text): in (1) the words '(b) if the action is transferred in "
                          "accordance with rule 3.4 [Claim for possession of land]' are missing, and the label '[Transfer of an action]' stands before 'or rule 3.5 ,'; "
                          "subrule (2) equals the official text. Amendment note agrees (no amending regulation).",
        "commentary_flag": "'As noted, justices of the Court of King's Bench have province-wide jurisdiction' refers back to Book C's 3.3 commentary (same sentence "
                           "idea; not in Books A or B). It agrees in substance with official 3.6(2) (a hearing or trial 'in any place specified by the Court other than "
                           "the judicial centre'); the further statement about 'judicial centres with sporadic sittings' has no source in the repository. Book B has no "
                           "commentary on 3.6.",
    },
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
        for cid, note in MANUAL_TEXT_KEEP.get(r, {}).items():
            # raw text is NOT dropped: it holds the rule text interleaved with the whole printed note, which exists nowhere else in the packet
            rec = src.get(cid)
            if rec and rec.get("operative_text_raw"):
                rec["operative_text_note"] = note
                comp[cid]["review_note"] = note
                comp[cid].pop("differences", None)
                log.append(f"{r} {cid}: raw text KEPT (interleaved with the printed note); wording compared by eye (manual review)")
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
