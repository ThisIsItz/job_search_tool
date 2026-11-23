import { NextResponse } from "next/server";
import { spawn } from "node:child_process";
import path from "node:path";

// Runs python -m app.main (fetch -> filter -> free Gemini AI filter) and
// blocks until it finishes. No paid Claude evaluation here, so this never
// costs money. It takes a few minutes (the AI filter reads every new job),
// which is why the client shows a loading state rather than treating this
// like a normal fast API call.
const REPO_ROOT = path.join(process.cwd(), "..");
const PYTHON = path.join(REPO_ROOT, ".venv", "bin", "python");

export async function POST() {
  return new Promise<NextResponse>((resolve) => {
    const proc = spawn(PYTHON, ["-m", "app.main"], { cwd: REPO_ROOT });
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
      const totalMatch = stdout.match(/Total new candidates after filters\+dedup: (\d+)/);
      resolve(NextResponse.json({
        ok: true,
        newCandidates: totalMatch ? parseInt(totalMatch[1], 10) : null,
        log: stdout,
      }));
    });
  });
}
