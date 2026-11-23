import {Job} from "@/app/types";

// Rough rates, only for comparing/filtering salaries across currencies — not for display.
const APPROX_EUR_PER_UNIT: Record<string, number> = {
  EUR: 1, USD: 0.9, GBP: 1.17, CAD: 0.65, AUD: 0.6, CHF: 1.05, THB: 0.026,
};

type SalaryFields = Pick<Job, "salary_min" | "salary_max" | "salary_currency">;

export function annualSalaryInEur(job: SalaryFields): number | null {
  const amount = job.salary_min ?? job.salary_max;
  const rate = job.salary_currency ? APPROX_EUR_PER_UNIT[job.salary_currency] : undefined;
  if (amount == null || rate === undefined) return null;
  return amount * rate;
}

function formatAmount(amount: number, currency: string | null): string {
  if (!currency) return Math.round(amount).toLocaleString("en");
  return new Intl.NumberFormat("en", {style: "currency", currency, notation: "compact", maximumFractionDigits: 0})
    .format(amount);
}

export function formatSalary(job: SalaryFields): string {
  const {salary_min: min, salary_max: max, salary_currency: currency} = job;
  if (min == null && max == null) return "—";
  if (min == null || max == null || min === max) return formatAmount((min ?? max) as number, currency);
  return `${formatAmount(min, currency)}–${formatAmount(max, currency)}`;
}
