<template>
  <div class="relative h-full w-full">
    <slot></slot>
    <slot name="overlay"></slot>
    <div
      v-for="(layer, index) in layers"
      :key="index"
      :ref="
        (el) => {
          layerElements[index] = el as HTMLElement | null;
        }
      "
      class="image-fade-layer absolute inset-0 h-full w-full bg-no-repeat will-change-[opacity] backface-hidden"
      :style="{
        backgroundImage: layer.url ? `url(${JSON.stringify(layer.url)})` : 'none',
        backgroundSize: objectFit,
        backgroundPosition: position,
        opacity: layer.opacity,
        zIndex: index === activeLayer ? 10 : 20,
      }"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onBeforeUnmount } from "vue";
import { imageProfile, needsSequentialFade, type ImageProfile } from "@/utils/image-transition";

const props = withDefaults(
  defineProps<{ src: string; duration?: number; objectFit?: string; position?: string }>(),
  { duration: 300, objectFit: "contain", position: "center bottom" },
);

const layers = ref([
  { url: "", opacity: 0 },
  { url: "", opacity: 0 },
]);
const layerElements: (HTMLElement | null)[] = [];
const activeLayer = ref(0);
const masks: (ImageProfile | null)[] = [null, null];
let animation: Animation | null = null;
let generation = 0;
let currentImageLoadPromise = Promise.resolve();
let resolveLoad: (() => void) | undefined;

function settleLayers() {
  const old = activeLayer.value;
  const next = 1 - old;
  const opacity = (index: number) => {
    const element = layerElements[index];
    return element ? Number(getComputedStyle(element).opacity) : layers.value[index].opacity;
  };
  const nextOpacity = opacity(next);
  if (layers.value[next].url && (nextOpacity >= 0.5 || opacity(old) === 0)) {
    activeLayer.value = next;
  }
  animation?.cancel();
  animation = null;
  for (let i = 0; i < layers.value.length; i++) {
    if (i === activeLayer.value && layers.value[i].url) layers.value[i].opacity = 1;
    else {
      layers.value[i] = { url: "", opacity: 0 };
      masks[i] = null;
    }
  }
}

async function loadImage(url: string): Promise<HTMLImageElement> {
  const load = (crossOrigin: boolean) =>
    new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image();
      if (crossOrigin) image.crossOrigin = "anonymous";
      image.onload = () => resolve(image);
      image.onerror = reject;
      image.src = url;
    });
  // Readable alpha masks for asset URLs; retain support for external non-CORS images.
  const image = await load(true).catch(() => load(false));
  await image.decode().catch(() => {});
  return image;
}

async function fade(index: number, to: number, duration: number) {
  const layer = layers.value[index];
  const from = layer.opacity;
  layer.opacity = to;
  const element = layerElements[index];
  if (!element || duration <= 0) {
    await nextTick();
    return;
  }
  const effect = element.animate([{ opacity: from }, { opacity: to }], {
    duration,
    easing: "ease-in-out",
  });
  animation = effect;
  try {
    await effect.finished;
  } finally {
    effect.cancel();
    if (animation === effect) animation = null;
  }
}

async function updateImage(url: string, run: number) {
  if (!url || url.toLowerCase() === "none") {
    for (let i = 0; i < layers.value.length; i++) {
      layers.value[i] = { url: "", opacity: 0 };
      masks[i] = null;
    }
    return;
  }
  if (layers.value[activeLayer.value].url === url) return;
  const image = await loadImage(url);
  if (run !== generation) return;
  const profile = imageProfile(url, image);
  const old = activeLayer.value;
  const next = 1 - old;
  const hasOld = !!layers.value[old].url;
  const sequential = hasOld && needsSequentialFade(masks[old], profile);
  const duration = matchMedia("(prefers-reduced-motion: reduce)").matches
    ? 0
    : Math.max(0, props.duration);
  if (duration === 0) {
    layers.value[old] = { url, opacity: 1 };
    masks[old] = profile;
    layers.value[next] = { url: "", opacity: 0 };
    masks[next] = null;
    await nextTick();
    return;
  }
  const phaseDuration = sequential ? duration / 2 : duration;
  if (sequential) {
    await fade(old, 0, phaseDuration);
    if (run !== generation) return;
  }
  layers.value[next] = { url, opacity: 0 };
  masks[next] = profile;
  await nextTick();
  if (run !== generation) return;
  await fade(next, 1, phaseDuration);
  if (run !== generation) return;
  activeLayer.value = next;
  layers.value[old] = { url: "", opacity: 0 };
  masks[old] = null;
}

watch(
  () => props.src,
  (url) => {
    const run = ++generation;
    settleLayers();
    resolveLoad?.();
    currentImageLoadPromise = new Promise<void>((resolve) => {
      resolveLoad = resolve;
    });
    const done = resolveLoad;
    void updateImage(url, run)
      .catch((error) => {
        if (run === generation) console.warn(`加载图片失败: ${url}`, error);
      })
      .finally(done);
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  generation++;
  animation?.cancel();
  resolveLoad?.();
});

defineExpose({ waitForLoad: () => currentImageLoadPromise });
</script>
