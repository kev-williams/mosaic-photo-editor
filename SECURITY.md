# Security

Mosaic Photo-Editor runs on your own computer. The app server listens only on `127.0.0.1`, the window's
helper accepts requests only from this computer with a random key created at each start, and no photos
are sent anywhere.

The AI cut-out model is loaded from Hugging Face with `trust_remote_code`, which runs the model's own
Python code. To pin an exact, reviewed version set `MOSAIC_AI_MODEL_REVISION` to a commit hash of the
model repository.

## Reporting a vulnerability

Please report security problems privately through
[GitHub security advisories](https://github.com/kev-williams/mosaic-photo-editor/security/advisories/new),
not in a public issue. You'll get a reply within a week.
