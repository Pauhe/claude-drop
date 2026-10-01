# Plan review — GPT-6 Astra, 2026-09-11

Review of the first implementation plan. Verdict REWORK. Found the unconditional rmtree in Store.delete, two deterministically failing tests, the --full regression, the Kuma keyword mismatch, and the unenforced snapshot protocol. All findings are addressed in the current plan; contracts C1-C11 in the design exist because of this review.

**Verdict: REWORK.** The architecture is viable, but this plan is not ready for task-by-task execution. It contains a destructive deletion bug, deterministic test failures, broken installer delivery, and implementations that contradict the settled requirements for streaming, pagination, durability, health, and `--full`.

References below identify tasks, steps, and exact statements because the embedded source has no line numbers. This is a static review; I did not access files or run tests.

## Critical

**1. An unknown drop ID can cause deletion outside its blob directory.**  
**Task 2, Step 4, `Store.delete`:**

```python
shutil.rmtree(self.blobs / drop_id, ignore_errors=True)
```

This executes even when the database deleted **zero rows**. Task 4 exposes it directly through `DELETE /api/drops/{drop_id}` without ID validation.

A decoded `..` path segment makes the target `blobs/..`, the data root. A request preserving an encoded dot segment can reach this path. `rmtree` can delete data before encountering an error; `ignore_errors=True` hides the damage.

**Required:** Validate IDs against their exact generated format before filesystem access, enforce containment, and never delete a directory for an ID that was not found in the index. Add API and store tests proving malformed and unknown IDs leave all data intact. The eventual HTTP 404 does not make the current operation safe.

## High

**2. Upload limits are enforced after multipart ingestion, not while streaming the request.**  
**Task 4, Step 3, `upload(request, files: list[UploadFile])` and `_read_bounded`.**

FastAPI resolves the `UploadFile` list by parsing the multipart body before calling this handler. Oversized files have already been received and spooled when `_read_bounded` reads them. Likewise, `len(files)` rejects excessive file counts after parsing.

This violates a global constraint and permits substantial temporary-disk consumption before the service quota is consulted. The proxy’s 550 MB ceiling does not enforce either individual-file size or file count.

**Required:** Specify an actual streaming multipart ingestion implementation, including limits on parts, bytes, temporary storage, disconnect cleanup, and partial-publication behavior. Test streamed requests, not just an already-constructed multipart upload.

**3. Decoder concurrency and memory limits do not compose.**  
**Tasks 1, 4 and 6.**

```python
normalised = await run_in_threadpool(images.normalise, data)
```

This uses the shared framework thread pool, with no decoder-specific concurrency budget or per-image timeout. Multiple requests can decode simultaneously.

An 80-million-pixel RGBA image alone needs approximately 320 MB for one pixel buffer. `exif_transpose`, conversions and resizing can retain additional buffers. The proposed **1 GB container limit is not justified even for one worst-case operation**, much less several.

Simply wrapping a thread await in a timeout would not terminate the underlying decoder.

**Required:** Define decoder admission limits, an enforceable timeout mechanism, and a measured memory budget. Test health responsiveness under decoding load. Also move the synchronous retention sweep off the event loop:

```python
deleted = store.sweep_expired()
```

**4. SQLite transactions and capacity checks are unsafe under the specified threading model.**  
**Task 2, Step 4; Task 4, Step 3.**

All operations share one connection:

```python
sqlite3.connect(..., check_same_thread=False)
```

Disabling the thread check does not give independent transactions to concurrent callers. Concurrent `with self._db:` blocks can commit or roll back another operation’s work. Upload publication, synchronous API routes and the sweeper all access this connection.

Separately, two publishers can both pass `_check_capacity(incoming)` before either writes its blobs, exceeding the quota or reserve. `usage_bytes()` can race deletion between enumeration and `stat()`.

**Required:** Specify transaction ownership and synchronization, including capacity reservations through publication. Add concurrent publication/deletion/sweep tests; serial unit tests cannot establish these invariants.

**5. The snapshot cursor protocol is not implemented.**  
**Task 2, Step 4, `Store.list`; Task 7, Step 3, `collect`.**

The server executes `MAX(seq)` and the row query separately. The row query has no `seq <= snapshot` constraint and no shared read transaction.

The CLI then changes its target boundary on every page:

```python
snapshot = max(snapshot, page["snapshot_max_seq"])
```

It therefore follows a moving frontier instead of consuming the original snapshot. With sustained uploads, a pull can keep extending indefinitely. The API has neither the documented `cursor` parameter nor any way to submit a fixed sequence upper bound.

The snapshot test fetches only one page; it never tests stability.

**Required:** Define and implement the pagination contract before Task 2: capture one boundary, enforce it on all pages, preserve selectors, and test publication and deletion between pages.

**6. Local publication is neither atomic nor durable.**  
**Task 7, Step 3, `fetch`, `publish`, `write_cursor`.**

The implementation:

- Does not fsync downloaded files or directories.
- Moves files into their final locations individually.
- Deletes `latest/` before constructing its replacement.
- Overwrites `latest.jpg` and `latest.png` directly.
- Writes the cursor directly, without atomic replacement or fsync.

A disk-full error during publication can leave half-replaced local state while printing:

```text
download failed, nothing changed
```

A crash can preserve the advanced cursor while losing downloaded data. The existing failure test only fails during HTTP downloading, before any of these mutations.

**Required:** Specify staged publication and crash recovery, including durable cursor ordering. Inject failures during file publication, latest-directory replacement, and cursor replacement.

**7. `--full` knowingly fails the design and is introduced too late.**  
**Task 7, Step 4.**

Falling back from HEIC to the 2000-pixel `view_jpg` is readable, but it is **not full resolution**. The design explicitly requires both. The self-review’s claim that this resolves the requirement is incorrect.

There is also no `/full` variant anywhere in the server interfaces or storage model. Returning raw JPEG/PNG originals can retain uncorrected orientation and chooses local extensions from misleading uploaded filenames.

**Required:** Define readable full-resolution variants in Task 1 and carry them through storage, quota accounting, API, CLI and tests. This cannot be deferred to a CLI worker who owns only Task 7.

**8. The CLI prints both image variants, while its tests expect one path per drop.**  
**Task 7, Steps 1 and 3.**

Every fake image advertises both variants. The implementation downloads and prints both:

```python
variants = ["view_jpg"]
variants.append("view_png")
```

Consequently, these assertions fail:

- `test_pull_downloads_and_prints_absolute_paths`: expects 1 path, gets 2.
- Failed-download recovery: expects 1, gets 2.
- Pagination: expects 5, gets 10.
- Batch selection: expects 2, gets 4.
- List preserving cursor: expects 1, gets 2.

This also numbers **variants** rather than images in `latest/`, and makes the skill read screenshots twice.

**Required:** Define one primary returned path per drop, with companion variants available separately. Align stdout, numbered batches, skill instructions and tests.

**9. Fixed latest paths can refer to different images, and immutable paths are overwritten.**  
**Task 7, Step 3, `publish`.**

After pulling a PNG image and then a JPEG image, `latest.jpg` advances but `latest.png` remains the old image. The skill explicitly says to prefer `latest.png` whenever it exists, so it can analyse the wrong screenshot.

Publication decides what is an image solely from the resulting filename suffix. A raw or failed upload named `something.jpg` can overwrite `latest.jpg` with invalid image bytes.

The filename also lacks variant identity. Pulling a PNG normally and later with `--full` writes the original PNG over the normalized PNG at the same “immutable” path. Using only six ID characters additionally introduces avoidable collisions.

**Required:** Select latest companions from the same newest successfully normalized image; remove obsolete companions; use full immutable identity plus variant identity in filenames.

**10. `/healthz` and the proposed monitors do not implement storage health.**  
**Task 4, Step 3; Task 6 healthcheck; Task 9, Step 5.**

`healthz` always returns HTTP 200. Its status depends only on the write probe; low free space and a stalled sweep never change it. The Docker healthcheck consequently reports healthy even for `"status":"degraded"`.

The Kuma keyword is also wrong for the response serialization:

```text
Configured: "status": "ok"
Actual:     "status":"ok"
```

An exact keyword check will report a healthy service as down.

Moreover, `delete(..., ignore_errors=True)` can fail to reclaim blobs, while the sweep still records success.

**Required:** Specify health failure thresholds and return a failing HTTP status for them. Test unwritable storage, low reserve, stale/failed sweep, and failed physical cleanup. Use status-based monitoring or a correct structured response check.

**11. Installer delivery is broken in several independent ways.**  
**Task 8, Step 4.**

- `Path` is used in `api.py` but never imported.
- Inside the container, `Path(__file__).parent.parent.parent / "install.sh"` resolves to **`/install.sh`**, while the Dockerfile copies it to **`/app/install.sh`**.
- `/cli/drop` and `/skill/SKILL.md` are requested by the installer but only described as future work; no concrete handlers or static mapping are provided.
- Changing the build context to the repository root requires changing `COPY requirements.txt .` too. The adjustment instructions omit it.
- `server/.dockerignore` is no longer the ordinary context-root ignore file. Updating it does not establish the requested root-context exclusions.
- No step rebuilds/redeploys the already-running Task 6 container after Task 8 changes.

**Required:** Provide the complete final Dockerfile, root-context ignore arrangement, asset routes, and an HTTP installation test against the built image. Use the final build context from Task 6 onward.

**12. Task 2 cannot reach its stated passing gate.**  
**Task 2, Step 1, `test_quota_rejects_rather_than_evicting`.**

```python
quota_bytes=len(png_bytes)
kept = publish_png(store, png_bytes)
```

The implementation correctly counts the original **plus both variants**. The first publish therefore raises `QuotaExceeded`; the test never reaches the second publish.

**Required:** Set capacity using the actual complete stored size and assert that the first publication fits while the next does not. Keep separate coverage proving variant bytes count.

## Medium

**13. Image normalization misses several promised properties.**  
**Task 1, Step 4.**

- Palette conversion uses `convert("RGB")`, discarding palette transparency before either encoder can preserve or flatten it.
- PNG encoding does not explicitly clear inherited metadata. Pillow can retain EXIF through `img.info`; the JPEG-only metadata test does not establish stripping for PNG variants.
- `opened.info.get("lossless")` is not a reliable lossless-WebP detector for the pinned Pillow decoder. The plan provides no test establishing it.
- `convert("RGB")` is not an ICC-profile-to-sRGB transformation.
- HEIF detection accepts only four major brands and ignores compatible brands; valid HEIF inputs outside those cases become raw.
- No task enforces the verified **10 MB per image** vision limit. A 2000×2000 RGBA PNG can exceed it.

**Required:** Add fixtures and pixel/metadata assertions for these cases, plus a defined policy for readable variants exceeding 10 MB.

**14. First-run and selector behavior silently loses intended results.**  
**Task 7, Step 3, `collect`.**

`FIRST_RUN_WINDOW = timedelta(minutes=30)` invents a restriction absent from the design. Older retained drops are skipped, and once a newer drop advances the cursor, those older drops remain skipped.

`--id`, `--batch`, `--since` and `--last` inspect only one 500-row page. The store can exceed 500 rows between sweeps, so recent IDs and batches can disappear from these selectors. `--last` selects from the oldest 500 rows.

The fake server ignores `since`, hiding the first-run behavior entirely.

**Required:** Remove the unapproved first-run window, paginate selectors, and provide direct ID lookup. Test against a server that enforces the actual filters.

**15. Crash recovery leaks blobs and publication durability is incomplete.**  
**Task 2, Step 4.**

A process termination after rename but before insertion bypasses Python’s exception cleanup. The orphan is under `blobs/`; startup only sweeps `staging/`, and retention only examines indexed rows. Those bytes remain indefinitely.

The populated staging directory itself is not fsynced before rename. Fsyncing individual files and the destination parent does not establish durability of every directory entry inside the published directory.

The “crash” test injects a catchable exception, exercises cleanup, and ends with:

```python
assert store.sweep_staging() >= 0
```

That assertion cannot establish crash recovery.

**Required:** Add unreferenced-blob recovery and process-level crash tests at the actual publication boundaries. Close the SQLite connection during app shutdown as well.

**16. Per-file receipts and recent uploads are broken in the UI.**  
**Task 5, Step 3.**

Failure receipts are appended to `recent`, then immediately erased by:

```javascript
await refresh();
// refresh():
list.textContent = "";
```

Successful files have no dedicated receipt display. When all files are rejected, an earlier batch sentence can remain visible and copyable.

`limit=40` retrieves the **oldest** 40 drops because the API is ascending; reversing them does not retrieve the newest 40.

There is no original-download control, although the design makes the web UI the original-download surface.

Finally, authenticated access is irrelevant to whether the attachment/octet-stream thumbnail response works in the target browsers. The plan’s thumbnail claim needs browser verification.

**Required:** Separate receipts from the recent list, reset batch state for every attempt, implement recent selection, add original-download links, and test actual UI interactions.

**17. Time parsing and command output have unhandled edge cases.**  
**Tasks 2 and 7.**

- Store filters compare ISO strings without normalizing incoming offsets to UTC; string ordering does not reliably represent chronological ordering across offsets.
- `--since 25:00` raises an uncaught `ValueError`.
- Other invalid/future time inputs use `SystemExit(string)`, yielding exit 1 rather than the promised usage exit 2.
- `--last 0` falls through to incremental mode; negative values produce unintended slicing.
- `drop list` prints six-character IDs, while `--id` requires exact full IDs.
- Raw suffixes bypass `slug` sanitization, allowing control characters back into local filenames and newline-delimited stdout.

**Required:** Normalize timestamps, validate positive numeric arguments, route usage errors through argparse, print usable IDs, and sanitize the entire generated filename.

**18. Batches have no uniqueness enforcement or lost-response recovery.**  
**Task 4, Step 3.**

```python
batch = secrets.token_hex(3)
```

This is a 24-bit identifier with no uniqueness constraint. A collision merges unrelated uploads under one selector.

A lost upload response followed by retry creates a completely new batch and duplicate drops. Immutable IDs make **pull retries** reusable; they do not solve upload retries. The design explicitly asks for a lost-response/retry test, but none exists.

**Required:** Use collision-resistant batch IDs with enforced uniqueness, and specify/test the chosen upload retry and receipt-recovery behavior.

**19. Rollout and verification steps cannot run as written.**  
**Tasks 8–10.**

- Nothing installs the CLI and skill on the dev VM and the host before Task 10 invokes them.
- Camera capture does not establish HEIC conversion; the background explicitly says iOS may upload JPEG. Test an actual HEIC from Files as well.
- `file ~/drop/latest.jpg` does not verify orientation.
- Merely checking that `latest.png` exists can pass using a stale previous file.
- Asking for 25 images does not prove that all were sent in one API request.
- Stopping the container for one failed pull and immediately restarting it will usually miss a 300-second Kuma interval, especially with retries. A notification is not a deterministic outcome.
- Task 10 says “Files: none” but instructs workers to modify and commit `docs/design.md`.

**Required:** Add installation, deployment and evidence steps with explicit prerequisites and observable assertions.

## Test quality and missing coverage

Beyond the failures above, several tests do not test what their names claim:

| Location | Problem |
|---|---|
| Task 1, bomb test | 250,000 pixels against a 1,000-pixel Pillow limit already triggers Pillow’s hard rejection. It passes before the proposed explicit guard; test between one and two times the limit. |
| Task 1, GIF test | Checks variant count, not first-frame pixels. It already passes against Step 4. |
| Task 1, misleading-extension test | Passes no filename; it cannot test the upload-to-normalizer boundary. |
| Task 1, palette test | Asserts only `kind == "image"`, missing transparency and color corruption. |
| Task 7, fake downloads | Returns PNG bytes even for `view_jpg`; tests accept a PNG masquerading as `latest.jpg`. |
| Task 7, filename test | Claims non-ASCII coverage but its supplied filename is ASCII. |
| Task 8, installer test | `|| true` masks installer failure; local-copy installation never tests HTTP delivery. |
| Task 5, static tests | Searching for `"paste"` and `"drop batch"` cannot establish functional handlers or receipts. |

Missing automated coverage includes free-space rejection, interrupted ingestion, concurrent upload/pull, concurrent local pulls, publication-stage failures, real snapshot paging, metadata stripping across formats, `--since` preserving an existing cursor, and stale-sweep health.

## Design coverage and task composition

The self-review’s “every section maps to a task” is too weak: many mapped tasks do not deliver their assigned requirements.

| Design area | Assessment |
|---|---|
| Tailnet-only access, no application auth | Implemented consistently; no host port is appropriate. |
| Blob/index storage and publication | Present, but deletion, concurrency, durability and recovery need correction. |
| Image normalization | Partial; transparency, metadata, WebP classification and full resolution are incomplete. |
| Resource bounds | Streaming enforcement and decoder timeout missing; memory sizing unproven. |
| Retention | Age reads and periodic sweep present; physical cleanup failures hidden. Count can exceed 500 until a sweep. |
| HTTP API | `/full`, documented pagination cursor and fixed snapshot enforcement missing; `view` versus `view_jpg` contract drift unresolved. |
| CLI layout and cursor | Present but fails atomicity, durability, variant identity and latest-companion requirements. |
| Upload page | Receipts, newest-list behavior and original downloads incomplete. |
| Skill | No explicit drop-ID branch; fallback widening and stale-PNG preference undermine exact selection. |
| Infrastructure | Mostly specified; health monitoring, final image deployment and VM installation incomplete. |
| Testing and rollout | Several required scenarios omitted or unable to prove their stated outcome. |

Task 7 is too large for one review gate: transport/pagination, selection/time parsing, local transactional publication, image selection, output and error handling should be separate tasks. Task 4 likewise combines bounded ingestion, threaded storage access, API design and lifecycle health. Task 3 is small but worth its own gate because retention is an independent invariant.

For workers seeing only their task plus the header, the plan must contain **final contracts before implementation begins**. Task 8 retroactively changing Task 6’s build layout and Task 7 retroactively inventing server full-resolution behavior are particularly poor boundaries.

## Mandatory changes before Task 1 starts

1. Fix the deletion contract and specify storage concurrency, capacity reservation and crash recovery.
2. Finalize variant semantics: normalized/full, one primary path per drop, immutable identity and latest companions.
3. Replace moving pagination with a fixed, server-enforced snapshot protocol.
4. Specify real streaming ingestion and enforceable decoder resource bounds.
5. Define durable local publication and cursor advancement, including failure recovery.
6. Correct the deterministic test failures and add tests for the core invariants above.
7. Provide the final container layout, installer routes, health semantics, deployment and installation sequence.
8. Split the oversized API and CLI tasks so each has a coherent passing review gate.

**REWORK:** revise the executable plan first. These are prerequisite contracts and correctness defects, not cleanup that individual task workers can safely discover and resolve independently.