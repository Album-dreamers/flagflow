## ADDED Requirements

### Requirement: Server-configured confirmation flag
The system SHALL expose a boolean configuration value `ui.confirmation_required` (YAML config,
default `false`) that determines whether mutating flag/value actions require user confirmation
in the UI.

#### Scenario: Default is unconfirmed
- **WHEN** `ui.confirmation_required` is omitted from a config file
- **THEN** the system SHALL behave as if it were `false`

#### Scenario: Config value is set explicitly
- **WHEN** a config file sets `ui: { confirmation_required: true }`
- **THEN** the running server SHALL treat confirmation as required for that deployment

### Requirement: GraphQL exposes the confirmation flag
The GraphQL API SHALL expose the configured value as a root-level field `confirmationRequired`
of type `Boolean`, resolved from server configuration, with no client-supplied arguments.

#### Scenario: Client queries the flag
- **WHEN** a client sends a GraphQL query including `confirmationRequired` at the root
- **THEN** the response SHALL contain the current value of `ui.confirmation_required` from the
  server's loaded config

### Requirement: UI confirms Apply and Reset when required
When `confirmationRequired` is `true`, the UI SHALL require explicit user confirmation, via an
antd confirmation dialog component, before executing the Apply (save) or Reset action on a flag
or a value. When `confirmationRequired` is `false`, Apply and Reset SHALL execute immediately
without a confirmation step, as they do today.

#### Scenario: Apply requires confirmation on a flagged environment
- **WHEN** `confirmationRequired` is `true` and a user clicks "apply" on a dirty flag or value
- **THEN** the UI SHALL show a confirmation dialog with the message "This changes production"
  **AND** the apply mutation SHALL NOT be sent until the user confirms

#### Scenario: Reset requires confirmation on a flagged environment
- **WHEN** `confirmationRequired` is `true` and a user clicks "reset" on an overridden flag or
  value
- **THEN** the UI SHALL show a confirmation dialog with the message "This changes production"
  **AND** the reset mutation SHALL NOT be sent until the user confirms

#### Scenario: Apply and Reset stay immediate when not required
- **WHEN** `confirmationRequired` is `false` and a user clicks "apply" or "reset"
- **THEN** the UI SHALL send the corresponding mutation immediately, with no confirmation dialog

### Requirement: UI confirms Delete unconditionally, with production-aware copy
The UI SHALL continue to require confirmation before deleting a flag or a value regardless of
`confirmationRequired`. When `confirmationRequired` is `true`, the confirmation dialog's message
SHALL read "This changes production"; when `false`, it SHALL use the existing generic delete
confirmation copy.

#### Scenario: Delete confirms with production copy when required
- **WHEN** `confirmationRequired` is `true` and a user clicks "delete" on a flag or value
- **THEN** the UI SHALL show a confirmation dialog with the message "This changes production"
  **AND** the delete mutation SHALL NOT be sent until the user confirms

#### Scenario: Delete confirms with generic copy when not required
- **WHEN** `confirmationRequired` is `false` and a user clicks "delete" on a flag or value
- **THEN** the UI SHALL show a confirmation dialog with the existing generic delete message
  **AND** the delete mutation SHALL NOT be sent until the user confirms

### Requirement: Confirmation applies equally to flags and values
The behavior described above SHALL be identical for flag actions (`Flag.jsx`) and value actions
(`Value.jsx`) — no action available on one SHALL be exempt on the other.

#### Scenario: Value apply behaves the same as flag apply
- **WHEN** `confirmationRequired` is `true` and a user clicks "apply" on a dirty value
- **THEN** the UI SHALL show the same confirmation behavior as it does for a dirty flag
