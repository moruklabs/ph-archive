# AGENTS Instructions

This file provides guidance for contributors and automated agents working in this repository.

## Purpose
The AGENTS.md outlines required checks and conventions to help keep the project consistent and secure.

## Required checks
- Run `python -m py_compile capture_and_parse.py` before committing any changes to Python code.
- Run `shellcheck` on any shell scripts if available (e.g., `shellcheck setup.sh`).

## Style
- Keep scripts simple and avoid storing HTML. This project only stores RSS feeds from the configured endpoints.
- Validate file paths using `is_safe_path` to prevent path traversal.

