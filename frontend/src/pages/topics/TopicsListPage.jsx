import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createTopic, searchTopics } from "../../api/topics";
import { useToast } from "../../components/common/Toast";
import { TopicCard } from "./components/TopicCard";

export function TopicsListPage() {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [newName, setNewName] = useState("");

  const { data: topics, isLoading, error } = useQuery({ queryKey: ["topics", ""], queryFn: () => searchTopics("") });

  const createMutation = useMutation({
    mutationFn: (name) => createTopic({ name }),
    onSuccess: () => {
      setNewName("");
      queryClient.invalidateQueries({ queryKey: ["topics"] });
      showToast("Topic created.", { variant: "success" });
    },
    onError: (err) => showToast(err.message || "Could not create topic.", { variant: "error" }),
  });

  return (
    <div className="page">
      <div className="page-header">
        <h1>Topics</h1>
        <form
          style={{ display: "flex", gap: "var(--space-2)" }}
          onSubmit={(e) => {
            e.preventDefault();
            if (newName.trim()) createMutation.mutate(newName.trim());
          }}
        >
          <input className="text-input" placeholder="New topic name" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <button type="submit" className="btn btn-primary" disabled={createMutation.isPending || !newName.trim()}>
            + New topic
          </button>
        </form>
      </div>

      {isLoading && (
        <div style={{ display: "grid", gap: "var(--space-3)" }}>
          {[...Array(3)].map((_, i) => (
            <div key={i} className="skeleton" style={{ height: 80 }} />
          ))}
        </div>
      )}

      {/* A failing query leaves `topics` undefined, so this has to render
          instead of the list - not alongside it. Mapping over it
          unconditionally crashed the whole page whenever /topics errored. */}
      {!isLoading && error && (
        <div
          className="card"
          style={{ padding: "var(--space-4)", background: "var(--color-danger-bg)", borderColor: "var(--color-danger)" }}
        >
          <strong>Couldn’t load topics.</strong>
          <p className="muted" style={{ margin: "4px 0 0" }}>{error.message}</p>
        </div>
      )}

      {!isLoading && !error && !topics?.length && (
        <div className="empty-state">No topics yet. Create one above, or they’ll appear as documents get reviewed.</div>
      )}

      {!isLoading && !error && topics?.length > 0 && (
        <div style={{ display: "grid", gap: "var(--space-3)" }}>
          {topics.map((topic) => (
            <TopicCard key={topic.id} topic={topic} />
          ))}
        </div>
      )}
    </div>
  );
}
