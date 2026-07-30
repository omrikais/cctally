interface Props {
  labels?: string[];
}

// #424 — the same quiet, row-qualifying account-chip vocabulary used by the
// merged 5-hour table. A pooled weekly row can have more than one owner.
export function PeriodAccountChips({ labels }: Props) {
  if (!labels || labels.length === 0) return null;
  return (
    <span
      className="period-account-chips"
      aria-label={`Accounts: ${labels.join(', ')}`}
    >
      {labels.map((label) => (
        <span
          key={label}
          className="period-account-chip"
          title={`Weekly cycle owned by ${label}`}
        >
          {label}
        </span>
      ))}
    </span>
  );
}
