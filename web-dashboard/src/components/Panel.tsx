import type { PropsWithChildren, ReactNode } from "react";

interface PanelProps extends PropsWithChildren {
  title: ReactNode;
  right?: ReactNode;
  className?: string;
}

export default function Panel({ title, right, children, className = "" }: PanelProps) {
  return (
    <section className={`rounded-xl border border-arena-border bg-arena-panel/90 shadow-glow ${className}`}>
      <header className="flex items-center justify-between border-b border-arena-border px-4 py-3">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-arena-muted">{title}</h2>
        {right ? <div>{right}</div> : null}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}
