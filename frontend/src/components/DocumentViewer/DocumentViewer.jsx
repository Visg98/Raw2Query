import { useQuery } from "@tanstack/react-query";
import {
  getDocumentDownloadUrl,
  getDocumentFileUrl,
  getDocumentPreview,
} from "../../api/documents";
import styles from "./DocumentViewer.module.css";

/**
 * Shows an original upload in the UI instead of downloading it.
 *
 * The bug this replaces: the review pane pointed an `<iframe>` straight at
 * the raw bytes and hoped the browser would render them. Browsers only ship
 * viewers for PDF, common web images, plain text and HTML — so every .docx,
 * .xlsx, .pptx, .csv and .eml (and anything uploaded without a usable
 * content type) went to the download manager instead. No
 * `Content-Disposition` value fixes that; the header says whether to
 * display or save, it can't supply a renderer the browser doesn't have.
 *
 * So the server decides which renderer applies (`GET /documents/{id}/preview`)
 * and, for the types with no native one, converts the file into structured
 * elements first. This component just dispatches on `kind`:
 *
 *   pdf | image | html      -> stream the original, browser renders it
 *   audio | video           -> stream the original into a native player
 *   text                    -> server-decoded text in a <pre>
 *   elements                -> server-parsed headings/paragraphs/tables
 *   unsupported             -> an explicit "download to open" card
 *
 * The last case matters as much as the rest: an unpreviewable file now says
 * so, rather than silently starting a download nobody asked for.
 *
 * Used by the review screen's preview pane and the Ask page's source cards.
 */
export function DocumentViewer({ documentId, filename, height = 480, showDownload = true }) {
  const {
    data: preview,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["documentPreview", documentId],
    queryFn: () => getDocumentPreview(documentId),
    // A stored original never changes, and partitioning a large scan isn't
    // cheap — don't re-run it on every remount of the pane.
    staleTime: Infinity,
    retry: false,
  });

  const label = preview?.filename || filename || "document";

  if (isLoading) {
    return <div className={`skeleton ${styles.frame}`} style={{ height }} />;
  }

  if (error) {
    return (
      <Fallback
        documentId={documentId}
        label={label}
        message={error.message || "Could not load a preview for this file."}
        showDownload={showDownload}
      />
    );
  }

  return (
    // `data-testid`/`data-preview-kind` are the handles the browser tests
    // assert on: CSS-module class names are hashed, and "which renderer did
    // this file get?" is exactly what those tests need to read.
    <div className={styles.viewer} data-testid="document-viewer" data-preview-kind={preview?.kind || "unknown"}>
      <div className={styles.content} style={{ height }}>
        <PreviewBody preview={preview} documentId={documentId} label={label} showDownload={showDownload} />
      </div>
      <div className={styles.toolbar}>
        {preview?.truncated ? (
          <span className="muted">Preview shortened — download the file for the full contents.</span>
        ) : (
          <span className="muted">{formatMeta(preview)}</span>
        )}
        {/* The fallback card already offers its own download, so don't
            show a second button next to it. */}
        {showDownload && preview?.kind !== "unsupported" && (
          <a className="btn btn-sm" href={getDocumentDownloadUrl(documentId)} download>
            Download
          </a>
        )}
      </div>
    </div>
  );
}

function PreviewBody({ preview, documentId, label, showDownload }) {
  const src = getDocumentFileUrl(documentId);

  switch (preview?.kind) {
    case "pdf":
      return <iframe className={styles.fill} title={label} src={src} />;

    case "image":
      return <img className={styles.image} src={src} alt={label} />;

    case "audio":
      // `unstructured` would happily transcribe these, but a transcript
      // isn't a preview of a sound file — the reviewer wants to hear it.
      return (
        <div className={styles.media}>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption -- an
              uploaded recording has no caption track to offer. */}
          <audio controls src={src} className={styles.audio} />
          <p className="muted" style={{ margin: 0, overflowWrap: "anywhere" }}>
            {label}
          </p>
        </div>
      );

    case "video":
      return (
        <div className={styles.media}>
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video controls src={src} className={styles.video} />
        </div>
      );

    case "html":
      // Native rendering, but an uploaded HTML file is untrusted content:
      // `sandbox` with no `allow-scripts` blocks its scripts, forms and
      // navigation while still laying the page out.
      return <iframe className={styles.fill} title={label} src={src} sandbox="" />;

    case "text":
      return <pre className={styles.text}>{preview.text}</pre>;

    case "elements":
      return (
        <div className={styles.doc}>
          {preview.elements.map((element, index) => (
            <PreviewElement key={index} element={element} />
          ))}
        </div>
      );

    default:
      return (
        <Fallback
          documentId={documentId}
          label={label}
          message={preview?.message || "This file can't be previewed in the browser."}
          showDownload={showDownload}
        />
      );
  }
}

function PreviewElement({ element }) {
  switch (element.type) {
    case "heading":
      return <h4 className={styles.heading}>{element.text}</h4>;

    case "list_item":
      return (
        <div className={styles.listItem}>
          <span aria-hidden="true">•</span>
          <span>{element.text}</span>
        </div>
      );

    case "page_break":
      return <hr className={styles.pageBreak} />;

    case "table":
      // `rows` is cell *text*, parsed server-side from the table markup, so
      // there is nothing here to inject as HTML. First row as the header:
      // that's what every spreadsheet and document table this renders has.
      return (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                {(element.rows[0] || []).map((cell, i) => (
                  <th key={i}>{cell}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {element.rows.slice(1).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );

    default:
      return <p className={styles.paragraph}>{element.text}</p>;
  }
}

function Fallback({ documentId, label, message, showDownload }) {
  return (
    <div className={styles.fallback} data-testid="preview-fallback">
      <p style={{ margin: 0, fontWeight: 500, overflowWrap: "anywhere" }}>{label}</p>
      <p className="muted" style={{ margin: 0 }}>
        {message}
      </p>
      {showDownload && (
        <a className="btn btn-sm" href={getDocumentDownloadUrl(documentId)} download>
          Download file
        </a>
      )}
    </div>
  );
}

function formatMeta(preview) {
  if (!preview) return "";
  const size = preview.file_size;
  const readable =
    size >= 1024 * 1024
      ? `${(size / (1024 * 1024)).toFixed(1)} MB`
      : size >= 1024
        ? `${Math.round(size / 1024)} KB`
        : `${size} B`;
  return `${preview.media_type} · ${readable}`;
}
