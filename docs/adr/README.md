# Architecture Decision Records (ADRs)

## What are ADRs?

Architecture Decision Records (ADRs) are lightweight documents that capture important architectural decisions made during the development of a project. Each ADR records:

- The context surrounding a decision
- The decision itself
- The consequences and trade-offs of that decision

ADRs provide a historical record of why certain architectural choices were made, helping current and future team members understand the reasoning behind the system's design.

## How to Write an ADR

1. Copy the template from `template.md`
2. Fill in each section with relevant information
3. Use clear, concise language
4. Focus on the "why" behind the decision
5. Reference related ADRs when applicable

## Naming Convention

ADRs follow a numbered naming convention:

```
NNNN-title.md
```

Where:
- `NNNN` is a zero-padded four-digit sequential number (e.g., 0001, 0002, 0003)
- `title` is a short, kebab-case description of the decision

Example: `0001-use-postgresql-for-primary-database.md`

## Status Lifecycle

Each ADR has a status that reflects its current state:

| Status | Description |
|--------|-------------|
| **proposed** | The decision is under consideration but not yet finalized |
| **accepted** | The decision has been approved and is being implemented or is in effect |
| **superseded** | The decision has been replaced by a newer ADR (reference the superseding ADR) |

### Status Transitions

```
proposed → accepted → superseded
```

- New ADRs start as `proposed`
- Once the decision is finalized and adopted, update the status to `accepted`
- If a later decision overrides this one, update the status to `superseded` and reference the new ADR

## Referencing ADRs

When referencing other ADRs, use the format: `[ADR-NNNN](NNNN-title.md)`

For example: "This decision supersedes [ADR-0003](0003-use-rest-api.md)."
