## Context

`ui/src/Dashboard/Flag.jsx` and `ui/src/Dashboard/Value.jsx` each render a `Buttons` component
with Apply, Reset, and Delete actions that mutate a flag/value via GraphQL mutations
(`SAVE_FLAG_MUTATION`, `RESET_FLAG_MUTATION`, `DELETE_FLAG_MUTATION` and their `Value` analogues).
Apply and Reset fire immediately with no confirmation; Delete already asks via an antd
`Popconfirm` ("Are you sure to delete this flag?"). None of this is environment-aware.

PR #77 attempted to add a confirmation step for Flag apply/reset by hardcoding the production
hostname `flags.prom.evo-nl.dev` and a Ukrainian message string directly in `Flag.jsx`, then
calling `window.confirm()`. It also never touched `Value.jsx`, which has the same button shape.

The server already has one precedent for "config value exposed to the frontend as a boolean
capability flag": `ldapEnabled`/`oidcEnabled` under the root `authMethods` GraphQL field, sourced
from `featureflags/config.py`'s `Config.ldap`/`Config.oidc` and resolved in
`featureflags/graph/graph.py` (`root_auth_methods`, `AuthMethodsNode`). This change follows that
same path rather than inventing a new one.

## Goals / Non-Goals

**Goals:**
- A single server-configured boolean, `ui.confirmation_required`, controls whether Apply/Reset/
  Delete on flags and values require confirmation.
- The condition is defined in YAML config and reaches the UI exclusively through GraphQL — no
  hostname sniffing, no client-side environment detection.
- Confirmation is rendered with antd `Popconfirm` (the component already used for Delete),
  not `window.confirm`.
- Behavior is identical across `Flag.jsx` and `Value.jsx`.

**Non-Goals:**
- Per-project or per-flag confirmation scoping — this is a single environment-wide toggle.
- Confirming the enable/disable `Switch` itself (it only marks local state dirty; the actual
  write happens on Apply, which is already covered).
- Localization/i18n of the confirmation copy — English text is sufficient for now.
- Changing Delete's behavior when `confirmation_required` is `false`: it keeps confirming
  unconditionally in every environment, as it does today.

## Decisions

**Config shape: nest under `ui:`.**
`configs/local.yaml` gets a new top-level `ui:` section:
```yaml
ui:
  confirmation_required: false
```
`featureflags/config.py` gains a `UiSettings(BaseSettings)` with `confirmation_required: bool =
False`, and `Config.ui: UiSettings = UiSettings()`. Chosen over a flat `confirmation_required`
key at the config root because this is UI-presentation concern, not a server-behavior toggle like
`readonly`, and `ui:` gives future UI-only settings a home without further root-level sprawl.

**GraphQL exposure: root-level scalar `Field`, not a new `Node`.**
`authMethods` is a `Node` because it groups two related booleans. `confirmationRequired` is a
single, independent boolean, so it follows the simpler `authenticated` pattern instead — a root
`Field("confirmationRequired", Boolean, root_confirmation_required)` backed by:
```python
async def root_confirmation_required() -> list[bool]:
    return [runtime_config.ui.confirmation_required]
```
No new `Node`/dataclass needed. Naming matches the proposal's request exactly:
`confirmationRequired` in GraphQL, `confirmation_required` in Python/YAML (existing
camelCase-in-graph / snake_case-in-Python convention, same as `ldapEnabled` / `ldap_enabled`).

**Frontend fetch: extend the existing `AUTH_QUERY` in `ui/src/context/auth.jsx`.**
That query already fetches boot-time capability flags (`authMethods`) alongside actual auth
state (`authenticated`) — `confirmationRequired` is the same shape of thing (a capability flag
fetched once at app start) and `AuthProvider` already wraps the whole app in `main.jsx`. A
separate context/provider would duplicate the query-on-boot plumbing for no benefit. Exposed as
`useAuth().auth.confirmationRequired`.

**UI component: extract a shared `ConfirmableButton` (or equivalent small wrapper) used by
Apply, Reset, and Delete in both `Flag.jsx` and `Value.jsx`.**
Today `DeleteButton` in `Flag.jsx` and `Value.jsx` are near-duplicate `Popconfirm`-wrapped
buttons. Rather than writing six more copies (Apply × 2 files, Reset × 2 files, plus updating
Delete × 2 files), factor a helper that takes `{onClick, requireConfirmation, title, children,
...buttonProps}` and renders either a plain `Button` or a `Button` wrapped in `Popconfirm` with
the given title. `requireConfirmation` is always `true` for Delete (unconditional, as today) and
equal to `confirmationRequired` for Apply/Reset. Delete's title becomes "This changes production"
when `confirmationRequired` is true, and stays "Are you sure to delete this flag?" (or the value
equivalent) when false — so its always-on guard doesn't lose its current copy in the common case.

**Remove PR #77's approach entirely** rather than layering on top of it: delete
`PRODUCTION_HOSTNAME`, `PRODUCTION_CHANGE_CONFIRMATION`, `confirmProductionChange`, and the
`window.confirm` call from `Flag.jsx`.

## Risks / Trade-offs

- **[Risk]** A single global flag can't express "confirm for project X but not Y." →
  **Mitigation**: not a current requirement (proposal scopes this to one environment-wide
  toggle); can be revisited as a follow-up capability if a real need shows up.
- **[Risk]** Extracting `ConfirmableButton` touches both `Flag.jsx` and `Value.jsx`'s existing
  `DeleteButton`, `ResetButton`, and inline Apply `Button`, which is a slightly larger diff than
  the minimal PR #77 change. → **Mitigation**: the duplication these two files already have
  (identical `ResetButton`/`DeleteButton` implementations) is pre-existing; consolidating it here
  is in scope because it's the same code path being touched, not scope creep.
- **[Risk]** Config default (`false`) means no environment gets this protection until an operator
  opts in via YAML. → **Mitigation**: intentional — this is additive/non-breaking by design per
  the proposal; enabling it on the actual production config file is a follow-up deploy step, not
  part of this change.

## Migration Plan

1. Ship backend config field + GraphQL field (additive, defaults to `false` everywhere — no
   behavior change until an operator sets `ui.confirmation_required: true` in a config file).
2. Ship frontend changes (query, `ConfirmableButton`, wiring in both files). With the flag
   defaulting to `false`, this ships dark — Apply/Reset stay frictionless, Delete's copy is
   unchanged.
3. Set `ui.confirmation_required: true` on the production config (e.g. whatever config backs
   `flags.prom.evo-nl.dev`) as a separate, explicit follow-up change.
4. Rollback: revert step 3 (set back to `false`/omit) requires no code change or redeploy of the
   confirmation logic itself — it's a config flip.

## Open Questions

- Exact production config file/deployment path to flip `ui.confirmation_required: true` on is
  outside this repo's `configs/` (only `local.yaml`/`test.yaml` live here) — needs to happen in
  whatever config source backs the real prod deployment, out of scope for this change's tasks.
