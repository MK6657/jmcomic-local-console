import { defineConfig } from 'vite';
import {readFileSync} from 'node:fs';

export default defineConfig({
  plugins: [{name: 'bundle-vue-licenses', generateBundle() {
    for (const pkg of ['vue', '@vue/runtime-core','@vue/runtime-dom','@vue/reactivity','@vue/shared']) {
      this.emitFile({type:'asset',fileName:`LICENSE-${pkg.replace('@','').replace('/','-')}.txt`,source:readFileSync(new URL(`./node_modules/${pkg}/LICENSE`,import.meta.url),'utf8')});
    }
  }}],
  resolve: { alias: { vue: 'vue/dist/vue.runtime.esm-bundler.js' } },
  define: { __VUE_OPTIONS_API__: false, __VUE_PROD_DEVTOOLS__: false, __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: false, 'process.env.NODE_ENV': '"production"' },
  build: {
    outDir: './dist', emptyOutDir: false,
    lib: { entry: 'src/transfer.ts', name: 'AtelierTransfer', formats: ['iife'], fileName: () => 'transfer.js', cssFileName: 'transfer' },
    target: 'es2020',
  },
});
