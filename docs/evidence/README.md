# Evidence Files

This directory is designated for local verification and evaluation artifacts generated during Phase 1–4 gates.

## Invariants
1. **Kept Outside Git**:
   Raw evaluation dumps, benchmark traces, decision logs, video recordings, and screenshots are kept outside Git to prevent repository bloat and eliminate any risk of accidental secret or token leakage.
2. **Git Tracking Policy**:
   The directory contents are excluded in `.gitignore` (`docs/evidence/*`), while this `README.md` is committed to document directory purpose and guidelines.
3. **Artifact Integrity**:
   Deterministic SHA-256 audit logs, contract verification logs, and test-guard reports are verified in-memory or via standard test runs. Any exported evidence bundles for hackathon evaluation should be stored locally here before external upload.

