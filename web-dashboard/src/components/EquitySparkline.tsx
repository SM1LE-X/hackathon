interface EquitySparklineProps {
  values: number[];
  width?: number;
  height?: number;
}

export default function EquitySparkline({ values, width = 96, height = 26 }: EquitySparklineProps) {
  if (values.length < 2) {
    return <div className="h-[26px] w-[96px] rounded border border-arena-border/60 bg-black/20" />;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1e-6, max - min);

  const points = values.map((value, idx) => {
    const x = (idx / (values.length - 1)) * width;
    const y = height - ((value - min) / range) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const first = values[0];
  const last = values[values.length - 1];
  const stroke = last >= first ? "#2AD38B" : "#FF5A72";

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="rounded border border-arena-border/60 bg-black/20">
      <polyline fill="none" stroke={stroke} strokeWidth="2" points={points.join(" ")} />
    </svg>
  );
}
