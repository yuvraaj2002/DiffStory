// Renders launch.html to frames with headless Chrome, then encodes the MP4 with ffmpeg.
//   npm i --no-save puppeteer-core  (once, in this folder)
//   node render.mjs [launch|motion]                 -> diffstory-<name>.mp4
//   node render.mjs [launch|motion] --stills 3,10   -> still-3.png, still-10.png
import puppeteer from "puppeteer-core";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const FPS = 30;
const name = process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : "launch";
const chrome = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const stillsArg = process.argv.indexOf("--stills");

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ["--hide-scrollbars"] });
const page = await browser.newPage();
await page.setViewport({ width: 1080, height: 1080 });
await page.goto("file://" + join(here, `${name}.html`), { waitUntil: "networkidle0" });
await page.evaluate(() => window.ready);
const stage = await page.$("#stage");
const DURATION = (await page.evaluate(() => window.DURATION)) || 30;

if (stillsArg > 0) {
  const outDir = process.argv[stillsArg + 2] || here;
  for (const t of process.argv[stillsArg + 1].split(",").map(Number)) {
    await page.evaluate(t => window.render(t), t);
    await stage.screenshot({ path: join(outDir, `still-${t}.png`) });
  }
} else {
  const frames = mkdtempSync(join(tmpdir(), "diffstory-frames-"));
  const total = FPS * DURATION;
  for (let i = 0; i < total; i++) {
    await page.evaluate(t => window.render(t), i / FPS);
    await stage.screenshot({ path: join(frames, `f${String(i).padStart(4, "0")}.jpg`), type: "jpeg", quality: 95 });
    if (i % 90 === 0) console.log(`frame ${i}/${total}`);
  }
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", join(frames, "f%04d.jpg"),
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "slow", "-movflags", "+faststart",
    join(here, `diffstory-${name}.mp4`)], { stdio: "inherit" });
  rmSync(frames, { recursive: true });
  console.log(`wrote diffstory-${name}.mp4`);
}
await browser.close();
