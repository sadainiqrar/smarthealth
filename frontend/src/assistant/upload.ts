// A patient's report upload, from the assistant's prompt input. The upload is a Part A
// call (spec 9.6) -- the file goes to the clinical record, not to the assistant; the
// assistant is told the document's id and links it to the matching test order.

import { api, readToken, refreshOnce } from "@/api";

export interface Uploaded {
  id: string;
  filename: string;
}

let patientId: string | null = null;

export async function uploadReport(file: File, retried = false): Promise<Uploaded> {
  patientId ??= (await api<{ id: string }>("/patients/me")).id;
  const response = await fetch(
    `/api/patients/${patientId}/documents?filename=${encodeURIComponent(file.name)}`,
    { method: "POST", headers: { Authorization: `Bearer ${readToken() ?? ""}` }, body: file },
  );
  if (response.status === 401 && !retried && (await refreshOnce())) return uploadReport(file, true);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // keep the status text
    }
    throw new Error(`Upload failed: ${detail}`);
  }
  const document = (await response.json()) as { id: string; filename: string };
  return { id: document.id, filename: document.filename };
}
