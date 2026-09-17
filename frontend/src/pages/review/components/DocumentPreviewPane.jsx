import { DocumentViewer } from "../../../components/DocumentViewer";

/**
 * The original file, shown next to the extracted fields so a reviewer can
 * check extraction accuracy visually rather than trust confidence scores
 * alone.
 *
 * This used to be an `<img>` for images and an `<iframe>` for everything
 * else — which meant every non-PDF, non-image upload (.docx, .xlsx, .pptx,
 * .csv, .eml, anything with no usable content type) *downloaded* when the
 * review page opened instead of appearing here. `DocumentViewer` picks a
 * real renderer per file type; see its header for the full story.
 */
export function DocumentPreviewPane({ documentId, filename }) {
  return (
    <div className="card" style={{ padding: "var(--space-3)", alignSelf: "start", position: "sticky", top: "var(--space-4)" }}>
      <DocumentViewer documentId={documentId} filename={filename} height={560} />
    </div>
  );
}
