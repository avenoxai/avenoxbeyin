# V3 source, lifecycle and platform contracts

## Final V3.0.0 native verification

**Windows, macOS and Ubuntu × Python 3.11/3.13: all six jobs passed, each with 114 V3 tests, development 10/10 and holdout 6/6.** [Final run](https://github.com/avenoxai/avenoxbeyin/actions/runs/35029453887), source `5b99e4bb96729c663eae76f1383b9675c6e7419c`. [Machine evidence](evidence/product-native-ci.json).

This final run includes direct ZIP/V2 installation, interrupted install and rollback retries, Windows manifest and launcher paths, and direct context refresh without hooks. Earlier local Mac/Linux runs also passed the 47 legacy regressions. The exact release ZIP was installed, its extracted directory removed, then its CLI used to verify source freshness and same-version update checks. [Asset evidence](evidence/release-asset.json). Live model and Desktop app-host workflows are [separate](LIVE-CLIENTS.md).

Everything below is a historical source snapshot; its pending statements are superseded by the final native result above.


## Release candidate repair verification, 16 September 2026

After the extracted-ZIP installation and interrupted-rollback repairs, independent macOS Python 3.14.2 and network-disabled Linux Python 3.13.15 runs each passed **47/47 legacy + 111/111 V3 tests**. Development 10/10 and holdout 6/6 remain unchanged. The 12 product and 17 updater cases include tests-first failures for manifest-derived package version, stock V2 writer retirement from an extracted release, retry before the root entrypoint exists, and retry of an already-started rollback. All now pass. [Frozen source hashes and run evidence](evidence/product-release-fixes-final.json).

The prior six-job native CI success at `ea215a8` covered 107 tests. It does not cover these latest installer/rollback changes; a new native run is required. Earlier sections below are historical source snapshots.


## Productization offline acceptance, 16 September 2026

The current productization source passed **47/47 legacy regressions + 106/106 V3 tests** independently on **macOS Python 3.14.2** and **Linux container Python 3.13.15**. Linux ran with network disabled, a read-only repository/root filesystem and temporary writable state. The 24 product/update acceptance cases cover package validation and preflight, initial V2 rollback, interrupted-update recovery, user-edit conflicts and bundled skill copy rollback. The full suite also covers structured task creation and quarantine of ambiguous double frontmatter.

The frozen semantic evaluation passed development **10/10** and holdout **6/6** on both platforms. Recall, abstention and fact retention are 1.0; forbidden results and privacy leaks are zero. Diagnostic precision is 0.95 development and 1.0 holdout. No holdout expectations changed. [Exact source/test hashes and sanitized run evidence](evidence/product-offline-final.json) and [tests-first product contract](evidence/product-acceptance-contract.json) are recorded separately from earlier measurements.

The legacy README regression initially failed after the intentional V3 root README rewrite. Its input now points to the archived `docs/V2-README.md`; all five original assertions remain intact and passed on both platforms. Only that affected subset was rerun after the test-path correction.

**Native Windows productization CI has not been run by this lane.** The six-job native CI result below belongs to the earlier 62-test source snapshot and must not be read as verification of the new updater/migration package. Actual client sessions and public release status are separate from these synthetic offline checks.


## Latest native CI result, 16 September 2026

**All six hosted jobs passed** on Windows, macOS and Ubuntu, each with Python 3.11 and 3.13. Every job ran **62 V3 tests** and the frozen development/holdout evaluation. This is real Windows runner execution, including the installed encoded PowerShell hook command under a minimal PATH.

[Successful GitHub run](https://github.com/avenoxai/avenoxbeyin/actions/runs/35025071466), tested code commit `89c9f37d6e4e4650edd71b1d52493b4ed84ed6f2`. [Machine evidence and hashes](evidence/native-ci.json).

The first native Windows run failed on path separators in citations, regex replacement of backslashes during reinstall, and PowerShell discovery under a minimal PATH. The implementation now persists forward-slash source references, inserts instruction text literally and resolves built-in PowerShell from the system directory. A reinstall path regression was added; expectations were not relaxed. The failed run is retained in the evidence.

This verifies offline runtime, source, queue, skills and installation behavior on those platforms. Real Codex/Claude/Antigravity model sessions were tested on macOS separately; native CI does not establish a Windows model session or Codex Desktop UI continuity.

The sections below preserve earlier contracts and pre-CI measurements; their pending statuses are historical and superseded by this result.


All fixtures are synthetic, all state is in temporary directories outside the temporary vault, and hook/installer subprocesses receive a temporary HOME/USERPROFILE. No model call, remote provider, real user vault or package dependency is required. Existing semantic fixture and expected results remain frozen.

## Source adapter API, frozen before implementation

`beyin_v3_sync.SyncEngine(vault_root, state_dir)` exposes `.store`, `sync()`, `update_task(id, expected_revision, changes)`, and `receipt(event_id, summary, refs, harness)`.

- `sync()` returns `{status, indexed, deleted, warnings, conflicts}`. Markdown is authoritative. JSON frontmatter is supported; plain Markdown retains its whole body. Duplicate explicit IDs produce conflicts and exclude ambiguous derived records. Rename updates citations without duplicating a record.
- `update_task` changes canonical task metadata with an expected revision, preserving the exact Markdown body. A source CAS journal survives source-replace failure; restart recovers once. An intervening manual edit produces conflict and is not overwritten.
- `receipt` creates a source Markdown receipt and stable `{id,event_id,status,source}` result. Same event/summary/refs is idempotent across harnesses. ID reuse with different data and manually altered receipt files are conflicts, not overwritten. Receipt files are excluded from ordinary source retrieval.

## Lifecycle and installation contract

`beyin_v3_hook.py --vault PATH --state PATH --harness codex|claude|antigravity|hermes|opencode|omp|kiro|dsh` accepts JSON on stdin. Codex, Claude, Hermes, OpenCode, OMP, Kiro and DSH receive a single `hookSpecificOutput.additionalContext` JSON response from the shared handler for startup context. Kiro's installed `beyin_v3_kiro.py` adapter extracts plain text for Kiro's command-hook stdout (see `KIRO.md`). DSH's installed `beyin_v3_dsh.py` adapter forwards the Claude-shaped payload to the same handler and stays silent outside the vault (see `DSH.md`). Hermes reaches the adapter through the vault-owned plugin in `beyin_v3_hermes.py`; OpenCode through `.opencode/plugins/beyin-v3.js`; OMP through `.omp/hooks/pre/beyin-v3.ts`. Antigravity `PreInvocation` with `invocationNum: 0` returns `injectSteps[].ephemeralMessage`; later invocations do not reinject. Antigravity `Stop` queues only when `fullyIdle` is exactly true and returns `decision: stop`.

`enqueue_event(vault,state,payload,harness)` returns a stable event ID, and `drain_queue(vault,state)` returns `{processed,failed,pending}`. Queue files contain metadata rather than transcript text. `--drain-queue` is a deterministic worker entry point. `BEYIN_V3_NO_SPAWN=1` disables detached workers while allowing already-synced context to be read. A failed worker leaves pending work; repeated delivery or competing workers must not acknowledge one event twice. A crash after source sync and before acknowledgement may retry idempotent source synchronization, with no duplicate revision/event.

`install_v3.py --vault PATH --state PATH` installs managed adapter files, bounded instruction blocks and bindings for all three harnesses. Repeating it preserves identical output and unrelated configuration/trust data. Paths with spaces and Unicode are exercised by executing the generated installed command. Native Windows uses the built-in `powershell.exe` only as the quoted Python launcher; no separate PowerShell 7 or `pwsh` installation is required. `--uninstall` restores original owned files under the installer's conflict safeguards.

## Tests-first evidence

`tests/v3_sync_test.py` was written before SyncEngine existed: its initial run reported 10/10 failures for the absent module. `tests/v3_hook_test.py` initial run had three failures for the absent installer; the hook implementation arrived concurrently and passed the other six cases. Two additional queue crash/concurrency tests were then frozen: the real concurrent-drain test initially reported two acknowledgements for one event. These failures were not resolved by weakening expected results.

Run from a public repository checkout:

```sh
python -m unittest discover -s tests -p 'v3_sync_test.py'
python -m unittest discover -s tests -p 'v3_hook_test.py'
python -m unittest discover -s tests -p 'v3_*test.py'
python scripts/evaluate_v3.py
```

## Native platform evidence

| Platform | Configured CI | Actual evidence in this work |
|---|---|---|
| Ubuntu | Python 3.11, 3.13 | Linux container Python 3.13.15: 47 legacy + 61 V3 passed with network disabled; distinct from hosted native CI |
| macOS | Python 3.11, 3.13 | Local Python 3.14.2: 47 legacy + 61 V3 passed after final source freeze |
| Windows | Python 3.11, 3.13 | Native hosted jobs passed, 62 V3 tests plus semantic evaluation on each version |

`.github/workflows/v3.yml` contains the read-only native OS matrix with standard-library-only tests. Authoring the workflow does not run CI. No push or public CI trigger was performed by this test lane. Interpreter/OS simulation is not native Windows evidence. CLI/model sign-in and real companion continuity are outside these offline gates.

## Additional source-integrity contracts

Four source regressions were frozen before their fixes: a manual source edit without incrementing frontmatter revision must advance the effective revision once and invalidate a stale expected revision; unsupported metadata produces `degraded` status; Markdown receipts have immutable timezone-aware ISO `created_at`; no-query snapshots include only trusted active/waiting records allowed by the requested audience and remain within budget. A separate lifecycle regression requires a saved receipt to appear in restart context marked as historical evidence, rather than silently disappearing because receipts are excluded from ordinary source scanning.

All five initial focused runs failed for the intended missing behavior. These cases extend source/lifecycle reliability and do not modify the frozen semantic scenario answers. Queue acknowledgement tests use explicit event IDs; without a caller-supplied stable delivery identity, transport-level exactly-once delivery is not established by this suite. Retry safety additionally depends on idempotent source synchronization.


## Final independent macOS run

On macOS with Python **3.14.2**, the frozen-source command `python3 -m unittest discover -s tests -p 'v3_*test.py'` passed **56/56 tests** in 1.026 seconds. This includes **14 source-sync** and **12 hook/installer** tests, alongside the runtime, semantic and parent-owned shared-skill suite. The complete stdout/stderr contained `OK`; no tests were skipped in this run.

`python3 scripts/evaluate_v3.py` also passed: development **10/10**, holdout **6/6**; required recall, abstention accuracy and structured fact retention all **1.0**, forbidden results/privacy leaks **0**, harness equality **true**. Diagnostic precision remained **0.95** development and **1.0** holdout. No semantic fixture changes or holdout-directed tuning occurred during this source/lifecycle phase.

Frozen production SHA256 values evaluated:

| File | SHA256 |
|---|---|
| `template/.claude/scripts/beyin_v3.py` | `ab99584fea287ac58281a9c7601ce1b47af66a953439690be3e7778ffed22b87` |
| `template/.claude/scripts/beyin_v3_sync.py` | `8444e9be6192224bef89acb7c3f9a1ceeff473b4f9e1b13c7f0cb31da892238a` |
| `template/.claude/scripts/beyin_v3_hook.py` | `ad015719c62b8e5f98203011fcf5992d3d7a8aedbec3652b5b662b42bbd16df2` |
| `scripts/install_v3.py` | `d28d21fa25ff2923b707469bb89674855faa8b01e2d8d82668bab6fd8560ff1c` |

The generated installed command was executed locally with spaces and Unicode in its vault/state paths. Queue failure injection covered worker failure and the boundary after source synchronization but before acknowledgement; both recovered without duplicate source history. Competing drains acknowledged the single explicit-ID event once. These are synthetic executable behavior checks, not proof that a particular hosted CLI has trusted and dispatched its lifecycle hooks.


## Ordinary note snapshot regression

A real-host observation prompted an additional tests-first synthetic case: startup snapshots must include plain Markdown notes and ordinary `fact` records with no status field, while excluding completed tasks. The initial focused test failed because both statusless sources were omitted. This differs from task-status filtering: the absence of a workflow status does not make an ordinary note inactive. No frozen semantic fixture expectation changed.

After the fix, an independent macOS rerun passed **57/57** complete V3 tests (including 15 sync tests), with development **10/10** and holdout **6/6** unchanged. Core SHA256 for this newer run: `657ce6f4866b5217bca4dd33a9cee58f3990b3b29c7f2d867f87644aa380c268`. Earlier hashes/counts above are retained as historical run evidence. Explicit statusless tasks remain unknown/excluded; statusless non-task notes/facts are eligible. General empty-query retrieval behavior was not changed.


## Final offline audit, 2026-09-16

The final independent run passed **47/47 existing regressions + 61/61 V3 tests** on both **macOS Python 3.14.2** and **Linux container Python 3.13.15**. Linux used `python:3.13-slim` with `--network none`, read-only root filesystem/repository, and temporary writable state. Both environments also passed development 10/10 and holdout 6/6. The exact synthetic Python smoke extracted from QUICKSTART passed locally.

The final audit added regression coverage for native Windows launcher inspection, deterministic symlink-denial copy fallback, CLI Unicode JSON under cp1252, preservation of existing UTF-8 settings under a simulated cp1252 file default, and UTF-8 JSON stdin. CLI stdout and installer settings tests reproduced failures before fixes. The stdin fix arrived concurrently and passed its first test run; it is not described as a measured red-to-green result. Encoded launcher inspection decodes UTF-16LE before looking for the target module and still executes the installed command on the actual host.

These encoding and fallback injections are portable simulations, **not native Windows execution**. At the time of that earlier offline run, Windows CI was pending; the native result above supersedes it. No push, public CI trigger, real vault access or model/provider call was performed by this test lane.

[Final machine evidence](evidence/offline-final.json) binds source/test hashes, per-suite exit codes and output hashes, image identity, aggregate semantic metrics and the documentation smoke. Earlier counts/hashes in this document are historical checkpoints. Actual hosted client delivery is recorded separately in the live-client report.
