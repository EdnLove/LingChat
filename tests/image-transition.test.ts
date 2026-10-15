import assert from "node:assert/strict";
import { test } from "node:test";
import { needsSequentialFade, type ImageProfile } from "../src/utils/image-transition.ts";

function profile(values: number[], opaque = false, aspectRatio = 1): ImageProfile {
  return { alpha: Uint8Array.from(values), opaque, aspectRatio };
}

test("same silhouette and small alpha variations crossfade", () => {
  assert.equal(needsSequentialFade(profile([0, 255, 255, 0]), profile([0, 253, 254, 0])), false);
});

test("a pose moving to a different part of the canvas fades out first", () => {
  assert.equal(needsSequentialFade(profile([255, 255, 0, 0]), profile([0, 0, 255, 255])), true);
});

test("opaque backgrounds keep crossfading even at different aspect ratios", () => {
  assert.equal(
    needsSequentialFade(profile([255, 255], true, 1), profile([255, 255], true, 2)),
    false,
  );
});

test("transparent images with different framing fade out first", () => {
  assert.equal(
    needsSequentialFade(profile([0, 255], false, 1), profile([0, 255], false, 0.7)),
    true,
  );
});

test("unreadable masks conservatively avoid overlapping character poses", () => {
  assert.equal(needsSequentialFade(null, profile([0, 255])), true);
});

test("empty transparent masks do not divide by zero", () => {
  assert.equal(needsSequentialFade(profile([0, 0]), profile([0, 0])), false);
});
