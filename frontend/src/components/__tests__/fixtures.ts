import type { SearchResultItem } from "../../types/contracts";

export function makeSearchItem(overrides: Partial<SearchResultItem> = {}): SearchResultItem {
  return {
    id: "image-1",
    searchSessionId: "search-1",
    libraryId: "library-main",
    docId: "doc-1",
    sha256: "a".repeat(64),
    name: "示例人物写真.jpg",
    relativePath: "人物写真/示例人物写真.jpg",
    libraryName: "图片数据库",
    rank: 1,
    matchState: "high",
    rankSource: "fused",
    width: 2048,
    height: 3072,
    tags: ["原神", "雷电将军", "站立"],
    matchedTags: ["雷电将军", "原神"],
    confidence: 0.923,
    rawScore: 0.81234,
    normalizedScore: 0.93456,
    sizeBytes: 1_572_864,
    imageUrl: "http://127.0.0.1:8765/api/images/image-1/preview",
    thumbnailUrl: "http://127.0.0.1:8765/api/images/image-1/thumbnail",
    ...overrides,
  };
}
