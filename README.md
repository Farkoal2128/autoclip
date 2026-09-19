# AutoClip

**Open-source, local-first AI video clipper.** Long video in → ranked, reframed, caption-burned clips out.

[![CI](https://github.com/artbyjazi/autoclip/actions/workflows/ci.yml/badge.svg)](https://github.com/artbyjazi/autoclip/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)

Paste a YouTube link or Twitch VOD URL, or drop a file. AutoClip transcribes it, uses an LLM to find the moments worth clipping, applies a fast centered crop for the target ratio, burns in animated captions, and exports platform-ready MP4s. Manual Layout editing handles subject-specific framing.

No accounts. No uploads to anyone's servers. No watermarks. No subscription.

---

## Why this exists

Existing open-source clippers stop at "works on my machine" — no UI, janky reframing, ugly captions. AutoClip ships the whole loop: ingest → clips → review → export.

Two ways to run it:

- **Fully local** — Whisper + Ollama. Nothing leaves your machine, no API costs.
- **Bring your own key** — Anthropic, OpenAI, Gemini, or any OpenAI-compatible endpoint (OpenRouter, Groq, DeepSeek, LM Studio) for better clip selection.

Only transcript *text* is ever sent to a provider — never video or audio. With Ollama, nothing is sent at all.

## Status

Working end to end: ingest, transcription, highlight detection across four providers, static reframing, four caption styles, export at three aspect ratios, manual Layout editing, and the full review UI. Verified on real footage — see [what "verified" means](#what-has-and-hasnt-been-verified).

## Requirements

| | |
|---|---|
| **Python** | 3.11 or 3.12. |
| **ffmpeg** | A *full* build with `libass` and `libx264`. Both `ffmpeg` and `ffprobe` on PATH. See below — the default package is the wrong one on macOS and Windows. |
| **Node** | 20+, to build the UI. Not needed at runtime. |
| **GPU** | Optional. NVIDIA or Apple Silicon speeds up transcription several-fold; CPU works, just slower. |


## Quick Install via Powershell
```bash
$ErrorActionPreference = "Stop"

Write-Host "`n=== Installing AutoClip prerequisites ===" -ForegroundColor Cyan

winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
winget install -e --id astral-sh.uv --accept-package-agreements --accept-source-agreements

$env:Path = (
    [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [Environment]::GetEnvironmentVariable("Path", "User")
)

Write-Host "`n=== Downloading AutoClip ===" -ForegroundColor Cyan

$videosDir = Join-Path $HOME "Videos"
$autoClipDir = Join-Path $videosDir "autoclip"

New-Item -ItemType Directory -Force -Path $videosDir | Out-Null

if (-not (Test-Path $autoClipDir)) {
    git clone --branch main --single-branch https://github.com/Farkoal2128/autoclip.git $autoClipDir
}

Set-Location $autoClipDir

git checkout main
git pull

Write-Host "`nAutoClip folder:" -ForegroundColor Green
Write-Host (Get-Location)

Write-Host "`n=== Creating Python environment ===" -ForegroundColor Cyan

uv python install 3.11

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    uv venv --python 3.11
}

$venvPython = Join-Path $autoClipDir ".venv\Scripts\python.exe"
$autoClipExe = Join-Path $autoClipDir ".venv\Scripts\autoclip.exe"

Write-Host "`n=== Choosing transcription acceleration ===" -ForegroundColor Cyan

$nvidia = Get-CimInstance Win32_VideoController |
    Where-Object { $_.Name -match "NVIDIA" }

$installGpu = $false

if ($nvidia) {
    Write-Host "`nNVIDIA GPU detected:" -ForegroundColor Green

    $nvidia | ForEach-Object {
        Write-Host "  $($_.Name)"
    }

    Write-Host ""
    Write-Host "NVIDIA GPU transcription is optional." -ForegroundColor Yellow
    Write-Host "It installs roughly 2 GB of NVIDIA CUDA libraries."
    Write-Host "Choose N if you prefer CPU transcription and a smaller install."
    Write-Host ""

    $answer = Read-Host "Install NVIDIA GPU transcription support? (Y/N)"

    if ($answer -match '^(y|yes)$') {
        $installGpu = $true
    }
}

if (-not $nvidia) {
    Write-Host "`nNo NVIDIA GPU detected." -ForegroundColor Yellow
    Write-Host "AutoClip will use CPU transcription."
}

Write-Host "`n=== Installing AutoClip ===" -ForegroundColor Cyan

if ($installGpu) {
    Write-Host "Installing NVIDIA GPU transcription support..." -ForegroundColor Green
    uv pip install --python $venvPython -e ".[gpu]"
}

if (-not $installGpu) {
    Write-Host "Installing standard AutoClip..." -ForegroundColor Green
    uv pip install --python $venvPython -e .
}

Write-Host "`n=== Building AutoClip interface ===" -ForegroundColor Cyan

Set-Location (Join-Path $autoClipDir "frontend")

npm install
npm run build

Set-Location $autoClipDir

Write-Host "`n=== Checking installation ===" -ForegroundColor Cyan

& $autoClipExe doctor

Write-Host "`n=== Creating desktop shortcut ===" -ForegroundColor Cyan

& $autoClipExe install-shortcut

Write-Host "`n=== Starting AutoClip ===" -ForegroundColor Green

& $autoClipExe serve
```

### Add a provider

Clip selection needs a language model. Either paste a key in **Settings → Keys**, or:

```bash
autoclip config set-secret anthropic
```

Keys go into your OS keyring — Credential Manager, Keychain, or Secret Service — never into a config file. If no keyring backend exists, AutoClip falls back to a file **and says so**, in the UI and in `doctor`.

For a fully local setup, install [Ollama](https://ollama.com) and pull a model instead:

```bash
ollama pull llama3.1:8b
```

Clip quality tracks model quality closely. A 7B model returns valid JSON full of mediocre picks; a frontier model is noticeably better at spotting a real hook. That's the honest trade for running offline.


## Using it

Everything the UI does is also on the CLI:

| command | does |
|---|---|
| `autoclip doctor` | check this machine and explain what's missing |
| `autoclip serve` | start the web app |
| `autoclip clip <url\|file>` | run the whole pipeline and export |
| `autoclip jobs` | list recent jobs |
| `autoclip providers` | check which providers are reachable |
| `autoclip styles` | list caption presets |
| `autoclip config show` | print settings |
| `autoclip update-ytdlp` | update yt-dlp after a YouTube change |

### Find more clips without retranscribing

On a completed project's Review page, choose **Find more clips** to start another
highlight pass from the same source. AutoClip creates a new project but reuses
the existing source media, extracted audio, transcript, and silence map. The
large audio file is hard-linked rather than copied, so the second project does
not consume another full audio file on disk.

Follow-up passes tell the AI which word ranges were already selected and also
filter substantial overlaps after the model responds. A third pass avoids clips
from both earlier passes, and so on. Change the active provider or clip settings
before starting another pass if you want a different model or a different clip
budget.

### Caption styles

| style | look |
|---|---|
| `bold_pop` | chunky white, heavy outline, spoken word grows and turns yellow |
| `karaoke_fill` | words fill with colour exactly as they're spoken |
| `clean_lower` | minimal lower third, no animation |
| `boxed` | high-contrast text on a solid block |

Fonts are bundled under the SIL Open Font License, so nothing is fetched at runtime.

## Troubleshooting

Everything here is a real failure hit during development, not hypothetical.

**`autoclip doctor` says Python is wrong.** AutoClip currently supports Python 3.11 and 3.12. Create the environment with `uv venv --python 3.11`.

**Transcription fails with "Library cublas64_12.dll is not found".** The CUDA runtime libraries aren't installed. `uv pip install -e ".[gpu]"`. AutoClip registers their location itself — pip installs them somewhere the OS loader doesn't search, which is why the error is so unhelpful.

**"Requested int8_float16 compute type, but the target device does not support..."** Clear `whisper.compute_type` in `~/.autoclip/config.json` and let AutoClip choose. It queries the backend for what's actually supported rather than guessing from your GPU model.

**Captions don't appear in exports, or ffmpeg says "No such filter: ass".** Your ffmpeg has no libass. `doctor` reports this and prints the right command for your platform. On macOS that means `brew install ffmpeg-full`, not `brew install ffmpeg`; on Windows the full Gyan build, not "essentials".

**YouTube downloads fail with a bot check.** As of 2026, YouTube blocks most anonymous downloads and proof-of-origin tokens no longer clear it. Set **Settings → Ingest → cookies from browser** to a browser you're signed into, and **close that browser** first — it locks its cookie database while running. Uploading a file always works and needs none of this.

**Twitch VODs.** Paste a normal VOD URL such as `https://www.twitch.tv/videos/123456789`. Live channel pages are not accepted because AutoClip needs a finite source duration before transcription. Public VODs normally need no cookies; restricted VODs can use the same **cookies from browser** setting when your signed-in Twitch account has access.

**Exports are slower than expected.** Check `doctor` for GPU encoding. A build can list `h264_nvenc` and still be unusable if your driver is older than the NVENC API it was compiled against; AutoClip probes this and falls back to CPU encoding, which is identical quality and just slower.

**No sound in the review player.** Click **Test audio** under the player. It measures the actual signal leaving the video element and tells you whether the problem is in AutoClip or between your browser and your speakers.

## How it works

```
ingest → prepare → transcribe → highlights → reframe → captions → export
```

Each stage writes artifacts to `~/.autoclip/work/{job_id}/`, so a retry resumes at the stage that failed rather than starting over. Two decisions carry most of the design:

**Highlight detection returns word indices, not timestamps.** Models are unreliable at arithmetic and completely reliable at copying a number they can see. Timing is looked up from measured word timings afterwards.

**Reframe is intentionally static.** Each clip gets one centered crop for its target aspect ratio. If the subject needs different framing, use the Layout editor rather than a second automatic tracking system.

[ARCHITECTURE.md](docs/ARCHITECTURE.md) covers the rest, including why several odd-looking choices exist.


## Contributing

Prompts and caption styles are the highest-leverage places to start, and neither needs deep knowledge of the codebase — `backend/autoclip/prompts/highlight_v1.txt` is plain text and affects output quality more than most code changes. See [CONTRIBUTING.md](docs/CONTRIBUTING.md).

## Legal

AutoClip bundles [yt-dlp](https://github.com/yt-dlp/yt-dlp). **Only download content you own or have the rights to process.** AutoClip contains no workarounds for DRM or paywalled content and never will.

## License

MIT — see [LICENSE](LICENSE). Bundled fonts (Anton, Inter) are under the SIL Open Font License.
