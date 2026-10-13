## 1. Backend config

- [x] 1.1 Add `UiSettings(BaseSettings)` with `confirmation_required: bool = False` to
      `featureflags/config.py`
- [x] 1.2 Add `ui: UiSettings = UiSettings()` to `Config` in `featureflags/config.py`
- [x] 1.3 Add a commented `ui:` example (or explicit `ui: { confirmation_required: false }`) to
      `configs/local.yaml`, matching the style of other optional sections already documented
      there (e.g. `oidc`)

## 2. GraphQL exposure

- [x] 2.1 Add `root_confirmation_required()` resolver in `featureflags/graph/graph.py` returning
      `[runtime_config.ui.confirmation_required]`
- [x] 2.2 Register `Field("confirmationRequired", Boolean, root_confirmation_required)` on the
      Root node in `featureflags/graph/graph.py`, alongside the existing `authenticated` field
- [x] 2.3 Confirm the field resolves via a manual GraphQL query against the running dev server
      (or existing GraphQL test harness, if present)

## 3. Frontend data fetch

- [x] 3.1 Add `confirmationRequired` to `AUTH_QUERY` in `ui/src/context/auth.jsx`
- [x] 3.2 Expose it on the `auth` object returned by `AuthProvider` (default to `false` while
      `loading`, matching the existing `authenticated` fallback pattern)

## 4. Shared confirmation UI component

- [x] 4.1 Add a small shared component (e.g. `ConfirmableButton`) that renders a plain antd
      `Button` when confirmation isn't required, or wraps it in `Popconfirm` with a given title
      when it is — place it wherever `Flag.jsx`/`Value.jsx`'s other shared UI helpers live
- [x] 4.2 Confirm it supports the three call sites' needs: always-confirm (Delete, title varies
      by `confirmationRequired`), conditionally-confirm (Apply/Reset, only when
      `confirmationRequired` is true)

## 5. Wire up Flag.jsx

- [x] 5.1 Remove `PRODUCTION_HOSTNAME`, `PRODUCTION_CHANGE_CONFIRMATION`,
      `confirmProductionChange`, and the `window.confirm` usage added by PR #77 from
      `ui/src/Dashboard/Flag.jsx` (if present on this branch) — not present on this branch
      (PR #77 is unmerged), nothing to remove
- [x] 5.2 Replace `ResetButton`'s plain `Button` with `ConfirmableButton`, gated on
      `useAuth().auth.confirmationRequired`, title "This changes production"
- [x] 5.3 Replace the inline Apply `Button` in `Buttons` with `ConfirmableButton`, same gating
      and title
- [x] 5.4 Update `DeleteButton` to use `ConfirmableButton` (or keep `Popconfirm` directly) so its
      title becomes "This changes production" when `confirmationRequired` is true, and stays the
      existing generic copy when false — confirmation itself remains unconditional

## 6. Wire up Value.jsx

- [x] 6.1 Replace `ResetButton`'s plain `Button` with `ConfirmableButton`, gated on
      `useAuth().auth.confirmationRequired`, title "This changes production"
- [x] 6.2 Replace the inline Apply `Button` in `Buttons` with `ConfirmableButton`, same gating
      and title
- [x] 6.3 Update `DeleteButton` to use `ConfirmableButton` (or keep `Popconfirm` directly) so its
      title becomes "This changes production" when `confirmationRequired` is true, and stays the
      existing generic copy when false

## 7. Verification

- [x] 7.1 Manually verify in the browser with `ui.confirmation_required: false` (default): Apply/
      Reset fire immediately on both Flag and Value pages, Delete shows its existing generic
      confirmation
- [x] 7.2 Manually verify with `ui.confirmation_required: true`: Apply/Reset on both Flag and
      Value pages show "This changes production" and only mutate after confirming; Delete shows
      the same message
- [x] 7.3 Run existing frontend and backend test suites to confirm no regressions — 146/146
      backend tests pass (`lets test`); frontend has no test suite, `npm run build` passes clean
- [x] 7.4 Update `CHANGELOG.md` with an entry describing the new `ui.confirmation_required`
      config option
