import { Button, Popconfirm } from 'antd';

// Renders a plain Button, or a Button wrapped in an antd Popconfirm when
// `requireConfirmation` is true — used for Apply/Reset/Delete actions that
// mutate a flag or value, so the same confirmation UX can be reused across
// Flag.jsx and Value.jsx instead of duplicating Popconfirm wiring per button.
export const ConfirmableButton = ({
  requireConfirmation,
  title,
  onConfirm,
  onClick,
  children,
  ...buttonProps
}) => {
  if (!requireConfirmation) {
    return (
      <Button onClick={onClick} {...buttonProps}>
        {children}
      </Button>
    );
  }

  return (
    <Popconfirm
      title={title}
      onConfirm={onConfirm || onClick}
      okText="Yes"
      cancelText="No"
    >
      <Button {...buttonProps}>{children}</Button>
    </Popconfirm>
  );
};
