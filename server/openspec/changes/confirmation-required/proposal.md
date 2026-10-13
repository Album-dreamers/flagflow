## Why

Applying, resetting, or deleting a flag/value on a production-like environment is currently
indistinguishable in the UI from doing so locally — a misclick has the same one-click cost
everywhere. PR #77 patched this for `Flag.jsx` only, by hardcoding a production hostname and a
Ukrainian confirmation string in JSX and calling `window.confirm()`. That approach can't be
configured per-environment without a frontend redeploy, doesn't localize, produces an unstyled
blocking dialog, and misses `Value.jsx`, which has the identical apply/reset/delete surface.

## What Changes

- Add a `ui.confirmation_required` boolean to the server config (YAML), defaulting to `false`.
- Expose it to the frontend as a root-level GraphQL field `confirmationRequired`, following the
  existing `authenticated`/`authMethods` pattern (config → resolver → hiku `Field`/`Node`).
- Fetch it once at app boot alongside the existing auth query and expose it via `useAuth()`.
- When `confirmationRequired` is true, wrap Apply and Reset actions on both `Flag.jsx` and
  `Value.jsx` in an antd `Popconfirm` (title: "This changes production") — these currently fire
  immediately with no confirmation at all.
- Extend the existing Delete `Popconfirm` (already present on both files) so its title also
  becomes "This changes production" when `confirmationRequired` is true; delete keeps confirming
  unconditionally in all environments, this only changes the copy shown on flagged environments.
- Remove the `window.confirm`/hardcoded-hostname logic added in PR #77.
- **BREAKING**: none — new config key defaults to `false`, matching today's unconfirmed behavior.

## Capabilities

### New Capabilities
- `confirmation-required`: server-configured flag, propagated via GraphQL, that gates a
  production-change confirmation dialog on flag/value apply, reset, and delete actions in the UI.

### Modified Capabilities
(none — no existing spec capabilities are on record in this repo yet)

## Impact

- Backend: `featureflags/config.py` (new `UiSettings`), `configs/*.yaml`,
  `featureflags/graph/graph.py` (new root field/resolver).
- Frontend: `ui/src/context/auth.jsx` (query + context value), `ui/src/Dashboard/Flag.jsx`,
  `ui/src/Dashboard/Value.jsx` (button wiring), likely a small shared `ConfirmableButton`/helper
  to avoid repeating `Popconfirm` boilerplate across both files' Apply/Reset/Delete buttons.
- No DB schema or API contract changes beyond the additive GraphQL field.
