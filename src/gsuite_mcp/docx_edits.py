"""OOXML tracked-change manipulation for .docx files.

Pure functions: input bytes → output bytes. No I/O. No Drive API.
"""

import copy
import datetime as _dt
import io
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"


class NotFoundError(ValueError):
    """find_text was not located in the document."""


class CrossParagraphError(ValueError):
    """find_text spans a paragraph boundary (not supported in v1)."""


def _register_namespace() -> None:
    ET.register_namespace("w", W_NS)


def _next_id(counter: list[int]) -> int:
    counter[0] += 1
    return counter[0]


def _make_del(author: str, date: str, rev_id: int, deleted_text: str,
              rpr: Optional[ET.Element]) -> ET.Element:
    del_el = ET.Element(f"{W}del", {
        f"{W}id": str(rev_id),
        f"{W}author": author,
        f"{W}date": date,
    })
    r = ET.SubElement(del_el, f"{W}r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    dt = ET.SubElement(r, f"{W}delText", {"xml:space": "preserve"})
    dt.text = deleted_text
    return del_el


def _make_ins(author: str, date: str, rev_id: int, inserted_text: str,
              rpr: Optional[ET.Element]) -> ET.Element:
    ins_el = ET.Element(f"{W}ins", {
        f"{W}id": str(rev_id),
        f"{W}author": author,
        f"{W}date": date,
    })
    r = ET.SubElement(ins_el, f"{W}r")
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = ET.SubElement(r, f"{W}t", {"xml:space": "preserve"})
    t.text = inserted_text
    return ins_el


def insert_tracked_change(
    docx_bytes: bytes,
    find_text: str,
    replace_text: str,
    author: str,
) -> bytes:
    """Insert tracked-change revision marks for find_text → replace_text.

    Handles matches within a single paragraph, spanning any number of runs.
    Cross-paragraph matches raise CrossParagraphError.
    """
    _register_namespace()

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        document_xml = z.read("word/document.xml")
        all_names = z.namelist()
        other_files = {
            name: z.read(name) for name in all_names if name != "word/document.xml"
        }

    root = ET.fromstring(document_xml)
    rev_counter = [100]
    date = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def paragraph_flat(
        p: ET.Element,
    ) -> tuple[str, list[tuple[ET.Element, int, int]]]:
        """Return (concatenated_text, list of (run_element, start_in_flat, end_in_flat))."""
        flat: list[str] = []
        runs: list[tuple[ET.Element, int, int]] = []
        cursor = 0
        for run in p.findall(f"{W}r"):
            t = run.find(f"{W}t")
            if t is None:
                continue
            text = t.text or ""
            runs.append((run, cursor, cursor + len(text)))
            flat.append(text)
            cursor += len(text)
        return "".join(flat), runs

    found = False
    for para in list(root.iter(f"{W}p")):
        flat, runs = paragraph_flat(para)
        idx = flat.find(find_text)
        if idx < 0:
            continue

        # Found within this paragraph. Locate start and end runs.
        match_end = idx + len(find_text)
        start_run_entry = None
        end_run_entry = None
        for entry in runs:
            run_el, r_start, r_end = entry
            if r_start <= idx < r_end and start_run_entry is None:
                start_run_entry = entry
            if r_start < match_end <= r_end:
                end_run_entry = entry
        if start_run_entry is None or end_run_entry is None:
            # Should not happen, but defensive
            raise NotFoundError(
                f"internal: could not locate runs for {find_text!r}"
            )

        start_run, start_r_start, _ = start_run_entry
        end_run, end_r_start, end_r_end = end_run_entry
        start_offset = idx - start_r_start
        end_offset = match_end - end_r_start

        start_t = start_run.find(f"{W}t")
        end_t = end_run.find(f"{W}t")
        start_text = start_t.text or ""
        end_text = end_t.text or ""

        # Capture rPr from the starting run for formatting inheritance
        start_rpr = start_run.find(f"{W}rPr")

        # Build deleted text = tail of start_run + all intermediate run text + head of end_run
        if start_run is end_run:
            deleted_text = start_text[start_offset:end_offset]
            head = start_text[:start_offset]
            tail = end_text[end_offset:]
        else:
            head = start_text[:start_offset]
            tail = end_text[end_offset:]
            deleted_parts = [start_text[start_offset:]]
            start_idx_in_runs = runs.index(start_run_entry)
            end_idx_in_runs = runs.index(end_run_entry)
            for entry in runs[start_idx_in_runs + 1:end_idx_in_runs]:
                mid_run = entry[0]
                mid_t = mid_run.find(f"{W}t")
                if mid_t is not None and mid_t.text:
                    deleted_parts.append(mid_t.text)
            deleted_parts.append(end_text[:end_offset])
            deleted_text = "".join(deleted_parts)

        # Mutate start_run: keep only "head" (may be empty string)
        start_t.text = head
        start_t.set("xml:space", "preserve")

        # Remove intermediate runs (between start and end, exclusive) from the paragraph
        if start_run is not end_run:
            start_idx_in_runs = runs.index(start_run_entry)
            end_idx_in_runs = runs.index(end_run_entry)
            for entry in runs[start_idx_in_runs + 1:end_idx_in_runs]:
                para.remove(entry[0])
            # Mutate end_run: keep only "tail" (may be empty)
            end_t.text = tail
            end_t.set("xml:space", "preserve")

        # Build del and ins
        del_el = _make_del(
            author, date, _next_id(rev_counter), deleted_text, start_rpr
        )
        ins_el = _make_ins(
            author, date, _next_id(rev_counter), replace_text, start_rpr
        )

        # Insert del + ins right after start_run
        para_children = list(para)
        insert_at = para_children.index(start_run) + 1
        para.insert(insert_at, del_el)
        para.insert(insert_at + 1, ins_el)

        # If single-run case and there's a non-empty tail, we also need to split
        # the start_run into a trailing run (because we already mutated start_t
        # to contain only `head`, the tail is lost without a new trailing run).
        if start_run is end_run and tail:
            trailing_run = ET.Element(f"{W}r")
            if start_rpr is not None:
                trailing_run.append(copy.deepcopy(start_rpr))
            trailing_t = ET.SubElement(
                trailing_run, f"{W}t", {"xml:space": "preserve"}
            )
            trailing_t.text = tail
            para.insert(insert_at + 2, trailing_run)

        # If start_run is now empty (head == "") and it's a different run from end_run,
        # it's acceptable to leave an empty run; Word handles it. Remove for cleanliness.
        if not head and start_run is not end_run:
            para.remove(start_run)

        # If end_run is now empty (tail == "") and different from start_run, remove it too.
        if start_run is not end_run and not tail:
            if end_run in list(para):
                para.remove(end_run)

        found = True
        break

    if not found:
        # Check if it spans paragraphs (for better error message)
        whole = "".join((t.text or "") for t in root.iter(f"{W}t"))
        if find_text in whole:
            raise CrossParagraphError(
                f"find_text spans a paragraph boundary (not supported): {find_text!r}"
            )
        raise NotFoundError(f"find_text not located: {find_text!r}")

    # Re-serialize and rebuild the zip
    new_document_xml = ET.tostring(root, xml_declaration=True, encoding="UTF-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", new_document_xml)
        for name, data in other_files.items():
            z.writestr(name, data)
    return out.getvalue()
