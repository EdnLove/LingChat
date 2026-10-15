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
      class="image-fade-layer absolute inset-0 h-full w-full will-change-[opacity] backface-hidden"
      :style="{
        opacity: layer.opacity,
        zIndex: index === activeLayer ? 10 : 20,
      }"
    >
      <img
        v-if="layer.url"
        :ref="
          (el) => {
            layerImages[index] = el as HTMLImageElement | null;
          }
        "
        :crossorigin="layer.crossOrigin ? 'anonymous' : undefined"
        :src="layer.url"
        class="pointer-events-none absolute"
        :style="imageStyle"
        alt=""
        draggable="false"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, nextTick, onBeforeUnmount } from "vue";
import { imageProfile, needsInstantSwap, type ImageProfile } from "@/utils/image-transition";

const props = withDefaults(
  defineProps<{ src: string; duration?: number; objectFit?: string; position?: string }>(),
  { duration: 300, objectFit: "contain", position: "center bottom" },
);

const layers = ref([
  { url: "", opacity: 0, crossOrigin: true },
  { url: "", opacity: 0, crossOrigin: true },
]);
const layerElements: (HTMLElement | null)[] = [];
const layerImages: (HTMLImageElement | null)[] = [];
const activeLayer = ref(0);
const masks: (ImageProfile | null)[] = [null, null];
let animation: Animation | null = null;
let generation = 0;
let currentImageLoadPromise = Promise.resolve();
let resolveLoad: (() => void) | undefined;

const imageStyle = computed(() => {
  const height = /^auto (\d+(?:\.\d+)?%)$/.exec(props.objectFit)?.[1];
  if (height) {
    // Preserve the height-based framing used for narrow avatar viewports.
    const [x = "center", y = "center"] = props.position.split(/\s+/);
    const anchors: Record<string, string> = {
      left: "0%",
      right: "100%",
      top: "0%",
      bottom: "100%",
      center: "50%",
    };
    const left = anchors[x] ?? x;
    const top = anchors[y] ?? y;
    return {
      width: "auto",
      height,
      maxWidth: "none",
      maxHeight: "none",
      left,
      top,
      transform: `translate(calc(-1 * ${left}), calc(-1 * ${top}))`,
    };
  }
  return {
    width: "100%",
    height: "100%",
    maxWidth: "none",
    maxHeight: "none",
    objectFit: props.objectFit as "contain" | "cover",
    objectPosition: props.position,
  };
});

function settleLayers() {
  const old = activeLayer.value;
  const next = 1 - old;
  const opacity = (index: number) => {
    const element = layerElements[index];
    return element ? Number(getComputedStyle(element).opacity) : layers.value[index].opacity;
  };
  const nextOpacity = opacity(next);
  if (layers.value[next].url && nextOpacity >= 0.5) {
    activeLayer.value = next;
  }
  animation?.cancel();
  animation = null;
  for (let i = 0; i < layers.value.length; i++) {
    if (i === activeLayer.value && layers.value[i].url) layers.value[i].opacity = 1;
    else {
      layers.value[i] = { url: "", opacity: 0, crossOrigin: true };
      masks[i] = null;
    }
  }
}

async function decodeLayer(index: number, run: number): Promise<HTMLImageElement | null> {
  await nextTick();
  if (run !== generation) return null;
  const image = layerImages[index];
  if (!image) return null;
  try {
    await image.decode();
  } catch (error) {
    if (run !== generation) return null;
    if (!image.complete || !image.naturalWidth) {
      if (!layers.value[index].crossOrigin) throw error;
      // Retry the same display element for external images without CORS headers.
      layers.value[index].crossOrigin = false;
      return decodeLayer(index, run);
    }
  }
  return run === generation ? image : null;
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
      layers.value[i] = { url: "", opacity: 0, crossOrigin: true };
      masks[i] = null;
    }
    return;
  }
  if (layers.value[activeLayer.value].url === url) return;
  const old = activeLayer.value;
  const next = 1 - old;
  // Decode the actual hidden display element. Updating the visible element's URL
  // can drop its old texture before the new one is ready, even after preloading.
  layers.value[next] = { url, opacity: 0, crossOrigin: true };
  const image = await decodeLayer(next, run);
  if (!image || run !== generation) return;
  const profile = imageProfile(url, image);
  masks[next] = profile;
  const hasOld = !!layers.value[old].url;
  const instant = hasOld && needsInstantSwap(masks[old], profile);
  const duration = matchMedia("(prefers-reduced-motion: reduce)").matches
    ? 0
    : Math.max(0, props.duration);
  if (duration === 0 || instant) {
    // Swap visibility in one DOM update; never replace a visible image's source.
    layers.value[next].opacity = 1;
    layers.value[old].opacity = 0;
    activeLayer.value = next;
    await nextTick();
    return;
  }
  await fade(next, 1, duration);
  if (run !== generation) return;
  activeLayer.value = next;
  layers.value[old] = { url: "", opacity: 0, crossOrigin: true };
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
