import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "vendor-motion", test: /node_modules[\/](motion|motion-dom|motion-utils|framer-motion)[\/]/, priority: 20 },
            { name: "vendor-react", test: /node_modules[\/](react|react-dom|scheduler)[\/]/, priority: 20 },
            { name: "vendor-ui", test: /node_modules[\/](radix-ui|@radix-ui|lucide-react|class-variance-authority|clsx|tailwind-merge)[\/]/, priority: 10 },
          ],
        },
      },
    },
  },
  server: { port: 5173, strictPort: true },
});
