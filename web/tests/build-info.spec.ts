import { test, expect } from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { dashboardBuildInfo } from "../build-info";

test("source archives report unknown or use a validated explicit build stamp", () => {
  const directory = mkdtempSync(join(tmpdir(), "hortator-build-test-"));
  const saved = process.env.HORTATOR_BUILD_INFO;
  try {
    delete process.env.HORTATOR_BUILD_INFO;
    const unknown = dashboardBuildInfo(directory);
    expect(unknown.commit).toBeNull();
    expect(unknown.dirty).toBeNull();
    process.env.HORTATOR_BUILD_INFO = JSON.stringify({
      commit: "a".repeat(40),
      commit_title: "Archive build",
      committed_at: "2026-09-07T22:00:00+02:00",
      dirty: false,
      private_extra: "omit this",
    });
    const stamped = dashboardBuildInfo(directory);
    expect(stamped.commit).toBe("a".repeat(40));
    expect(stamped.commit_title).toBe("Archive build");
    expect(stamped.committed_at).toBe("2026-09-07T20:00:00Z");
    expect(stamped.provenance).toBe("build");
    expect(stamped).not.toHaveProperty("private_extra");
  } finally {
    if (saved === undefined) delete process.env.HORTATOR_BUILD_INFO;
    else process.env.HORTATOR_BUILD_INFO = saved;
    rmSync(directory, { recursive: true });
  }
});

test("malformed explicit build metadata fails without echoing its contents", () => {
  const directory = mkdtempSync(join(tmpdir(), "hortator-build-test-"));
  const saved = process.env.HORTATOR_BUILD_INFO;
  try {
    for (const raw of [
      "not-json-test-only",
      "null",
      JSON.stringify({ commit: ["a".repeat(40)] }),
    ]) {
      process.env.HORTATOR_BUILD_INFO = raw;
      expect(() => dashboardBuildInfo(directory)).toThrow(
        /HORTATOR_BUILD_INFO/,
      );
    }
  } finally {
    if (saved === undefined) delete process.env.HORTATOR_BUILD_INFO;
    else process.env.HORTATOR_BUILD_INFO = saved;
    rmSync(directory, { recursive: true });
  }
});
