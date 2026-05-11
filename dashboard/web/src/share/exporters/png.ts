// Spec §11.1 — client-side rasterization of an SVG body into a PNG blob.
// No server endpoint; runs entirely in the browser canvas.
//
// Scale defaults to 2 for retina sharpness. Background fill is required:
// the SVG body draws onto a transparent canvas, which some PDF/viewers
// render as solid black — we paint the theme's bg first so the PNG looks
// like the on-screen preview.
//
// Fallback path: some browsers return null from canvas.toBlob (notably
// older Safari). The toDataURL + fetch sequence produces an equivalent
// PNG blob. The blob URL is revoked in `finally` so a thrown error
// doesn't leak the URL.

export async function svgToPng(
  svgString: string,
  scale: number,
  backgroundColor: string,
): Promise<Blob> {
  const blob = new Blob([svgString], { type: 'image/svg+xml' });
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth * scale;
    canvas.height = img.naturalHeight * scale;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Could not acquire 2D canvas context');
    ctx.fillStyle = backgroundColor;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    const out = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob((b) => resolve(b), 'image/png');
    });
    if (out) return out;
    // Fallback path — some browsers' toBlob returns null.
    const dataUrl = canvas.toDataURL('image/png');
    const resp = await fetch(dataUrl);
    return resp.blob();
  } finally {
    URL.revokeObjectURL(url);
  }
}
