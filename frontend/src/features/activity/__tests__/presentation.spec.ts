import { describe, expect, it } from "vitest";

import { formatDuration } from "../presentation";

describe("activity presentation", () => {
  it("shows subsecond completed jobs as less than one second", () => {
    expect(
      formatDuration(
        "2026-07-26T04:36:36.216Z",
        "2026-07-26T04:36:36.256Z",
      ),
    ).toBe("< 1 秒");
  });
});
