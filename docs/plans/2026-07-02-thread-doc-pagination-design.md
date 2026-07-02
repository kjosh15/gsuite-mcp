# Feature request: bounded, paginated reads for Gmail threads & Google Docs

**Repo:** gsuite-mcp (covers both Gmail `get_thread` and Drive `read_file_content`)
**Problem class:** oversized payloads / silent truncation on large threads and multi-page docs

## Motivation

Large Gmail threads and multi-page Google Docs frequently exceed a single tool
response's practical size. Today the client works around this with out-of-band
extraction, and when a response *is* clipped it is not signalled — the model
cannot tell a complete read from a truncated one. Two structural wins are on the
table:

1. **Quoted-history duplication** is a large fraction of oversized-thread bytes.
   Each reply re-embeds the prior message's body. An N-message thread carries
   roughly O(N²) redundant text.
2. **No field projection.** `read_file_content` returns the full payload
   (including comment threads) even when the caller only needs plain body text.

## Proposed changes

### 1. `get_thread` — message-body controls

- **`strip_quoted_history` (bool, default preserve current behavior).**
  When true, return only each message's *net-new* body text — the content
  authored in that message, with quoted/duplicated history from earlier
  messages removed. Detection should cover standard quote markers (`>` prefixes,
  `On <date>, <sender> wrote:` blocks, `gmail_quote` containers, forwarded /
  `-----Original Message-----` separators).
- **Per-message fetch mode.** An option to fetch bodies message-by-message
  rather than all-at-once, so a 50-message thread can be walked in bounded
  chunks (e.g. `message_offset` / `message_limit`, or an opaque page cursor over
  the message list) instead of one large blob.

### 2. `read_file_content` — field projection

- Allow requesting a **subset of the document** — e.g. `fields=["body"]` to get
  plain text without comment threads (and vice-versa). Omitting `fields`
  preserves today's full-payload behavior. Cuts payload size whenever comments
  aren't needed.

### 3. Explicit truncation & pagination (applies to both tools)

- **Truncation is never silent.** Any time a response is clipped, the payload
  must carry an explicit signal (e.g. `truncated: true` plus a
  `next_cursor` / `next_page_token`) so the caller knows more remains and how
  to fetch it.
- **Documented pagination** on both tools so full content is reachable by
  following cursors to completion.

## Acceptance criteria

- [ ] A **50-message Gmail thread** can be fully read via documented pagination
      — no reliance on an out-of-band extraction workaround.
- [ ] A **multi-page Google Doc** can be fully read via documented pagination
      — no out-of-band workaround.
- [ ] **Truncation is always explicit** — never silent — on every response from
      both tools.
- [ ] `get_thread` can return per-message net-new body text with quoted history
      stripped.
- [ ] `read_file_content` can return a projected subset (e.g. body without
      comments).

## Notes / open questions

- Quote-stripping is heuristic; define behavior when a quote boundary can't be
  detected confidently (prefer *keep* text over dropping net-new content).
- Cursor stability: page tokens should tolerate the thread/doc changing between
  calls (or document that they don't).
- Interaction between `strip_quoted_history` and per-message mode — both on at
  once should still be well-defined.
