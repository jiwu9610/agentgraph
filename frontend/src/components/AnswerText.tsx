// Renders an assistant answer: markdown for formatting, with inline citation
// markers like `(guide.md #2)` upgraded to buttons that toggle the retrieved
// snippet inline. Each marker occurrence holds its own expanded state, so
// repeated citations toggle independently.

import { Fragment, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Citation } from "../types";

/** Matches inline citation markers of the form `(source.md #3)`. */
const CITATION_PATTERN = /\(([^\s()]+\.md) #(\d+)\)/g;

function CitationToggle({
  label,
  snippet,
}: {
  label: string;
  snippet: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="citation-link"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      {open && <span className="citation-snippet">{snippet}</span>}
    </>
  );
}

/**
 * Split citation markers out of a text run. Markers that match a citation
 * carrying a snippet become toggles; anything else stays plain text.
 */
function splitCitations(text: string, citations: Citation[]): ReactNode {
  // Fresh regex per call: the shared pattern's lastIndex must not leak
  // between text runs.
  const re = new RegExp(CITATION_PATTERN.source, "g");
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    const [label, source, indexText] = m;
    if (m.index > cursor) {
      parts.push(text.slice(cursor, m.index));
    }
    const cite = citations.find(
      (c) => c.source === source && c.index === Number(indexText),
    );
    if (cite?.snippet) {
      parts.push(<CitationToggle label={label} snippet={cite.snippet} />);
    } else {
      parts.push(label);
    }
    cursor = m.index + label.length;
  }
  if (parts.length === 0) {
    return text;
  }
  if (cursor < text.length) {
    parts.push(text.slice(cursor));
  }
  return parts.map((part, i) => <Fragment key={i}>{part}</Fragment>);
}

/** Recursively upgrade citation markers within rendered markdown children. */
function withCitations(node: ReactNode, citations: Citation[]): ReactNode {
  if (typeof node === "string") {
    return splitCitations(node, citations);
  }
  if (Array.isArray(node)) {
    return node.map((child, i) => (
      <Fragment key={i}>{withCitations(child, citations)}</Fragment>
    ));
  }
  return node;
}

/**
 * An assistant message body: markdown-rendered text whose inline citation
 * markers become snippet toggles for the given citations.
 */
export function AnswerText({
  text,
  citations,
}: {
  text: string;
  citations: Citation[];
}) {
  return (
    <ReactMarkdown
      components={{
        p: ({ children }) => <p>{withCitations(children, citations)}</p>,
        li: ({ children }) => <li>{withCitations(children, citations)}</li>,
        strong: ({ children }) => (
          <strong>{withCitations(children, citations)}</strong>
        ),
        em: ({ children }) => <em>{withCitations(children, citations)}</em>,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
