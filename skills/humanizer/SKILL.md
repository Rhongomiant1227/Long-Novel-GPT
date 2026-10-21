# Humanizer

This project-local skill is a bounded prose-quality stage for completed
chapter drafts. It is not an AI-detector evasion tool.

## Contract

The stage may change only language-layer properties:

- mechanical sentence structure and repeated transitions
- empty summaries, explanation-heavy prose, and over-regular rhythm
- action/dialogue transitions and paragraph pacing

It must not change titles, chapter numbers, chapter boundaries, story facts,
character relationships, world rules, causal order, clues, or ending state.

## Pipeline Position

`draft -> critic -> incomplete-tail repair -> humanizer -> format normalization -> tail recheck -> save`

The source draft is retained in memory until the rewritten result passes
validation. A failed or malformed result is discarded and the original draft
is saved. The stage writes its decision history to `state.json`.

## Modes

- `off`: disabled.
- `review`: request a JSON language-quality report; never changes prose.
- `rewrite`: request a complete body-only rewrite and apply the guard checks.

Use `--humanizer-model` to assign a separate model. The normal recommendation
is to use a prose-oriented model for `rewrite` and a stricter model only for
`review`; do not assume that a reasoning model is the best prose editor.
