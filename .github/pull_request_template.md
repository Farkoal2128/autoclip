<!--
Thanks for contributing. Nothing here is bureaucracy — each item exists because
skipping it has produced a real bug in this codebase.
-->

## What this changes

<!-- And why. If it fixes an issue, link it. -->

## How you verified it

<!--
"Tests pass" is not verification on its own — several bugs here passed every
unit test and only surfaced on real media. Say what you actually ran.
-->

- [ ] `ruff check . && ruff format --check .`
- [ ] `pytest -m "not slow"`
- [ ] `pytest -m slow` (real ffmpeg renders — required for anything touching export or captions)
- [ ] `cd frontend && npm run build` (if the UI changed)

## If you touched the pipeline

- [ ] **Reframe/Layout changes:** verified center-crop behavior and any manual layout changes against representative footage.
- [ ] **Database changes:** added a new migration rather than editing a shipped one. Editing one silently diverges existing installs from fresh ones.
- [ ] **Filtergraph changes:** used `ffmpeg.relative_filter_workspace()` rather than interpolating a user-controlled path. Escaping rules there are genuinely awkward and fail quietly.
- [ ] **New capability assumed:** probed it rather than inferred it. Both `nvenc` and the Whisper compute type shipped bugs from assuming a capability that was advertised but unusable.
