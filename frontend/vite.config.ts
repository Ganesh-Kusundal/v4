import { defineConfig } from 'vite';

// base './' so dist/ loads under the FastAPI mount at /ui/
export default defineConfig({ base: './' });
