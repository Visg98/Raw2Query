import { apiFetch } from "./client";

/**
 * Ask a question.
 *
 * `chatId` is the conversation to append to. Omit it for a brand-new chat:
 * the backend creates the session as part of answering and returns its
 * `chat_id`, so an unused "New chat" never becomes a row.
 *
 * @param {{ question: string, topicIds?: string[], chatId?: string | null }} body
 */
export function runQuery(body) {
  return apiFetch("/query", {
    method: "POST",
    json: {
      question: body.question,
      topic_ids: body.topicIds?.length ? body.topicIds : null,
      chat_id: body.chatId || null,
    },
  });
}
