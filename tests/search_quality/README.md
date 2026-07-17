# Search quality MVP

This directory contains offline tooling only. It does not change production search
ranking or filtering.

## Privacy and annotation rules

- `dataset.json` expands to 60 `pending` templates. It contains no human labels,
  private image paths, or claims about relevance.
- Copy it to `dataset.local.json` before entering real queries, local image paths,
  library identifiers, or annotations. `*.local.json` is ignored by Git.
- A real answerable case becomes usable only after `annotation.status` is changed to
  `human_verified` and `relevant_images` contains human-reviewed image references.
- A real `no-answer` case must be human-reviewed, keep `relevant_images` empty, and
  declare its underlying `mode` (`text`, `image`, or `combined`).
- `synthetic_fixture` is reserved for deterministic unit tests and is rejected unless
  `--allow-synthetic` is supplied.

The committed `deterministic_*.json` files are explicitly synthetic fixtures. They
must never be reported as a human baseline.

## Dataset schema v1

Each expanded item contains:

```json
{
  "id": "stable-case-id",
  "query_type": "text | image | combined | no-answer",
  "mode": "text | image | combined",
  "query": {"text": "...", "image": "local/path.jpg"},
  "library_scope": {
    "mode": "all_enabled | single | selected",
    "library_ids": []
  },
  "relevant_images": [
    {
      "image_id": "library-id:relative/path.jpg",
      "sha256": "64-lowercase-hexadecimal-characters"
    }
  ],
  "annotation": {
    "status": "pending | human_verified | synthetic_fixture",
    "annotator": null,
    "annotated_at": null,
    "notes": ""
  }
}
```

`query` uses text for text mode, image for image mode, and both for combined mode.
`sha256` is additive and optional for compatibility with older fixtures. When it is
present, evaluation uses it as the content-global identity across Collections;
otherwise evaluation falls back to `image_id`. Invalid digests, conflicting digests
for one `image_id`, and duplicate content identities within one label set fail
closed.

Generate an explicit pending-only file if preferred:

```powershell
python tests/search_quality/evaluate.py template `
  --count 60 `
  --output tests/search_quality/dataset.local.json
```

## Build a two-Collection local evaluation fixture without API calls

`multicollection_fixture.py` splits an existing single Collection by its two
indexed top-level directories. It takes a locked, read-only snapshot of the source,
reuses every stored 1024-dimensional vector and cache entry, adds image-cache keys
from the indexed vectors, and never copies private images. Each generated Collection
keeps the source `collection_uuid` as explicit cache lineage while receiving new root
and document IDs for its real subdirectory.

The output root must be absent or empty, and both JSON outputs must not exist. The
tool publishes through temporary paths, refuses source overlap/non-empty targets,
and removes incomplete outputs on failure. Its default smoke runs every dataset case
through both Collections and the production federated ranker with an embedding client
that fails on any attempted API call. It verifies the legacy fallback for compatibility,
then marks the already-produced candidate sets with permissive in-memory quality
settings and requires the unified `confidence_v2` path; it does not deploy an
uncalibrated `search-quality.json` into either generated workspace.

```powershell
python tests/search_quality/multicollection_fixture.py `
  --source-workspace D:\Zvec\workspace `
  --image-root D:\Pictures\Library `
  --dataset tests/search_quality/dataset.local.json `
  --output-root artifacts/search-quality-multicollection `
  --libraries-output tests/search_quality/libraries-multicollection.local.json `
  --dataset-output tests/search_quality/dataset-multicollection.local.json `
  --query-source-root .
```

The generated dataset keeps every annotation `pending`, maps suggestion/label IDs to
the new libraries, removes the split top-directory prefix, and changes every scope to
`all_enabled`. For `capture.py`, pass both generated `image_root` directories as
repeated `--query-source-root` values; `review_web.py` resolves them directly from
the generated libraries manifest. All generated paths above are ignored by Git.

## Review annotations without editing JSON

`review.py` is a standard-library-only helper for reviewing one local case at a
time. It only accepts ignored `*.local.json` files inside this directory, rejects
synthetic fixtures and unsafe relative paths, and writes updates atomically. Start
by checking the current file and listing pending cases:

```powershell
python tests/search_quality/review.py validate
python tests/search_quality/review.py summary
python tests/search_quality/review.py list
python tests/search_quality/review.py show text-001
```

If a different local dataset is used, put the global option before the command:

```powershell
python tests/search_quality/review.py `
  --dataset tests/search_quality/my-review.local.json `
  list
```

Suggested images are explicitly unverified. `accept` copies all suggestions into a
`review_draft`; `replace` stages an exact set of manually selected `image_id`
values. Both commands leave `annotation.status` as `pending` and leave
`relevant_images` empty, so draft work cannot accidentally enter an evaluation:

```powershell
python tests/search_quality/review.py accept text-001

python tests/search_quality/review.py replace text-002 `
  --image-id "library-id:photos/sunset-01.jpg" `
  --image-id "library-id:photos/sunset-02.jpg"
```

Only an explicit `confirm` for one case moves its reviewed selection into
`relevant_images` and writes `human_verified`, `annotator`, `annotated_at`, and
`notes`. An answerable case cannot be confirmed with an empty selection:

```powershell
python tests/search_quality/review.py confirm text-001 `
  --annotator "reviewer-name" `
  --notes "Inspected the full-size images."
```

`confirm` can also accept suggestions or exact IDs directly, without a separate
draft command:

```powershell
python tests/search_quality/review.py confirm text-003 `
  --annotator "reviewer-name" `
  --accept-suggested
```

A `no-answer` case requires a separate explicit flag and always keeps
`relevant_images` empty:

```powershell
python tests/search_quality/review.py confirm no-answer-001 `
  --annotator "reviewer-name" `
  --no-answer `
  --notes "Confirmed absent after inspecting the library."
```

Use `status text-001` to inspect one case, or `status`/`summary` for aggregate
progress. There is intentionally no bulk-confirm command: all 60 cases remain
pending until a reviewer confirms them individually.

## Visual local human review

`review_web.py` provides a local visual workflow for the same one-case-at-a-time
state machine. It uses Python's loopback HTTP server and Pillow only for verified,
in-memory JPEG thumbnails. It never uploads an image, calls a model API, or writes a
thumbnail to disk.

Start it with the pending dataset, a captured candidate run, and the local library
configuration:

```powershell
python tests/search_quality/review_web.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/after-v2-band-live.local.json `
  --libraries tests/search_quality/libraries.local.json `
  --query-source-root . `
  --max-library-images 5000 `
  --open-browser
```

The terminal prints a random-token URL bound only to `127.0.0.1`. Keep that URL
private and stop the server with Ctrl+C. The page shows:

- query text and a safely resolved reference image;
- unverified AI suggestions, always unchecked by default;
- the captured run results; and
- every other indexed-format image in the case's enabled Collection scope, so a
  reviewer can find relevant images outside Top-K.

The query image itself is shown as a reference but excluded from selectable labels,
matching production self-image exclusion. Disabled Collections are excluded from an
`all_enabled` scope; explicitly selecting a disabled Collection fails closed. Each
library is capped by `--max-library-images` instead of silently truncating review.
When a selected file can be safely resolved, the reviewer hashes its current bytes
and stores both `image_id` and `sha256`; selecting the same query bytes through a
different Collection/path alias is rejected.

Saving requires both the random CSRF token and an explicit human-confirmation
checkbox. No-answer cases require a second no-answer checkbox and cannot submit
image IDs. The browser may submit only image IDs already present in that case's
whitelisted Collection scope. The server reloads the dataset under a lock and
compares the page's SHA-256/mtime revision before calling
`review.confirm_human_review()`, which performs the same validation and atomic
`*.local.json` replacement as the CLI. A simultaneous CLI edit returns HTTP 409 and
does not overwrite either reviewer's work.

Media URLs contain only random opaque tokens. Resolved files must remain inside an
explicit image/query root; symlink, junction, reparse-point and traversal escapes are
rejected. Extensions are taken from the production indexer's
`SUPPORTED_EXTENSIONS`; files over 100 MiB, images over 40 million pixels, invalid
Pillow decodes, oversized request bodies, wrong Host headers, and missing tokens fail
closed. Thumbnails are regenerated in memory and are never persisted.

## Candidate run schema v1

The evaluator consumes captured results rather than executing production search.
Runs produced by `capture.py` retain the compatibility `score` plus all production
quality fields:

```json
{
  "schema_version": 1,
  "name": "candidate-build-name",
  "score_semantics": {
    "text": "lower_is_better",
    "image": "lower_is_better",
    "combined": "higher_is_better"
  },
  "score_fields": {
    "text": "raw_score",
    "image": "raw_score",
    "combined": "confidence"
  },
  "baseline_eligible": true,
  "draft": false,
  "cases": [
    {
      "id": "stable-case-id",
      "status": "ok | no_reliable_match | legacy_fallback",
      "candidate_count": 50,
      "filtered_count": 45,
      "latency_ms": 123.4,
      "api_requests": 1,
      "backend_requests": 3,
      "library_ids": ["library-a", "library-b"],
      "results": [
        {
          "image_id": "library-id:relative/path.jpg",
          "sha256": "64-lowercase-hexadecimal-characters",
          "rank": 1,
          "score": 0.123,
          "raw_score": 0.123,
          "normalized_score": 0.9385,
          "confidence": 0.9385,
          "match_state": "high",
          "rank_source": "text"
        }
      ]
    }
  ]
}
```

For text/image, `score` is the raw cosine distance and lower is better. For combined
queries, `score` is the unified confidence and higher is better. `score_fields`
keeps calibration on the correct scale while old fixture runs without that block
remain backward-compatible. Candidate results must already be in displayed rank
order.

`api_requests` counts non-empty model-service request IDs reported by the search
result, not HTTP polling. It can be zero when an embedding came from cache or the
index. `backend_requests` is diagnostic only and counts the submit and poll HTTP
requests made for that case.

## Capture from a persistent backend

Configure credentials and start the persistent backend through the desktop host or
the backend launcher first. `capture.py` deliberately has no DashScope API-key
option and never reads or prints the model API key. It authenticates only with the
backend's short-lived Bearer token, preferably supplied through an environment
variable or token file.

Text-only example:

```powershell
$env:ZVEC_BACKEND_TOKEN = "<persistent-backend-bearer-token>"
python tests/search_quality/capture.py `
  --dataset tests/search_quality/dataset.local.json `
  --output tests/search_quality/candidate.local.json `
  --base-url http://127.0.0.1:8765 `
  --top-k 10 `
  --candidate-k 50
```

> **Disk-space warning:** the persistent production search endpoint exports a copy
> of every returned image into its configured results directory. Capturing 60 cases
> with `--top-k 50` can therefore create thousands of files and consume many
> gigabytes. The safe default is `--top-k 10 --candidate-k 50`: retrieval still
> considers 50 candidates per Collection while only ten displayed results are
> copied. Run quality capture with a dedicated, disposable results directory when
> possible, verify free disk space first, and clean that directory through the
> normal product-owned cleanup workflow. `capture.py` never deletes search-result
> directories or other user data.

Image and combined queries must be copied to a direct child of the query directory
configured when the already-running backend was started. For a native backend, the
host and backend paths are normally the same:

```powershell
python tests/search_quality/capture.py `
  --dataset tests/search_quality/dataset.local.json `
  --output tests/search_quality/candidate.local.json `
  --base-url http://127.0.0.1:8765 `
  --query-source-root D:\image-library `
  --query-staging-root D:\zvec-query-staging `
  --backend-query-root D:\zvec-query-staging
```

The tool resolves dataset-relative image paths, copies each image under a random
collision-resistant filename, verifies the staged file remains a direct child, and
deletes only that file after the job finishes. The staging directory must already
exist and must be the exact host directory used by the native backend.

`--query-source-root` may be repeated when relative query images come from more than
one tree, for example once for the private image library and once for repository-local
no-answer fixtures. Resolution fails closed if the same relative path exists under
multiple configured roots.

Pending annotations are rejected before any backend request. During dataset drafting,
`--allow-pending-draft` permits capture but writes `draft: true` and
`baseline_eligible: false`; that output must not be promoted to a baseline. Captures
run sequentially because the persistent backend owns the job queue and parallel
submission would not reduce model cost or produce cleaner latency measurements.

## AI-assisted draft exploration

`draft_report.py` is a deliberately separate, draft-only path for inspecting a
pending local dataset whose proposed labels are stored in
`suggested_relevant_images`. It never treats those suggestions as human annotations,
never changes `relevant_images`, and never produces a production-loadable
`search-quality.json`.

```powershell
python tests/search_quality/draft_report.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/before-run.local.json `
  --output tests/search_quality/draft-report.local.json `
  --markdown tests/search_quality/draft-report.local.md
```

The JSON and Markdown are always marked with:

```json
{
  "draft": true,
  "baseline_eligible": false,
  "label_source": "ai_assisted_suggestions"
}
```

Draft metrics use the same strict Precision@5, legacy returned-precision, Recall@5,
MRR@5, no-answer, result-count, latency, and API-request formulas as the formal
evaluator. The report also explores observed `confidence` and `raw_score` thresholds
independently for text, image, and combined modes. It lists candidates satisfying the
exploratory false-return and recall-drop constraints, but those candidates are
diagnostics only.

The formal evaluator still requires `human_verified` labels. `calibrate.py` and
`quality_gate.py` do not accept this report type or pending AI suggestions. After
human review, copy accepted suggestions into `relevant_images`, change the annotation
status to `human_verified`, and run the formal tools separately. Keep all draft
artifacts named `*.local.json` or `*.local.md` so they remain ignored by Git.

## Failure analysis

Use `failure_report.py` to inspect why individual queries fail before changing
production ranking code:

```powershell
python tests/search_quality/failure_report.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/before-run.local.json `
  --output tests/search_quality/failure-report.local.json `
  --markdown tests/search_quality/failure-report.local.md
```

For every query, the JSON records Top-5 relevance flags, first relevant rank, missed
labels, false positives, score distributions, the Top-1/Top-2 gap, and the largest
adjacent ranking-score gap. No-answer cases additionally record their highest returned
confidence. The Markdown compares text, image, combined, and no-answer failure modes,
lists the worst queries, and derives algorithm recommendations from the observed data.

Pending `suggested_relevant_images` are accepted only for exploratory diagnosis. If
any are used, both outputs are unconditionally marked `draft: true` and
`baseline_eligible: false`; they remain invalid as a calibration input or release gate.

## Evaluate and compare

```powershell
python tests/search_quality/evaluate.py evaluate `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/candidate.local.json `
  --output tests/search_quality/evaluation.local.json `
  --markdown tests/search_quality/evaluation.local.md

python tests/search_quality/evaluate.py compare `
  --before tests/search_quality/baseline.local.json `
  --after tests/search_quality/evaluation.local.json `
  --output tests/search_quality/comparison.local.json `
  --markdown tests/search_quality/comparison.local.md
```

The report includes two intentionally distinct precision metrics, plus Recall@5, MRR@5,
no-answer recognition accuracy and false-return rate, average returned count,
average/P95 latency, and API request totals:

- `strict_precision_at_5` is standard fixed-denominator Precision@5: relevant items in
  the first five divided by five, so unfilled slots count as misses.
- `precision_at_5` is the schema-v1 legacy selective metric: relevant items divided by
  the number actually returned in the first five. Markdown labels it as returned
  precision rather than standard Precision@5.

The strict metric prevents a system from appearing more precise merely by returning
one item. The legacy field remains in schema v1 reports for historical diagnostics,
but calibration and the formal quality gate use `strict_precision_at_5`.
Evaluation reports also include a canonical SHA-256 fingerprint of the expanded
dataset (queries, scopes, annotation state, and relevance labels). Formal gates and
baseline-backed calibration fail closed when fingerprints or evaluation coverage do
not match; regenerate old reports that predate this additive schema-v1 field.

Each report also contains a `collection_fairness` block. Its primary metric is
relevance-conditioned cross-Collection pairwise accuracy: for every human-labelled
relevant image, it checks whether that image ranks ahead of returned non-relevant
candidates from another Collection. A relevant image absent from the returned list
ranks below every returned candidate. SHA-256 is the global identity when present,
so the same bytes copied into another Collection still satisfy the relevance label.

This metric intentionally does not assume that Collections deserve equal result
shares or infer fairness from Collection size. Ordering among equally relevant images
is not penalized. A single-Collection dataset, an `all_enabled` run that omits its
actual `library_ids`, or a run with no cross-Collection comparison evidence is
reported as `insufficient_coverage`; it is not silently treated as fair.

`baseline.json` is intentionally pending. Promote a completed human evaluation report
to `baseline.local.json`; do not invent values in the committed placeholder.

## Fail-closed release pipeline

`pipeline.py` is the resumable, one-command coordinator for the formal workflow. It
does not edit the reviewed dataset and its `status` and `run --dry-run` modes do not
need a DashScope API key, backend token, or running backend. Use a new attempt
directory for every quality iteration; the split, captures, calibrated configuration,
and gate report in an attempt are intentionally immutable evidence:

```powershell
$Dataset = "<human-reviewed-dataset.json>"
$Manifest = "<frozen-split-manifest.json>"
$CalibrationDataset = "<frozen-calibration-dataset.json>"
$ValidationDataset = "<frozen-validation-dataset.json>"
$Attempt = "artifacts/search-quality-release/<attempt-id>"

python tests/search_quality/pipeline.py status `
  --dataset $Dataset `
  --work-dir $Attempt `
  --split-manifest $Manifest `
  --calibration-dataset $CalibrationDataset `
  --validation-dataset $ValidationDataset

python tests/search_quality/pipeline.py run `
  --dataset $Dataset `
  --work-dir $Attempt `
  --split-manifest $Manifest `
  --calibration-dataset $CalibrationDataset `
  --validation-dataset $ValidationDataset `
  --dry-run
```

The read-only status includes the full dataset fingerprint, query-corpus fingerprint,
annotation counts, current stage, and exact next commands. It reports
`review_required` until the source contains exactly 60 cases and all 60 are
`human_verified`. A pending case, `synthetic_fixture`, missing reviewer attribution,
or any count other than 60 fails closed before a split or backend request can occur.

When the project already has an authoritative frozen manifest, pass all three split
arguments to every `status`, `prepare`, and `run` invocation. The pipeline adopts the
manifest's recorded seed and verifies its deterministic assignments and both subset
files against the current source. It never creates a second split in `$Attempt`.
Supplying only one or two of the three paths fails closed.

If no authoritative split exists yet, omit all three split arguments and freeze one
after review:

```powershell
python tests/search_quality/pipeline.py prepare `
  --dataset $Dataset `
  --work-dir $Attempt
```

This writes only inside `$Attempt`: `holdout-split.json`,
`calibration-dataset.json`, and `validation-dataset.json`. The required default
corpus shape produces exactly 36 calibration and 24 validation cases. Existing
artifacts are accepted only when their complete parsed contents reproduce the same
source fingerprint, seed, 40% validation fraction, assignments, and subset
fingerprints. Partial, edited, or stale artifacts are never repaired or overwritten;
start a new attempt directory instead.

With the three explicit split arguments, `prepare` is a read-only adoption check: it
does not copy, regenerate, or alter the already-frozen files.

Run `status` again and execute the two printed `capture.py` commands against the
actual persistent backend. The first stage requires both:

- `validation-before-run.json`, captured before deploying the new thresholds; and
- `calibration-run.json`, captured from the 36-case calibration subset.

If those immutable formal captures already exist elsewhere, point the pipeline at
them with `--calibration-run <path>` and `--validation-before-run <path>` instead of
copying or renaming them. The later after capture can likewise be supplied with
`--validation-after-run <path>`. Their exact source-byte hashes are retained in
`pipeline-state.json`.

The calibration capture must report the unified `confidence_v2` ranking path and one
identical, complete fusion configuration for every case. A legacy distance/RRF run or
mixed per-Collection fusion settings are rejected. Calibration preserves those
captured v2 options explicitly instead of silently falling back to `confidence_v1`.

`capture.py` reads only the persistent-backend Bearer token through
`ZVEC_BACKEND_TOKEN` (or a token file). The user's model API key remains configured
inside the already-running backend and is not passed to or recorded by the pipeline.
Add the correct query staging and query source roots shown by `status` for image and
combined cases. When the input is a frozen calibration or validation subset,
`capture.py` copies its manifest-bound `split` identity into the raw run. A formal
validation run without that identity, or with `role=calibration`, is rejected before
metrics are evaluated.

Resume the same command after each external step:

```powershell
python tests/search_quality/pipeline.py run `
  --dataset $Dataset `
  --work-dir $Attempt `
  --split-manifest $Manifest `
  --calibration-dataset $CalibrationDataset `
  --validation-dataset $ValidationDataset
```

With both initial captures present, `run` calibrates only the 36-case role, writes
`search-quality.json`, binds every input byte hash in `pipeline-state.json`, and then
stops at `after_capture_required`. Deploy that exact configuration to every enabled
Collection's persistent backend workspace, restart or reload the backend, and run the
printed validation-after capture command. The after capture must be newer than the
calibration artifact. Every captured case must also report `configured=true` and
runtime threshold, gap, confidence-band, and fusion settings that equal the generated
configuration; merely copying or renaming an old run cannot satisfy this proof.

The next `run` invocation passes the complete 60-case human source plus the frozen
manifest to `quality_gate.py`. The gate independently reconstructs the split and
evaluates only its 24 validation cases, while the report remains cryptographically
bound to the full human source. It writes `validation-report/`. A passing attempt
ends at `passed`; a metrics failure ends at `failed` while preserving the diagnostic
report. Use a new attempt directory for subsequent tuning so a failed run cannot be
silently replaced.

The pipeline additionally rejects draft or non-baseline captures, non-persistent
capture kinds, `top_k < 5`, `candidate_k < 50`, missing split bindings, query-corpus
drift, wrong case coverage, calibration/validation leakage, identical before/after
runs, configuration edits, and unbound or partial gate output. Exit code `0` means a
valid status/dry-run or a passed attempt, `1` means the formal metrics gate failed,
and `2` means either an expected external stage is still missing or validation failed;
the printed stage/error distinguishes those cases.

## Freeze separate calibration and validation sets

Do not tune thresholds and make the release decision on the same human-reviewed
queries. After every case has been explicitly reviewed, create a deterministic
holdout split and freeze its manifest before capturing calibration or validation
runs:

```powershell
python tests/search_quality/split.py `
  --dataset tests/search_quality/dataset.local.json `
  --manifest tests/search_quality/holdout-split.local.json `
  --calibration-output tests/search_quality/calibration-dataset.local.json `
  --validation-output tests/search_quality/validation-dataset.local.json `
  --seed search-quality-mvp-v1 `
  --validation-fraction 0.40
```

The splitter uses a stable SHA-256 rank within each `mode × query_type` stratum.
For every text, image, and combined mode, both the answerable and no-answer strata
must contain at least two cases. One or more cases from every stratum are retained
in each role; an undersized or missing stratum fails closed. Calibration and
validation IDs are mutually exclusive and their union must equal the complete
annotated source dataset.

The manifest records the seed, strategy, source fingerprint, role IDs, subset
fingerprints, and per-stratum coverage. Both generated datasets copy the reviewed
annotations unchanged and carry explicit `split.role` metadata. Pending cases,
unattributed `human_verified` cases, fingerprint drift, assignment tampering, and
cross-role overlap are rejected. `--allow-synthetic` exists only for automated
fixtures and must never be used for release data.

Capture and evaluate the calibration subset independently. Then calibrate with the
frozen manifest so an accidental full-source run is filtered and a validation
dataset is explicitly rejected:

```powershell
python tests/search_quality/calibrate.py `
  --dataset tests/search_quality/calibration-dataset.local.json `
  --run tests/search_quality/calibration-candidate-run.local.json `
  --baseline tests/search_quality/calibration-baseline.local.json `
  --split-manifest tests/search_quality/holdout-split.local.json `
  --output tests/search_quality/search-quality.json
```

The generated threshold configuration contains a `holdout_split` provenance block.
Its evaluated coverage is calibration-only. A baseline made from the unsplit source
or from the validation role fails the existing dataset fingerprint check.

Run the formal before/after quality gate only on the generated validation dataset
and its independently captured runs:

```powershell
python tests/search_quality/quality_gate.py `
  --dataset tests/search_quality/dataset.local.json `
  --before-run tests/search_quality/validation-before-run.local.json `
  --after-run tests/search_quality/validation-after-run.local.json `
  --split-manifest tests/search_quality/holdout-split.local.json `
  --output-dir artifacts/search-quality-validation
```

Formal mode requires `--dataset` to be the complete human-reviewed source dataset
named by the manifest. It deterministically reconstructs the exact validation subset,
writes that evidence as `<output-dir>/validation-dataset.json`, and records separate
`source_dataset_source` and `validation_dataset_source` bindings. Before/after runs
must contain exactly the validation IDs: calibration IDs, unknown IDs, missing
validation cases, a calibration-role run, or a changed subset fingerprint fail
closed. The JSON and Markdown reports record both dataset paths, the manifest path,
manifest fingerprint, source dataset fingerprint, validation dataset fingerprint,
and evaluated validation case count.

Do not replace either role file by hand. Any query, scope, annotation, or relevance
change alters the source fingerprint: regenerate the manifest and both subsets, then
recapture the affected runs. Keep the versioned seed unchanged when an exact split
reproduction is required.

## One-command MVP acceptance report

Run both captures against the same human-verified dataset, generate all JSON and
Markdown artifacts, and enforce the first-phase acceptance thresholds in one command:

```powershell
python tests/search_quality/quality_gate.py `
  --dataset tests/search_quality/dataset.local.json `
  --before-run tests/search_quality/validation-before-run.local.json `
  --after-run tests/search_quality/validation-after-run.local.json `
  --split-manifest tests/search_quality/holdout-split.local.json `
  --output-dir artifacts/search-quality-report
```

The output directory contains `before-evaluation.json/.md`,
`after-evaluation.json/.md`, and `comparison.json/.md`. The comparison includes a
machine-readable `quality_gate` block. Exit code `0` means all checks passed; exit
code `1` means at least one check failed while the diagnostic reports were still
written.

Before a report can certify a release, keep its complete 60-case human source,
manifest, exact generated 24-case validation subset, and both raw capture files
inside the repository and replay it independently:

```powershell
python scripts/verify_search_quality_gate.py `
  --report artifacts/search-quality-report/comparison.json `
  --repository-root .
```

The release verifier does not trust stored metrics or PASS booleans. It requires 60
human-verified source cases, deterministically regenerates the manifest's 36/24 split,
and proves the stored validation artifact is exactly that assignment. It also rejects
duplicate/non-finite JSON, ambiguous or external source paths, missing/mismatched
raw-run split metadata, file-hash or canonical-run-fingerprint drift, and any
difference between the stored comparison and a fresh execution of `quality_gate.py`
(apart from generation time and equivalent validated path spelling).

The schema-v1 gate currently requires:

- no-answer false-return rate below 10%;
- standard fixed-denominator Precision@5 (`strict_precision_at_5`) improvement of
  at least 0.15;
- Recall@5 drop of at most 0.03;
- P95 latency increase of at most 20%; and
- no increase in total API requests over the same evaluated cases;
- at least one assessable answerable case, with human relevance labels drawn from at
  least two Collections across the evaluated cross-Collection cases;
- cross-Collection pairwise accuracy of at least 0.80, limiting relevance-conditioned
  inversions to 20%; and
- worst directional Collection-pair accuracy of at least 0.50, so no direction ranks
  cross-Collection noise above relevant images more often than below them.

Pending or synthetic annotations are rejected. Formal mode also rejects a missing
`--split-manifest`; release metrics are always computed from the frozen validation
role, never from calibration queries. `--allow-test-fixtures` exists only for
deterministic automated tests and must not be used for a release decision. It allows
legacy tests to omit the manifest and bypasses the formal cross-Collection coverage
requirement so single-Collection synthetic fixtures remain useful; production gates
never receive either bypass.

## Calibrate thresholds

```powershell
python tests/search_quality/calibrate.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/candidate.local.json `
  --baseline tests/search_quality/baseline.local.json `
  --output tests/search_quality/search-quality.json
```

Calibration jointly searches observed score thresholds, adjacent confidence gaps,
and maximum confidence drop from the Top1 result separately for text, image, and
combined modes. A parameter combination is eligible only when:

- no-answer false-return rate is strictly below 10%; and
- Recall@5 drops by no more than 3 percentage points from baseline.

Eligible combinations maximize standard fixed-denominator Precision@5 with
deterministic tie-breaking by Recall@5, MRR@5, legacy returned precision, false-return
rate, result count, and the least aggressive equivalent parameters. A reject-all
boundary is evaluated so the candidate space is complete, but a zero-hit operating
point is never emitted as a deployable calibration. The legacy `precision_at_5`
field remains unchanged for historical report compatibility.
The output is `zvec-search-quality-thresholds` schema v2 and can be copied to the
workspace as `search-quality.json`. Its production-facing sections are:

```json
{
  "schema_version": 2,
  "text": {
    "minimum_score": 0.3,
    "minimum_confidence": 0.85,
    "score_gap": 0.08,
    "max_confidence_drop": 0.12
  },
  "image": {
    "minimum_score": 0.2,
    "minimum_confidence": 0.9,
    "score_gap": 0.06,
    "max_confidence_drop": 0.10
  },
  "combined": {
    "minimum_score": 0.01,
    "minimum_confidence": 0.61,
    "score_gap": 0.1,
    "max_confidence_drop": 0.15
  },
  "fusion": {"mode": "confidence_v1"}
}
```

For text/image, `minimum_score` is the maximum accepted cosine distance and
`minimum_confidence` uses `1 - distance / 2`. For `confidence_v1` combined
search, `minimum_score` is the minimum weighted-RRF score and
`minimum_confidence` uses `score * 61`, clamped to `[0, 1]`. A
`confidence_v2` run must declare the combined score field as `confidence` or
`normalized_score`; its production `minimum_score` is the compatibility value
`0.0`, and filtering uses `minimum_confidence` directly. The calibrator rejects
v2 runs captured on the legacy RRF scale. The file also retains calibration
diagnostics. Each mode needs at least one answerable and one human-reviewed
no-answer case.
After threshold filtering, `score_gap` stops the result list at the first adjacent
confidence drop at least that large. `max_confidence_drop` independently stops the
list when a candidate is farther below the Top1 confidence than the configured
band, which removes gradual low-quality tails that have no single large adjacent
gap. Existing schema v1 files remain loadable. Missing `score_gap` and
`max_confidence_drop` use compatibility defaults `0.20` and `1.0`, respectively.

Calibration records `confidence_v1` unless the captured run contains an explicit
fusion configuration or the experiment supplies one on the command line. For a
reproducible v2 experiment, add `--fusion-mode confidence_v2`; the optional flags
`--agreement-reward`, `--rank-decay`, `--weak-channel-floor`, and
`--weak-channel-penalty` override the documented v2 defaults. The diagnostics block
records whether fusion settings came from the run, CLI, API, or the v1 default.

Deploy the generated `search-quality.json` to the root of every Collection workspace
that should use calibrated confidence ranking. A Collection without that file uses
the explicit legacy ranking/filtering fallback; calibration is not shared implicitly
between Collection workspaces.

## Explore combined fusion parameters

`fusion_tune.py` is a draft-only diagnostic for captures that retain
`image_confidence`, `text_confidence`, channel ranks, and rank agreement. It can
search query weights, the bounded agreement reward, weak-channel behavior, the
minimum confidence, and the adjacent score gap without making another model call:

```powershell
python tests/search_quality/fusion_tune.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/combined-v2-top50.local.json `
  --output tests/search_quality/fusion-tuning.local.json `
  --markdown tests/search_quality/fusion-tuning.local.md
```

The run may contain only the dataset's `mode=combined` subset; the tool does not
require text-only or image-only cases. Every candidate must retain a complete
confidence/rank pair for each present channel. A missing channel may omit both
fields (or use two nulls), but a half-present pair is rejected. At least one
answerable combined case and one combined no-answer case are required.

The default bounded grid evaluates 85,680 parameter sets and has a hard one-million
set safety limit. `image_weight` is searched globally and `text_weight` is always
its complement. Comma-separated CLI overrides support reproducible coarse and local
searches:

```powershell
python tests/search_quality/fusion_tune.py `
  --dataset tests/search_quality/dataset.local.json `
  --run tests/search_quality/combined-v2-top50.local.json `
  --output tests/search_quality/fusion-tuning-refined.local.json `
  --markdown tests/search_quality/fusion-tuning-refined.local.md `
  --image-weights 0.70,0.75,0.80,0.85,0.90 `
  --agreement-rewards 0.06,0.08,0.10 `
  --rank-decays 15,20,30 `
  --weak-channel-floors 0.40,0.45,0.50 `
  --weak-channel-penalties 0.06,0.08,0.10 `
  --minimum-confidences 0.88,0.90,0.92,0.94 `
  --score-gaps 0.15,0.20,0.25
```

Recall@5 drop is measured against the production `confidence_v2` defaults
recomputed on the same captured candidate pool. Eligible points must have a
no-answer false-return rate below 10% and no more than a three-point Recall@5 drop;
legacy returned precision is the schema-v1 primary objective. The report also
lists a separate strict fixed-denominator Precision@5 diagnostic. JSON and
Markdown list the best global
parameters, their metric differences from default v2, and the top eligible points.
The command still writes its diagnostic report when no point is eligible, but exits
with code `1`.

The command accepts only draft captures and always writes
`draft=true`, `baseline_eligible=false`, and
`production_config_generated=false`. Its output deliberately omits production
threshold sections, so it cannot be renamed into a loadable `search-quality.json`.
Use it to choose experiments; repeat the formal calibration after human review
before changing product defaults or query weights.
