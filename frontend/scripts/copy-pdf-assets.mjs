import { cp, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const source = dirname(require.resolve("pdfjs-dist/package.json"));
const target = resolve("public/pdfjs");
await mkdir(target, { recursive: true });
for (const name of ["cmaps", "standard_fonts", "wasm", "iccs", "LICENSE"]) {
  await cp(resolve(source, name), resolve(target, name), { recursive: true });
}
await cp(resolve(source, "build/pdf.worker.min.mjs"), resolve(target, "pdf.worker.min.mjs"));
