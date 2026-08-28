# App Semantics Extractor

This repository contains the public research implementation of App Semantics Extractor. It
packages three parts of the implemented research pipeline:

1. UI-guided Android source analysis using Joern and Android resource metadata
2. LLM-based Predicate and Variable autoformalization
3. Pinned sample manifests for three open-source Android applications

The snapshot does not include runtime state tracking, agent interfaces, instrumentation, or
pre-action verification.

## Pipeline

```text
Android GUI, accessibility tree, and source code
                    ↓
UI anchor and source-context extraction
                    ↓
GUI/source fusion and LLM autoformalization
                    ↓
Predicate and Variable artifacts
```

## Installation

```bash
uv sync
cp .env.example .env
```

Set `OPENAI_API_KEY` only when running a live LLM generation path.

## Commands

```bash
uv run app-semantics-static --help
uv run app-semantics-generate --help
uv run app-semantics-replay --help
```

Live static analysis additionally requires Joern, an Android SDK, and the target app's
Gradle environment.

## Repository map

- `src/app_semantics_kb/static_analysis`: Android resource, UI-anchor, usage, slicing, and evidence extraction
- `src/app_semantics_kb/autoformalization`: captured-input parsing, context fusion, prompts, LLM calls, and Predicate merging
- `research_prototypes`: historical Tree-sitter and Kotlin PSI implementations described in the research report
- `samples`: pinned upstream app metadata and source-fetch scripts

## License status

The evaluated applications are fetched from their upstream repositories and retain their
respective licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The repository owner
must choose a top-level license before treating the research code as reusable open source.

## Status

This is a research artifact, not a production Android analysis service. Ground Truth, generated
predictions, and metric results will be published after the evaluation figures are finalized.
