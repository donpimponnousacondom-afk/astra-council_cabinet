import { execFileSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

export function dashboardBuildInfo(
  root = fileURLToPath(new URL("..", import.meta.url)),
) {
  const built_at = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const unknown = {
    commit: null,
    short_commit: null,
    commit_title: null,
    committed_at: null,
    branch: null,
    dirty: null,
    provenance: "unknown",
    built_at,
  };
  const git = (...args: string[]) =>
    execFileSync("git", ["-C", root, ...args], {
      encoding: "utf8",
      timeout: 3000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  try {
    if (
      realpathSync(git("rev-parse", "--show-toplevel")) === realpathSync(root)
    ) {
      const [commit, committed_at, ...subject] = git(
        "show",
        "-s",
        "--format=%H%n%cI%n%s",
        "HEAD",
      ).split("\n");
      return {
        commit,
        short_commit: commit.slice(0, 12),
        commit_title: subject.join("\n"),
        committed_at: new Date(committed_at)
          .toISOString()
          .replace(/\.\d{3}Z$/, "Z"),
        branch: git("branch", "--show-current") || null,
        dirty: Boolean(
          git("status", "--porcelain", "--untracked-files=normal"),
        ),
        provenance: "git",
        built_at,
      };
    }
  } catch {
    // A source archive or Docker build may not include Git.
  }
  if (!process.env.HORTATOR_BUILD_INFO) return unknown;
  let source;
  try {
    source = JSON.parse(process.env.HORTATOR_BUILD_INFO);
  } catch {
    throw new Error("HORTATOR_BUILD_INFO must be a JSON source stamp");
  }
  if (
    !source ||
    typeof source.commit !== "string" ||
    !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(source.commit) ||
    typeof source.commit_title !== "string" ||
    !source.commit_title ||
    typeof source.dirty !== "boolean" ||
    typeof source.committed_at !== "string" ||
    !/(?:Z|[+-]\d\d:\d\d)$/.test(source.committed_at)
  ) {
    throw new Error(
      "HORTATOR_BUILD_INFO needs commit, commit_title, committed_at with timezone, and dirty",
    );
  }
  return {
    commit: source.commit,
    short_commit: source.commit.slice(0, 12),
    commit_title: source.commit_title,
    committed_at: new Date(source.committed_at)
      .toISOString()
      .replace(/\.\d{3}Z$/, "Z"),
    branch: typeof source.branch === "string" ? source.branch : null,
    dirty: source.dirty,
    provenance: "build",
    built_at,
  };
}
