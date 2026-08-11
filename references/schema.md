# Canonical project schema

`project.json` is the maintainable source of truth. The current [CLI](../scripts/boardctl.py) validates a focused executable contract rather than a separate JSON Schema file. Start from the bundled [sample project](../assets/sample-project.json), keep every top-level object and collection, and use this reference for semantic extraction.

## Top-level shape

```json
{
  "meta": {},
  "project": {},
  "workflow": [],
  "members": [],
  "sources": [],
  "tasks": [],
  "decisions": [],
  "updates": [],
  "evidence": [],
  "deliveries": []
}
```

`meta` and `project` must be objects. Every other top-level key shown above must be an array. IDs must be non-empty strings and unique within each collection. Milestone IDs must be unique within `project.milestones`.

## Objects and fields

### `meta`

| Field | Meaning |
| --- | --- |
| `schema_version` | Data contract version, exactly the string `1.0` |
| `skill_version` | Valid SemVer synchronized with the bundled [VERSION](../VERSION) when the model is created or migrated |
| `theme` | `warm`, `clean`, `dark`, or `paper` |
| `audience` | `private`, `team`, or `public` |
| `language` | BCP-47-like tag such as `en` or `zh-CN`; defaults to `und` when rendered |
| `updated_at` | ISO 8601 timestamp of the model update |

### `project`

Use `id`, `name`, `goal`, `milestones`, and `cadence`. A milestone uses `id`, `name`, required `visibility`, and optional `due_date`. Missing or invalid visibility fails validation and filters closed from non-private views. There is no public default.

### `workflow`

Use `id`, `name`, `description`, `human_gate`, and required `visibility`. Missing or invalid visibility fails validation and filters closed from non-private views. There is no public default.

### `members`

Use `id`, `display_name`, `responsibility`, and `visibility`. Refer to members by ID from tasks and decisions. Prefer role labels over personal names for non-private views.

### `sources`

Each source ledger record uses:

| Field | Meaning |
| --- | --- |
| `id` | Stable source ID |
| `type` | Honest acquired form, such as `text`, `markdown`, `json`, `document`, `chat`, or `transcript` |
| `location` | Safe locator. Use an HTTPS URL only when the audience may see it, otherwise use a non-absolute local label |
| `sha256` | Exactly 64 hexadecimal characters for the acquired bytes |
| `revision` | Provider revision when available, otherwise a documented retrieval revision label |
| `read_at` | ISO 8601 acquisition time |
| `sensitivity` | `private`, `team`, or `public` |

The public and team views omit source hashes and unapproved extra fields. A non-private source location is kept only when it is a safe HTTP or HTTPS URL.

### `tasks`

Use `id`, integer `revision`, `description`, `owner`, `reported_status`, `verification_level`, `approval_state`, `visibility`, optional `due_date`, optional `blocked_reason`, and `source_refs`.

`reported_status` values:

```text
unknown | not_started | in_progress | blocked | done | conflict | cancelled
```

`verification_level` values, from weakest to strongest:

```text
none | source_report | source_backed | artifact_present | local_verified | target_verified | e2e_verified
```

- `none`: no supporting claim was inspected.
- `source_report`: a source reports the state.
- `source_backed`: the claim is corroborated by pinned source material.
- `artifact_present`: the named artifact exists and was inspected.
- `local_verified`: the relevant local behavior was run and checked.
- `target_verified`: the user-visible target environment was checked.
- `e2e_verified`: the required end-to-end user path was completed.

`approval_state` values are `draft`, `reviewed`, and `accepted`. Local validation, tests, manifest hashes, browser checks, and other technical evidence never change this field. Use `reviewed` only after explicit human review of the record and `accepted` only after explicit human acceptance. Approval does not raise verification, and verification does not imply approval.

For a `done` task above `source_report`, the validator requires at least one evidence record referencing that task. The Agent must still verify that the evidence actually supports the claimed level.

### `decisions`

Use `id`, integer `revision`, `topic`, `conclusion`, `decision_state`, `decided_by`, `decided_at`, `supersedes`, `source_refs`, and explicit `visibility` for newly extracted records.

`decision_state` values are `proposed`, `decided`, `reversed`, and `conflict`. `supersedes` is always an array of existing decision IDs, possibly empty. It may not point to the same decision, an unknown decision, or create a cycle.

The merge command never updates a decision in place. A changed conclusion is a new decision with a new ID and explicit `supersedes` history.

### `updates`

Use `id`, `date`, `title`, `summary`, `sections`, `task_changes`, `risks`, `learnings`, `source_refs`, and explicit `visibility`. Keep reported claims in the summary while task truth remains in `tasks`.

### `evidence`

Use `id`, `task_id`, `artifact`, optional `command`, optional `sha256`, `target`, `recorded_at`, `result`, `source_refs`, and explicit `visibility`. `task_id` must reference an existing task. Evidence records explain what was checked; they do not automatically calculate a verification level.

### `deliveries`

Use `id`, `name`, `description`, optional `source_refs`, and explicit `visibility`. This is a record of delivery state, not authority to deliver. Private output preserves valid source references. Team and public output exposes only whitelisted delivery fields and removes `source_refs` entirely.

## Visibility and privacy closure

Audience order is `public`, then `team`, then `private`. A record is visible when its classification is no more restrictive than the requested audience.

An omitted renderer visibility defaults to private. Workflow and milestone visibility is also required by validation. Sources use `sensitivity` instead of `visibility`. Newly extracted records should always set an explicit classification so intent survives maintenance.

Non-private views whitelist fields, remove hidden source references, replace hidden member or task relationships with generic summaries, and remove private extension fields. Do not rely on that mechanical pass to recognize semantic secrets; inspect the actual audience output.

## Migration from older data

Older project files may omit workflow or milestone visibility or contain a non-SemVer `meta.skill_version`. Do not infer that missing visibility means public.

1. Create a separate private migration copy; do not overwrite the current truth source.
2. Set each workflow and milestone visibility from an explicit human-reviewed audience decision. Use `private` while uncertain.
3. Set `meta.skill_version` to the exact SemVer in [VERSION](../VERSION).
4. Run `validate`, render the intended audience into a new directory, and repeat static, privacy, hash, viewport, and print checks.
5. Replace the old canonical file only after the user explicitly approves that overwrite.

The merge patch does not support `meta`, `project.milestones`, or `workflow`, so this migration must be prepared as a full canonical copy rather than forced through `merge`.

## Stable IDs and revisions

- Derive IDs from durable identity and keep them unchanged when wording, status, owner, or position changes.
- Never recycle an ID for a different entity.
- Task and decision revisions are integers. A task patch supplies `base_revision`; successful merge increments the stored revision.
- Source `revision` is a string supplied by the source system when possible. If unavailable, label the retrieval revision honestly and retain `sha256` as the byte identity.
- Append-only source, evidence, and update IDs must be new. Duplicate IDs stop the merge.

## Merge patch shape

Only these top-level collections are accepted:

```json
{
  "sources": [],
  "evidence": [],
  "updates": [],
  "tasks": [
    {
      "id": "task-review",
      "base_revision": 2,
      "reported_status": "blocked",
      "blocked_reason": "Awaiting source review"
    }
  ],
  "decisions": []
}
```

The merge validates the entire resulting model. It rejects unsupported top-level changes, duplicate or unknown IDs, stale task revisions, in-place decision edits, missing decision `supersedes`, and an existing output that differs from the supplied base.

## Validator coverage

`validate` deterministically checks required top-level containers, IDs, selected enums, revisions, source hashes and references, decision history, evidence task references, evidence for elevated `done` claims, unsafe URL protocols, embedded active HTML patterns, absolute local paths, and common secret patterns.

Passing validation does not prove extraction completeness, semantic privacy, artifact correctness, target behavior, or approval. Those remain Harness and human-review responsibilities.
