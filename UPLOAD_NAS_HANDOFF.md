# NAS Fanqie Upload Handoff

This file is for another Codex/agent that only needs to run the Fanqie upload workflow after the user copies a minimal package to `Z:\SSD\...`.

## Scope

This handoff is only about:

- uploading existing chapter `.txt` files to Fanqie
- running the daily upload scheduler
- keeping/refreshing the browser login profile used by the uploader

This handoff is **not** about:

- generating novels
- running the Python long-novel writers
- frontend/backend app development

## Current Local Facts

- Main uploader script: [scripts/fanqie_upload.mjs](D:\Long-Novel-GPT\scripts\fanqie_upload.mjs)
- Daily scheduler: [scripts/fanqie_daily_scheduler.mjs](D:\Long-Novel-GPT\scripts\fanqie_daily_scheduler.mjs)
- Upload config: [fanqie_daily_jobs.json](D:\Long-Novel-GPT\fanqie_daily_jobs.json)
- Node dependency file: [package.json](D:\Long-Novel-GPT\package.json)
- Lockfile: [package-lock.json](D:\Long-Novel-GPT\package-lock.json)

Important config detail:

- `fanqie_daily_jobs.json` now uses `.run/fanqie_writer_seed_profile` as the default `seedProfileDir`
- this replaced the old missing `openclaw` path

Uploader behavior detail:

- if the browser is not logged into Fanqie writer backend, the uploader can now wait on the visible login page instead of requiring terminal Enter confirmation
- for unattended scheduler runs, a valid login profile is still required

## Books Currently Configured For Daily Upload

The scheduler currently uploads these two books:

- `inscribed_immortal` -> book title `删史成仙`
- `crowd_blackbox` -> book title `众生黑匣`

Chapter sources:

- [auto_projects/inscribed_immortal/manuscript/chapters_txt](D:\Long-Novel-GPT\auto_projects\inscribed_immortal\manuscript\chapters_txt)
- [auto_projects/crowd_blackbox/manuscript/chapters_txt](D:\Long-Novel-GPT\auto_projects\crowd_blackbox\manuscript\chapters_txt)

## Minimum Files To Copy To NAS

Recommended destination root:

- `Z:\SSD\fanqie-upload\`

Copy at least these paths, preserving relative structure:

- `package.json`
- `package-lock.json`
- `fanqie_daily_jobs.json`
- `scripts/fanqie_upload.mjs`
- `scripts/fanqie_daily_scheduler.mjs`
- `auto_projects/inscribed_immortal/manuscript/chapters_txt/`
- `auto_projects/crowd_blackbox/manuscript/chapters_txt/`
- `.run/fanqie_writer_seed_profile/`

Recommended writable runtime directories to create or copy:

- `.run/fanqie_daily_profiles/`
- `.run/fanqie_daily_artifacts/`
- `.run/fanqie_author_checkin_profile/`
- `.run/fanqie_author_checkin_artifacts/`

Optional state/log carry-over:

- `.run/fanqie_daily_scheduler_state.json`
- `.run/fanqie_daily_scheduler.log`

If you want a clean scheduler state on NAS, do **not** copy those two files.

## What Does Not Need To Be Copied

For upload-only deployment, these are not required:

- `.venv/`
- `auto_novel.py`
- `backend/`
- `frontend/`
- `llm_api/`
- the rest of `auto_projects/*` except each book's `manuscript/chapters_txt`
- novel generation drafts/outlines/plots unless the user explicitly wants them archived too

## Expected NAS Layout

Recommended minimal tree:

```text
Z:\SSD\fanqie-upload\
  package.json
  package-lock.json
  fanqie_daily_jobs.json
  scripts\
    fanqie_upload.mjs
    fanqie_daily_scheduler.mjs
  auto_projects\
    inscribed_immortal\
      manuscript\
        chapters_txt\
    crowd_blackbox\
      manuscript\
        chapters_txt\
  .run\
    fanqie_writer_seed_profile\
    fanqie_daily_profiles\
    fanqie_daily_artifacts\
    fanqie_author_checkin_profile\
    fanqie_author_checkin_artifacts\
```

If the folder structure is preserved, `fanqie_daily_jobs.json` should continue to work without path edits because it uses relative paths.

## Runtime Requirements On NAS

Minimum requirements:

- Windows environment strongly preferred
- `Node.js` installed and available in `PATH`
- a local Chrome/Chromium executable that Playwright can launch
- a desktop session / visible browser path available for login refresh when the profile expires

Current uploader dependency:

- `playwright-core`

Install command at the NAS root:

```powershell
npm ci
```

The script uses an existing local Chrome install; it does not require downloading Playwright browsers if Chrome is present.

## Manual Upload Commands

Run from the NAS package root.

Upload next 3 chapters for `删史成仙`:

```powershell
node .\scripts\fanqie_upload.mjs `
  --book-title 删史成仙 `
  --chapters-dir .\auto_projects\inscribed_immortal\manuscript\chapters_txt `
  --auto-next-count 3 `
  --ai-usage no `
  --profile-dir .\.run\fanqie_writer_seed_profile `
  --artifacts-dir .\.run\fanqie_daily_artifacts\inscribed_immortal `
  --start-url https://fanqienovel.com/main/writer/?enter_from=author_zone
```

Upload next 3 chapters for `众生黑匣`:

```powershell
node .\scripts\fanqie_upload.mjs `
  --book-title 众生黑匣 `
  --chapters-dir .\auto_projects\crowd_blackbox\manuscript\chapters_txt `
  --auto-next-count 3 `
  --ai-usage no `
  --profile-dir .\.run\fanqie_writer_seed_profile `
  --artifacts-dir .\.run\fanqie_daily_artifacts\crowd_blackbox `
  --start-url https://fanqienovel.com/main/writer/?enter_from=author_zone
```

## Scheduler Command

Run one immediate scheduler pass:

```powershell
node .\scripts\fanqie_daily_scheduler.mjs --config .\fanqie_daily_jobs.json --once --run-now --skip-delay
```

Run the normal scheduler loop:

```powershell
node .\scripts\fanqie_daily_scheduler.mjs --config .\fanqie_daily_jobs.json
```

## Login Profile Notes

The unattended scheduler depends on a valid logged-in Fanqie writer browser profile.

Primary profile intended for reuse:

- `.run/fanqie_writer_seed_profile`

How to think about profiles:

- `fanqie_writer_seed_profile` = reusable seed profile with login state
- `fanqie_daily_profiles/<job-id>` = per-job working copy used by the scheduler

If uploads suddenly fail by redirecting to `writer/login`, the most likely issue is expired login state.

## How To Refresh Login On NAS

If the seed profile is no longer logged in:

1. Run a **visible** uploader command without `--headless`.
2. Keep `--profile-dir .\.run\fanqie_writer_seed_profile`.
3. Let a human log into the Fanqie writer page in the opened browser.
4. Retry the upload command or scheduler after login completes.

Useful visible-mode variant:

```powershell
node .\scripts\fanqie_upload.mjs `
  --book-title 删史成仙 `
  --chapters-dir .\auto_projects\inscribed_immortal\manuscript\chapters_txt `
  --auto-next-count 1 `
  --ai-usage no `
  --profile-dir .\.run\fanqie_writer_seed_profile `
  --artifacts-dir .\.run\fanqie_daily_artifacts\inscribed_immortal `
  --start-url https://fanqienovel.com/main/writer/?enter_from=author_zone `
  --keep-open
```

Notes:

- the user may need to interrupt before final publish if this command was launched only for login refresh
- if a human is present, prefer refreshing login first and then rerunning the real upload command

## Where To Check Failures

Scheduler-level files:

- `.run/fanqie_daily_scheduler.log`
- `.run/fanqie_daily_scheduler_state.json`

Per-book artifacts:

- `.run/fanqie_daily_artifacts\inscribed_immortal\run.log`
- `.run/fanqie_daily_artifacts\crowd_blackbox\run.log`

Common failure patterns:

- missing browser login profile
- redirected to `writer/login`
- Fanqie page structure changed, causing route/button detection failure
- Chrome path missing on the new machine

## Recent Known Good Result

On local machine, on `2026-03-27`:

- `删史成仙` successfully uploaded chapters `86`, `87`, `88`
- `众生黑匣` successfully uploaded chapters `52`, `53`, `54`

This confirms that:

- current `fanqie_upload.mjs` can still complete the publish flow
- route parsing for these two books is currently working
- the current login/profile approach is valid when the profile is authenticated

## Practical Advice For Another Codex

If asked to make NAS upload work quickly, do this in order:

1. Verify `Node.js` and Chrome exist on the NAS host.
2. Run `npm ci`.
3. Verify `.run/fanqie_writer_seed_profile` was copied over.
4. Try a visible manual upload for one book first.
5. If login is expired, stop and let the human log in.
6. Only after one manual upload succeeds, enable or test the daily scheduler.

Do not start by debugging the full novel-generation stack. This handoff is upload-only.
