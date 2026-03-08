#!/usr/bin/env node

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const STEP_SEQUENCE = [
  { name: "ingest_parse", label: "导入全文", scope: "single" },
  { name: "chapter_chunking", label: "切分章节", scope: "single" },
  { name: "story_scripting", label: "章节剧本", scope: "chapter" },
  { name: "shot_detailing", label: "分镜细化", scope: "chapter" },
  { name: "storyboard_image", label: "分镜出图", scope: "chapter" },
  { name: "consistency_check", label: "分镜校核", scope: "chapter" },
  { name: "segment_video", label: "视频片段", scope: "chapter" },
  { name: "stitch_subtitle_tts", label: "成片输出", scope: "single" },
];

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const webRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(webRoot, "..", "..");

function printHelp() {
  console.log(`Usage: npm run playwright:demo-workflow -- [options]

Options:
  --base-url <url>           Web app URL. Default: http://127.0.0.1:3000
  --stop-after-step <name>   Final step to approve. Default: chapter_chunking
  --headed                   Run with a visible browser window
  --timeout-ms <ms>          Per-action timeout. Default: 180000
  --slow-mo-ms <ms>          Slow down browser actions. Default: 0
  --output-dir <dir>         Artifact directory. Default: output/playwright/demo-workflow
  --no-batch-chapter-steps   Use single-chapter actions for chapter-scoped steps
  --help                     Show this message

Example:
  npm run playwright:demo-workflow -- --headed --stop-after-step story_scripting
`);
}

function parseNumber(value, flag) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    throw new Error(`Invalid value for ${flag}: ${value}`);
  }
  return parsed;
}

function parseArgs(argv) {
  const options = {
    baseUrl: process.env.N2V_WEB_URL ?? "http://127.0.0.1:3000",
    stopAfterStep: process.env.N2V_STOP_AFTER_STEP ?? "chapter_chunking",
    headed: false,
    timeoutMs: parseNumber(process.env.N2V_PLAYWRIGHT_TIMEOUT_MS ?? "180000", "N2V_PLAYWRIGHT_TIMEOUT_MS"),
    slowMoMs: parseNumber(process.env.N2V_PLAYWRIGHT_SLOW_MO_MS ?? "0", "N2V_PLAYWRIGHT_SLOW_MO_MS"),
    outputDir: path.resolve(
      process.env.N2V_PLAYWRIGHT_OUTPUT_DIR ?? path.join(repoRoot, "output", "playwright", "demo-workflow")
    ),
    batchChapterSteps: process.env.N2V_BATCH_CHAPTER_STEPS !== "false",
  };

  for (let index = 0; index < argv.length; index += 1) {
    const raw = argv[index];
    const eq = raw.indexOf("=");
    const flag = eq >= 0 ? raw.slice(0, eq) : raw;
    const inlineValue = eq >= 0 ? raw.slice(eq + 1) : null;
    const nextValue = () => {
      if (inlineValue !== null) return inlineValue;
      index += 1;
      if (index >= argv.length) {
        throw new Error(`Missing value for ${flag}`);
      }
      return argv[index];
    };

    switch (flag) {
      case "--base-url":
        options.baseUrl = nextValue();
        break;
      case "--stop-after-step":
        options.stopAfterStep = nextValue();
        break;
      case "--timeout-ms":
        options.timeoutMs = parseNumber(nextValue(), flag);
        break;
      case "--slow-mo-ms":
        options.slowMoMs = parseNumber(nextValue(), flag);
        break;
      case "--output-dir":
        options.outputDir = path.resolve(nextValue());
        break;
      case "--headed":
        options.headed = true;
        break;
      case "--headless":
        options.headed = false;
        break;
      case "--no-batch-chapter-steps":
        options.batchChapterSteps = false;
        break;
      case "--batch-chapter-steps":
        options.batchChapterSteps = inlineValue === null ? true : inlineValue !== "false";
        break;
      case "--help":
      case "-h":
        printHelp();
        process.exit(0);
        break;
      default:
        throw new Error(`Unknown argument: ${raw}`);
    }
  }

  const stepNames = new Set(STEP_SEQUENCE.map((step) => step.name));
  if (!stepNames.has(options.stopAfterStep)) {
    throw new Error(
      `Unsupported --stop-after-step value: ${options.stopAfterStep}. Expected one of: ${Array.from(stepNames).join(", ")}`
    );
  }

  return options;
}

function sanitizeFilePart(value) {
  return value.replace(/[^a-z0-9_-]+/gi, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "artifact";
}

function log(message) {
  const stamp = new Date().toISOString();
  console.log(`[${stamp}] ${message}`);
}

async function ensureVisibleAndEnabled(page, locator, label, timeoutMs) {
  await locator.waitFor({ state: "visible", timeout: timeoutMs });
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const disabled = await locator.isDisabled().catch(() => true);
    if (!disabled) return;
    await page.waitForTimeout(250);
  }
  throw new Error(`${label} stayed disabled for more than ${timeoutMs}ms`);
}

async function waitForStepStatus(page, stepName, expectedStatuses, timeoutMs) {
  const stepButton = page.getByTestId(`step-button-${stepName}`);
  await stepButton.waitFor({ state: "visible", timeout: timeoutMs });

  const wanted = new Set(expectedStatuses);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = (await stepButton.getAttribute("data-step-status")) ?? "";
    if (wanted.has(status)) {
      return status;
    }
    await page.waitForTimeout(500);
  }

  const currentStatus = (await stepButton.getAttribute("data-step-status")) ?? "unknown";
  throw new Error(`Step ${stepName} did not reach one of [${expectedStatuses.join(", ")}]; current status: ${currentStatus}`);
}

async function getVisibleText(locator) {
  try {
    if (!(await locator.isVisible())) return null;
    return (await locator.innerText()).trim();
  } catch {
    return null;
  }
}

async function throwIfUiError(page) {
  const errorText = await getVisibleText(page.getByTestId("workflow-error-message"));
  if (errorText) {
    throw new Error(`Workflow UI reported an error: ${errorText}`);
  }
}

async function waitForWorkflowIdle(page, timeoutMs) {
  const pending = page.getByTestId("workflow-pending-action");
  const runButton = page.getByTestId("run-current-step-button");
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    await throwIfUiError(page);
    const pendingVisible = await pending.isVisible().catch(() => false);
    const runVisible = await runButton.isVisible().catch(() => false);
    const runDisabled = runVisible ? await runButton.isDisabled().catch(() => true) : true;

    if (!pendingVisible && !runDisabled) {
      await page.waitForLoadState("networkidle", { timeout: 2_000 }).catch(() => {});
      await throwIfUiError(page);
      return;
    }
    await page.waitForTimeout(500);
  }

  throw new Error(`Workflow did not settle within ${timeoutMs}ms`);
}

async function clickAndWaitForPost(page, locator, label, pathFragment, timeoutMs) {
  await ensureVisibleAndEnabled(page, locator, label, timeoutMs);
  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST" && response.url().includes(pathFragment),
    { timeout: timeoutMs }
  );

  await locator.click();
  const response = await responsePromise;
  if (!response.ok()) {
    throw new Error(`${label} failed with HTTP ${response.status()} for ${response.url()}`);
  }
  await waitForWorkflowIdle(page, timeoutMs);
  return response;
}

async function waitForProjectPage(page, timeoutMs) {
  await page.waitForURL(/\/projects\/[^/]+$/, { timeout: timeoutMs });
  await page.getByTestId("project-page").waitFor({ state: "visible", timeout: timeoutMs });
  await page.getByTestId("project-title").waitFor({ state: "visible", timeout: timeoutMs });
  await page.getByTestId("step-button-ingest_parse").waitFor({ state: "visible", timeout: timeoutMs });
  await page.getByTestId("source-documents-section").waitFor({ state: "visible", timeout: timeoutMs });
}

async function writeSummary(outputDir, summary) {
  await writeFile(path.join(outputDir, "run-summary.json"), `${JSON.stringify(summary, null, 2)}\n`, "utf8");
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const stopIndex = STEP_SEQUENCE.findIndex((step) => step.name === options.stopAfterStep);
  const lastSingleStepIndex = STEP_SEQUENCE.findIndex((step) => step.name === "chapter_chunking");
  if (!options.batchChapterSteps && stopIndex > lastSingleStepIndex) {
    throw new Error("--no-batch-chapter-steps is only supported when --stop-after-step is chapter_chunking or earlier");
  }
  const stepsToRun = STEP_SEQUENCE.slice(0, stopIndex + 1);
  const screenshots = [];
  let captureIndex = 0;

  await mkdir(options.outputDir, { recursive: true });

  const browser = await chromium.launch({
    headless: !options.headed,
    slowMo: options.slowMoMs,
  });

  const context = await browser.newContext({
    viewport: { width: 1440, height: 1200 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(options.timeoutMs);
  page.setDefaultNavigationTimeout(options.timeoutMs);

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      log(`browser console error: ${msg.text()}`);
    }
  });

  async function capture(label) {
    captureIndex += 1;
    const fileName = `${String(captureIndex).padStart(2, "0")}-${sanitizeFilePart(label)}.png`;
    const fullPath = path.join(options.outputDir, fileName);
    await page.screenshot({ path: fullPath, fullPage: true });
    screenshots.push(fullPath);
    return fullPath;
  }

  const summary = {
    baseUrl: options.baseUrl,
    startedAt: new Date().toISOString(),
    stopAfterStep: options.stopAfterStep,
    batchChapterSteps: options.batchChapterSteps,
    screenshots,
    projectUrl: null,
    importedProjectId: null,
    completedSteps: [],
  };

  try {
    log(`Opening ${options.baseUrl}`);
    await page.goto(options.baseUrl, { waitUntil: "domcontentloaded" });
    await page.getByTestId("home-page").waitFor({ state: "visible", timeout: options.timeoutMs });

    const importButton = page.getByTestId("home-import-demo-button");
    await ensureVisibleAndEnabled(page, importButton, "home import demo button", options.timeoutMs);
    await capture("home-before-import");

    log("Importing the 1408 demo project");
    const importResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" && response.url().includes("/api/v1/demo-cases/1408/import"),
      { timeout: options.timeoutMs }
    );
    await importButton.click();
    const importResponse = await importResponsePromise;
    if (!importResponse.ok()) {
      throw new Error(`import demo project failed with HTTP ${importResponse.status()} for ${importResponse.url()}`);
    }
    const importedProject = await importResponse.json();

    summary.importedProjectId = importedProject.id ?? null;
    await waitForProjectPage(page, options.timeoutMs);
    summary.projectUrl = page.url();

    await throwIfUiError(page);
    await page.getByText("1408.txt", { exact: true }).waitFor({ state: "visible", timeout: options.timeoutMs });
    await capture("project-after-import");

    for (const step of stepsToRun) {
      log(`Running step ${step.name}`);
      const stepButton = page.getByTestId(`step-button-${step.name}`);
      await ensureVisibleAndEnabled(page, stepButton, `step button ${step.name}`, options.timeoutMs);
      await stepButton.click();
      await waitForWorkflowIdle(page, options.timeoutMs);

      const runAll = step.scope === "chapter" && options.batchChapterSteps;
      const runButton = runAll
        ? page.getByTestId("run-current-step-all-chapters-button")
        : page.getByTestId("run-current-step-button");
      const runPath = runAll ? `/steps/${step.name}/run-all-chapters` : `/steps/${step.name}/run`;

      await clickAndWaitForPost(page, runButton, `run ${step.name}`, runPath, options.timeoutMs);
      await waitForStepStatus(page, step.name, ["REVIEW_REQUIRED", "REWORK_REQUESTED", "APPROVED"], options.timeoutMs);
      await capture(`${step.name}-after-run`);

      const approveButton = runAll
        ? page.getByTestId("approve-current-step-all-chapters-button")
        : page.getByTestId("approve-current-step-button");
      const approvePath = runAll ? "/approve-all-chapters" : "/approve";

      await clickAndWaitForPost(page, approveButton, `approve ${step.name}`, approvePath, options.timeoutMs);
      await waitForStepStatus(page, step.name, ["APPROVED"], options.timeoutMs);

      if (step.name === "chapter_chunking") {
        await page.getByTestId("chapter-list").waitFor({ state: "visible", timeout: options.timeoutMs });
        await page.waitForFunction(
          () => document.querySelectorAll('[data-testid^="chapter-button-"]').length > 0,
          undefined,
          { timeout: options.timeoutMs }
        );
      }

      summary.completedSteps.push(step.name);
      await capture(`${step.name}-after-approve`);
    }

    summary.finishedAt = new Date().toISOString();
    await writeSummary(options.outputDir, summary);
    log(`Workflow completed through ${options.stopAfterStep}`);
    log(`Artifacts written to ${options.outputDir}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    summary.finishedAt = new Date().toISOString();
    summary.error = message;
    try {
      await capture("failure");
    } catch {
      // Ignore screenshot failures during teardown.
    }
    await writeSummary(options.outputDir, summary);
    throw error;
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.stack ?? error.message : String(error);
  console.error(message);
  process.exitCode = 1;
});
