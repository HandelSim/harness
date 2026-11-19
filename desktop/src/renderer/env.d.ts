// Asset imports handled by esbuild loaders.
declare module '*.css';
declare module '*.woff2' { const url: string; export default url; }
declare module '*.svg' { const url: string; export default url; }
declare module '*.png' { const url: string; export default url; }
declare const __DEV__: boolean;
