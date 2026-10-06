# Decisions

Choices made where the contract is silent. Where it allowed more than one reading, the one taken is the one least
likely to lose or corrupt data or to contradict the contract.

## `set --in-include` still refuses to disturb other settings (CR-B)

If the included file is also included elsewhere in the same theatre, writing a new key into it changes the
configuration at every place that includes it. `set --in-include` then exits 3 ("write-back would disturb …"), just
like any other `set` whose write-back would change a setting other than the one named.

Reason: the contract calls `--in-include` "the step 1 rule", and step 1 writes with this check in place. Refusing
loses nothing: the files are untouched, and the user can use an override or edit the shared file by hand. Skipping the
check would silently change settings the user never named.

## Overrides go next to the nearest include reference (CR-A)

When includes nest (`troupe.yml` → `outer.yaml` → `coder.yaml`), a new key under `coder.yaml` content is written as
an override in `outer.yaml`, the file that holds that include reference, even though `outer.yaml` is itself included.

Reason: this follows the contract's wording ("the file that contains the include reference"). Values already
overridden in `outer.yaml` live there, so writing the new key there too keeps them together.

## Merge keys are ordinary keys; troupe never writes them (issue #2)

`<<` is not resolved as a merge: the configuration is read with the YAML 1.2 core schema, which has no merge keys, so
`<<` is shown and read as a key like any other. Any `set` whose path contains `<<` (including via `--expand-alias`)
exits 3. Reason: the contract says merge keys are never written; leaving them unresolved keeps `get` and `show`
consistent with the schema the rest of the configuration uses.

## What an expanded alias looks like (issue #2)

- The copy is rendered from the anchored value, not copied as text, so it never repeats an `&anchor` (a repeated
  anchor would redirect later aliases). Aliases nested inside the anchored value are expanded in the copy too; only the
  expanded alias gets the `# expanded from *name by troupe` comment.
- When the alias itself is the path being set, the copy is the new value, so the comment keeps its two spaces.
- An alias that is a list item gets the comment on the dash line and the copy below it (`-  # expanded …`).
- A comment already after the alias stays, after troupe's comment.
- An alias inside a flow collection cannot get a block-style copy or an end-of-line comment without rewriting the
  collection: `--expand-alias` exits 3 there.
- With nested aliases (`c: *b`, where `b` holds `y: *a`), `--expand-alias` expands the outermost alias on the path.
