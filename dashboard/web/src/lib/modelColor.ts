export interface ModelColorStyle {
  backgroundColor: string;
  color: string;
}

export interface ModelColorAllocator {
  styleFor(model: string | null | undefined): ModelColorStyle | undefined;
}

interface Oklab {
  l: number;
  a: number;
  b: number;
}

const HUE_STEP = 137.50776405003785;
const LIGHTNESS = [0.48, 0.56] as const;
const CHROMA = [0.14, 0.18] as const;
const CANDIDATE_COUNT = 96;
const LIGHT_TEXT = '#f8fafc';
const DARK_TEXT = '#090b10';

export function logicalModelKey(model: string | null | undefined): string {
  if (!model?.trim()) return '';
  return model.trim().toLowerCase()
    .replace(/\[[^\]]+\]$/, '')
    .replace(/-\d{8}$/, '');
}

function fnv1a(value: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function linearToSrgb(value: number): number {
  return value <= 0.0031308
    ? 12.92 * value
    : 1.055 * value ** (1 / 2.4) - 0.055;
}

function srgbToLinear(value: number): number {
  return value <= 0.04045
    ? value / 12.92
    : ((value + 0.055) / 1.055) ** 2.4;
}

function channelHex(value: number): string {
  return Math.round(clamp01(value) * 255).toString(16).padStart(2, '0');
}

function oklchToHex(lightness: number, chroma: number, hue: number): string {
  const radians = hue * Math.PI / 180;
  const a = chroma * Math.cos(radians);
  const b = chroma * Math.sin(radians);

  const lRoot = lightness + 0.3963377774 * a + 0.2158037573 * b;
  const mRoot = lightness - 0.1055613458 * a - 0.0638541728 * b;
  const sRoot = lightness - 0.0894841775 * a - 1.291485548 * b;
  const l = lRoot ** 3;
  const m = mRoot ** 3;
  const s = sRoot ** 3;

  const red = linearToSrgb(
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
  );
  const green = linearToSrgb(
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
  );
  const blue = linearToSrgb(
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  );
  return `#${channelHex(red)}${channelHex(green)}${channelHex(blue)}`;
}

function hexChannels(hex: string): [number, number, number] {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) {
    throw new Error(`Expected a six-digit hex color, got ${hex}`);
  }
  return [1, 3, 5].map(
    (offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255,
  ) as [number, number, number];
}

function hexToOklab(hex: string): Oklab {
  const [red, green, blue] = hexChannels(hex).map(srgbToLinear);
  const l = Math.cbrt(
    0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue,
  );
  const m = Math.cbrt(
    0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue,
  );
  const s = Math.cbrt(
    0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue,
  );
  return {
    l: 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    a: 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    b: 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  };
}

export function oklabDistance(a: string, b: string): number {
  const left = hexToOklab(a);
  const right = hexToOklab(b);
  return Math.hypot(
    left.l - right.l,
    left.a - right.a,
    left.b - right.b,
  );
}

function relativeLuminance(hex: string): number {
  const [red, green, blue] = hexChannels(hex).map(srgbToLinear);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(a: string, b: string): number {
  const left = relativeLuminance(a);
  const right = relativeLuminance(b);
  const lighter = Math.max(left, right);
  const darker = Math.min(left, right);
  return (lighter + 0.05) / (darker + 0.05);
}

function candidateColors(key: string): string[] {
  const preferredHue = fnv1a(key) % 360;
  const candidates: string[] = [];
  const seen = new Set<string>();
  for (let index = 0; index < CANDIDATE_COUNT; index += 1) {
    const hue = (preferredHue + index * HUE_STEP) % 360;
    const lightness = LIGHTNESS[index % LIGHTNESS.length];
    const chroma = CHROMA[Math.floor(index / LIGHTNESS.length) % CHROMA.length];
    const color = oklchToHex(lightness, chroma, hue);
    if (seen.has(color)) continue;
    seen.add(color);
    candidates.push(color);
  }
  return candidates;
}

function foregroundFor(backgroundColor: string): string {
  return contrastRatio(backgroundColor, LIGHT_TEXT)
    >= contrastRatio(backgroundColor, DARK_TEXT)
    ? LIGHT_TEXT
    : DARK_TEXT;
}

export function createModelColorAllocator(): ModelColorAllocator {
  const assignments = new Map<string, ModelColorStyle>();

  return {
    styleFor(model) {
      const key = logicalModelKey(model);
      if (!key) return undefined;
      const existing = assignments.get(key);
      if (existing) return existing;

      const assignedBackgrounds = [...assignments.values()]
        .map((style) => style.backgroundColor);
      const candidates = candidateColors(key);
      let best = candidates[0];
      let bestDistance = -1;
      if (assignedBackgrounds.length > 0) {
        for (const candidate of candidates) {
          const distance = Math.min(
            ...assignedBackgrounds.map(
              (background) => oklabDistance(candidate, background),
            ),
          );
          if (distance > bestDistance) {
            best = candidate;
            bestDistance = distance;
          }
        }
      }

      const style = {
        backgroundColor: best,
        color: foregroundFor(best),
      };
      assignments.set(key, style);
      return style;
    },
  };
}
