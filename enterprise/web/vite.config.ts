import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, '');
  const apiMode = env.VITE_API_MODE || 'mock';
  const gatewayTarget =
    apiMode === 'demo' || apiMode === 'gateway'
      ? 'http://127.0.0.1:5188'
      : 'http://127.0.0.1:9380';

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 9223,
      strictPort: false,
      proxy: {
        '/enterprise/api': {
          target: gatewayTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
    },
  };
});
