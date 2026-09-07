import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { dashboardBuildInfo } from "./build-info";
const build = dashboardBuildInfo();
export default defineConfig({
  define: { __DASHBOARD_BUILD__: JSON.stringify(build) },
  plugins: [
    react(),
    {
      name: "hortator-build-info",
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "build-info.json",
          source: JSON.stringify(build, null, 2),
        });
      },
    },
  ],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
