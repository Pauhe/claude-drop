# Plan review 2 — GPT-6 Astra, 2026-09-11

Second review of the implementation plan, after the first REWORK was addressed. Verdict REWORK again. Found the NameError from an entry-point guard above its definitions, putdata() memory blowup, the Starlette max_part_size that does not exist in the pinned version, companion selection by filename suffix, a failure test that injected no failure, and the install-before-route ordering. All addressed in the current plan.

**The revision fixes several earlier findings, but it is still REWORK.** C4 and C7 remain materially unimplemented, C2 contains incompatible promises, and the tasks cannot pass in the stated order.

This review uses the embedded material only. Upstream browsing failed, so I could not independently verify dependency sources during this turn. I have not run the suites.

1. **Previous findings: fixed, partial, or moved**

| Finding | Assessment |
|---|---|
| Unsafe deletion | **Mostly fixed.** Unknown IDs no longer trigger deletion. However, `ID_PATTERN.match()` with `$` accepts a trailing newline: use `fullmatch()`. C1 also needs an explicit exception for publication rollback and startup orphan recovery, which intentionally remove directories without rows. |
| Full-resolution readable variants | **Not fixed completely.** `_bounded_jpeg()` can shrink `full_jpg` to 1000 px while its metadata still claims original dimensions. Original JPEG/PNG/GIF/WebP can exceed 10 MB or 8000 px despite being a supported format. |
| One primary path per drop | **Fixed at selection level.** Companions and local filenames still introduce failures described below. |
| Fixed snapshot pagination | **Fixed for concurrent insertions.** The server bounds rows, and the CLI captures the first boundary. This is a sequence boundary, not preservation against concurrent deletion/expiry, which is reasonable here. |
| Multipart resource enforcement | **Not fixed.** The selected framework mechanism does not impose the claimed per-file limit. |
| Decoder timeout | **Defensible omission for this tool**, provided memory use and input admission are corrected. A subprocess pool is not inherently necessary. Pixel count and a semaphore limit concurrent work, but do not bound decoder duration. |
| Durable local publication | **Not fixed.** Files change before companion downloads finish; multiple replacements have no rollback or recovery protocol. |
| First-run window and cursor selection | **Substantially fixed.** First pull pages through retained drops; explicit selectors avoid the cursor. Cursor durability still depends on the broken publication implementation. |
| Health status and Kuma matching | **Status-code issue fixed.** Health can still report healthy when quota prevents uploads or cleanup silently fails. |
| SQLite connection isolation | **Fixed for transaction isolation.** Each worker thread gets its own connection. Lifecycle cleanup is incomplete. |
| SIGKILL recovery | **Core recovery implemented; test is credible.** Literal C1 contradicts it, and cleanup failures are concealed. |
| Deterministically failing tests/task ordering | **Not fixed.** Tasks 2, 6, 11 and 12 have concrete problems; Task 13 depends on Task 14. |

2. **The specific code that will or will not run**

**Task 6 Step 4 — `request.form()` is a blocker.**

FastAPI `0.115.6` constrains Starlette to the `>=0.40.0,<0.42.0` range. That range predates the public `Request.form(max_part_size=...)` parameter. The supplied call therefore raises `TypeError`; inspecting the signature cannot reveal another parameter that accomplishes the missing operation.

Even with a newer Starlette exposing that parameter, `max_part_size` limits ordinary multipart fields, **not uploaded file bodies**. File parts follow the spool-writing branch. Consequently, upgrading alone does not implement the 25 MB limit.

There are two further defects:

- In an application request, Starlette converts multipart parsing failures into an HTTP exception with status **400**. Catching only `MultiPartException` does not reliably produce the specified 413.
- `files = [v ... if k == "files"]` accepts string fields as well as uploads. A field named `files` then reaches `await item.read()` and fails. Explicitly parsed forms also need closing to release their temporary files.

A custom multipart parser is unnecessary for this single-user service. A defensible alternative is **explicitly documented post-spooling file-size rejection**, before reading the whole file or decoding it, with an aggregate request cap and bounded temporary storage. That changes C4’s guarantee and requires corresponding tests. If rejection during each file’s arrival remains mandatory, a mechanism that actually counts file bytes is required.

**Task 1 Step 4 — `_strip()` is a memory blocker, not merely a performance concern.**

```python
clean.putdata(list(img.getdata()))
```

For a 40 Mpx RGB image, the list can hold roughly 320 MB of references plus about 2.56 GB of pixel tuples on a typical 64-bit CPython build—before image buffers and encoder allocations. One accepted image can exceed the entire container limit.

Use a pixel-buffer operation that does not materialize Python objects per pixel, and verify metadata removal. Task 2’s suggestion to reconsider this only “if too slow” understates the problem.

The upload route also reads bytes **before acquiring the semaphore**. Waiting requests retain their originals, so the semaphore alone does not bound total request memory.

**Tasks 1–2 — `_bounded_jpeg()` violates both size and resolution contracts.**

After trying qualities 95, 80 and 65, it returns:

```python
return _encode_jpeg(_fit(img, MAX_EDGE // 2), 65)
```

That result is never checked against the limit. It also silently changes dimensions without updating `Variant.width` and `height`.

The 60,000-byte noisy-image test has no supported guarantee of passing; the final 1000 px encoding can remain much larger. The 500-byte test requires `view_jpg` to exist even when the configured bound cannot be met, and never checks its size. Task 2 changes only the palette branch, so it does not resolve these failures.

**Task 4 Step 3 — `BEGIN` / `commit()` is not a runtime blocker.**

With the supplied default SQLite transaction mode:

- The write methods commit before returning.
- Ordinary reads do not leave an implicit write transaction open.
- Explicit `BEGIN` establishes the intended read transaction.
- `commit()` ends it.

Sequential calls on the same thread therefore work. `finally: conn.commit()` is poor exception structure—rollback on failure is clearer—but there is no demonstrated nested-transaction failure in the supplied call graph. Do not redesign this merely because it uses explicit `BEGIN`.

**Task 5 Step 1 — the SIGKILL test is substantially correct.**

With the separately instructed `from pathlib import Path` added:

- The parent computes the server import path correctly.
- `_insert` is replaced before publication.
- The kill occurs after rename and blob-directory fsync.
- Linux `subprocess` reports SIGKILL as `-9`.
- The parent checks that an orphan actually exists before recovery.

Importing `Path` later inside the generated script is not a defect: the f-string expression is evaluated by the parent. Add a timeout and include captured stderr in failure diagnostics so an unrelated child failure is diagnosable.

**Task 11 Steps 3–4 — every CLI command fails as written.**

Step 3 ends with:

```python
if __name__ == "__main__":
    sys.exit(main())
```

Step 4 says to **append** `publish()` and `pull()` below it. `main()` calls `build_parser()`, which evaluates `p.set_defaults(handler=pull)` before `pull` exists. The result is `NameError`, including for `drop list`.

Move the entry-point guard below all definitions.

**Task 12 Step 3 — `_update_latest()` still breaks publication.**

For a normal PNG pull it:

1. Publishes immutable files.
2. Replaces `latest/`.
3. Overwrites `latest.png`.
4. Starts downloading `view_jpg` directly into `latest.jpg`.

If that download fails, the old JPEG may already be truncated, the PNG is new, and `latest/` is new. The CLI nevertheless reports “nothing changed.”

Other problems:

- Choosing companions by **local filename suffix** is wrong. A full pull of a PNG original loses its extension under the new naming code, so the function deletes `latest.png` despite a lossless variant being available.
- A TIFF `--full` pull selects `full_jpg` and removes the PNG companion even though that drop has `view_png`.
- The fallback copies arbitrary selected bytes to `latest.jpg`; it does not establish JPEG encoding.
- Companions must be selected from variant metadata and fully staged before publication.

**Task 13 Step 3 — `ASSETS` is correct inside the image.**

The Dockerfile places the module at `/app/app/api.py`; therefore:

```python
Path(__file__).parent.parent
```

is `/app`, matching the `COPY` destinations.

It is **incorrect for an ordinary source checkout**, where it resolves to `<repo>/server`. The monkeypatched tests prove routing, not the actual packaged paths. Add a built-image check for all three real assets; provide a checkout configuration only if checkout serving is intended.

3. **Further defects and missing contract implementation**

**Local publication needs a defined transaction/recovery strategy.** Task 12’s two renames leave a gap:

```python
os.rename(batch_dir, retired)
os.rename(batch_new, batch_dir)
```

Failure at the second rename leaves `latest/` absent. There is no rollback, journal, or startup repair. Separate replacement of JPEG and PNG can expose mismatched generations.

Neither copied batch files nor their directories are fsynced. The cursor’s parent directory is not fsynced after replacement. Staging under `state_dir()` also permits cross-filesystem moves into a separately mounted `~/drop`; `shutil.move()` then becomes a copy rather than an atomic rename.

Define the visibility and crash guarantees first, then implement a generation/pointer scheme or another concrete recoverable protocol. `flock` serializes writers; it does not make this sequence transactional.

**Task 12’s publication-failure test does not inject the claimed failure.** Making `latest/` mode `0500` does not prevent renaming it: rename permission belongs to its parent. Cleanup failure is suppressed with `ignore_errors=True`. The pull can return success, so the assertion expecting exit 1 fails. Inject failures at specific rename, copy, companion-download and fsync boundaries.

**Original filenames lose their extensions.**

```python
Path(drop["filename"]).stem
...
VARIANT_SUFFIX["orig"] == ""
```

Every original loses its suffix, including raw documents and full-resolution supported images. Use detected media type for supported image extensions and sanitize the complete raw filename where appropriate.

**C2/C3 cannot both hold as written.** An original may be full resolution and format-supported but exceed acceptance limits. An unsupported-format original may have no fitting full-resolution JPEG. Failed images deliberately have no readable variant.

Specify when `--full` succeeds, warns, or fails. Do not silently reduce resolution or fall back to unreadable original bytes while promising universal readability.

**C10’s uniqueness check races.** Two requests can both obtain the same candidate before either publishes a row. The test generates 50 IDs without publishing anything, so it tests random luck rather than collision handling. Reserve batch IDs atomically or serialize allocation through first publication; force collisions in tests.

**C9 is only partially implemented.**

- Full quota is not checked.
- A root-directory probe does not establish that staging and SQLite are writable.
- Concurrent `writable()` calls share `.writable`; one can delete the other’s probe and produce a false failure.
- `delete()` and `recover()` suppress filesystem errors; a sweep can report success and refresh health while reclaiming nothing.
- `START_TIME` belongs to module import, not the app instance.

Define the admission-health predicate precisely and test it.

**Several promised tests are absent or inadequate.**

- Publication racing deletion and retention.
- Concurrent uploads contending for the remaining quota.
- Deterministic batch collision handling.
- API decode-concurrency enforcement.
- Client pagination with an insertion between pages—the current CLI test checks only that `max_seq` appears.
- Truncated HTTP downloads. `copyfileobj()` does not independently verify the advertised content length before publication.
- Crash/failure during local publication and cursor persistence.

Additional smaller gaps include HEIF compatible-brand detection scanning only bytes 16–64 without respecting the box length or four-byte alignment; `drop list --since` remaining single-page despite C8’s wording; and the verification task requesting dimensions “the page showed” when the page displays none.

4. **Do the tasks compose in order? No.**

| Task | Problem at its commit gate |
|---|---|
| 1 | `tests.conftest` imports rely on an uncreated package marker and can collide with an installed `tests` package. Use unambiguous test helpers. |
| 2 | JPEG byte-bound implementation is not repaired; its expected all-green result is unsupported. |
| 6 | Multipart call fails; several upload tests also call `/api/drops`, which is introduced only in Task 7. |
| 10 | Placeholder assets make the image build independently. This part is fixed. |
| 11 | Appended definitions sit below the executing entry-point guard. |
| 12 | Publication-failure test uses ineffective permissions; implementation is not durable. |
| 13 | Dev VM installation uses the HTTPS proxy created only in Task 14. Host installation also expects connectivity before that route exists. |
| 15 | Dimension verification neither displays dimensions in the UI nor measures them in the supplied Python snippet. |

The self-review’s assertion that every commit passes is therefore false.

**Verdict: REWORK.**

Only these changes must happen **before Task 1 starts**:

- Resolve C2/C3’s full-resolution, readability and byte-limit conflict, including explicit failure behavior.
- Replace C4’s incorrect multipart contract with a supported, version-pinned ingestion strategy and honest resource bounds.
- Remove pixel-list metadata stripping and specify bounded encoding with accurate dimensions.
- Replace Task 12’s publication design with a concrete recoverable protocol covering companions, directories and cursor durability.
- Clarify C1’s cleanup exceptions; specify strict ID validation, atomic batch reservation and the actual health predicate.
- Correct the known test failures, CLI definition order, Task 6/7 dependency and proxy-before-install ordering; add deterministic tests for the critical guarantees.