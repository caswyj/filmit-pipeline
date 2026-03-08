import { expect, test } from "@playwright/test";

const apiBase = process.env.N2V_API_URL ?? "http://127.0.0.1:8000";

test.describe("n2v web smoke", () => {
  test("home page renders core entry points", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByTestId("home-page")).toBeVisible();
    await expect(page.getByTestId("home-demo-section")).toBeVisible();
    await expect(page.getByTestId("home-create-project-section")).toBeVisible();
    await expect(page.getByTestId("home-project-list")).toBeVisible();
    await expect(page.getByTestId("home-import-demo-button")).toBeVisible();
  });

  test("project page renders workflow shell for a newly created project", async ({ page, request }) => {
    const createResponse = await request.post(`${apiBase}/api/v1/projects`, {
      data: {
        name: `playwright-smoke-${Date.now()}`,
        target_duration_sec: 90,
        style_profile: {
          preset_id: "cinematic",
          custom_style: "",
          custom_directives: "",
        },
      },
    });

    expect(createResponse.ok()).toBeTruthy();
    const project = (await createResponse.json()) as { id: string; name: string };

    await page.goto(`/projects/${project.id}`);

    await expect(page.getByTestId("project-page")).toBeVisible();
    await expect(page.getByTestId("project-title")).toHaveText(project.name);
    await expect(page.getByTestId("source-documents-section")).toBeVisible();
    await expect(page.getByTestId("step-selection")).toBeVisible();
    await expect(page.getByTestId("artifact-preview")).toBeVisible();
    await expect(page.getByTestId("workflow-action-panel")).toBeVisible();
    await expect(page.getByTestId("run-current-step-button")).toBeVisible();
    await expect(page.getByTestId("step-button-ingest_parse")).toBeVisible();
  });
});
