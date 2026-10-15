# Migration ledger

Every component moved into Sourcebridge begins with an immutable ledger record
under `migration/records/`. The record captures:

- source repository remote and exact committed revision;
- destination component;
- checksum for every extracted source path;
- path-level Git commit history, timestamps, and subjects.

Create a record only from a clean source repository:

```text
python3 cli/record_migration.py \
  --source-repo ../source-repository \
  --component neutral-evidence \
  --destination core/evidence \
  path/to/source
```

The record is created before copying, extracting with `git filter-repo` or
`git subtree`, or refactoring a component. A migration changes the component
record only by adding a new, checked record; it never overwrites the evidence
for a previous import.

The ledger does not replace the source repository or its issue tracker. It
provides a durable, reviewable link between source history and the extracted
Sourcebridge component. Cross-repository issue continuations must retain the
original issue URL and state in the component's migration report.
