# Research prototypes

This directory preserves the intermediate static-analysis implementations described in the
project report. They document the path from syntax-level parsing to the current Joern pipeline
and are not part of the supported execution path.

## Tree-sitter

The Tree-sitter prototype explored Kotlin/Java AST queries for Android resource references,
data models, and GUI-related code. It exposed limitations in type resolution and cross-file
reference tracking.

## Kotlin PSI

The PSI prototype used the Kotlin compiler frontend and `BindingContext` to identify state
hubs, extract raw state candidates, collect XML/View metadata, create State-View mapping
candidates, and locate common UI event sources. Its final Predicate refinement and
control/data-flow transition analysis were not completed.

The original `android.jar`, Gradle caches, IDE metadata, build outputs, and local sample apps are
excluded. The maintained Joern-based implementation is under
`src/app_semantics_kb/static_analysis`.
