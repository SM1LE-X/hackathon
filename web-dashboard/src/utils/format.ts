export const fmtPrice = (value: number | null): string => (value === null ? "-" : value.toFixed(4));

export const fmtQty = (value: number | null): string => (value === null ? "-" : value.toFixed(2));

export const fmtCash = (value: number): string =>
  value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });

export const fmtSigned = (value: number): string => {
  const text = value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  return value > 0 ? `+${text}` : text;
};

export const fmtClock = (timestamp: number): string => {
  const d = new Date(timestamp);
  return d.toLocaleTimeString();
};
