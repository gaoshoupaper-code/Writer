import type { ReactNode } from "react";

type AppShellProps = {
  topBar: ReactNode;
  sidebar: ReactNode;
  children: ReactNode;
};

export function AppShell({ topBar, sidebar, children }: AppShellProps) {
  return (
    <main className="dashboard-shell">
      {topBar}
      <div className="dashboard-body">
        {sidebar}
        <section className="dashboard-main">{children}</section>
      </div>
    </main>
  );
}
