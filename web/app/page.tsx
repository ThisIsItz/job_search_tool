"use client";

import {useEffect, useMemo, useState} from "react";
import {Job, SaveState, SortKey} from "@/app/types";
import Toolbar from "@/app/components/Toolbar";
import JobsTable from "@/app/components/JobsTable";
import JobDetailPanel from "@/app/components/JobDetailPanel";
import {annualSalaryInEur} from "@/lib/salary";

const NUMERIC_SORT_KEYS: SortKey[] = ["match_score", "stack_fit", "salary"];

function sortValue(job: Job, key: SortKey): unknown {
  return key === "salary" ? annualSalaryInEur(job) : job[key];
}

export default function Page() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [aiStatusFilter, setAiStatusFilter] = useState("all");
  const [myStatusFilter, setMyStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [minSalaryEur, setMinSalaryEur] = useState("");

  const [sortKey, setSortKey] = useState<SortKey>("stack_fit");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const [selectedUrl, setSelectedUrl] = useState<string | null>(null);
  const [draftStatus, setDraftStatus] = useState("");
  const [draftNotes, setDraftNotes] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");

  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);

  async function fetchNewJobs() {
    setFetching(true);
    setFetchError(null);
    try {
      const res = await fetch("/api/fetch", {method: "POST"});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      await load();
    } catch (e: any) {
      setFetchError(e.message);
    } finally {
      setFetching(false);
    }
  }

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/jobs");
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setJobs(await res.json());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(NUMERIC_SORT_KEYS.includes(key) || key === "posted_at" ? "desc" : "asc");
    }
  }

  const rows = useMemo(() => {
    let r = jobs.filter((j) => {
      if (aiStatusFilter !== "all" && j.status !== aiStatusFilter) return false;
      if (myStatusFilter !== "all") {
        if (myStatusFilter === "none" ? j.my_status : j.my_status !== myStatusFilter) return false;
      }
      const salaryEur = annualSalaryInEur(j);
      if (minSalaryEur && salaryEur !== null && salaryEur < Number(minSalaryEur)) return false;
      if (search) {
        const hay = `${j.company} ${j.title}`.toLowerCase();
        if (!hay.includes(search.toLowerCase())) return false;
      }
      return true;
    });

    r = [...r].sort((a, b) => {
      let av: any = sortValue(a, sortKey);
      let bv: any = sortValue(b, sortKey);
      if (NUMERIC_SORT_KEYS.includes(sortKey)) {
        av = av ?? -1;
        bv = bv ?? -1;
      } else {
        av = (av ?? "").toString().toLowerCase();
        bv = (bv ?? "").toString().toLowerCase();
      }
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return r;
  }, [jobs, aiStatusFilter, myStatusFilter, search, minSalaryEur, sortKey, sortDir]);

  // Derived from `jobs` by url (rather than a frozen snapshot object) so
  // the open panel reflects the latest score/description/status after a
  // background refresh instead of showing stale AI analysis.
  const selected = useMemo(
    () => jobs.find((j) => j.url === selectedUrl) ?? null,
    [jobs, selectedUrl]
  );

  function openPanel(j: Job) {
    setSelectedUrl(j.url);
    setDraftStatus(j.my_status ?? "");
    setDraftNotes(j.notes ?? "");
    setSaveState("idle");
  }

  async function saveStatus(url: string, myStatus: string, notes: string, isPanelSave: boolean) {
    if (isPanelSave) setSaveState("saving");
    try {
      const res = await fetch("/api/status", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({url, my_status: myStatus || null, notes: notes || null}),
      });
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setJobs((prev) =>
        prev.map((j) => (j.url === url ? {...j, my_status: myStatus || null, notes: notes || null} : j))
      );
      if (isPanelSave) setSaveState("saved");
    } catch (e) {
      if (isPanelSave) setSaveState("error");
      else alert("Failed to save status: " + (e as Error).message);
    }
  }

  return (
    <>
      <header>
        <div className="header-row">
          <div>
            <h1>Job Search Board</h1>
            <div className="subtitle">
              Reads and writes data/seen_jobs.sqlite3 directly — no export/import step.
            </div>
          </div>
          <a className="btn secondary" href="/apply">Preparar candidatura</a>
        </div>
      </header>

      <Toolbar
        aiStatusFilter={aiStatusFilter}
        onAiStatusFilterChange={setAiStatusFilter}
        myStatusFilter={myStatusFilter}
        onMyStatusFilterChange={setMyStatusFilter}
        search={search}
        onSearchChange={setSearch}
        minSalaryEur={minSalaryEur}
        onMinSalaryEurChange={setMinSalaryEur}
        count={rows.length}
        total={jobs.length}
        onRefresh={load}
        onFetchNew={fetchNewJobs}
        fetching={fetching}
      />
      {fetchError && <p className="error-state">{fetchError}</p>}

      <main>
        {loading && <div className="loading-state">Loading...</div>}
        {error && (
          <div className="error-state">
            Can't read db: {error}
            <br />
            Check that `make run` was run at least once.
          </div>
        )}
        {!loading && !error && (
          rows.length === 0 ? (
            <div className="empty-state">Nothing matches the current filter/search.</div>
          ) : (
            <JobsTable
              rows={rows}
              sortKey={sortKey}
              sortDir={sortDir}
              onToggleSort={toggleSort}
              onSelectJob={openPanel}
              onQuickStatusChange={(job, status) => saveStatus(job.url, status, job.notes ?? "", false)}
            />
          )
        )}
      </main>

      {selected && (
        <JobDetailPanel
          job={selected}
          draftStatus={draftStatus}
          onDraftStatusChange={setDraftStatus}
          draftNotes={draftNotes}
          onDraftNotesChange={setDraftNotes}
          saveState={saveState}
          onSave={() => saveStatus(selected.url, draftStatus, draftNotes, true)}
          onClose={() => setSelectedUrl(null)}
        />
      )}
    </>
  );
}
