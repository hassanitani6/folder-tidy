# Changelog

Versions follow [Semantic Versioning](https://semver.org/).

## 0.1.0 — unreleased

First public release: `init` (an example rules file), `sort` (preview by default; `--apply` to move), `undo`
(preview by default; puts back every file from one run) and `doctor`. Files are never replaced, destinations must
stay inside the sorted folder, symbolic links are never followed, and every move is journaled before it happens.
