/// <reference types="vite/client" />

// This tells TypeScript the shape of `import.meta.env`, so `VITE_API_BASE` is
// typed. Add new VITE_* variables here as you introduce them.
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
