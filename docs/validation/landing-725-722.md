# Temporary exact landing evidence: PRs 725 and 722

The assistant's execution environment repeatedly restarted before long full test
runs finished. Incomplete logs are not PASS evidence. This additive workflow
runs the same unfiltered V3 test and frozen evaluation commands on GitHub-hosted
Linux runners, with Python 3.11 and 3.14. It changes no existing workflow, test,
skip, assertion, branch protection, release gate or runtime.

## Source equivalence

The workflow is triggered by a pull request changing its own file. It does NOT
test the workflow PR's merge checkout. A pinned checkout action retrieves the
immutable base below into `candidate/`, then merges the fixed contributor heads.
The job fails unless the reconstructed Git tree matches the expected value.
The workflow and this document are absent from the tested tree; the workflow
explicitly checks its own absence. The synthetic merge commit ID can differ
because of identity/time, but all tracked file bytes and modes must be identical.

- Base: `21cd4bc6aee88f1b471c5ed832b29ece3f47687b`
- First head (#725): `320dd111f8c9bd620d62247fc9763d3ae9002c37`
- First expected tree: `82bf1c00ca93ecc0b2698adecb77c7375181a612`
- Second head (#722): `970381889abaf32de5b9d386ffcb2fe45c384751`
- Second expected tree: `b6efea2432e26a4144fffee734075aa176b117b8`

Only completed successful full-test and evaluation steps for both Python versions
establish evidence. Existing native platform CI and independent code review remain
necessary. Before landing, recompute the actual current-base merge tree and compare
it with the tested tree. Changed heads, changed base contents, or adding this
workflow PR to main invalidate the corresponding equivalence claim. This temporary
PR is an evidence carrier, not part of either candidate, and should not be merged
as a shortcut to landing the runtime changes.

## Access and outputs

Actions are SHA-pinned to versions already used in this repository. Token scope is
`contents: read`; checkout credentials are not persisted. No secrets are referenced,
no artifact upload occurs, and no deployment, publication or Git push exists.
Only standard Actions logs record tree metadata and synthetic test results.
Each job has fresh writable HOME/state directories, uses the existing CI environment
flags, and runs the full discovery pattern without selection or skips added here.
