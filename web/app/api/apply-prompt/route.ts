import { NextResponse } from "next/server";
import { spawn } from "node:child_process";
import path from "node:path";

// This endpoint doesn't touch SQLite directly — it shells out to the
// Python pipeline's app.application_prompt (profile.yaml loading, the
// grounding/security prompt text, and the job_details lookup by URL all
// already live there; duplicating that in TypeScript would just invite the
// two copies to drift).
const REPO_ROOT = path.join(process.cwd(), "..");
const PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (!body || typeof body.input !== "string" || !body.input.trim()) {
    return NextResponse.json({ error: "Missing 'input' in request body" }, { status: 400 });
  }

  const payload = JSON.stringify({
    input: body.input,
    want_cover_letter: body.wantCoverLetter ?? true,
    questions: Array.isArray(body.questions) ? body.questions : [],
  });

  return new Promise<NextResponse>((resolve) => {
    const proc = spawn(PYTHON, ["-m", "app.application_prompt"], { cwd: REPO_ROOT });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", (err) => {
      resolve(NextResponse.json({ error: `Failed to start Python: ${err.message}` }, { status: 500 }));
    });
    proc.on("close", (code) => {
      if (code !== 0) {
        resolve(NextResponse.json({ error: stderr || `Python exited with code ${code}` }, { status: 500 }));
        return;
      }
      try {
        resolve(NextResponse.json(JSON.parse(stdout)));
      } catch {
        resolve(NextResponse.json({ error: `Bad output from generator: ${stdout}` }, { status: 500 }));
      }
    });
    proc.stdin.write(payload);
    proc.stdin.end();
  });
}
