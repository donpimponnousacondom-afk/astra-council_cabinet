import { useState } from "react";
import type { ReactNode } from "react";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import type { RecordData } from "./api";

export type Column = {
  id: string;
  label: string;
  width?: string;
  value?: (row: RecordData) => string | number;
  render: (row: RecordData) => ReactNode;
};

export function WorkbenchTable({
  rows,
  columns,
  label,
  selected,
  rowClass,
}: {
  rows: RecordData[];
  columns: Column[];
  label: string;
  selected?: string;
  rowClass?: (row: RecordData) => string;
}) {
  const [sort, setSort] = useState<{ id: string; descending: boolean } | null>(
    null,
  );
  const column = columns.find((c) => c.id === sort?.id);
  const ordered = column?.value
    ? [...rows].sort((a, b) => {
        const x = column.value!(a),
          y = column.value!(b);
        const result =
          typeof x === "number" && typeof y === "number"
            ? x - y
            : String(x).localeCompare(String(y), undefined, {
                numeric: true,
                sensitivity: "base",
              });
        return sort?.descending ? -result : result;
      })
    : rows;
  return (
    <div
      className="table-scroll"
      tabIndex={0}
      aria-label={`${label} table scroll area`}
    >
      <table className="workbench-table" aria-label={label}>
        <colgroup>
          {columns.map((c) => (
            <col key={c.id} style={{ width: c.width }} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.id}
                scope="col"
                aria-sort={
                  sort?.id === c.id
                    ? sort.descending
                      ? "descending"
                      : "ascending"
                    : undefined
                }
              >
                {c.value ? (
                  <button
                    onClick={() =>
                      setSort({
                        id: c.id,
                        descending: sort?.id === c.id && !sort.descending,
                      })
                    }
                  >
                    {c.label}
                    {sort?.id === c.id ? (
                      sort.descending ? (
                        <ArrowDown size={11} />
                      ) : (
                        <ArrowUp size={11} />
                      )
                    ) : (
                      <ChevronsUpDown size={11} />
                    )}
                  </button>
                ) : (
                  c.label
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ordered.map((row) => (
            <tr
              key={row.id}
              data-record-id={row.id}
              className={`${selected === row.id ? "selected" : ""} ${rowClass?.(row) || ""}`}
            >
              {columns.map((c) => (
                <td key={c.id}>{c.render(row)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && (
        <div className="empty-rows">
          No matching records. Clear the filter or add a record.
        </div>
      )}
    </div>
  );
}
