import { defineConfig } from "astro/config";
import node from "@astrojs/node";
import tailwindcss from "@tailwindcss/vite";

// AcademicTok frontend. Server-rendered (Node adapter) so Reddit-style paths
// like /r/<field>/<post-id> resolve on demand without enumerating every paper
// at build time. Pages are thin shells; data is fetched client-side from the
// Flask API on :8080.
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
  server: { port: 4321 },
  vite: { plugins: [tailwindcss()] },
});
