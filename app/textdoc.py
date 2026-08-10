"""Pure text-document state machines for the Seance document lane."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import Limits


def _json_size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class AcceptedTextEdit:
    doc_id: str
    rev: int
    author_id: str
    connection_id: str
    author_seq: int | None
    edit: TextEdit


class TextDocError(Exception):
    """Base class for pure textdoc state-machine failures."""


class TextDocReject(TextDocError):
    """A rejected edit or reset that the session layer can surface as doc-reject."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class TextDocLimit(TextDocReject):
    """A document cap or size limit was exceeded."""

    def __init__(self, detail: str):
        super().__init__("too_large", detail)


class TextDocStaleWindow(TextDocReject):
    """The requested base revision predates the retained op-log window."""

    def __init__(self):
        super().__init__("stale", "base revision is outside the retained op log")


@dataclass(frozen=True)
class _OpLogEntry:
    rev: int
    edit: TextEdit
    prior_text: str
    size: int


class TextDoc:
    """Pure replace-range text document with stale-edit transforms."""

    def __init__(
        self,
        *,
        doc_id: str,
        title: str,
        kind: str,
        text: str,
        default: bool,
        limits: Limits,
    ) -> None:
        self._limits = limits
        self._doc_id = doc_id
        self._title = title
        self._kind = kind
        self._default = default
        self._rev = 0
        self._text = text
        self._oplog: list[_OpLogEntry] = []
        self._oplog_bytes = 0
        self._retry_cache: dict[tuple[str, int], AcceptedTextEdit | TextDocReject] = {}
        self._author_seq_highwater: dict[str, int] = {}
        self._validate_doc_meta()
        self._validate_doc_text(text)

    @property
    def rev(self) -> int:
        return self._rev

    @property
    def default(self) -> bool:
        return self._default

    def set_default(self, value: bool) -> None:
        self._default = value

    def snapshot(self) -> dict:
        return {
            "id": self._doc_id,
            "title": self._title,
            "kind": self._kind,
            "rev": self._rev,
            "text": self._text,
            "default": self._default,
        }

    def apply_edit(
        self,
        *,
        base_rev: int,
        edit: TextEdit,
        author_id: str,
        connection_id: str,
        author_seq: int | None,
    ) -> AcceptedTextEdit:
        retry_key = self._retry_key(connection_id, author_seq)
        if retry_key is not None:
            cached = self._retry_cache.get(retry_key)
            if cached is not None:
                if isinstance(cached, TextDocReject):
                    raise cached
                return cached
            previous_seq = self._author_seq_highwater.get(connection_id)
            if previous_seq is not None and author_seq <= previous_seq:
                raise TextDocReject("duplicate", "author sequence already processed")

        try:
            accepted = self._apply_edit(
                base_rev=base_rev,
                edit=edit,
                author_id=author_id,
                connection_id=connection_id,
                author_seq=author_seq,
            )
        except TextDocReject as exc:
            if retry_key is not None:
                self._record_retry(retry_key, exc)
            raise

        if retry_key is not None:
            self._record_retry(retry_key, accepted)
        return accepted

    def _apply_edit(
        self,
        *,
        base_rev: int,
        edit: TextEdit,
        author_id: str,
        connection_id: str,
        author_seq: int | None,
    ) -> AcceptedTextEdit:
        self._validate_base_rev(base_rev)
        self._validate_edit_payload(edit)
        base_text, transforms = self._resolve_base(base_rev)
        self._validate_range(base_text, edit)

        canonical = edit
        for entry in transforms:
            canonical = _transform_edit(canonical, entry.edit)

        before = self._text
        replaced = before[canonical.start : canonical.end]
        after = before[: canonical.start] + canonical.text + before[canonical.end :]
        self._validate_doc_text(after)

        self._rev += 1
        self._text = after
        accepted = AcceptedTextEdit(
            doc_id=self._doc_id,
            rev=self._rev,
            author_id=author_id,
            connection_id=connection_id,
            author_seq=author_seq,
            edit=canonical,
        )
        self._append_op(_OpLogEntry(
            rev=self._rev,
            edit=canonical,
            prior_text=replaced,
            size=_json_size(
                {
                    "rev": self._rev,
                    "edit": {
                        "start": canonical.start,
                        "end": canonical.end,
                        "text": canonical.text,
                    },
                    "prior": replaced,
                }
            ),
        ))
        return accepted

    def reset(self, text: str) -> dict:
        self._validate_doc_text(text)
        self._rev += 1
        self._text = text
        self._oplog.clear()
        self._oplog_bytes = 0
        self._retry_cache.clear()
        self._author_seq_highwater.clear()
        return self.snapshot()

    def _validate_doc_meta(self) -> None:
        if len(self._doc_id) > self._limits.max_doc_id_len:
            raise TextDocLimit(f"doc id too long ({self._limits.max_doc_id_len} chars)")
        if len(self._title) > self._limits.max_doc_title_len:
            raise TextDocLimit(f"doc title too long ({self._limits.max_doc_title_len} chars)")
        if not isinstance(self._kind, str):
            raise TextDocReject("invalid", "doc kind must be a string")

    def _validate_doc_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TextDocReject("invalid", "doc text must be a string")
        if len(text) > self._limits.max_doc_text:
            raise TextDocLimit(f"doc text too large ({self._limits.max_doc_text} chars)")

    def _validate_base_rev(self, base_rev: int) -> None:
        if not _is_int(base_rev) or base_rev < 0 or base_rev > self._rev:
            raise TextDocReject("stale", "base revision is not current")

    def _validate_edit_payload(self, edit: TextEdit) -> None:
        if not isinstance(edit.text, str):
            raise TextDocReject("invalid", "edit text must be a string")
        if len(edit.text) > self._limits.max_doc_edit_text:
            raise TextDocLimit(
                f"edit text too large ({self._limits.max_doc_edit_text} chars)"
            )
        if edit.start == edit.end and edit.text == "":
            raise TextDocReject("invalid", "empty edit is not allowed")

    def _resolve_base(self, base_rev: int) -> tuple[str, list[_OpLogEntry]]:
        if base_rev == self._rev:
            return self._text, []
        if not self._oplog:
            raise TextDocStaleWindow()
        first_retained = self._oplog[0].rev - 1
        if base_rev < first_retained:
            raise TextDocStaleWindow()

        base_text = self._text
        transforms: list[_OpLogEntry] = []
        for entry in reversed(self._oplog):
            if entry.rev <= base_rev:
                break
            transforms.append(entry)
            base_text = (
                base_text[: entry.edit.start]
                + entry.prior_text
                + base_text[entry.edit.start + len(entry.edit.text) :]
            )
        transforms.reverse()
        return base_text, transforms

    def _validate_range(self, text: str, edit: TextEdit) -> None:
        if not _is_int(edit.start) or not _is_int(edit.end):
            raise TextDocReject("invalid", "edit range must be integers")
        if edit.start < 0 or edit.end < edit.start or edit.end > len(text):
            raise TextDocReject("invalid", "edit range is out of bounds")

    def _append_op(self, entry: _OpLogEntry) -> None:
        self._oplog.append(entry)
        self._oplog_bytes += entry.size
        while (
            len(self._oplog) > self._limits.max_doc_oplog
            or self._oplog_bytes > self._limits.max_doc_oplog_bytes
        ):
            dropped = self._oplog.pop(0)
            self._oplog_bytes -= dropped.size

    def _retry_key(self, connection_id: str, author_seq: int | None) -> tuple[str, int] | None:
        if not _is_int(author_seq):
            return None
        return connection_id, author_seq

    def _record_retry(
        self,
        key: tuple[str, int],
        outcome: AcceptedTextEdit | TextDocReject,
    ) -> None:
        connection_id, author_seq = key
        previous_seq = self._author_seq_highwater.get(connection_id)
        if previous_seq is None or author_seq > previous_seq:
            self._author_seq_highwater[connection_id] = author_seq
        self._retry_cache.pop(key, None)
        self._retry_cache[key] = outcome
        while len(self._retry_cache) > self._limits.max_doc_oplog:
            del self._retry_cache[next(iter(self._retry_cache))]


class TextDocCollection:
    """Pure multi-document collection with per-document text-edit state."""

    def __init__(self, limits: Limits) -> None:
        self._limits = limits
        self._docs: dict[str, TextDoc] = {}

    def create_doc(
        self,
        doc_id: str,
        title: str,
        kind: str,
        text: str,
        default: bool = False,
    ) -> dict:
        if doc_id in self._docs:
            raise TextDocReject("invalid", "doc id already exists")
        if len(self._docs) >= self._limits.max_docs_per_session:
            raise TextDocLimit(
                f"document count exceeds maximum ({self._limits.max_docs_per_session} docs)"
            )
        make_default = default or not any(doc.default for doc in self._docs.values())
        doc = TextDoc(
            doc_id=doc_id,
            title=title,
            kind=kind,
            text=text,
            default=make_default,
            limits=self._limits,
        )
        if make_default:
            for existing in self._docs.values():
                existing.set_default(False)
        self._docs[doc_id] = doc
        return doc.snapshot()

    def snapshot(self) -> list[dict]:
        return [doc.snapshot() for doc in self._docs.values()]

    def apply_edit(self, doc_id: str, **kwargs) -> AcceptedTextEdit:
        return self._get_doc(doc_id).apply_edit(**kwargs)

    def reset_doc(self, doc_id: str, text: str, base_rev: int) -> dict:
        doc = self._get_doc(doc_id)
        if not _is_int(base_rev) or base_rev != doc.rev:
            raise TextDocReject("stale", "base revision is not current")
        return doc.reset(text)

    def _get_doc(self, doc_id: str) -> TextDoc:
        try:
            return self._docs[doc_id]
        except KeyError as exc:
            raise TextDocReject("invalid", "unknown document") from exc


def _transform_edit(edit: TextEdit, applied: TextEdit) -> TextEdit:
    if edit.start == edit.end:
        point = _transform_insert_point(edit.start, applied)
        return TextEdit(point, point, edit.text)
    return TextEdit(
        _transform_range_start(edit.start, applied),
        _transform_range_end(edit.end, applied),
        edit.text,
    )


def _transform_insert_point(pos: int, applied: TextEdit) -> int:
    new_len = len(applied.text)
    delta = new_len - (applied.end - applied.start)
    if pos < applied.start:
        return pos
    if applied.start == applied.end:
        return pos + new_len
    if pos == applied.start:
        return applied.start
    if pos <= applied.end:
        return applied.start + new_len
    return pos + delta


def _transform_range_start(pos: int, applied: TextEdit) -> int:
    new_len = len(applied.text)
    delta = new_len - (applied.end - applied.start)
    if pos < applied.start:
        return pos
    if applied.start == applied.end:
        return pos + new_len
    if pos == applied.start:
        return applied.start
    if pos < applied.end:
        return applied.start
    if pos == applied.end:
        return applied.start + new_len
    return pos + delta


def _transform_range_end(pos: int, applied: TextEdit) -> int:
    new_len = len(applied.text)
    delta = new_len - (applied.end - applied.start)
    if pos < applied.start:
        return pos
    if applied.start == applied.end:
        return pos + new_len
    if pos <= applied.start:
        return pos
    if pos <= applied.end:
        return applied.start + new_len
    return pos + delta
