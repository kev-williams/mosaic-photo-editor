# Contributing to Mosaic Photo-Editor

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Privacy first: no photos in the repository

Photos of people are personal data and often someone else's copyright. This repository is set up so
they can't be committed by accident:

- `.gitignore` is an **allowlist**: every file is ignored unless it is listed there. When you add a new
  source file, add it to `.gitignore` on purpose.
- A pre-commit check (`scripts/check-privacy.sh`) blocks image, raw, video and data files, `.env`
  files, files with image data under another name, and anything over 5 MB. The installer turns it on;
  in a fresh clone run `git config core.hooksPath .githooks`. Please don't bypass it with `--no-verify`.
- CI runs the same check on every pull request.
- Tests generate their test images in memory. Never add sample photos.
- In issues, describe the problem or share a screenshot of the interface. Only attach a photo of a
  person if you have their permission, and prefer stock photos with a clear licence.

## Development setup

```bash
git clone https://github.com/kev-williams/mosaic-photo-editor.git
cd mosaic-photo-editor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt          # add -r requirements-ai.txt for the AI features
git config core.hooksPath .githooks
streamlit run app.py
```

`MOSAIC_AI_FAKE=1 streamlit run app.py` swaps the AI models for simple stand-ins (an oval mask and
repeated edge pixels), so you can work on the interface without downloading 1 GB of models.

Use `MOSAIC_PHOTOS_DIR` and `MOSAIC_STATE_DIR` to keep test runs away from your real folders.

## Checks

```bash
ruff check .
pytest
scripts/check-privacy.sh --all
```

## Code layout

- `mosaic/core.py` is pure image processing with no interface code, so it can be reused by a
  command-line tool or a future native app. Keep it that way.
- `mosaic/ai.py` holds the models. Results are cached per photo; keep expensive work out of
  per-crop code paths so editing stays instant.
- `mosaic/ui/` is the Streamlit interface. The settings panel and editors run as fragments, so a
  click only refreshes the part of the page it affects.
- Interface text: plain, short, no emoji. British spelling is used throughout.

## Pull requests

- One change per pull request, with a short description of what and why.
- Add or update tests for behaviour changes.
- Add a line to `CHANGELOG.md` under an `Unreleased` heading.

## Releases (maintainers)

Bump `__version__` in `mosaic/__init__.py`, add the version to `CHANGELOG.md`, then run
`./scripts/release.sh`.
