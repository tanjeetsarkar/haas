import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { approverPort, atlasDevApiPlugins } from './dev/apiProxy';

export default defineConfig(({ command, isPreview, mode }) => ({
  plugins: [
    react(),
    ...atlasDevApiPlugins(command, isPreview, fileURLToPath(new URL('../../../', import.meta.url)), undefined, mode),
  ],
  server: {
    host: '127.0.0.1',
    port: mode === 'approver' ? approverPort : 3000,
    strictPort: true,
    allowedHosts: ['127.0.0.1', 'localhost'],
    cors: false,
  },
  preview: {
    host: '127.0.0.1',
    strictPort: true,
    allowedHosts: ['127.0.0.1', 'localhost'],
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
}));
