export type SourceVersion = {
  commit: string | null;
  short_commit: string | null;
  commit_title: string | null;
  committed_at: string | null;
  branch: string | null;
  dirty: boolean | null;
  provenance: string;
};
export type RuntimeVersion = SourceVersion & {
  package_version: string;
  started_at: string;
};
declare const __DASHBOARD_BUILD__: SourceVersion & { built_at: string };
export const dashboardBuild = __DASHBOARD_BUILD__;

export function Version({ version }: { version?: RuntimeVersion }) {
  const mismatch =
    version?.commit &&
    dashboardBuild.commit &&
    version.commit !== dashboardBuild.commit;
  return (
    <section className="build-banner" aria-label="Running version">
      <div className="build-identity">
        <strong>Running server</strong>
        <code>{version?.short_commit || "Commit unavailable"}</code>
        {version?.committed_at && (
          <time dateTime={version.committed_at}>{version.committed_at}</time>
        )}
        {version?.dirty && (
          <span className="build-state">Uncommitted changes at startup</span>
        )}
      </div>
      <p className="build-title">
        {version?.commit_title ||
          "Commit metadata was not supplied by this server."}
      </p>
      {mismatch && (
        <p className="build-mismatch" role="status">
          The dashboard and server are from different commits. Rebuild or
          refresh the dashboard after deploying the intended version.
        </p>
      )}
      <details className="build-details">
        <summary>Build details</summary>
        <dl>
          <div>
            <dt>Server started</dt>
            <dd>{version?.started_at || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Server commit</dt>
            <dd>{version?.commit || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Server branch</dt>
            <dd>{version?.branch || "Unavailable / detached"}</dd>
          </div>
          <div>
            <dt>Package version</dt>
            <dd>{version?.package_version || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Dashboard built</dt>
            <dd>{dashboardBuild.built_at}</dd>
          </div>
          <div>
            <dt>Dashboard commit</dt>
            <dd>{dashboardBuild.commit || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Dashboard commit date</dt>
            <dd>{dashboardBuild.committed_at || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Dashboard commit title</dt>
            <dd>{dashboardBuild.commit_title || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Dashboard source</dt>
            <dd>
              {dashboardBuild.dirty === null
                ? "Unknown"
                : dashboardBuild.dirty
                  ? "Uncommitted changes at build time"
                  : "Clean at build time"}
            </dd>
          </div>
        </dl>
        <p>
          Dates use ISO 8601 UTC. Server identity is captured at startup;
          dashboard identity is embedded at build time. Uncommitted builds can
          differ even when their commits match.
        </p>
      </details>
    </section>
  );
}
