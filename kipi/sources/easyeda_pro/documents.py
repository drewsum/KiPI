"""The EasyEDA Pro document stream: ``{header}||{body}|`` lines, replayed to a final state.

The stream is append-only. A record may be written several times, deleted with an empty
body, or written by another client; :class:`Document` keeps the winning version, exactly
as EasyEDA Pro's consistency layer does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from kipi.sources.easyeda_pro.archive import EasyedaError

SEP = "||"
DocumentKey = Tuple[str, str]


class RecordError(ValueError):
    """A log line does not follow the ``{header}||{body}|`` grammar."""


def split_record(line: str) -> Tuple[dict, Any]:
    """Split one log line into its header and its inner record.

    ``str.split("||")`` is wrong: a JSON string in the record can contain ``||``. Parse
    the header first, then the rest as its own JSON value.
    """
    decoder = json.JSONDecoder()
    text = line.strip()
    if not text:
        raise RecordError("empty line")
    outer, pos = decoder.raw_decode(text)
    if not isinstance(outer, dict):
        raise RecordError("the header is not a JSON object")
    rest = text[pos:].lstrip()
    if not rest.startswith(SEP):
        raise RecordError("the header is not followed by '||'")
    rest = rest[len(SEP):].lstrip()
    # A deletion is written as an empty body, ``|||`` or ``||""``.
    if rest in ("", "|") or rest.startswith("|"):
        return outer, None
    inner, _ = decoder.raw_decode(rest)
    if inner is None or inner == "":
        return outer, None
    return outer, inner


def normalize_id(outer: dict) -> str:
    """The replay key: the outer ``id`` alone (its type is not part of the key)."""
    value = outer.get("id")
    if value is None:
        return f"type:{outer.get('type')}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    if text[:1] in "[{":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            return text
    return text


def _client_rank(client: Any) -> str:
    return "" if client is None else str(client)


def _beats(candidate: Tuple[int, str], incumbent: Tuple[int, str]) -> bool:
    """The V4 rule: larger ticket wins; on a tie, the larger document client wins."""
    if candidate[0] != incumbent[0]:
        return candidate[0] > incumbent[0]
    return candidate[1] > incumbent[1]


@dataclass
class Document:
    """One ``DOCHEAD`` section, replayed to its final records."""

    head: Optional[dict] = None
    records: Dict[str, dict] = field(default_factory=dict)
    deleted: bool = False
    line_count: int = 0
    _stamps: Dict[str, Tuple[int, str]] = field(default_factory=dict)

    @property
    def doc_type(self) -> str:
        return str((self.head or {}).get("docType") or "UNKNOWN")

    @property
    def uuid(self) -> str:
        return str((self.head or {}).get("uuid") or "")

    @property
    def client(self) -> str:
        return _client_rank((self.head or {}).get("client"))

    @property
    def version(self) -> str:
        head = self.head or {}
        return str(head.get("editVersion") or head.get("version") or "")

    @property
    def title(self) -> str:
        for record in self.records.values():
            if record.get("type") == "META":
                return str(record.get("title") or "")
        return ""

    def add(self, outer: dict, inner: Any, client: Optional[Any] = None) -> None:
        self.line_count += 1
        key = normalize_id(outer)
        stamp = (int(outer.get("ticket") or 0),
                 _client_rank(self.client if client is None else client))
        if key in self._stamps and not _beats(stamp, self._stamps[key]):
            return
        self._stamps[key] = stamp

        record_type = str(outer.get("type") or "")
        if record_type == "DELETE_DOC":
            if isinstance(inner, dict):
                self.deleted = bool(inner.get("isDelete"))
            return
        if inner is None:
            self.records.pop(key, None)
            return
        record = dict(inner) if isinstance(inner, dict) else {"value": inner}
        record.setdefault("type", record_type)
        if outer.get("id") is not None:
            record.setdefault("id", outer.get("id"))
        record["_ticket"] = stamp[0]
        self.records[key] = record

    def live(self) -> List[dict]:
        return [record for record in self.records.values() if not record.get("deleted")]

    def of_type(self, record_type: str) -> List[dict]:
        return [record for record in self.records.values()
                if record.get("type") == record_type]


def parse_stream(text: str) -> Tuple[List[Document], List[str]]:
    """Every document in a stream, plus a message for each line that could not be read."""
    documents: List[Document] = []
    by_key: Dict[DocumentKey, Document] = {}
    errors: List[str] = []
    current: Optional[Document] = None
    current_client: Optional[Any] = None

    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            outer, inner = split_record(line)
        except (RecordError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"line {lineno}: {exc}")
            continue

        if outer.get("type") == "DOCHEAD":
            head = inner if isinstance(inner, dict) else {}
            current_client = head.get("client")
            key: DocumentKey = (str(head.get("docType") or ""), str(head.get("uuid") or ""))
            current = by_key.get(key) if key[1] else None
            if current is None:
                current = Document(head=head)
                documents.append(current)
                if key[1]:
                    by_key[key] = current
            continue

        if current is None:          # a truncated stream: keep what can be read
            current = Document(head=None)
            documents.append(current)
        current.add(outer, inner, current_client)

    return documents, errors


def pick_pcb(documents: Iterable[Document], progress=None,
             assumed: Optional[List[str]] = None) -> Document:
    """The board document: the last live PCB document that still has records."""
    boards = [doc for doc in documents
              if doc.doc_type == "PCB" and not doc.deleted and doc.records]
    if not boards:
        raise EasyedaError(
            "The EasyEDA Pro project has no PCB document, so there is no board to analyze.")
    if len(boards) > 1 and assumed is not None:
        assumed.append(
            f"EasyEDA Pro: the project has {len(boards)} PCB documents; the last one is read.")
        if progress is not None:
            progress.warn(f"Project has {len(boards)} PCB documents; reading the last one")
    return boards[-1]
