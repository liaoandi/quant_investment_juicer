#!/usr/bin/env python3
import argparse
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
REL_NS = {"pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def w_tag(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def paragraph_text(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == w_tag("t") and node.text:
            parts.append(node.text)
        elif node.tag == w_tag("tab"):
            parts.append("    ")
        elif node.tag == w_tag("br"):
            parts.append("\n")
    return "".join(parts).strip()


def paragraph_image_rids(p: ET.Element) -> list[str]:
    rid_key = f"{{{R_NS}}}embed"
    out = []
    for node in p.findall(f".//{{{A_NS}}}blip"):
        rid = node.attrib.get(rid_key, "").strip()
        if rid:
            out.append(rid)
    return out


def paragraph_style(p: ET.Element) -> str:
    style = p.find("./w:pPr/w:pStyle", NS)
    if style is None:
        return ""
    return style.attrib.get(w_tag("val"), "").strip()


def heading_level(style: str) -> int:
    if not style:
        return 0

    style_low = style.lower()
    m = re.search(r"heading\s*([1-6])", style_low)
    if m:
        return int(m.group(1))

    m = re.search(r"标题\s*([1-6])", style)
    if m:
        return int(m.group(1))

    if style_low in {"title", "标题"}:
        return 1

    return 0


def is_bullet(p: ET.Element) -> bool:
    return p.find("./w:pPr/w:numPr", NS) is not None


def cell_text(tc: ET.Element) -> str:
    items = []
    for p in tc.findall("./w:p", NS):
        t = paragraph_text(p)
        if t:
            items.append(t.replace("\n", " "))
    return " ".join(items).strip()


def table_to_md(tbl: ET.Element) -> list[str]:
    rows = []
    for tr in tbl.findall("./w:tr", NS):
        cols = [cell_text(tc) for tc in tr.findall("./w:tc", NS)]
        if cols:
            rows.append(cols)

    if not rows:
        return []

    # Skip image-only placeholder tables.
    has_any_text = any(any(c.strip() for c in r) for r in rows)
    if not has_any_text:
        return []

    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (width - len(r)))

    lines = []
    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return lines


def load_image_relationships(zf: zipfile.ZipFile) -> dict[str, str]:
    rel_xml = zf.read("word/_rels/document.xml.rels")
    root = ET.fromstring(rel_xml)
    out: dict[str, str] = {}
    for rel in root.findall("pr:Relationship", REL_NS):
        rid = rel.attrib.get("Id", "").strip()
        typ = rel.attrib.get("Type", "")
        target = rel.attrib.get("Target", "").strip()
        if rid and "image" in typ and target:
            out[rid] = target
    return out


def normalize_word_target(target: str) -> str:
    # Targets in rels are relative to "word/".
    norm = posixpath.normpath(target).replace("\\", "/")
    return f"word/{norm}"


def export_image(
    zf: zipfile.ZipFile,
    rid: str,
    rel_map: dict[str, str],
    image_dir: Path,
    copied: set[str],
) -> Path | None:
    target = rel_map.get(rid)
    if not target:
        return None
    source = normalize_word_target(target)
    if source not in copied:
        payload = zf.read(source)
        name = Path(source).name
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / name).write_bytes(payload)
        copied.add(source)
    return image_dir / Path(source).name


def docx_to_markdown(docx_path: Path, image_dir: Path) -> str:
    with zipfile.ZipFile(docx_path) as zf:
        xml = zf.read("word/document.xml")
        rel_map = load_image_relationships(zf)

        root = ET.fromstring(xml)
        body = root.find("./w:body", NS)
        if body is None:
            return ""

        lines: list[str] = []
        copied_sources: set[str] = set()
        emitted_rids: set[str] = set()
        md_image_prefix = image_dir.name

        for child in body:
            if child.tag == w_tag("p"):
                text = paragraph_text(child)
                img_rids = paragraph_image_rids(child)

                if text:
                    inline_heading = re.match(r"^(#{1,6})\s+(.+)$", text)
                    if inline_heading:
                        lines.append(text)
                        lines.append("")
                    else:
                        level = heading_level(paragraph_style(child))
                        if level > 0:
                            lines.append(f"{'#' * level} {text}")
                        elif is_bullet(child):
                            lines.append(f"- {text}")
                        else:
                            lines.append(text)
                        lines.append("")

                for rid in img_rids:
                    if rid in emitted_rids:
                        continue
                    out_path = export_image(zf, rid, rel_map, image_dir, copied_sources)
                    if not out_path:
                        continue
                    lines.append(f"![图表]({md_image_prefix}/{out_path.name})")
                    lines.append("")
                    emitted_rids.add(rid)
            elif child.tag == w_tag("tbl"):
                tbl_lines = table_to_md(child)
                if tbl_lines:
                    lines.extend(tbl_lines)
                    lines.append("")

                # Some image blocks are wrapped inside tables in this document.
                for rid in paragraph_image_rids(child):
                    if rid in emitted_rids:
                        continue
                    out_path = export_image(zf, rid, rel_map, image_dir, copied_sources)
                    if not out_path:
                        continue
                    lines.append(f"![图表]({md_image_prefix}/{out_path.name})")
                    lines.append("")
                    emitted_rids.add(rid)

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert .docx to basic markdown.")
    parser.add_argument("docx", type=Path, help="Input .docx file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output .md file (default: same name next to input)",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        help="Directory to export embedded images (default: <output_stem>_assets)",
    )
    args = parser.parse_args()

    if not args.docx.exists():
        raise FileNotFoundError(f"Input file not found: {args.docx}")

    out = args.output if args.output else args.docx.with_suffix(".md")
    image_dir = args.image_dir if args.image_dir else out.with_suffix("")
    image_dir = image_dir.parent / f"{image_dir.name}_assets"
    md = docx_to_markdown(args.docx, image_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote markdown: {out}")
    print(f"Exported images: {image_dir}")


if __name__ == "__main__":
    main()
