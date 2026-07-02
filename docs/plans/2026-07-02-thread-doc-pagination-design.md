# Design: bounded, paginated reads for Gmail threads & Google Docs

**Repo:** gsuite-mcp
**Status:** design (approved for planning)
**Date:** 2026-07-02

## Provenance

This began as a feature request filed against **claude.ai's first-party Gmail
and Drive connectors** (`get_thread`, `read_file_content`) — tool names that do
not exist in this repo. It is being implemented **here** instead, as net-new
read tools in gsuite-mcp, because this is the server we control end-to-end: we
own the truncation signalling, the pagination contract, and the deployment. The
claude.ai connector source is not editable by us.

The original tool names map onto two new gsuite-mcp tools:

| claude.ai connector | new gsuite-mcp tool |
| --- | --- |
| `get_thread` (Gmail) | `read_thread` |
| `read_file_content` (Docs) | `read_document` |

## Motivation

Large Gmail threads and multi-page Google Docs routinely exceed a single tool
response's practical size. Two structural problems:

1. **Silent truncation.** When a response is clipped, nothing signals it. The
   model cannot tell a complete read from a partial one.
2. **Wasted bytes.** Threads re-embed quoted history in every reply (~O(N²)
   redundant text across an N-message thread); doc reads return comment threads
   even when only body text is wanted.

gsuite-mcp currently has **no** thread reader, and no body-projecting document
reader — `download_file` only raw-exports bytes, and `read_paragraph_at_path`
only navigates structure. Both tools below are additive.

## Design overview

Two new tools sharing one **response envelope** so "never silent truncation" is
a single, uniformly-tested convention:

- Every read response carries `truncated: bool` and `next_cursor: str | None`.
- `next_cursor` is an opaque base64-encoded JSON token (never parsed by the
  caller).
- A `max_bytes` param (default 100_000) bounds each response. Content is clipped
  only at a **safe boundary** — a whole message for threads, a whole structural
  element for docs — never mid-unit.
- Full content is always reachable by following `next_cursor` until it is
  `None`.

### Tool 1: `read_thread` (Gmail)

New pure-function module `src/gsuite_mcp/gmail_quotes.py` + tool wiring in
`gmail_ops.py` and `server.py`.

**Params**

| name | type | default | meaning |
| --- | --- | --- | --- |
| `thread_id` | str | — | Gmail thread id |
| `strip_quoted_history` | bool | `False` | return only net-new body text per message |
| `message_limit` | int \| None | `None` | max messages per page (in addition to `max_bytes`) |
| `cursor` | str \| None | `None` | opaque page token from a prior call |
| `max_bytes` | int | `100_000` | response size budget |

**Pagination.** Threads are **append-only**: existing messages never change,
new ones append at the tail. The cursor encodes a message offset; already-seen
offsets stay valid across calls. If new messages arrived mid-pagination, the
response sets `thread_changed: true` and continues — the change only affects the
unseen tail, so this is safe (see "Cursor stability").

**Quote-stripping (`strip_quoted_history: true`).** Detection lives in a pure,
unit-testable function. It prefers the `text/plain` MIME part; if only HTML is
present, it converts to text first. Detected markers:

- `>`-prefixed quoted lines
- `On <date>, <sender> wrote:` attribution blocks
- `gmail_quote` HTML containers
- `-----Original Message-----` separators
- forwarded-message separators

**Fallback = keep.** If no boundary can be detected confidently, return the full
body unchanged. Each message reports `quoted_history_stripped: bool` so the
caller knows whether stripping actually occurred. This guarantees we never drop
net-new content to an over-eager heuristic.

**Interaction with pagination.** Orthogonal by construction: stripping is a
per-message body transform; pagination is message-list windowing. Both enabled
at once is well-defined.

**Response shape (sketch)**

```json
{
  "thread_id": "…",
  "messages": [
    {
      "id": "…", "from": "…", "to": "…", "date": "…", "subject": "…",
      "body": "…",
      "quoted_history_stripped": true
    }
  ],
  "truncated": true,
  "next_cursor": "…",
  "thread_changed": false
}
```

### Tool 2: `read_document` (Google Docs)

Tool wiring in `docs_ops.py` (body text extraction) + reuse of the existing
Drive comments wrapper (`drive_ops.py`).

**Params**

| name | type | default | meaning |
| --- | --- | --- | --- |
| `file_id` | str | — | Google Doc file id |
| `fields` | list[str] \| None | `None` | subset to return: `["body"]`, `["comments"]`, or both. Omit = both. |
| `cursor` | str \| None | `None` | opaque page token from a prior call |
| `max_bytes` | int | `100_000` | response size budget |

**Body extraction.** Plain text pulled from Docs v1 `documents.get` structural
elements, paginated by structural-element index. **Comments** come from the
Drive comments API already wrapped for `manage_comments`.

**Field projection.** `fields=["body"]` returns text with no comment threads;
`["comments"]` returns comments only; omitting `fields` returns both (the
"full" payload). Cuts payload size whenever comments aren't needed.

**Response shape (sketch)**

```json
{
  "file_id": "…",
  "body": "…",
  "comments": [ … ],
  "truncated": true,
  "next_cursor": "…",
  "revision_id": "…"
}
```

Projected-out fields are omitted from the response entirely (not returned empty).

## Cursor stability

The one contested decision, resolved per tool:

- **Gmail (`read_thread`) — soft flag, never errors.** Append-only semantics
  mean an old offset never points at shifted content. A concurrent new reply
  sets `thread_changed: true`; pagination continues safely.
- **Docs (`read_document`) — fail-fast `STALE_CURSOR`.** The cursor embeds the
  doc `revisionId`. If the doc changed since the cursor was issued
  (`revisionId` differs), the tool returns a typed `STALE_CURSOR` error carrying
  the current `revision_id`; the caller restarts pagination from the beginning.

**Why fail-fast for Docs.** Doc edits can shift structural-element indices
anywhere in the document, so continuing from a recorded index could silently
skip or duplicate content — the exact failure this feature exists to eliminate.
Rejected alternatives: (B) best-effort continue — reintroduces silent
corruption; (C) server-side snapshot — violates the "no database, no state"
constraint. Fail-fast matches the project's architecture rules ("fail fast at
boundaries", "typed errors"). The limitation is documented, not hidden.

## Testing

Mirrors the existing per-module test split.

- **`gmail_quotes.py`** — fixture-based unit tests, one per quote format, plus a
  "no confident boundary → keep full body" case and an HTML-only case.
- **`read_thread`** — mocked Gmail service: multi-page walk to completion,
  `max_bytes` boundary-safe clipping (never mid-message), cursor round-trip,
  `message_limit` interaction, `thread_changed` path, and
  `strip_quoted_history` × pagination combined.
- **`read_document`** — mocked Docs + comments services: multi-page walk,
  boundary-safe clipping (never mid-structural-element), field projection
  (`body` only / `comments` only / both), cursor round-trip, and the
  `STALE_CURSOR` path when `revisionId` changes between calls.

## Acceptance criteria

- [ ] A 50-message Gmail thread is fully readable via `read_thread` pagination —
      no out-of-band extraction workaround.
- [ ] A multi-page Google Doc is fully readable via `read_document` pagination.
- [ ] Truncation is always explicit (`truncated` + `next_cursor`) on every
      response from both tools; clipping is always at a safe boundary.
- [ ] `read_thread` returns per-message net-new body with quoted history
      stripped when `strip_quoted_history: true`, and reports
      `quoted_history_stripped` per message.
- [ ] `read_document` returns a projected subset via `fields` (e.g. body without
      comments).
- [ ] Docs pagination returns `STALE_CURSOR` (not corrupt content) when the doc
      changes mid-pagination; Gmail pagination reports `thread_changed` and
      continues.

## Out of scope (this spec)

- Writing/mutating threads or docs (read-only feature).
- HTML-fidelity body rendering — bodies are returned as plain text.
- Cross-tool unified cursor format beyond the shared `truncated` / `next_cursor`
  envelope.
