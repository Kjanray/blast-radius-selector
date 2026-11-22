# Blast Radius selector

For each patch: apply it in a symlink overlay of sqlglot at the pinned commit, find the code units whose AST changed, map them to candidate tests with a coverage / file-open / class-usage map built from sqlglot's own test suite at the pinned commit (map.json.gz), run the cheap candidates against the patched tree, and report the ones that fail. Candidates that cost more CPU to verify than to include are included without running.
