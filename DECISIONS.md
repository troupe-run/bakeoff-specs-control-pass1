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
