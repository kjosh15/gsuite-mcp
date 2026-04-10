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

    Single-run case: entire find_text exists within one <w:t>. Extended in
    Task 16 to handle multi-run matches within a paragraph.
    Raises NotFoundError if find_text is not located.
    Raises CrossParagraphError if the match spans paragraph boundaries.
    """
    _register_namespace()

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        document_xml = z.read("word/document.xml")
        all_names = z.namelist()
        other_files = {
            name: z.read(name) for name in all_names if name != "word/document.xml"
        }

    root = ET.fromstring(document_xml)
    rev_counter = [100]  # arbitrary starting ID
    date = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    found = False
    # Iterate paragraphs
    for para in root.iter(f"{W}p"):
        runs = list(para.findall(f"{W}r"))
        # Single-run scan: does any single run's <w:t> text contain find_text?
        for run in runs:
            t_elems = run.findall(f"{W}t")
            if not t_elems:
                continue
            t = t_elems[0]
            if t.text and find_text in t.text:
                # Found in this single run. Split the text.
                before, after = t.text.split(find_text, 1)
                rpr = run.find(f"{W}rPr")
                # Mutate the current run to contain only "before"
                t.text = before
                # Build del and ins elements
                del_el = _make_del(author, date, _next_id(rev_counter), find_text, rpr)
                ins_el = _make_ins(author, date, _next_id(rev_counter), replace_text, rpr)
                # Build a trailing run with "after"
                trailing_run = None
                if after:
                    trailing_run = ET.Element(f"{W}r")
                    if rpr is not None:
                        trailing_run.append(copy.deepcopy(rpr))
                    trailing_t = ET.SubElement(
                        trailing_run, f"{W}t", {"xml:space": "preserve"}
                    )
                    trailing_t.text = after
                # Insert del, ins, (trailing_run?) immediately after `run` in para
                para_children = list(para)
                insert_at = para_children.index(run) + 1
                para.insert(insert_at, del_el)
                para.insert(insert_at + 1, ins_el)
                if trailing_run is not None:
                    para.insert(insert_at + 2, trailing_run)
                found = True
                break
        if found:
            break

    if not found:
        raise NotFoundError(
            f"find_text not located in a single run (multi-run lookup in Task 16): {find_text!r}"
        )

    # Re-serialize and rebuild the zip
    new_document_xml = ET.tostring(root, xml_declaration=True, encoding="UTF-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", new_document_xml)
        for name, data in other_files.items():
            z.writestr(name, data)
    return out.getvalue()
