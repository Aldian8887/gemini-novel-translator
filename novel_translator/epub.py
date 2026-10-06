"""EPUB ZIP preservation: translate text slots, never rebuild HTML from model output."""
from __future__ import annotations

import copy
import html.entities
import io
import os
import posixpath
import re
import stat
import subprocess
import tempfile
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from lxml import etree as ET

from .errors import InvalidEpub, InvalidResponse
from .models import Run, Unit, digest, validate_text
from .storage import check_cancel, publish_file

XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
MAX_ARCHIVE = 150 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
MAX_MEMBER = 32 * 1024 * 1024
BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article",
          "li", "blockquote", "figcaption", "dt", "dd", "td", "th", "caption",
          "title", "body", "a", "label", "navLabel", "docTitle", "docAuthor"}
EXCLUDED = {"script", "style", "code", "pre", "kbd", "samp", "math", "svg"}
ENGLISH = re.compile(r"^en(?:[-_].*)?$", re.I)


def local(element):
    return ET.QName(element).localname if isinstance(element.tag, str) else ""


def parse_xml(data: bytes, name: str):
    parser = ET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                          recover=False, remove_blank_text=False, strip_cdata=False,
                          huge_tree=False)
    try:
        # libxml2 can silently drop unresolved entities in attributes when DTD
        # loading is disabled. Resolve only the standard XHTML entity vocabulary
        # locally, before parsing; never load an external DTD or expand user DTDs.
        encoding = "utf-8-sig"
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif data.startswith(b"\x00<\x00?"):
            encoding = "utf-16-be"
        elif data.startswith(b"<\x00?\x00"):
            encoding = "utf-16-le"
        else:
            match = re.match(br"\s*<\?xml[^>]*encoding\s*=\s*['\"]([^'\"]+)['\"]", data[:200])
            if match:
                encoding = match[1].decode("ascii")
        decoded = data.decode(encoding)
        protected = r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>"
        if "<!ENTITY" in re.sub(protected, "", decoded).upper():
            raise InvalidEpub(f"Deklarasi entity khusus tidak didukung: {name}")

        def entity(match):
            key = match.group(1)
            if key is None or key in {"amp", "lt", "gt", "quot", "apos"}:
                return match[0]
            codepoint = html.entities.name2codepoint.get(key)
            if codepoint is None:
                raise InvalidEpub(f"Entity XML tidak dikenal: {name}")
            return f"&#{codepoint};"

        decoded = re.sub(protected + r"|&([A-Za-z][A-Za-z0-9]*);", entity, decoded)
        decoded = re.sub(r"^(\s*<\?xml[^>]*encoding\s*=\s*['\"])[^'\"]+",
                         lambda m: m[1] + "utf-8", decoded)
        data = decoded.encode("utf-8")
        tree = ET.parse(io.BytesIO(data), parser)
        # No user-defined entities, including internal DTD expansion. A plain
        # EPUB 2 XHTML external DOCTYPE is preserved without fetching it.
        for dtd in (tree.docinfo.internalDTD, tree.docinfo.externalDTD):
            if dtd is not None and list(dtd.iterentities()):
                raise InvalidEpub(f"Deklarasi entity khusus tidak didukung: {name}")
        if any(isinstance(n, ET._Entity) for n in tree.iter()):
            raise InvalidEpub(f"Entity XML tidak terselesaikan: {name}")
        return tree
    except (ET.XMLSyntaxError, ValueError, LookupError, UnicodeError):
        raise InvalidEpub(f"XML tidak valid: {name}. Perbaiki EPUB sumber terlebih dahulu.") from None


def resolve_reference(base: str, href: str):
    try:
        url = urlsplit(href)
    except ValueError:
        raise InvalidEpub(f"URL tidak valid dalam {base}") from None
    if url.scheme or url.netloc:
        return None
    path = unquote(url.path)
    if "\\" in path or "\0" in path:
        raise InvalidEpub(f"Path tidak aman dalam {base}")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    if path.startswith("/") or path == ".." or path.startswith("../"):
        raise InvalidEpub(f"Referensi keluar dari arsip dalam {base}")
    return path, unquote(url.fragment)


def parts_exact(text, limit=1800):
    """Partition without normalizing a single source character."""
    while len(text) > limit:
        cut = max(text.rfind(" ", 0, limit + 1), text.rfind("\n", 0, limit + 1))
        if cut < limit // 3:
            cut = limit
        else:
            cut += 1
        yield text[:cut]
        text = text[cut:]
    if text:
        yield text


def context_text(node):
    pieces = []

    def visit(el):
        if local(el) in EXCLUDED or not isinstance(el.tag, str):
            return
        if el.text:
            pieces.append(el.text)
        for child in el:
            if local(child) == "br":
                pieces.append("\n")
            visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(node)
    return "".join(pieces)


def eligible(node):
    # Nearest explicit language declaration wins; translate=no on any ancestor wins.
    language = None
    for el in (node, *node.iterancestors()):
        if local(el) in EXCLUDED:
            return False
        if el.get("translate", "").lower() == "no" or "notranslate" in el.get("class", "").split():
            return False
        if language is None:
            language = el.get(XML_LANG) or el.get("lang")
    return not language or bool(ENGLISH.fullmatch(language.strip()))


@dataclass
class Slot:
    node_index: int
    field: str
    original: str
    parts: list


@dataclass
class Document:
    path: str
    tree: object
    slots: list[Slot]
    kind: str


class EpubBook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.documents = OrderedDict()
        self.units = []
        self.warnings = []
        self._load()

    def _load(self):
        if self.path.stat().st_size > MAX_ARCHIVE:
            raise InvalidEpub("EPUB melebihi batas 150 MiB.")
        # Snapshot source bytes once: parsing and source hash refer to the same input.
        source = self.path.read_bytes()
        self.source_hash = digest(source)
        try:
            with zipfile.ZipFile(io.BytesIO(source)) as archive:
                self.infos = archive.infolist()
                if len(self.infos) > 10000:
                    raise InvalidEpub("EPUB memiliki terlalu banyak anggota arsip.")
                seen, size = set(), 0
                for info in self.infos:
                    name = info.filename
                    pp = PurePosixPath(name)
                    if (not name or pp.is_absolute() or ".." in pp.parts or "\\" in name
                            or "\0" in name or re.match(r"^[a-zA-Z]:", name)
                            or name.rstrip("/") != str(pp) or name in seen):
                        raise InvalidEpub("Arsip memiliki path tidak aman atau nama anggota duplikat.")
                    seen.add(name)
                    if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                        raise InvalidEpub("Symlink atau ZIP terenkripsi tidak didukung.")
                    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise InvalidEpub("Metode kompresi ZIP tidak didukung EPUB.")
                    size += info.file_size
                    if info.file_size > MAX_MEMBER or size > MAX_TOTAL:
                        raise InvalidEpub("Ukuran EPUB setelah dekompresi melebihi batas aman.")
                self.entries = {i.filename: archive.read(i) for i in self.infos}
                self.comment = archive.comment
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise InvalidEpub("ZIP EPUB rusak, terenkripsi, atau CRC tidak cocok.") from None
        if self.entries.get("mimetype") != b"application/epub+zip":
            raise InvalidEpub("mimetype EPUB tidak valid.")
        if "META-INF/signatures.xml" in self.entries:
            raise InvalidEpub("EPUB bertanda tangan digital tidak didukung karena terjemahan membatalkan tanda tangan.")
        container_data = self.entries.get("META-INF/container.xml")
        if container_data is None:
            raise InvalidEpub("META-INF/container.xml tidak ditemukan.")
        container = parse_xml(container_data, "container.xml")
        roots = container.findall(f".//{{{CONTAINER}}}rootfile")
        if len(roots) != 1:
            raise InvalidEpub("Diperlukan EPUB dengan satu rootfile/rendition.")
        self.opf_path = roots[0].get("full-path", "")
        if self.opf_path not in self.entries:
            raise InvalidEpub("Package OPF tidak ditemukan.")
        self.opf = parse_xml(self.entries[self.opf_path], self.opf_path)
        root = self.opf.getroot()
        if root.tag != f"{{{OPF}}}package" or root.get("version") not in ("2.0", "3.0"):
            raise InvalidEpub("Package harus EPUB 2.0 atau EPUB 3.0.")
        metadata = root.find(f"{{{OPF}}}metadata")
        if metadata is None or not metadata.findall(f"{{{DC}}}language"):
            raise InvalidEpub("Metadata bahasa EPUB tidak ada.")
        titles = metadata.findall(f"{{{DC}}}title")
        self.title = "".join(titles[0].itertext()) if titles else self.path.stem
        self._check_encryption()
        manifest = root.find(f"{{{OPF}}}manifest")
        spine = root.find(f"{{{OPF}}}spine")
        if manifest is None or spine is None:
            raise InvalidEpub("Manifest atau spine tidak ditemukan.")
        items, paths = OrderedDict(), set()
        for item in manifest:
            if local(item) != "item":
                continue
            uid = item.get("id")
            href = item.get("href")
            if not uid or uid in items or not href:
                raise InvalidEpub("Manifest memiliki ID duplikat/kosong atau href kosong.")
            reference = resolve_reference(self.opf_path, href)
            target = reference[0] if reference else None
            if target is not None and (target not in self.entries or target in paths or reference[1]):
                raise InvalidEpub("Manifest menunjuk anggota yang hilang, duplikat, atau berfragmen.")
            paths.add(target)
            items[uid] = (target, item.get("media-type", ""), item.get("properties", ""))
        ordered = []
        for ref in spine:
            if local(ref) != "itemref":
                continue
            uid = ref.get("idref")
            if uid not in items:
                raise InvalidEpub("Spine menunjuk ID manifest yang tidak ada.")
            if items[uid][0] is None or items[uid][1] != "application/xhtml+xml":
                raise InvalidEpub("Spine non-XHTML/remote belum didukung; gunakan EPUB reflowable XHTML.")
            if uid not in ordered:
                ordered.append(uid)
        if not ordered:
            raise InvalidEpub("Spine EPUB kosong.")
        ordered.extend(uid for uid in items if uid not in ordered)
        for uid in ordered:
            name, media, _ = items[uid]
            if name and media in ("application/xhtml+xml", "application/x-dtbncx+xml"):
                kind = "xhtml" if media == "application/xhtml+xml" else "ncx"
                tree = parse_xml(self.entries[name], name)
                expected = f"{{{XHTML}}}html" if kind == "xhtml" else f"{{{NCX}}}ncx"
                if tree.getroot().tag != expected:
                    raise InvalidEpub(f"Namespace/root dokumen tidak sesuai: {name}")
                self.documents[name] = Document(name, tree, [], kind)
        self.warnings.extend(self._link_warnings())
        self._extract()

    def _check_encryption(self):
        data = self.entries.get("META-INF/encryption.xml")
        if data is None:
            return
        tree = parse_xml(data, "encryption.xml")
        allowed = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
        methods = tree.xpath("//*[local-name()='EncryptionMethod']")
        if not methods or any(m.get("Algorithm") not in allowed for m in methods):
            raise InvalidEpub("EPUB memakai DRM/enkripsi yang tidak didukung.")
        self.warnings.append("Obfuscation font dipertahankan bersama identifier aslinya.")

    def _link_warnings(self):
        warnings = []
        anchors = {}
        for name, doc in self.documents.items():
            anchors[name] = {e.get("id") for e in doc.tree.iter() if isinstance(e.tag, str) and e.get("id")}
        for name, doc in self.documents.items():
            for el in doc.tree.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr, value in el.attrib.items():
                    if ET.QName(attr).localname not in ("href", "src"):
                        continue
                    ref = resolve_reference(name, value)
                    if ref is None:
                        continue
                    path, fragment = ref
                    if path not in self.entries:
                        warnings.append(f"Referensi lokal sudah hilang di sumber: {name} -> {value}")
                    elif fragment and path in anchors and fragment not in anchors[path]:
                        warnings.append(f"Anchor sudah hilang di sumber: {name} -> {value}")
        return sorted(set(warnings))

    def _extract(self):
        for doc_index, doc in enumerate(self.documents.values()):
            nodes = list(doc.tree.iter())
            indexes = {node: index for index, node in enumerate(nodes)}
            groups = OrderedDict()
            contexts = {}

            def add(node, field, parent, owner=None):
                original = node.get(field[1:]) if field.startswith("@") else getattr(node, field)
                if not original or not re.search(r"[A-Za-z]", original) or not eligible(parent):
                    return
                if owner is None:
                    owner = parent
                    while local(owner) not in BLOCKS and owner.getparent() is not None:
                        owner = owner.getparent()
                owner_index = indexes[owner]
                if owner_index not in contexts:
                    contexts[owner_index] = context_text(owner)
                context = contexts[owner_index]
                # Keep local context near this slot for very large paragraphs.
                if len(context) > 5000:
                    start = max(0, context.find(original) - 500)
                    context = context[start:start + 5000]
                style = "/".join(reversed([local(e) for e in (parent, *parent.iterancestors())
                                          if local(e) not in {"html", "body"}]))
                slot = Slot(indexes[node], field, original, [])
                for part_index, piece in enumerate(parts_exact(original)):
                    core = piece.strip()
                    if not core or not re.search(r"[A-Za-z]", core):
                        slot.parts.append(piece)
                        continue
                    prefix = piece[:len(piece) - len(piece.lstrip())]
                    suffix = piece[len(piece.rstrip()):]
                    field_id = field.replace("@", "attr-")
                    rid = f"d{doc_index}n{indexes[node]}-{field_id}-{part_index}"
                    run = Run(rid, core, style, prefix, suffix)
                    slot.parts.append(run)
                    key = (owner_index, field if field.startswith("@") else "text")
                    groups.setdefault(key, []).append((run, context))
                if any(isinstance(p, Run) for p in slot.parts):
                    doc.slots.append(slot)

            def walk(node):
                if not isinstance(node.tag, str):
                    return
                name = local(node)
                if doc.kind == "ncx":
                    if name == "text" and local(node.getparent()) in {"navLabel", "docTitle", "docAuthor"}:
                        add(node, "text", node)
                elif name not in EXCLUDED:
                    # Translate document title plus all body text, including bare divs,
                    # tables, parent/tail text, footnotes, and navigation labels.
                    in_body = name == "body" or any(local(a) == "body" for a in node.iterancestors())
                    if in_body or name == "title":
                        add(node, "text", node)
                        for attr in ("alt", "title", "aria-label"):
                            if attr in node.attrib:
                                add(node, "@" + attr, node, node)
                else:
                    return
                for child in node:
                    walk(child)
                    if doc.kind == "xhtml" and (
                        name == "body" or any(local(a) == "body" for a in node.iterancestors())
                    ):
                        add(child, "tail", node)

            walk(doc.tree.getroot())
            for group_key, values in groups.items():
                block_key = f"d{doc_index}-b{group_key[0]}-{group_key[1]}"
                pending, size = [], 0
                for run, context in values:
                    if pending and (size + len(run.text) > 2400 or len(pending) >= 40):
                        self.units.append(Unit(f"u{len(self.units)}", doc.path, pending[0][1],
                                               tuple(r for r, _ in pending), block_key))
                        pending, size = [], 0
                    pending.append((run, context))
                    size += len(run.text)
                if pending:
                    self.units.append(Unit(f"u{len(self.units)}", doc.path, pending[0][1],
                                           tuple(r for r, _ in pending), block_key))

    @property
    def runs(self):
        return [r for u in self.units for r in u.runs]

    def _render(self, targets):
        expected = {r.id for r in self.runs}
        if set(targets) != expected:
            raise InvalidResponse("Terjemahan belum lengkap. EPUB final tidak dibuat.")
        changes = {}
        for name, doc in self.documents.items():
            tree = copy.deepcopy(doc.tree)
            nodes = list(tree.iter())
            for slot in doc.slots:
                text = "".join(p if isinstance(p, str) else p.prefix + validate_text(p, targets[p.id]) + p.suffix
                               for p in slot.parts)
                if slot.field.startswith("@"):
                    nodes[slot.node_index].set(slot.field[1:], text)
                else:
                    setattr(nodes[slot.node_index], slot.field, text)
            if doc.kind in {"xhtml", "ncx"}:
                # Only nodes actually translated acquire/change language; foreign
                # language islands and excluded code/SVG remain untouched.
                root = tree.getroot()
                root_language = root.get(XML_LANG) or root.get("lang")
                if doc.slots and ((doc.kind == "xhtml" and not root_language)
                                  or ENGLISH.fullmatch(root_language or "")):
                    root.set(XML_LANG, "id")
                if doc.slots and "lang" in root.attrib and ENGLISH.fullmatch(root.get("lang", "")):
                    root.set("lang", "id")
                for slot in doc.slots:
                    node = nodes[slot.node_index]
                    parent = node.getparent() if slot.field == "tail" else node
                    for ancestor in (parent, *parent.iterancestors()):
                        for attr in ("lang", XML_LANG):
                            if ENGLISH.fullmatch(ancestor.get(attr, "")):
                                ancestor.set(attr, "id")
            changes[name] = ET.tostring(tree, encoding="utf-8", xml_declaration=True, pretty_print=False)
        package = copy.deepcopy(self.opf)
        for lang in package.findall(f".//{{{DC}}}language"):
            if ENGLISH.fullmatch((lang.text or "").strip()):
                lang.text = "id"
        # No identifier/creator/title/cover/refinement or modified timestamp rewrite.
        changes[self.opf_path] = ET.tostring(package, encoding="utf-8", xml_declaration=True, pretty_print=False)
        return changes

    def _validate_output(self, path, changes, targets):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED
                    or infos[0].extra or archive.read("mimetype") != b"application/epub+zip"):
                raise InvalidEpub("Output melanggar aturan mimetype EPUB.")
            if len(infos) != len(self.entries) or set(archive.namelist()) != set(self.entries):
                raise InvalidEpub("Daftar anggota ZIP berubah.")
            if archive.testzip():
                raise InvalidEpub("CRC output tidak valid.")
            for name, original in self.entries.items():
                actual = archive.read(name)
                if name not in changes:
                    if actual != original:
                        raise InvalidEpub(f"Aset berubah: {name}")
                    continue
                tree = parse_xml(actual, name)
                old = self.opf if name == self.opf_path else self.documents[name].tree
                old_nodes, new_nodes = list(old.iter()), list(tree.iter())
                if len(old_nodes) != len(new_nodes):
                    raise InvalidEpub(f"Jumlah node berubah: {name}")
                allowed = {}
                allowed_attrs = {}
                if name in self.documents:
                    for slot in self.documents[name].slots:
                        value = "".join(p if isinstance(p, str) else p.prefix + targets[p.id].strip() + p.suffix
                                        for p in slot.parts)
                        allowed[slot.node_index, slot.field] = value
                        if slot.field.startswith("@"):
                            allowed_attrs.setdefault(slot.node_index, {})[slot.field[1:]] = value
                for i, (a, b) in enumerate(zip(old_nodes, new_nodes)):
                    if a.tag != b.tag or len(a) != len(b):
                        raise InvalidEpub(f"Struktur tag berubah: {name}")
                    for field in ("text", "tail"):
                        expected = allowed.get((i, field), getattr(a, field))
                        if name == self.opf_path and a.tag == f"{{{DC}}}language" and field == "text":
                            if ENGLISH.fullmatch((expected or "").strip()):
                                expected = "id"
                        if getattr(b, field) != expected:
                            raise InvalidEpub(f"Teks di luar terjemahan berubah: {name}")
                    attrs = dict(a.attrib)
                    for key, val in b.attrib.items():
                        if key in ("lang", XML_LANG) and val == "id" and (
                            ENGLISH.fullmatch(attrs.get(key, "")) or (
                                i == 0 and key == XML_LANG and key not in attrs
                                and (not attrs.get("lang") or ENGLISH.fullmatch(attrs["lang"]))
                            )
                        ):
                            attrs[key] = "id"
                    attrs.update(allowed_attrs.get(i, {}))
                    if attrs != dict(b.attrib):
                        raise InvalidEpub(f"Atribut/href/ID berubah tanpa izin: {name}")
        return {"crc": "passed", "archive_members": len(self.entries),
                "unchanged_asset_bytes": "passed", "xml_structure": "passed",
                "text_coverage": f"{len(targets)}/{len(self.runs)}",
                "source_link_warnings": self.warnings,
                "epubcheck": "not_run"}

    def export(self, targets, output: Path, overwrite=False, epubcheck_jar=None, cancel_event=None):
        output = Path(output).resolve()
        if output == self.path.resolve() or (output.exists() and os.path.samefile(output, self.path)):
            raise ValueError("Output tidak boleh menimpa EPUB sumber.")
        if output.exists() and not overwrite:
            raise FileExistsError("Output sudah ada. Pilih nama lain atau aktifkan timpa output.")
        changes = self._render(targets)
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".epub", dir=output.parent)
        os.close(fd)
        staged = Path(name)
        try:
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as archive:
                mime = zipfile.ZipInfo("mimetype")
                mime.compress_type = zipfile.ZIP_STORED
                archive.writestr(mime, b"application/epub+zip")
                for info in self.infos:
                    check_cancel(cancel_event)
                    if info.filename == "mimetype":
                        continue
                    archive.writestr(copy.copy(info), changes.get(info.filename, self.entries[info.filename]))
                archive.comment = self.comment
            # Windows fsync/_commit requires write access; a read-only handle
            # raises EBADF after all translations have already been cached.
            with staged.open("r+b") as handle:
                os.fsync(handle.fileno())
            check_cancel(cancel_event)
            report = self._validate_output(staged, changes, targets)
            if epubcheck_jar:
                try:
                    completed = subprocess.run(
                        ["java", "-jar", str(Path(epubcheck_jar).resolve()), str(staged)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                        check=False)
                except (OSError, subprocess.TimeoutExpired):
                    raise InvalidEpub("EPUBCheck tidak dapat dijalankan atau timeout. Output final belum diterbitkan.") from None
                if completed.returncode != 0:
                    raise InvalidEpub("EPUBCheck menolak output. Periksa EPUB sumber dengan EPUBCheck; hasil terjemahan tetap tersimpan di cache.")
                report["epubcheck"] = "passed"
            check_cancel(cancel_event)
            # Detect source replacement/deletion while a long API job was running.
            if not self.path.exists() or digest(self.path.read_bytes()) != self.source_hash:
                raise InvalidEpub("EPUB sumber berubah selama proses. Output dibatalkan.")
            publish_file(staged, output, overwrite)
            return report
        finally:
            staged.unlink(missing_ok=True)
