# DuckDB Research Layer Hardening Implementation Plan

1. Add typed query metadata and strict completeness errors while preserving default interactive behavior.
2. Add catalog execution locking and provenance fingerprint generation.
3. Update scanner/MCP callers to use verified metadata and strict completeness.
4. Add tests for safety metadata, truncation failure, provenance, and concurrent execution.
5. Run focused DuckDB tests, then broader checks, review and commit only these changes.
