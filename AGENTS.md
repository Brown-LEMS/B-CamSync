# Repository Instructions

## Project context

B-CamSync (B = Brown) is a reusable library for high-precision software synchronization of distributed cameras, intended for Brown lab projects and external researchers. It combines network clock synchronization with closed-loop frame-phase control.

The starting reference is the RPi implementation in the sibling `bapd8_acquisition` repository. That implementation coordinates two Pi 4 nodes, each managing a B0267 synchronization board and four cameras. Eight-camera experiments do not establish support for arbitrary numbers of independently controlled nodes. The new library's architecture, public API, packaging, and supported hardware remain to be decided through implementation work and explicit user decisions.

B-CamSync owns reusable synchronization functionality. BAPD8 retains its naturalistic nursing-home acquisition application, including recording, privacy controls, retention, segmentation, and data transfer. Do not modify the reference repository unless the user explicitly requests it. When consulting it, follow its local instructions and limit exploration to RPi and relevant shared components; exclude legacy Jetson and Pico code and platform-specific materials.

## Repository language

- Write all repository content in English, including code, comments, docstrings, documentation, configuration, test descriptions, and commit messages.
- Keep each Markdown paragraph on one source line; do not hard-wrap prose.

## Code-first workflow

- Treat executable code, public interfaces, and tests as the authority for implemented behavior. Establish what exists by inspecting them directly.
- Move from a concrete request to a focused implementation and relevant tests. Do not require a design document, written plan, or documentation update before ordinary code work.
- Do not treat a proposal in documentation or a prior discussion as an approved implementation contract. Ask only when a necessary unresolved choice materially affects the requested work.
- Keep implementations direct and clear. Do not add unsolicited fallback behavior, speculative extension points, or unrelated infrastructure.
- Add fine-grained progress reporting to operations expected to take a long time.
- Before using Python, check for a project virtual environment first and a Conda environment second. Ask the user before using a system Python interpreter.
- If a required dependency is unavailable, request installation rather than silently substituting or degrading the implementation.

## Testing

- This repository must include tests as implementation is introduced. Add or update meaningful tests alongside new behavior and bug fixes.
- Test observable behavior and failure conditions, including timing validation, control state transitions, and command identity where relevant; avoid tests that merely reproduce implementation details.
- Run the tests relevant to a change and any applicable required checks. Report what ran and any checks that could not run.
- Distinguish deterministic software tests from physical-device and optical validation. Simulated timing does not establish hardware synchronization accuracy.
- Do not create placeholder tests or install a test framework solely for repository initialization; choose tooling with the first implementation.

## Documentation

- Beyond this contributor policy and the README's brief project introduction, documentation serves only to explain durable design decisions: the choice, its rationale, meaningful alternatives, and consequences.
- Keep decision notes short and create them only when a non-obvious, consequential choice benefits from a lasting explanation. Use `docs/decisions/` if such notes become necessary; do not prepopulate it.
- Do not create separate project-memory files, implementation plans, task logs, progress reports, code inventories, or prose specifications that duplicate code and tests.
- Put interface contracts in types and concise API docstrings, and executable usage examples alongside code. Do not use standalone documents as runtime dependencies or prerequisites for understanding ordinary implementation work.
- Update a decision note only when its underlying decision changes. Do not require documentation churn for every code change.
- Separate measured results from targets and readiness thresholds. Do not imply universal sub-millisecond accuracy from hardware-specific experiments.

## Git workflow

- Make small, focused commits as coherent changes are completed. Keep unrelated changes in separate commits and use clear English commit messages.
- Include relevant tests with the behavior they validate. Review the staged diff and complete relevant checks before committing.
- Preserve changes made by the user or other contributors. Stage only files belonging to the current change.
