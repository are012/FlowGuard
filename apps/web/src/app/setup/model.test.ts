import { describe, expect, it } from "vitest";

import type { ImportCandidate } from "@/lib/types";

import { replaceCandidate } from "./model";

describe("replaceCandidate", () => {
  const before: ImportCandidate[] = [
    { candidate_id: "before" },
    { candidate_id: "grouped" },
    { candidate_id: "after" },
  ];

  it("replaces the grouped candidate in place with the server candidates", () => {
    const replacements = [
      { candidate_id: "split-1" },
      { candidate_id: "split-2" },
    ];

    expect(replaceCandidate(before, "grouped", replacements)).toEqual([
      { candidate_id: "before" },
      ...replacements,
      { candidate_id: "after" },
    ]);
    expect(before.map((candidate) => candidate.candidate_id)).toEqual([
      "before",
      "grouped",
      "after",
    ]);
  });

  it("removes the grouped candidate when the server returns no candidates", () => {
    expect(replaceCandidate(before, "grouped", [])).toEqual([
      { candidate_id: "before" },
      { candidate_id: "after" },
    ]);
  });
});
