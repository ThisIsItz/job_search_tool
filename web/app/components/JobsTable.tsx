import {MY_STATUS_LABEL, MY_STATUS_VALUES, SOURCE_LABEL} from "@/app/constants";
import {formatSalary} from "@/lib/salary";
import {Job, SortDir, SortKey} from "@/app/types";
import StatusBadge from "@/app/components/StatusBadge";

const COLUMNS: [SortKey, string][] = [
  ["company", "Company"], ["title", "Title"], ["location", "Location"], ["source", "Source"], ["salary", "Salary"],
  ["match_score", "Score"], ["stack_fit", "Stack fit"], ["status", "AI status"], ["my_status", "My status"],
  ["posted_at", "Date"],
];

const MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 10);
  const day = String(d.getUTCDate()).padStart(2, "0");
  const month = MONTHS_ES[d.getUTCMonth()];
  return `${day}-${month}-${d.getUTCFullYear()}`;
}

type JobsTableProps = {
  rows: Job[];
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
  onSelectJob: (job: Job) => void;
  onQuickStatusChange: (job: Job, status: string) => void;
};

export default function JobsTable({rows, sortKey, sortDir, onToggleSort, onSelectJob, onQuickStatusChange}: JobsTableProps) {
  return (
    <table>
      <thead>
        <tr>
          {COLUMNS.map(([key, label]) => (
            <th key={key} className={sortKey === key ? "active-sort" : ""} onClick={() => onToggleSort(key)}>
              {label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((j) => (
          <tr key={j.url} onClick={() => onSelectJob(j)}>
            <td className="company-cell">{j.company}</td>
            <td>{j.title}</td>
            <td className="loc-cell">
              {j.location}
              {j.other_locations.length > 0 && (
                <span className="more-locations" title={j.other_locations.map((o) => o.location).join(", ")}>
                  {" "}+{j.other_locations.length}
                </span>
              )}
            </td>
            <td className="loc-cell">{j.source ? SOURCE_LABEL[j.source] ?? j.source : "—"}</td>
            <td className="score-cell">{formatSalary(j)}</td>
            <td className="score-cell">{j.match_score ?? "—"}</td>
            <td className="score-cell">{j.stack_fit?.toFixed(1) ?? "—"}</td>
            <td><StatusBadge status={j.status} /></td>
            <td onClick={(e) => e.stopPropagation()}>
              <select
                className="my-status-select"
                value={j.my_status ?? ""}
                onChange={(e) => onQuickStatusChange(j, e.target.value)}
              >
                <option value="">—</option>
                {MY_STATUS_VALUES.map((v) => (
                  <option key={v} value={v}>{MY_STATUS_LABEL[v]}</option>
                ))}
              </select>
            </td>
            <td className="loc-cell">{formatDate(j.posted_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
