Initial audit (2026-09-27): all source files and directories inspected before edits.
Repository has staged initial files and pre-existing unstaged hybrid edits plus an unrelated web page; preserved.
Oracle golden baseline works as a library, but has no CLI or input validation.
Schemas are permissive; traversal cap and evidence ties depend on insertion order.
CI exists but schema script checks JSON syntax only; oracle CLI and generator arguments are broken.
Pure candidates are info-only skeletons. Hybrid build.zig is empty, Cargo manifests conflict,
and header/Rust/Zig disagree on buffer capacity, status and release calling convention.
Manifest paths incorrectly include repository directory prefix.
python3 3.14.7, Rust/Cargo 1.99 nightly and Git available; zig command and jsonschema absent.
