// Small pieces every page uses: error line, pager, debounce.
import { useEffect, useState } from "react";
import type { Page } from "./types";

export const PAGE_SIZE = 20;

// Errors render the API's message verbatim ("{error}: {detail}"), never our own wording.
export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="error">{error instanceof Error ? error.message : String(error)}</p>;
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

export function Pager({ page, onOffset }: { page: Page<unknown>; onOffset: (offset: number) => void }) {
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.limit, page.total);
  return (
    <div className="pager">
      <button disabled={page.offset === 0} onClick={() => onOffset(Math.max(0, page.offset - page.limit))}>
        Prev
      </button>
      <span>{from}-{to} of {page.total}</span>
      <button disabled={page.offset + page.limit >= page.total} onClick={() => onOffset(page.offset + page.limit)}>
        Next
      </button>
    </div>
  );
}

export function listPath(base: string, search: string, offset: number): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  if (search) params.set("search", search);
  return `${base}?${params}`;
}
