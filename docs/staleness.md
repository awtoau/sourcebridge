# Digest-pinned staleness

Sourcebridge keeps an append-only provenance link for each conversion
execution, rule-runtime result, generated artifact, or validation result. The
link records:

- the immutable project-profile revision and resolved profile digest;
- the digest of the ordered resolved rule revisions;
- the declared toolchain digest; and
- the profile's required full validation tier.

Profiles that need this evidence must declare `toolchain_digest` when they are
created. The link helpers reject a missing or mismatched declaration rather
than guessing from installed files:

```python
from sourcebridge.staleness import (
    link_conversion_artifact,
    link_validation_evidence,
)

link_conversion_artifact(
    database,
    "generated-artifact-id",
    profile_revision_id="project-profile-id",
    toolchain_digest="toolchain-v1",
)
link_validation_evidence(
    database,
    "validation-result-id",
    profile_revision_id="project-profile-id",
    toolchain_digest="toolchain-v1",
)
```

The #35 rule-runtime executor links its run automatically when the resolved
profile declares a toolchain digest. Historical rows are not updated when a
replacement profile is created or promoted.

Use the compact report for a current profile:

```sh
sourcebridge report-staleness \
  --database evidence/sourcebridge.db \
  --project renode \
  --profile-name csharp-to-rust
```

Pin a candidate replacement before it is promoted:

```sh
sourcebridge report-staleness \
  --database evidence/sourcebridge.db \
  --profile project-profile-<digest>
```

Each linked result is `current` or `stale_for_current_profile`. Staleness is
derived when the resolved profile, rule, or toolchain digest differs; the
historical link and original result remain unchanged. The report also returns
`required_validation_tier` and the latest certification status. A focused
check or a diagnosis can explain a stale result, but only the declared full
validation tier can certify a replacement as current.
