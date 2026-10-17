# Source-unit dispositions and output modes

Source-unit decisions are recorded against the existing component-decision
model.  A project profile revision resolves to its base profile, so recording
through a project profile keeps the selected overlay visible without creating
a second decision store:

```python
from sourcebridge.dispositions import (
    query_source_unit_dispositions,
    record_source_unit_disposition,
)

record_source_unit_disposition(
    database,
    profile_revision_id="project-profile-...",
    source_unit_id="unit-...",
    disposition="direct_translation",
    rationale="The unit is covered by the selected backend.",
    decided_by="reviewer",
)
units = query_source_unit_dispositions(
    database,
    profile_revision_id="project-profile-...",
)
```

Output is intentionally not a general renderer.  A backend supplies generated
text records and Sourcebridge adds deterministic evidence around them:

```python
from sourcebridge.dispositions import render_annotated_review, render_clean_release

artifacts = [{
    "source_unit_id": "unit-...",
    "artifact_path": "generated/example.rs",
    "content": "fn example() {}",
    "rule_id": "rule-...",
}]
review = render_annotated_review(
    database,
    profile_revision_id="project-profile-...",
    artifacts=artifacts,
)
release = render_clean_release(
    database,
    profile_revision_id="project-profile-...",
    artifacts=artifacts,
)
```

Annotated review text names the source unit, rule, selected profile, digest
provenance, and any `SOURCEBRIDGE INCOMPLETE` boundary.  Clean release text
contains no inline review markers.  It refuses `blocked`, `stubbed_boundary`,
or explicitly declared incomplete records and returns a sidecar report naming
all source units, replacements, exclusions, non-direct decisions, validation
identity, and output digests.  The CLI equivalent is:

```sh
sourcebridge disposition record --database evidence.db \
  --profile-revision project-profile-... --source-unit unit-... \
  --disposition direct_translation --rationale "Covered" --decided-by reviewer
sourcebridge render-output --database evidence.db \
  --profile-revision project-profile-... --mode clean-release \
  --manifest generated-manifest.json --output generated/release.rs
```

The clean command writes `generated/release.rs.provenance.json`.  This
prototype requires the selected project profile's declared toolchain digest;
it does not choose a renderer, run validation, or implement release policy.
