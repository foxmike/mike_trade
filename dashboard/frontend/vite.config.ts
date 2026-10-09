import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');

  return {
    base: '/',
    plugins: [react()],
    server: {
      // Development only: forwards API requests to local FastAPI. This proxy
      // is not part of the production build or deployed dashboard server.
      proxy: {
        '/api': {
          target: env.MIKE_TRADE_API_PROXY_TARGET ?? 'http://127.0.0.1:8765',
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      sourcemap: false,
      rollupOptions: {
        output: {
          entryFileNames: 'assets/[name]-[hash].js',
          chunkFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
        },
      },
    },
  };
});
