# Changelog

All notable changes to Mosaic Photo-Editor. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-10-31

### Added
- Shape **None (whole photo)**: converts and resizes without cropping (set the longest side).
- Batch: **Image Previews** filters (needs attention, no face found, adjusted, not adjusted, processed
  before, new), search by file name, and sorting.
- Click any thumbnail to open it in the crop editor (Upload and Batch).

### Changed
- The output format is now the first setting in the Output section (a dropdown, WebP by default).
- Batch: **Convert** moved to the bottom of the Output section; it applies any changed settings first.
- Batch: **Origin Folder** and **Destination Folder** are single dropdowns with up to 4 recent folders
  and **Choose Folder...** at the end. Files that already exist are always skipped.
- Upload: **Browse...** sits in the middle of the drop area; the start-up hint text is gone.
- Thumbnail warnings are spelled out (for example "Too little space above the head") and no longer cut off.
- Removed the example file name under the settings.

### Fixed
- Batch: **Done** now closes the crop editor.

## [1.0.1] - 2026-10-31

### Added
- Timing log (sidebar > Timing log): how long app start, AI warm-up, each AI run, each photo and each
  batch take, saved to `timings.csv` in the logs folder. Use it to compare the Mac app with Docker.
  Set `MOSAIC_TIMING_LOG=0` to turn it off.

### Changed
- Much faster start: the app no longer loads the AI libraries just to check they are installed.
- AI models now load in the background as soon as the app opens, so the first background replacement
  or extension no longer waits for them. Progress bars say when the models are still loading.
- New start-up screen with the app icon and progress while the app opens.
- Upload: one **Browse...** button for photos and whole folders, under a larger drop area.
- Larger logo and name, top left.
- Settings panel: shorter. The EXIF note is now a **?** you can hover.
- Folder file counts in the settings panel are cached, so settings stay quick with large Output folders.

### Fixed
- The top of the page was hidden behind the navigation bar.
- On Apple silicon Macs the app icon could start the app in Intel mode (Rosetta), so it failed to load.

## [1.0.0] - 2026-10-31

First public release.

### Added
- Native Mac app: opens in its own window from an app icon; closing the window, Cmd+Q or **Quit** stops it.
- Upload and Batch pages with a top navigation bar.
- Consistent headshot framing with face detection (head size and eye line), six crop shapes plus custom.
- Crop editor: drag, resize from the corners, arrow keys, and one-click headroom, zoom and move buttons.
  Changes show straight away.
- Before / after comparison for background replacement and the AI extender.
- AI background replacement (BiRefNet-portrait) with colour picker, hex code and eyedropper.
- AI extender (LaMa) for all shapes; portraits are lengthened below without generating clothing.
- AI runs inside the app on the Apple GPU; results cached per photo so crop changes are instant.
- **Apply changes**: set several options, then process once.
- Output as WebP, JPEG, PNG or GIF, with optional file-size caps.
- Native folder and file pickers on both pages.
- Processing history (SQLite): a **!** marks photos already exported with the same format and size in
  the last 7 days.
- Hold-to-confirm button to move everything in the Input and Output folders to the Trash.
- Docker setup as an alternative (without AI features).
- Privacy guards: allowlist `.gitignore`, pre-commit and CI checks that block photos and data files.
