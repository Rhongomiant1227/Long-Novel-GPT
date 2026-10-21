# Humanizer Integration

The project contains a controlled prose-quality stage named `humanizer`.
It is an editorial pass, not an attempt to evade AI detectors.

## Modes

- `off`: no additional request; this remains the default.
- `review`: ask the configured editor model for a JSON quality report and keep the original prose.
- `rewrite`: ask the editor model for a complete body-only rewrite, then validate it before the chapter is saved.

Example:

```powershell
python auto_novel.py `
  --project-dir auto_projects\example `
  --brief-file auto_projects\example\brief.md `
  --main-model fast/grok-4.6 `
  --sub-model fast/grok-4.5 `
  --critic-model fast/gpt-5.5 `
  --humanizer-mode rewrite `
  --humanizer-model fast/grok-4.5 `
  --humanizer-reasoning-effort high
```

The stage never asks the model to generate a heading. It strips accidental
headings from the response, preserves the existing chapter title and number,
checks protected project labels, rejects meta-text leaks, and rejects large
unexpected length changes. Original chapter files remain available through
the normal project history and the stage is recorded in `state.json` under
`humanizer_history`.

The direct `fast` provider reads `FAST_API_KEY` or
`%USERPROFILE%\.codex\fast_sbbbb_auth.json`. It does not use the local Codex
proxy. Production launch scripts are not switched automatically.

## Completed Project Pass

Use the batch tool after drafting a complete novel. It processes one chapter
at a time and writes a report under `humanizer_runs`. `rewrite` without
`--apply` validates proposed changes but does not modify the project.

```powershell
# Review the entire completed project without changing any manuscript file.
python scripts\humanize_project.py `
  --project-dir auto_projects\example `
  --mode review `
  --model fast/grok-4.6

# Validate rewrite output without applying it.
python scripts\humanize_project.py `
  --project-dir auto_projects\example `
  --mode rewrite `
  --model fast/grok-4.6

# Apply only validated rewrites. This writes a per-run backup and rebuilds
# full_novel.txt, chapters_txt, and chapters_manifest.json.
python scripts\humanize_project.py `
  --project-dir auto_projects\example `
  --mode rewrite `
  --model fast/grok-4.6 `
  --apply
```

The visible launcher accepts `fast45` and `fast46` as its third argument:

```bat
launch_visible_project.bat project_name nostop fast45
```

`fast45` uses Grok 4.5 for planning and prose, with Grok 4.6 reserved for
the lower-frequency critic, ending pass, and humanizer review. `fast46` is
available for an A/B run, but is materially slower in the direct probe.
