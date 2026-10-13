# Repository Instructions

- Write repository content and commit messages in English. Keep each Markdown paragraph on one source line.
- Keep `README.md` user-facing, design documents in `docs/`, and this file limited to contributor instructions.
- Treat executable code and public interfaces as the authority for implemented behavior. Keep interface contracts in types and concise API docstrings, with executable examples alongside code.
- Work directly from concrete requests. Do not require a written plan or documentation update before ordinary code changes, or create duplicate project-memory files, task logs, or code inventories.
- Keep code simple and focused. Do not add unsolicited fallback behavior, speculative extension points, or unrelated infrastructure.
- Ask only when a necessary unresolved choice materially affects the requested work. Keep explanations factual and direct, without unsolicited defensive caveats.
- Before using Python, check for a project virtual environment first and a Conda environment second. Always ask before using system Python.
- Request installation of missing required dependencies instead of substituting or degrading the implementation.
- Add fine-grained progress reporting to operations expected to take a long time.
- Add or update meaningful tests alongside implemented behavior and fixes, and run relevant checks. Test observable behavior and distinguish software checks from device and optical measurements.
- Attribute measured results to their experiments and state targets and readiness thresholds separately.
- When consulting `bapd8_acquisition`, follow its local instructions and inspect only RPi and relevant shared components. Exclude legacy Jetson and Pico materials, and do not modify that repository without an explicit request.
- Make small, focused Git commits. Review staged changes and complete applicable requested checks before committing; stage only task-related files and preserve others' work.
