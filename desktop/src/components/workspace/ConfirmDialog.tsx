type ConfirmDialogProps = {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  loading: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  loading,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation">
      <section className="modal-content" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
        <h2 className="modal-title" id="confirm-dialog-title">
          {title}
        </h2>
        <p className="modal-description">{description}</p>
        <div className="modal-actions">
          <button className="modal-button modal-cancel" type="button" onClick={onCancel} disabled={loading}>
            取消
          </button>
          <button className="modal-button modal-confirm" type="button" onClick={onConfirm} disabled={loading}>
            {loading ? "处理中" : confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
