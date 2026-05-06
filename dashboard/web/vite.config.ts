import { defineConfig } from 'vite';
import type { Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { promises as fs } from 'node:fs';
import path from 'node:path';

// Rolldown (Vite 8's bundler) rejects direct mutation of the `bundle`
// map, so we emit the renamed copy via this.emitFile and remove the
// leftover index.html from outDir in closeBundle.
const renameHtml = (): Plugin => {
  let outDir = '';
  return {
    name: 'rename-html',
    enforce: 'post',
    configResolved(config) {
      outDir = config.build.outDir;
    },
    generateBundle(_opts, bundle) {
      const indexAsset = bundle['index.html'];
      if (indexAsset && indexAsset.type === 'asset') {
        this.emitFile({
          type: 'asset',
          fileName: 'dashboard.html',
          source: indexAsset.source,
        });
      }
    },
    async closeBundle() {
      const indexPath = path.resolve(outDir, 'index.html');
      try {
        await fs.rm(indexPath);
      } catch (err: unknown) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
      }
    },
  };
};

// https://vitejs.dev/config/
export default defineConfig({
  // Python's DashboardHTTPHandler only serves `/` (→ dashboard.html) and
  // `/static/*` (→ STATIC_DIR). Absolute asset URLs emitted by Vite must
  // therefore live under `/static/` or they 404 at runtime. Setting `base`
  // prefixes every asset href/src in the built HTML with `/static/`, which
  // matches the Python handler's static serve path exactly. The Vite dev
  // server (port 5173) proxies `/static/*` to Python via the `server.proxy`
  // config below, so this base prefix is transparent in dev too.
  base: '/static/',
  plugins: [react(), renameHtml()],
  publicDir: 'public',
  build: {
    outDir: '../static',
    emptyOutDir: true,
    rollupOptions: {
      input: 'index.html',
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8789',
        // Vite's `changeOrigin: true` rewrites the upstream `Host` header to
        // match the target (127.0.0.1:8789). It does NOT touch the `Origin`
        // header (per http-proxy semantics — Origin rewrite is WebSocket-only;
        // see vite/dist/node/chunks/node.js where `rewriteOriginHeader` is
        // gated on the WS path). Python's Origin/Host parity CSRF check
        // requires BOTH headers to match, so we also rewrite `Origin` here
        // via the `configure` callback. Without one of these, POST /api/sync
        // would 403 in dev mode (browser sends Origin: http://localhost:5173,
        // Host gets rewritten to 127.0.0.1:8789 by changeOrigin, mismatch).
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            if (req.headers.origin) {
              proxyReq.setHeader('Origin', 'http://127.0.0.1:8789');
            }
          });
        },
      },
      '/static': {
        target: 'http://127.0.0.1:8789',
        changeOrigin: false,
        // With base: '/static/', Vite's dev HTML references /static/@vite/client,
        // /static/@react-refresh, and /static/src/main.tsx. Vite's proxy
        // middleware runs BEFORE its internal transform/public-dir middleware,
        // so without a bypass these URLs would be forwarded to Python (which
        // 404s them) and `npm run dev` would boot a blank page. `bypass`
        // returning req.url skips the proxy so Vite's own middleware wins.
        // The bare `/static/` and `/static/index.html` paths must also bypass:
        // those are the dev-server entry HTML (with HMR injected) that Vite
        // serves at the configured base — without bypassing them, navigating
        // to localhost:5173/ would 302 → /static/ and then 404 from Python.
        bypass: (req) => {
          const url = req.url ?? '';
          if (
            url === '/static/' ||
            url === '/static/index.html' ||
            url.startsWith('/static/@') ||
            url.startsWith('/static/src/') ||
            url.startsWith('/static/node_modules/')
          ) {
            return url;
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./__tests__/setup.ts'],
  },
});
