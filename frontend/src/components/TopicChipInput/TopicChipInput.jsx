import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createTopic, getTopicsByIds, searchTopics } from "../../api/topics";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import styles from "./TopicChipInput.module.css";

/**
 * Reusable chip-with-typeahead-and-inline-create topic picker, used
 * identically on the upload accordion and the query page's topic scoping
 * (decision #5). Selection is always optional narrowing, never required —
 * callers decide whether to block on it (they shouldn't, per decision #6).
 *
 * @param {string[]} selectedIds
 * @param {(ids: string[]) => void} onChange
 * @param {{id: string, name: string}[]} [knownTopics] - pre-fetched topics to
 *   resolve chip labels without waiting on the search query (e.g. suggested
 *   topics seeded from a review payload).
 */
export function TopicChipInput({ selectedIds = [], onChange, allowCreate = true, knownTopics = [] }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(0);
  const [createError, setCreateError] = useState(null);
  const inputRef = useRef(null);
  const wrapRef = useRef(null);
  const queryClient = useQueryClient();
  const debouncedQuery = useDebouncedValue(query, 250);

  // The committed selection, mirrored into a ref so async callbacks (the
  // create mutation's onSuccess) always append to the *current* list rather
  // than whatever it was when the request went out.
  const selectedIdsRef = useRef(selectedIds);
  useEffect(() => {
    selectedIdsRef.current = selectedIds;
  }, [selectedIds]);

  function commitIds(ids) {
    onChange?.(ids);
  }

  const { data: results = [] } = useQuery({
    queryKey: ["topics", debouncedQuery],
    queryFn: () => searchTopics(debouncedQuery),
    staleTime: 30_000,
  });

  // Labels accumulate instead of being derived from the current search.
  // `results` only holds whatever matches what's typed right now, so
  // deriving chip labels from it meant a selected topic's name turned back
  // into a raw uuid the moment you typed something that didn't match it
  // (and a freshly created topic showed as a uuid as soon as the box was
  // cleared) - it looked like the topic had been lost.
  const labelCache = useRef(new Map());
  const [, forceRelabel] = useState(0);

  function rememberLabels(topics) {
    let added = false;
    topics.forEach((t) => {
      if (t?.id && t?.name && labelCache.current.get(t.id) !== t.name) {
        labelCache.current.set(t.id, t.name);
        added = true;
      }
    });
    if (added) forceRelabel((n) => n + 1);
  }

  useEffect(() => {
    rememberLabels([...knownTopics, ...results]);
  }, [knownTopics, results]);

  // Anything still unlabelled is a selection we've never seen listed - a
  // suggested topic seeded from a review payload, or one outside the
  // typeahead window. Resolve those by id so a chip is never a bare uuid.
  const unresolvedIds = selectedIds.filter((id) => !labelCache.current.has(id));
  const { data: resolvedTopics = [] } = useQuery({
    queryKey: ["topics", "byIds", [...unresolvedIds].sort()],
    queryFn: () => getTopicsByIds(unresolvedIds),
    enabled: unresolvedIds.length > 0,
  });

  useEffect(() => {
    rememberLabels(resolvedTopics);
  }, [resolvedTopics]);

  const createMutation = useMutation({
    mutationFn: (name) => createTopic({ name }),
    onSuccess: (topic) => {
      rememberLabels([topic]); // label it before any refetch can race
      setCreateError(null);
      queryClient.invalidateQueries({ queryKey: ["topics"] });
      // get-or-create (decision #8) can hand back a topic that's already a
      // chip - appending blindly would duplicate it.
      const current = selectedIdsRef.current;
      if (!current.includes(topic.id)) commitIds([...current, topic.id]);
      setQuery("");
      inputRef.current?.focus();
    },
    // Without this a failed create was completely silent: the typed name
    // just sat in the box looking like it had been accepted.
    onError: (err) => setCreateError(err?.message || "Could not create that topic."),
  });

  const labelById = labelCache.current;

  const trimmedQuery = query.trim();
  const selectableResults = results.filter((t) => !selectedIds.includes(t.id));
  const exactMatch = results.some((t) => t.name.toLowerCase() === trimmedQuery.toLowerCase());
  const canCreate = allowCreate && Boolean(trimmedQuery) && !exactMatch;

  // One flat list of what Enter/the arrow keys can land on, so mouse and
  // keyboard selection go through exactly the same commit path.
  const options = useMemo(
    () => [
      ...selectableResults.map((topic) => ({ kind: "topic", topic })),
      ...(canCreate ? [{ kind: "create" }] : []),
    ],
    [selectableResults, canCreate],
  );

  // Clamped rather than reset from an effect: the option list shrinks
  // whenever a search settles, and a stale index must never point past it.
  const activeIndex = Math.min(highlighted, Math.max(options.length - 1, 0));
  const setActiveIndex = setHighlighted;

  const showDropdown = open && options.length > 0;

  function addTopic(id) {
    const current = selectedIdsRef.current;
    if (!current.includes(id)) commitIds([...current, id]);
    setQuery("");
    inputRef.current?.focus();
  }

  function removeTopic(id) {
    commitIds(selectedIdsRef.current.filter((existing) => existing !== id));
  }

  function commitOption(option) {
    if (!option) return;
    if (option.kind === "topic") {
      addTopic(option.topic.id);
    } else if (option.kind === "create" && !createMutation.isPending) {
      createMutation.mutate(trimmedQuery);
    }
  }

  function handleKeyDown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!options.length) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((i) => {
        const next = event.key === "ArrowDown" ? i + 1 : i - 1;
        return (next + options.length) % options.length;
      });
      return;
    }
    if (event.key === "Enter") {
      // Enter used to do nothing at all: typing a topic name and pressing
      // Enter left the text in the box, uncommitted, so the upload carried
      // no topic hint even though it looked like one had been picked.
      if (!trimmedQuery && !options.length) return;
      event.preventDefault();
      commitOption(options[activeIndex] || options[0]);
      return;
    }
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (event.key === "Backspace" && !query && selectedIds.length) {
      removeTopic(selectedIds[selectedIds.length - 1]);
    }
  }

  return (
    <div
      className={styles.wrap}
      ref={wrapRef}
      // Close only once focus genuinely leaves the widget. The old
      // `onBlur -> setTimeout(..., 100)` on the input raced every click:
      // mousedown on an option blurred the input, and 100ms later the
      // dropdown unmounted - so a deliberate click (mouseup after >100ms)
      // landed on a button that no longer existed and silently selected
      // nothing. Options now also preventDefault on mousedown, so the
      // input never loses focus to them in the first place.
      onBlur={(event) => {
        if (!wrapRef.current?.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <div className={styles.chips} onClick={() => inputRef.current?.focus()}>
        {selectedIds.map((id) => (
          <span key={id} className={styles.chip}>
            {labelById.get(id) || id}
            <button type="button" aria-label="Remove topic" onClick={() => removeTopic(id)}>
              ×
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          className={styles.input}
          type="text"
          value={query}
          placeholder={selectedIds.length ? "" : "Search or create a topic…"}
          role="combobox"
          aria-expanded={showDropdown}
          aria-autocomplete="list"
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
            setActiveIndex(0); // a new search invalidates the old highlight
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
        />
      </div>

      {showDropdown && (
        <div className={styles.dropdown} role="listbox">
          {options.map((option, index) => {
            const isActive = index === activeIndex;
            const className = `${styles.option} ${isActive ? styles.optionActive : ""}`;
            if (option.kind === "topic") {
              return (
                <button
                  type="button"
                  key={option.topic.id}
                  role="option"
                  aria-selected={isActive}
                  className={className}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => commitOption(option)}
                >
                  {option.topic.name}
                  {option.topic.description && <span className={styles.optionDesc}>{option.topic.description}</span>}
                </button>
              );
            }
            return (
              <button
                type="button"
                key="__create__"
                role="option"
                aria-selected={isActive}
                className={className}
                disabled={createMutation.isPending}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => commitOption(option)}
              >
                {createMutation.isPending ? `Creating “${trimmedQuery}”…` : `+ Create “${trimmedQuery}”`}
              </button>
            );
          })}
        </div>
      )}

      {createError && (
        <p className="muted" style={{ marginTop: "var(--space-1)", color: "var(--color-danger)" }}>
          {createError}
        </p>
      )}
    </div>
  );
}
