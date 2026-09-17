import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";

SyntaxHighlighter.registerLanguage("json", json);

export function JsonViewer({ value }) {
  return (
    <SyntaxHighlighter language="json" style={oneLight} customStyle={{ margin: 0, borderRadius: "var(--radius-md)", fontSize: 12.5 }}>
      {JSON.stringify(value, null, 2)}
    </SyntaxHighlighter>
  );
}
