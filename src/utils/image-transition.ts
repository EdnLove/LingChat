const SAMPLE_SIZE = 64;
const CACHE_LIMIT = 32;

export interface ImageProfile {
  aspectRatio: number;
  alpha: Uint8Array;
  opaque: boolean;
}

const profiles = new Map<string, ImageProfile>();

/** Cache small alpha masks, not decoded full-size character images. */
export function imageProfile(url: string, image: HTMLImageElement): ImageProfile | null {
  const cached = profiles.get(url);
  if (cached) {
    profiles.delete(url);
    profiles.set(url, cached);
    return cached;
  }
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = SAMPLE_SIZE;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(image, 0, 0, SAMPLE_SIZE, SAMPLE_SIZE);
    const pixels = context.getImageData(0, 0, SAMPLE_SIZE, SAMPLE_SIZE).data;
    const alpha = new Uint8Array(SAMPLE_SIZE * SAMPLE_SIZE);
    let transparent = 0;
    for (let i = 0; i < alpha.length; i++) {
      alpha[i] = pixels[i * 4 + 3];
      if (alpha[i] < 240) transparent++;
    }
    const profile = {
      aspectRatio: image.naturalWidth / image.naturalHeight,
      alpha,
      opaque: transparent / alpha.length < 0.01,
    };
    profiles.set(url, profile);
    if (profiles.size > CACHE_LIMIT) profiles.delete(profiles.keys().next().value!);
    return profile;
  } catch {
    // Cross-origin images without readable pixels still render via an out/in fade.
    return null;
  }
}

export function needsSequentialFade(
  before: ImageProfile | null,
  after: ImageProfile | null,
): boolean {
  if (!before || !after) return true;
  if (before.opaque && after.opaque) return false;
  if (Math.abs(before.aspectRatio / after.aspectRatio - 1) > 0.02) return true;
  let intersection = 0;
  let union = 0;
  for (let i = 0; i < before.alpha.length; i++) {
    intersection += Math.min(before.alpha[i], after.alpha[i]);
    union += Math.max(before.alpha[i], after.alpha[i]);
  }
  return union > 0 && intersection / union < 0.94;
}
