// The same job posted in several countries shares a group_id (assigned by
// app/duplicates.py after the AI filter). The board shows one row per group.

export type OtherLocation = { location: string; url: string };

type Groupable = {
  url: string;
  location: string;
  group_id: string | null;
  stack_fit: number | null;
  my_status: string | null;
};

// Prefers the posting you've already tracked, then the best stack fit.
function isBetterRepresentative(candidate: Groupable, current: Groupable): boolean {
  if (Boolean(candidate.my_status) !== Boolean(current.my_status)) return Boolean(candidate.my_status);
  return (candidate.stack_fit ?? -1) > (current.stack_fit ?? -1);
}

export function collapseDuplicateGroups<T extends Groupable>(rows: T[]): (T & { other_locations: OtherLocation[] })[] {
  const groups = new Map<string, T[]>();
  for (const row of rows) {
    const key = row.group_id ?? row.url;
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups.values()].map((members) => {
    const representative = members.reduce((best, m) => (isBetterRepresentative(m, best) ? m : best));
    const other_locations = members
      .filter((m) => m !== representative)
      .map((m) => ({location: m.location, url: m.url}));
    return {...representative, other_locations};
  });
}
