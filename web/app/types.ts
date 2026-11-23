export type SortKey = "company" | "title" | "location" | "source" | "salary" | "match_score" | "stack_fit" | "status" | "my_status" | "posted_at";
export type SortDir = "asc" | "desc";
export type SaveState = "idle" | "saving" | "saved" | "error";

export type Job = {
    url: string;
    company: string;
    title: string;
    location: string;
    posted_at: string | null;
    description: string;
    source: string | null;
    salary_min: number | null; // annual
    salary_max: number | null; // annual
    salary_currency: string | null;
    match_score: number | null;
    stack_fit: number | null; // 0-4, from ai_filter.py
    recommendation: string | null;
    genuine_gaps: string | null;
    transferable_strengths: string | null;
    risk_factors: string | null;
    my_status: string | null;
    notes: string | null;
    status: string; // apply | consider | skip | not_evaluated
};