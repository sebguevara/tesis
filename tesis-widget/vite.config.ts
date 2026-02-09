import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { viteSingleFile } from "vite-plugin-singlefile";
import { resolve } from "node:path";

export default defineConfig(({ mode }) => {
  const isEmbed = mode === "embed";
  const outDir = resolve(__dirname, "dist");

  if (isEmbed) {
    return {
      plugins: [svelte({ emitCss: false })],
      build: {
        outDir,
        emptyOutDir: true,
        target: "esnext",
        cssCodeSplit: false,
        lib: {
          entry: "src/main.ts",
          name: "ConversationalWidgetBundle",
          formats: ["iife"],
          fileName: () => "widget.js",
        },
      },
    };
  }

  return {
    plugins: [svelte(), viteSingleFile()],
    viteSingleFile: {
      inlinePattern: ["**/*.css"],
      removeViteModuleLoader: true,
    },
    build: {
      outDir,
      emptyOutDir: true,
      target: "esnext",
      assetsInlineLimit: 100000000,
      cssCodeSplit: false,
      rollupOptions: {
        output: {
          codeSplitting: false,
          manualChunks: undefined,
        },
      },
    },
  };
});
