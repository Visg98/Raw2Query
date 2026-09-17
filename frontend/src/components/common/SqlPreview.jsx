import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";

SyntaxHighlighter.registerLanguage("sql", sql);

export function SqlPreview({ sql: sqlText }) {
  if (!sqlText) return null;
  return (
    <SyntaxHighlighter language="sql" style={oneLight} customStyle={{ margin: 0, borderRadius: "var(--radius-md)", fontSize: 12.5 }}>
      {sqlText}
    </SyntaxHighlighter>
  );
}
