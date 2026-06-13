# Qwen Omni Realtime Provider Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated Qwen-Omni-Realtime WebSocket probe that records output audio, transcripts, usage tokens, and estimated RMB cost.

**Architecture:** Follow the existing Doubao probe pattern. Add a focused Qwen client module for protocol and usage logic, a CLI wrapper for local experiments, and tests that exercise the protocol with a fake WebSocket server.

**Tech Stack:** Python 3.12, `websockets.legacy`, pytest, local `.env` loading, PCM16 WAV helpers already present in the repo.

---

### Task 1: Qwen Protocol Tests

**Files:**
- Create: `tests/test_qwen_omni_realtime_client.py`
- Create: `app/qwen_omni_realtime_client.py`

- [x] **Step 1: Write failing tests**

Tests assert the Qwen client builds the authenticated WebSocket URL and session update event, parses provider usage, estimates cost, and completes one fake audio probe.

- [x] **Step 2: Run tests to verify failure**

Run: `uv run --with pytest pytest tests/test_qwen_omni_realtime_client.py -q`
Expected: import failure for `app.qwen_omni_realtime_client`.

- [x] **Step 3: Implement minimal Qwen client**

Create `app/qwen_omni_realtime_client.py` with dataclasses, event builders, WebSocket session, audio probe collection, sanitized events, usage parsing, and cost estimation.

- [x] **Step 4: Run focused tests**

Run: `uv run --with pytest pytest tests/test_qwen_omni_realtime_client.py -q`
Expected: all tests pass.

### Task 2: Qwen Probe CLI

**Files:**
- Create: `app/qwen_omni_realtime_probe.py`
- Modify: `configs/local.example.toml`
- Add: `docs/qwen-omni-realtime-probe-runbook.md`

- [x] **Step 1: Write CLI and docs**

The CLI reads `DASHSCOPE_API_KEY`, accepts `--model`, `--voice`, `--wav`, `--instructions`, and writes `qwen_omni_realtime_*` PCM/WAV/summary artifacts.

- [x] **Step 2: Run CLI help**

Run: `uv run python -m app.qwen_omni_realtime_probe --help`
Expected: exits 0 and shows Qwen probe options.

### Task 3: Verification

**Files:**
- All touched files.

- [x] **Step 1: Run focused tests**

Run: `uv run --with pytest pytest tests/test_qwen_omni_realtime_client.py tests/test_config.py -q`
Expected: pass.

- [x] **Step 2: Run full tests**

Run: `uv run --with pytest pytest -q`
Expected: pass.

- [x] **Step 3: Optional live probe**

If `DASHSCOPE_API_KEY` is present locally, run a short WAV probe and save summary artifacts. If the key is missing, report that live provider verification is blocked by missing credentials.

Result: live provider verification was skipped because local `.env` did not contain `DASHSCOPE_API_KEY` or `QWEN_OMNI_REALTIME_API_KEY`.
