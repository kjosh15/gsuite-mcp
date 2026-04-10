# gdrive-mcp Expansion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expand gdrive-mcp from 4 tools to 9 tools, closing feedback gaps around native append, Google Docs formatting preservation, tracked changes for .docx, OAuth scope coverage, and batch metadata.

**Architecture:** Replace service-account auth with user OAuth. Split single `server.py` into per-API-surface modules (`auth.py`, `drive_ops.py`, `docs_ops.py`, `sheets_ops.py`, `docx_edits.py`). Thin `server.py` remains as FastMCP tool-decorator layer. Tests mirror module split with TDD for each new tool.

**Tech Stack:** Python 3.12+, FastMCP, google-api-python-client (Drive v3 + Docs v1 + Sheets v4), google-auth-oauthlib (setup CLI only), pytest, pytest-asyncio, ruff. `.docx` manipulation uses stdlib (`zipfile` + `xml.etree.ElementTree`) — no `python-docx`.

**Design doc:** `docs/plans/2026-04-10-gdrive-mcp-expansion-design.md`

**Implementation order:**
1. Auth migration (Tasks 1-4) — foundation; everything else depends on it
2. Module split without behavior changes (Tasks 5-7) — refactor existing 4 tools
3. Batch metadata (Task 8) — simplest new tool; warmup
4. Append tool (Tasks 9-11) — three paths, one per mime type
5. Replace text tool (Tasks 12-13) — simple + regex paths
6. Manage comments tool (Task 14) — consolidated CRUD
7. Docx suggest edit (Tasks 15-17) — OOXML pure function + integration
8. Documentation + cleanup (Task 18)

---
