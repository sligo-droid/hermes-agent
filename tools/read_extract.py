"""Dependency-free, bounded document-to-text extraction for ``read_file``.

DOCX and XLSX are hostile ZIP containers.  Intake callers can supply strict
limits; legacy ``read_file`` callers retain bounded, generous defaults.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = [
    "DocumentExtractionLimits",
    "EXTRACTABLE_EXTENSIONS",
    "ExtractionError",
    "extract_document_text",
    "is_extractable_document",
]

EXTRACTABLE_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx"})
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CHUNK = 64 * 1024


class ExtractionError(Exception):
    """Raised when a supported-looking document cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class DocumentExtractionLimits:
    max_input_bytes: int = 50 * 1024 * 1024
    max_zip_members: int = 2048
    max_member_bytes: int = 16 * 1024 * 1024
    max_expanded_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 200
    max_output_chars: int = 2_000_000
    max_notebook_cells: int = 10_000
    max_notebook_source_items: int = 20_000
    max_sheets: int = 256
    max_rows_per_sheet: int = 5000
    max_cols: int = 256
    max_shared_strings: int = 500_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class _Output:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0

    def add(self, value: str) -> None:
        if self.size + len(value) > self.limit:
            raise ExtractionError("document output exceeds its character limit")
        self.parts.append(value)
        self.size += len(value)

    def finish(self) -> str:
        return "".join(self.parts).rstrip("\n") + "\n"


def _extension(path: str) -> str:
    ext = Path(path).suffix.lower()
    return ext if ext in EXTRACTABLE_EXTENSIONS else ""


def is_extractable_document(path: str) -> bool:
    return bool(_extension(path))


def extract_document_text(
    path: str,
    limits: DocumentExtractionLimits | None = None,
    *,
    extension: str = "",
) -> str:
    limits = limits or DocumentExtractionLimits()
    ext = extension.lower() if extension.lower() in EXTRACTABLE_EXTENSIONS else _extension(path)
    if ext == ".ipynb":
        return _extract_notebook(path, limits)
    if ext == ".docx":
        return _extract_docx(path, limits)
    if ext == ".xlsx":
        return _extract_xlsx(path, limits)
    raise ExtractionError(f"Unsupported document type: {path!r}")


def _read_file_bounded(path: str, limit: int) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
        if before.st_size > limit:
            raise ExtractionError("document input exceeds its byte limit")
        data = bytearray()
        with open(path, "rb") as handle:
            while len(data) <= limit:
                chunk = handle.read(min(_CHUNK, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        if len(data) > limit:
            raise ExtractionError("document input exceeds its byte limit")
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ExtractionError("document input cannot be read safely") from exc
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ) or len(data) != before.st_size:
        raise ExtractionError("document input changed while reading")
    return bytes(data)


def _source_text(source, limits: DocumentExtractionLimits, counter: list[int]) -> str:
    if isinstance(source, str):
        counter[0] += 1
        if counter[0] > limits.max_notebook_source_items:
            raise ExtractionError("notebook source item limit exceeded")
        return source
    if isinstance(source, list):
        out = _Output(limits.max_output_chars)
        for item in source:
            if isinstance(item, str):
                counter[0] += 1
                if counter[0] > limits.max_notebook_source_items:
                    raise ExtractionError("notebook source item limit exceeded")
                out.add(item)
        return "".join(out.parts)
    return ""


def _extract_notebook(path: str, limits: DocumentExtractionLimits) -> str:
    raw = _read_file_bounded(path, limits.max_input_bytes)
    try:
        nb = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError("Not a valid notebook") from exc
    if not isinstance(nb, dict):
        raise ExtractionError("Notebook root is not an object")
    cells = nb.get("cells")
    if not isinstance(cells, list):
        cells = [
            cell
            for ws in nb.get("worksheets", [])
            if isinstance(ws, dict)
            for cell in ws.get("cells", [])
        ]
    if not cells:
        raise ExtractionError("Notebook contains no cells")
    if len(cells) > limits.max_notebook_cells:
        raise ExtractionError("notebook cell limit exceeded")
    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}
    source_items = [0]
    out = _Output(limits.max_output_chars)
    readable = 0
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") not in labels:
            continue
        typ = str(cell["cell_type"])
        counts[typ] += 1
        readable += 1
        suffix = f" {counts[typ]}" if typ != "raw" else ""
        text = _source_text(cell.get("source", ""), limits, source_items).rstrip("\n")
        out.add(f"# ── {labels[typ]} cell{suffix} ──\n{text}\n\n")
    if not readable:
        raise ExtractionError("Notebook contains no readable cells")
    return out.finish()


def _normalized_member(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ExtractionError("archive member path is unsafe")
    normalized = posixpath.normpath(name)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ExtractionError("archive member path escapes the package")
    return normalized


class _SafeZip:
    def __init__(self, path: str, limits: DocumentExtractionLimits) -> None:
        self.path = path
        self.limits = limits
        try:
            self.before = os.stat(path, follow_symlinks=False)
            if self.before.st_size > limits.max_input_bytes:
                raise ExtractionError("document input exceeds its byte limit")
            self.zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ExtractionError("document is not a valid ZIP package") from exc
        except OSError as exc:
            raise ExtractionError("document package cannot be opened safely") from exc
        infos = self.zf.infolist()
        if len(infos) > limits.max_zip_members:
            self.zf.close()
            raise ExtractionError("archive member limit exceeded")
        self.infos: dict[str, zipfile.ZipInfo] = {}
        declared_total = 0
        for info in infos:
            name = _normalized_member(info.filename)
            if name in self.infos:
                self.zf.close()
                raise ExtractionError("archive contains duplicate normalized members")
            if info.flag_bits & 0x1:
                self.zf.close()
                raise ExtractionError("encrypted archive members are unsupported")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                self.zf.close()
                raise ExtractionError("archive compression method is unsupported")
            if info.file_size < 0 or info.compress_size < 0:
                self.zf.close()
                raise ExtractionError("archive member size is invalid")
            if not info.is_dir():
                if info.file_size > limits.max_member_bytes:
                    self.zf.close()
                    raise ExtractionError("archive member exceeds its byte limit")
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > limits.max_compression_ratio:
                    self.zf.close()
                    raise ExtractionError("archive compression ratio exceeds its limit")
                declared_total += info.file_size
                if declared_total > limits.max_expanded_bytes:
                    self.zf.close()
                    raise ExtractionError("archive expanded size exceeds its limit")
            self.infos[name] = info
        self.observed_total = 0

    def __enter__(self) -> "_SafeZip":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.zf.close()
        try:
            after = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ExtractionError("document package changed while reading") from exc
        if (self.before.st_dev, self.before.st_ino, self.before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ExtractionError("document package changed while reading")

    def has(self, name: str) -> bool:
        return name in self.infos

    def read(self, name: str) -> bytes:
        info = self.infos.get(name)
        if info is None:
            raise ExtractionError(f"Missing {name}")
        if info.is_dir():
            raise ExtractionError("required archive member is a directory")
        allowed = min(
            self.limits.max_member_bytes,
            self.limits.max_expanded_bytes - self.observed_total,
        )
        if allowed < 0:
            raise ExtractionError("archive expanded size exceeds its limit")
        data = bytearray()
        try:
            with self.zf.open(info, "r") as handle:
                while len(data) <= allowed:
                    chunk = handle.read(min(_CHUNK, allowed + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ExtractionError("archive member failed integrity verification") from exc
        if len(data) > allowed:
            raise ExtractionError("archive member grew beyond its byte limit")
        if len(data) != info.file_size:
            raise ExtractionError("archive member size conflicts with its directory entry")
        self.observed_total += len(data)
        return bytes(data)

    def xml(self, name: str) -> ET.Element:
        try:
            return ET.fromstring(self.read(name))
        except ET.ParseError as exc:
            raise ExtractionError(f"Malformed XML in {name}") from exc


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _relationship_target(owner_dir: str, target: str, *, root: str) -> str:
    if (
        not target
        or "\x00" in target
        or "\\" in target
        or target.startswith(("/", "//"))
        or _SCHEME_RE.match(target)
    ):
        raise ExtractionError("OOXML relationship target is unsafe")
    normalized = posixpath.normpath(posixpath.join(owner_dir, target))
    if normalized == ".." or normalized.startswith("../") or not normalized.startswith(root + "/"):
        raise ExtractionError("OOXML relationship target escapes its package root")
    return normalized


def _relationships(
    package: _SafeZip, path: str, *, owner_dir: str, root: str
) -> dict[str, str]:
    if not package.has(path):
        return {}
    xml = package.xml(path)
    tag = f"{{{_NS_PKG_REL}}}Relationship"
    result: dict[str, str] = {}
    for rel in xml.iter(tag):
        rid = str(rel.get("Id") or "")
        if not rid or rid in result:
            raise ExtractionError("OOXML relationship identity is duplicate or missing")
        if str(rel.get("TargetMode") or "").lower() == "external":
            raise ExtractionError("external OOXML relationships are unsupported")
        target = _relationship_target(owner_dir, str(rel.get("Target") or ""), root=root)
        if not package.has(target):
            raise ExtractionError("OOXML relationship target is missing")
        result[rid] = target
    return result


def _extract_docx(path: str, limits: DocumentExtractionLimits) -> str:
    with _SafeZip(path, limits) as package:
        _relationships(
            package,
            "word/_rels/document.xml.rels",
            owner_dir="word",
            root="word",
        )
        root = package.xml("word/document.xml")
    w = f"{{{_NS_W}}}"
    out = _Output(limits.max_output_chars)
    readable = False
    for para in root.iter(f"{w}p"):
        buf: list[str] = []
        for node in para.iter():
            if node.tag == f"{w}t":
                buf.append(node.text or "")
            elif node.tag == f"{w}tab":
                buf.append("\t")
            elif node.tag in {f"{w}br", f"{w}cr"}:
                buf.append("\n")
        value = "".join(buf)
        if value.strip():
            readable = True
        out.add(value + "\n")
    if not readable:
        raise ExtractionError("DOCX contains no extractable text")
    return out.finish()


def _extract_xlsx(path: str, limits: DocumentExtractionLimits) -> str:
    with _SafeZip(path, limits) as package:
        workbook = package.xml("xl/workbook.xml")
        rels = _relationships(
            package,
            "xl/_rels/workbook.xml.rels",
            owner_dir="xl",
            root="xl",
        )
        shared = _shared_strings(package, limits)
        sheets = _workbook_sheets(workbook, limits)
        out = _Output(limits.max_output_chars)
        visible = 0
        for name, state, rid in sheets:
            if state in {"hidden", "veryHidden"}:
                continue
            part = rels.get(rid)
            if not part:
                raise ExtractionError("worksheet relationship is missing")
            rows = _sheet_rows(package.read(part), shared, limits)
            visible += 1
            out.add(f"# ── Sheet: {name} ──\n")
            if rows:
                for row in rows:
                    out.add("\t".join(row) + "\n")
            else:
                out.add("(empty)\n")
            out.add("\n")
    if not visible:
        raise ExtractionError("XLSX has no visible sheets with content")
    return out.finish()


def _shared_strings(package: _SafeZip, limits: DocumentExtractionLimits) -> list[str]:
    if not package.has("xl/sharedStrings.xml"):
        return []
    try:
        root = ET.fromstring(package.read("xl/sharedStrings.xml"))
    except ET.ParseError as exc:
        raise ExtractionError("Malformed XML in xl/sharedStrings.xml") from exc
    s = f"{{{_NS_S}}}"
    result: list[str] = []
    for item in root.iter(f"{s}si"):
        if len(result) >= limits.max_shared_strings:
            raise ExtractionError("XLSX shared string limit exceeded")
        result.append("".join(t.text or "" for t in item.iter(f"{s}t")))
    return result


def _workbook_sheets(
    root: ET.Element, limits: DocumentExtractionLimits
) -> list[tuple[str, str, str]]:
    s, r = f"{{{_NS_S}}}", f"{{{_NS_REL}}}"
    sheets = [
        (sheet.get("name", "Sheet"), sheet.get("state", "visible"), sheet.get(f"{r}id", ""))
        for sheet in root.iter(f"{s}sheet")
    ]
    if len(sheets) > limits.max_sheets:
        raise ExtractionError("XLSX sheet limit exceeded")
    return sheets


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return max(idx - 1, 0)


def _sheet_rows(
    xml_bytes: bytes, shared: list[str], limits: DocumentExtractionLimits
) -> list[list[str]]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ExtractionError("Malformed worksheet XML") from exc
    s = f"{{{_NS_S}}}"
    rows: list[list[str]] = []
    for row in root.iter(f"{s}row"):
        if len(rows) >= limits.max_rows_per_sheet:
            raise ExtractionError("XLSX row limit exceeded")
        cells: dict[int, str] = {}
        max_col = -1
        for cell in row.iter(f"{s}c"):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col >= limits.max_cols:
                raise ExtractionError("XLSX column limit exceeded")
            cells[col] = _cell_value(cell, shared, s)
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)] if max_col >= 0 else [])
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    return rows


def _cell_value(cell: ET.Element, shared: list[str], s: str) -> str:
    value = cell.findtext(f"{s}v") or ""
    typ = cell.get("t", "")
    if typ == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if typ == "inlineStr":
        inline = cell.find(f"{s}is")
        return "" if inline is None else "".join(t.text or "" for t in inline.iter(f"{s}t"))
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    if typ == "e":
        return value or "#ERROR"
    return value
